"""Disk hygiene for things Claude Code's own `cleanupPeriodDays` sweep does NOT cover.

Observed on a real dev machine: ~/.claude/jobs held 22 GB of finished-session scratch space, and upstream
reports unbounded growth from mcp-logs-* and daemon.log. Everything here is dry-run unless apply=True,
and only touches paths on an explicit allow-list.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, sysinfo
from .config import Config


@dataclass
class Item:
    path: Path
    kind: str
    size_mb: float
    reason: str


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total_mb(self) -> float:
        return sum(i.size_mb for i in self.items)


def _size_mb(p: Path) -> float:
    if p.is_file():
        try:
            return p.stat().st_size / 1024 / 1024
        except OSError:
            return 0.0
    return sysinfo.dir_size_gb(p) * 1024


def _older_than(p: Path, days: float) -> bool:
    try:
        return (time.time() - p.stat().st_mtime) > days * 86400
    except OSError:
        return False


def _active_job_ids() -> set[str] | None:
    """Job ids of sessions that still exist (never delete those). None = could not tell: callers must fail CLOSED."""
    from . import procutil

    claude = procutil.find_claude()
    if not claude:
        return None
    r = procutil.run([str(claude), "agents", "--json"], timeout=30)
    if r.returncode != 0:
        return None
    try:
        import json

        sessions = json.loads(r.stdout or "[]")
    except ValueError:
        return None
    ids: set[str] = set()
    for s in sessions if isinstance(sessions, list) else []:
        sid = str(s.get("sessionId") or "")
        if sid:
            ids.add(sid)
            ids.add(sid[:8])
    return ids


def _last_activity(d: Path) -> float:
    """Newest mtime among the job dir, its tmp dir and tmp's entries: a dir's own mtime misses files written deeper."""
    newest = 0.0
    try:
        newest = d.stat().st_mtime
        tmp = d / "tmp"
        newest = max(newest, tmp.stat().st_mtime)
        for i, entry in enumerate(os.scandir(tmp)):
            if i > 400:
                break
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    return newest


def plan(cfg: Config) -> Plan:
    m = cfg.section("maintenance")
    out = Plan()
    jobs_days = float(m.get("jobs_retention_days", 14))
    log_days = float(m.get("log_retention_days", 14))
    ch = paths.claude_home()

    jobs = ch / "jobs"
    if jobs.is_dir():
        active = _active_job_ids()
        if active is None:
            out.skipped.append("jobs/: cannot tell which sessions are active (`claude agents --json` failed); not touching job dirs")
        for d in sorted(jobs.iterdir()) if active is not None else []:
            if not d.is_dir():
                continue
            if d.name in active or d.name[:8] in active:
                out.skipped.append(f"{d.name}: session still active")
                continue
            tmp = d / "tmp"
            if tmp.is_dir() and (time.time() - _last_activity(d)) > jobs_days * 86400:
                out.items.append(Item(tmp, "job-scratch", _size_mb(tmp), f"finished session scratch older than {jobs_days:.0f}d"))

    # MCP server logs (macOS ~/Library/Caches, Windows %LOCALAPPDATA%, Linux ~/.cache)
    for base in _cli_cache_dirs():
        for d in base.glob("*/mcp-logs-*"):
            for f in d.glob("*.jsonl"):
                if _older_than(f, 1):
                    out.items.append(Item(f, "mcp-log", _size_mb(f), "MCP log older than 1 day"))

    for name in ("shell-snapshots", "paste-cache"):
        d = ch / name
        if d.is_dir():
            for f in d.iterdir():
                if _older_than(f, 7):
                    out.items.append(Item(f, name, _size_mb(f), "older than 7 days"))

    daemon_log = ch / "daemon.log"
    cap = float(m.get("daemon_log_max_mb", 100))
    if daemon_log.is_file() and _size_mb(daemon_log) > cap:
        out.items.append(Item(daemon_log, "daemon-log", _size_mb(daemon_log), f"larger than {cap:.0f} MB (rotated, not deleted)"))

    for f in paths.logs_dir().glob("*") if paths.logs_dir().is_dir() else []:
        if f.is_file() and _older_than(f, log_days) and f.suffix in {".log", ".jsonl", ".old"}:
            out.items.append(Item(f, "hr-log", _size_mb(f), f"claude-hr log older than {log_days:.0f}d"))
    return out


def _cli_cache_dirs() -> list[Path]:
    cands = []
    if paths.IS_WINDOWS:
        cands.append(Path(os.environ.get("LOCALAPPDATA", "")) / "claude-cli-nodejs" / "Cache")
    elif paths.IS_MAC:
        cands.append(paths.home() / "Library" / "Caches" / "claude-cli-nodejs")
    else:
        cands.append(Path(os.environ.get("XDG_CACHE_HOME", paths.home() / ".cache")) / "claude-cli-nodejs")
    return [c for c in cands if c.is_dir()]


def apply(p: Plan) -> tuple[int, float]:
    """Delete the planned items. Returns (count, MB freed). Daemon log is rotated, never deleted blind."""
    n, freed = 0, 0.0
    for it in p.items:
        try:
            if it.kind == "daemon-log":
                rotated = it.path.with_suffix(".log.old")
                shutil.move(str(it.path), str(rotated))
            elif it.path.is_dir():
                shutil.rmtree(it.path)
            else:
                it.path.unlink()
            n += 1
            freed += it.size_mb
        except OSError:
            continue
    return n, freed
