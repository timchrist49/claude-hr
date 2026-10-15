"""Small, safe JSON file helpers: atomic writes, rotating backups, tolerant reads."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any


class JsonFileError(RuntimeError):
    """Raised when a JSON file exists but cannot be parsed. We never overwrite such files."""


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON. Missing/empty file -> default. Corrupt file -> JsonFileError (never silently clobbered)."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonFileError(f"{path} is not valid JSON ({exc}); refusing to modify it") from exc


def backup_file(path: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """Copy `path` into backup_dir/<name>.<timestamp>.bak and prune old copies. Returns the backup path."""
    if not path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = backup_dir / f"{path.name}.{stamp}.bak"
    n = 1
    while dest.exists():  # two writes inside one second
        dest = backup_dir / f"{path.name}.{stamp}-{n}.bak"
        n += 1
    shutil.copy2(path, dest)
    olds = sorted(backup_dir.glob(f"{path.name}.*.bak"))
    for old in olds[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def atomic_write_text(path: Path, text: str, newline: str = "\n") -> None:
    """Write text atomically (temp file in the same dir + os.replace).

    * A symlinked target (dotfiles repos) is written THROUGH the link, not replaced by a regular file.
    * The existing file's permission bits are kept (mkstemp would otherwise force 0600).
    * On Windows `os.replace` fails with PermissionError while another process holds the file for an instant, so
      it is retried briefly - concurrent launches must not crash on each other's bookkeeping.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        if mode is not None and os.name != "nt":
            os.chmod(tmp, mode)
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, data: Any, backup_dir: Path | None = None) -> None:
    if backup_dir is not None:
        backup_file(path, backup_dir)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
