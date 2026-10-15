"""Tiny cross-platform advisory lock based on O_EXCL lock files, with stale-owner detection."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import sysinfo


class LockTimeout(TimeoutError):
    pass


@contextmanager
def locked(path: Path, timeout: float = 30.0, stale_after: float = 600.0):
    """Acquire `path` exclusively. A lock whose owner PID is dead, or older than stale_after, is stolen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}\n".encode())
            break
        except FileExistsError:
            if _is_stale(path, stale_after):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout(f"timed out waiting for lock {path}") from None
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass


def _is_stale(path: Path, stale_after: float) -> bool:
    try:
        age = time.time() - path.stat().st_mtime
        owner = int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return True
    if owner and not sysinfo.pid_alive(owner):
        return True
    return age > stale_after
