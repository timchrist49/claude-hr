"""`claude-hr-hook stop`: the end-of-turn gate (plugin hr-gates). ONE process per turn:

    1. format the files Claude changed, with a formatter the repo already configures (fmt.py)
    2. run the repo's trusted fast checks and, if one fails, send the failure back to Claude (verify.py)
    3. append a metadata-only record to the audit trail (auditlog.py)

Everything fails open: any error, timeout or missing tool exits 0 without output, so a gate can never break a session.
Nothing is printed unless a trusted check failed, so a normal turn adds no tokens to the context.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import paths, procutil

STATE_KEEP = 50  # repositories remembered in state/gates.json


def run() -> int:
    """Entry point used by hooks.main. Prints the block decision (JSON) only when a trusted check failed."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        result = handle(payload)
        if result:
            sys.stdout.write(json.dumps(result))
    except Exception:
        _crash()
    return 0


def handle(payload: dict) -> dict | None:
    """The whole decision, separated from stdin/stdout so it can be tested. Returns the JSON to print or None."""
    if os.environ.get("CLAUDE_HR_GATES", "").lower() == "off":
        return None
    # Skip when a subagent is stopping (its parent session gets its own Stop) and in plan mode (nothing was changed on
    # purpose). `stop_hook_active` (Claude is continuing because a stop hook blocked) is deliberately NOT a skip: this gate
    # re-runs the real checks and counts its own blocks per working-tree state (verify.gate, default cap 2, well under
    # Claude Code's cap of 8 consecutive blocks), so a fix attempt is verified instead of waved through.
    if payload.get("agent_id") or payload.get("permission_mode") == "plan":
        return None
    from . import config

    cfg = config.load()
    if not cfg.flag("gates.enabled", True):
        return None
    root = _git_root(str(payload.get("cwd") or os.getcwd()))
    if root is None:
        return None

    from . import verify

    state = _State.load()
    key = _repo_key(root)
    fp_before = verify.fingerprint(root)
    if state.seen(key) == fp_before:
        return None  # nothing changed since the last time this gate looked at the tree
    started = time.monotonic()

    formatted: list[str] = []
    skipped = errors = 0
    formatters: list[str] = []
    if cfg.flag("format.enabled", True):
        from . import fmt

        rep = fmt.format_changed(
            root,
            timeout_s=float(cfg.get("format.timeout_s", 30)),
            max_files=int(cfg.get("format.max_files", 40)),
            max_kb=int(cfg.get("format.max_file_kb", 512)),
            skip_globs=tuple(cfg.get("format.skip_globs", []) or ()),
            allow=list(cfg.get("format.allow", []) or []) or None,
            clean_gate=cfg.flag("format.clean_gate", True),
        )
        formatted, skipped, errors = list(rep.formatted), len(rep.skipped), len(rep.errors)
        formatters = list(rep.formatters)

    outcome = None
    if cfg.flag("verify.gate", True):
        outcome = verify.gate(
            root,
            paths.state_dir(),
            max_blocks=int(cfg.get("verify.gate_max_blocks", 2)),
            timeout_s=int(cfg.get("verify.gate_timeout_s", 120)),
            tier=str(cfg.get("verify.tier", "fast")),
        )

    blocked = outcome is not None and outcome.action == "block"
    if not blocked:
        # remember the tree as it is NOW (after formatting) so the next idle Stop is a no-op. A blocked tree is NOT
        # remembered: if Claude stops again without changing anything, the failing checks must be looked at again.
        state.remember(key, verify.fingerprint(root) if formatted else fp_before)
        state.save()

    if cfg.flag("gates.trail", True):
        _trail(
            cfg, root, formatted, skipped, errors, formatters, outcome, time.monotonic() - started
        )

    if blocked:
        return {"decision": "block", "reason": outcome.reason}
    return None


def _trail(
    cfg, root: Path, formatted, skipped, errors, formatters, outcome, seconds: float
) -> None:
    from . import auditlog

    checks = [r.name for r in getattr(outcome, "results", []) or []] if outcome else []
    auditlog.record(
        "gate",
        repo=auditlog.repo_id(root),
        formatted=len(formatted),
        format_skipped=skipped,
        format_errors=errors,
        formatters=formatters,
        verify=outcome.action if outcome else "off",
        checks=checks,
        seconds=round(seconds, 2),
    )


def _git_root(cwd: str) -> Path | None:
    r = procutil.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=8)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    return Path((r.stdout or "").strip().splitlines()[0])


def _repo_key(root: Path) -> str:
    key = str(root.resolve())
    return key.casefold() if paths.IS_WINDOWS else key


class _State:
    """state/gates.json: the working-tree fingerprint each repository had when this gate last finished."""

    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls) -> _State:
        from .jsonio import read_json

        try:
            data = read_json(paths.state_dir() / "gates.json", {}) or {}
        except Exception:
            data = {}
        return cls(data if isinstance(data, dict) else {})

    def seen(self, key: str) -> str | None:
        entry = self.data.get(key)
        return entry.get("fp") if isinstance(entry, dict) else None

    def remember(self, key: str, fp: str) -> None:
        self.data.pop(key, None)  # re-insert last: dict order doubles as recency
        self.data[key] = {"fp": fp, "ts": int(time.time())}
        while len(self.data) > STATE_KEEP:
            self.data.pop(next(iter(self.data)))

    def save(self) -> None:
        from .jsonio import atomic_write_text

        try:
            paths.state_dir().mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                paths.state_dir() / "gates.json",
                json.dumps(self.data) + "\n",
                follow_symlinks=False,
            )
        except OSError:
            pass


def _crash() -> None:
    """Hooks run windowless: leave a trace for `doctor` / the user, never raise."""
    import traceback

    try:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.logs_dir() / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} claude-hr-hook stop\n{traceback.format_exc()}\n"
            )
    except OSError:
        pass
