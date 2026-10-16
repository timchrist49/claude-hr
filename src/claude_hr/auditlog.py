"""Metadata-only audit trail: one JSON line per gate event in ~/.claude-hr/audit/YYYY-MM.jsonl.

It answers "what did the automated checks do, and when" without keeping any code, command output or secret: only counts,
durations, check names, exit codes and a hash-based repo id. Best effort: recording can never raise or slow a turn.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths

MAX_STR = 120  # no free text: names and short reasons only
MAX_LINE = 3500  # one record is one write: small enough that nothing splits it
# How long an append waits for another process's append before it goes ahead unlocked. A record is one small write, so a
# holder is done in well under a millisecond: this only matters for a holder that is stuck, and the Stop hook must not wait on it.
LOCK_WAIT_S = 0.1
# errno values that mean "somebody else holds it, try again": msvcrt.locking (LK_NBLCK) fails with EACCES, flock(LOCK_NB) with
# EWOULDBLOCK/EAGAIN, a signal with EINTR. Anything else (ENOLCK, ENOTSUP, ENOSYS, EINVAL ...) is permanent for this file.
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR, errno.EDEADLK})
# Set once the OS refused locking outright (NFS without lockd, a filesystem without locks): the rest of this process then
# appends unlocked at once instead of re-learning it on every record.
_lock_unusable = False


def _try_lock(fd: int) -> bool | None:
    """Take an exclusive lock on byte 0 of the lock file without blocking. Owned by the open file, so the OS releases it
    when the process dies: there is never a stale lock to clean up.

    True: taken. False: another process holds it (retry for a moment). None: this system cannot lock this file at all, so
    waiting is pointless."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except ImportError:
        return None
    except OSError as exc:
        return False if exc.errno in _CONTENDED else None


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        # POSIX: closing the descriptor releases the flock
    except (OSError, ImportError):
        pass


@contextmanager
def _append_lock(target: Path):
    """Serialise appenders of one month file across processes, then yield (True when the lock is held).

    O_APPEND makes a single write atomic on POSIX, but on Windows the C runtime emulates it with seek-then-write, so two
    gates that finish together lose or overwrite each other's record. The lock is best effort and fails open: after
    LOCK_WAIT_S of contention, or at once when the system cannot lock this file at all (remembered for the process), or if
    the lock file cannot be opened, the caller appends anyway, as it did before the lock existed."""
    global _lock_unusable
    fd = None
    held = False
    try:
        if not _lock_unusable:
            flags = (
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            fd = os.open(target.with_suffix(".lock"), flags, 0o600)
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                got = _try_lock(fd)
                if got is None:
                    _lock_unusable = True
                    break
                if got:
                    held = True
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.002)
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                _unlock(fd)
            try:
                os.close(fd)
            except OSError:
                pass


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
        with _append_lock(f):  # fails open: an unobtainable lock never costs the record
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
    try:  # the append lock files of month files that are gone (an old lock whose month file exists is still in use)
        for lk in audit_dir().glob("*.lock"):
            try:
                if not lk.with_suffix(".jsonl").exists() and lk.stat().st_mtime < cutoff:
                    lk.unlink()
            except OSError:
                continue
    except OSError:
        pass
    return removed
