"""Metadata-only audit trail: one JSON line per gate event in ~/.claude-hr/audit/YYYY-MM.jsonl.

It answers "what did the automated checks do, and when" without keeping any code, command output or secret: only counts,
durations, check names, exit codes and a hash-based repo id. Best effort: recording can never raise or slow a turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import paths

MAX_STR = 120  # no free text: names and short reasons only
MAX_LINE = (
    3500  # one O_APPEND write below the pipe/page size stays intact when two gates finish together
)


def audit_dir() -> Path:
    return paths.hr_home() / "audit"


def repo_id(root: Path, remote: str = "") -> str:
    """A stable, non-reversible id for a repository: sha256(remote url or path) plus the folder name for humans."""
    basis = remote.strip() or str(Path(root).resolve())
    return f"{Path(root).name[:40]}-{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:10]}"


def _clean(value, depth: int = 0):
    """Keep only JSON-safe scalars and small containers; cap strings. Never keeps anything that looks like content."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STR]
    if isinstance(value, (list, tuple)) and depth < 2:
        return [_clean(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, dict) and depth < 2:
        return {str(k)[:40]: _clean(v, depth + 1) for k, v in list(value.items())[:20]}
    return str(value)[:MAX_STR]


def record(event: str, **fields) -> bool:
    """Append one event. Returns False (never raises) when it could not be written."""
    try:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": str(event)[:40]}
        entry.update({k: _clean(v) for k, v in fields.items()})
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        if len(line) > MAX_LINE:
            line = json.dumps({"ts": entry["ts"], "event": entry["event"], "truncated": True})
        d = audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        paths.make_private(d)
        f = d / f"{time.strftime('%Y-%m')}.jsonl"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(f, flags | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def tail(n: int = 20) -> list[dict]:
    """The last n events across the newest month files (oldest first). Unreadable lines are skipped."""
    out: list[dict] = []
    try:
        files = sorted(audit_dir().glob("*.jsonl"))
    except OSError:
        return out
    for f in reversed(files):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
            if len(out) >= n:
                return list(reversed(out))
    return list(reversed(out))


def prune(keep_days: int = 90) -> int:
    """Delete month files that ended more than keep_days ago. Returns how many were removed."""
    removed = 0
    cutoff = time.time() - keep_days * 86400
    try:
        files = list(audit_dir().glob("*.jsonl"))
    except OSError:
        return 0
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
