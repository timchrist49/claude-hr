"""Ownership-tracked three-way merge of a managed baseline into a user's settings.json.

Goals: re-runnable (idempotent), never clobbers a value the user chose, updatable (a new
baseline value replaces our *own* earlier value), and fully revertible.

Model
  * Dicts are flattened to leaf paths so user-added siblings (e.g. their own `env` vars) survive.
  * Paths in `atomic` are treated as a single leaf (e.g. `statusLine`).
  * Lists are additive sets (identity = canonical JSON of the item): we add missing items,
    remember which ones we added, and never remove items the user wrote.
  * `state` records, per leaf, the value we applied and what was there before (`orig`).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

Path = tuple[str, ...]
_ABSENT = {"present": False}


def _enc(path: Path) -> str:
    return json.dumps(list(path), separators=(",", ":"))


def _canon(item: Any) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def _get(doc: Any, path: Path) -> tuple[bool, Any]:
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _set(doc: dict, path: Path, value: Any) -> None:
    cur = doc
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = copy.deepcopy(value)


def _delete(doc: dict, path: Path) -> None:
    parents = []
    cur = doc
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        parents.append((cur, key))
        cur = cur[key]
    if isinstance(cur, dict):
        cur.pop(path[-1], None)
    # prune now-empty parents (only dicts we may have created)
    for parent, key in reversed(parents):
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]
        else:
            break


def _flatten(doc: dict, atomic: set[Path], prefix: Path = ()) -> tuple[dict[Path, Any], dict[Path, list]]:
    leaves: dict[Path, Any] = {}
    lists: dict[Path, list] = {}
    for key, value in doc.items():
        path = prefix + (key,)
        if isinstance(value, dict) and value and path not in atomic:
            sub_leaves, sub_lists = _flatten(value, atomic, path)
            leaves.update(sub_leaves)
            lists.update(sub_lists)
        elif isinstance(value, list):
            lists[path] = value
        else:
            leaves[path] = value
    return leaves, lists


@dataclass
class Report:
    set: list[str] = field(default_factory=list)  # newly written or updated
    kept: list[str] = field(default_factory=list)  # user value differs; left alone
    removed: list[str] = field(default_factory=list)  # retired by a newer baseline / revert
    unchanged: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.set)} set, {self.unchanged} unchanged, "
            f"{len(self.kept)} kept (user override), {len(self.removed)} removed"
        )


def _dotted(path: Path) -> str:
    return ".".join(path)


def apply_baseline(
    current: dict,
    baseline: dict,
    state: dict | None,
    *,
    atomic: set[Path] | None = None,
    force: bool = False,
) -> tuple[dict, dict, Report]:
    atomic = atomic or set()
    doc = copy.deepcopy(current)
    st: dict = copy.deepcopy(state) if state else {}
    st.setdefault("leaves", {})
    st.setdefault("lists", {})
    report = Report()
    base_leaves, base_lists = _flatten(baseline, atomic)

    # ---- leaves -----------------------------------------------------------------------------
    for path, want in base_leaves.items():
        key = _enc(path)
        present, have = _get(doc, path)
        rec = st["leaves"].get(key)
        if rec is None:
            if not present:
                _set(doc, path, want)
                st["leaves"][key] = {"applied": want, "orig": _ABSENT}
                report.set.append(_dotted(path))
            elif have == want:
                st["leaves"][key] = {"applied": want, "orig": {"present": True, "value": have}}
                report.unchanged += 1
            elif force:
                st["leaves"][key] = {"applied": want, "orig": {"present": True, "value": have}}
                _set(doc, path, want)
                report.set.append(_dotted(path))
            else:
                report.kept.append(_dotted(path))
            continue
        applied = rec["applied"]
        if present and have == applied:
            if want != applied:
                _set(doc, path, want)
                rec["applied"] = want
                report.set.append(_dotted(path))
            else:
                report.unchanged += 1
        elif force:
            _set(doc, path, want)
            rec["applied"] = want
            report.set.append(_dotted(path))
        else:
            report.kept.append(_dotted(path))  # user edited or deleted our value

    # leaves we owned that the baseline no longer mentions -> retire if untouched
    for key in [k for k in st["leaves"] if tuple(json.loads(k)) not in base_leaves]:
        path = tuple(json.loads(key))
        rec = st["leaves"].pop(key)
        present, have = _get(doc, path)
        if present and have == rec["applied"]:
            _restore(doc, path, rec["orig"])
            report.removed.append(_dotted(path))

    # ---- lists ------------------------------------------------------------------------------
    for path, want_items in base_lists.items():
        key = _enc(path)
        rec = st["lists"].setdefault(key, {"added": [], "declined": []})
        present, have = _get(doc, path)
        have_list: list = list(have) if present and isinstance(have, list) else []
        have_keys = {_canon(i) for i in have_list}
        added = {_canon(i): i for i in rec["added"]}
        want_keys = {_canon(i) for i in want_items}
        changed = False
        for item in want_items:
            ck = _canon(item)
            if ck in have_keys:
                continue
            if ck in added and not force:  # we added it before; the user removed it -> respect that
                report.kept.append(f"{_dotted(path)}[{ck}]")
                continue
            have_list.append(copy.deepcopy(item))
            have_keys.add(ck)
            added[ck] = item
            changed = True
            report.set.append(f"{_dotted(path)}+{ck}")
        for ck in list(added):  # retire items we added that left the baseline
            if ck not in want_keys:
                if ck in have_keys:
                    have_list = [i for i in have_list if _canon(i) != ck]
                    have_keys.discard(ck)
                    changed = True
                    report.removed.append(f"{_dotted(path)}-{ck}")
                del added[ck]
        if changed or (not present and have_list):
            _set(doc, path, have_list)
        rec["added"] = list(added.values())

    # lists we tracked that vanished from the baseline entirely
    for key in [k for k in st["lists"] if tuple(json.loads(k)) not in base_lists]:
        path = tuple(json.loads(key))
        rec = st["lists"].pop(key)
        present, have = _get(doc, path)
        if present and isinstance(have, list):
            drop = {_canon(i) for i in rec["added"]}
            kept_items = [i for i in have if _canon(i) not in drop]
            if len(kept_items) != len(have):
                report.removed.append(_dotted(path))
            if kept_items:
                _set(doc, path, kept_items)
            else:
                _delete(doc, path)
    return doc, st, report


def _restore(doc: dict, path: Path, orig: dict) -> None:
    if orig.get("present"):
        _set(doc, path, orig["value"])
    else:
        _delete(doc, path)


def revert(current: dict, state: dict | None) -> tuple[dict, Report]:
    """Undo everything `apply_baseline` did that the user has not since changed."""
    doc = copy.deepcopy(current)
    report = Report()
    if not state:
        return doc, report
    for key, rec in state.get("leaves", {}).items():
        path = tuple(json.loads(key))
        present, have = _get(doc, path)
        if present and have == rec["applied"]:
            _restore(doc, path, rec["orig"])
            report.removed.append(_dotted(path))
        else:
            report.kept.append(_dotted(path))
    for key, rec in state.get("lists", {}).items():
        path = tuple(json.loads(key))
        present, have = _get(doc, path)
        if present and isinstance(have, list):
            drop = {_canon(i) for i in rec.get("added", [])}
            left = [i for i in have if _canon(i) not in drop]
            if len(left) != len(have):
                report.removed.append(_dotted(path))
            if left:
                _set(doc, path, left)
            else:
                _delete(doc, path)
    return doc, report
