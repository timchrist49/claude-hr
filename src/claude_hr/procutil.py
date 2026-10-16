"""Process / network helpers shared by the launcher and the control CLI.

Import cost matters: this module is imported on EVERY `claude-hr` start, and on Windows importing urllib /
subprocess / socket eagerly cost ~180 ms. Heavy modules are therefore imported inside the functions that need them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import paths

_OPENER = None  # loopback opener that ignores HTTP(S)_PROXY, built on first use


def _opener():
    global _OPENER
    if _OPENER is None:
        import urllib.request

        _OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENER


def _exts() -> list[str]:
    if not paths.IS_WINDOWS:
        return [""]
    pathext = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    # .exe first: it is what native installers drop, and it avoids probing a dozen script extensions per directory
    return [".exe"] + [e for e in pathext if e != ".exe"]


def iter_which(name: str, path: str | None = None):
    """Yield every executable called `name` on PATH, lazily and in order (stops costing once the caller stops)."""
    search = path if path is not None else os.environ.get("PATH", "")
    exts = _exts()
    seen: set[str] = set()
    for directory in search.split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            cand = os.path.join(directory, name + ext)
            key = os.path.normcase(cand)
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                yield Path(cand)


def _env_path(env: dict[str, str] | None) -> str:
    """PATH of `env` (case-insensitive key on Windows, where os.environ's own key is `Path`), else of this process."""
    if env is None:
        return os.environ.get("PATH", "")
    for key, value in env.items():
        if key.upper() == "PATH":
            return value
    return ""


def resolve_exe(name: str, env: dict[str, str] | None = None) -> str | None:
    """Absolute path of the bare command `name`, searched ONLY through absolute PATH entries; a name that already has a
    path separator is returned as given (the caller chose it).

    Never the current directory: on Windows CreateProcess (and shutil.which) look there before PATH, and the current
    directory of a hook or of `claude-hr-ctl` run from a freshly cloned repository is attacker-controlled (a committed
    `git.exe` would run on every `git` call). Relative and empty PATH entries are skipped for the same reason on every OS.
    """
    if os.sep in name or (os.altsep and os.altsep in name):
        return name
    absolute = os.pathsep.join(
        d for d in _env_path(env).split(os.pathsep) if d and os.path.isabs(d)
    )
    for found in iter_which(name, absolute):
        return str(found)
    return None


def which_all(name: str, path: str | None = None) -> list[Path]:
    """Every executable called `name` on PATH, in order (shutil.which only returns the first)."""
    return list(iter_which(name, path))


def _is_shim(path: Path) -> bool:
    """A claude-hr `claude` shim: lives in our bin dir, or is a tiny script that just calls claude-hr (other CLAUDE_HR_HOME)."""
    try:
        if Path(os.path.realpath(path)).parent == Path(os.path.realpath(paths.bin_dir())):
            return True
        if os.path.getsize(path) < 1024:
            return b"claude-hr" in Path(path).read_bytes()
    except OSError:
        pass
    return False


def _native_fallbacks() -> list[Path]:
    exe = "claude.exe" if paths.IS_WINDOWS else "claude"
    return [
        paths.home() / ".local" / "bin" / exe,
        paths.claude_home() / "local" / exe,
        Path("/usr/local/bin") / exe,
    ]


def find_claude(configured: str = "") -> Path | None:
    """Locate the REAL Claude Code binary, skipping any claude-hr shim named `claude`.

    The answer is cached per PATH value (scanning a long Windows PATH took ~110 ms on every launch).
    """
    for explicit in (os.environ.get("CLAUDE_HR_CLAUDE_BIN"), configured):
        if explicit and os.path.isfile(explicit):
            return Path(explicit)
    import zlib

    # stable digest (str hash() is salted per process) of everything that can change the answer
    stamp = f"{zlib.crc32((os.environ.get('PATH', '') + str(paths.bin_dir())).encode()) & 0xFFFFFFFF:08x}"
    cache = paths.state_dir() / "claude-path.txt"
    try:
        cached_stamp, _, cached = cache.read_text(encoding="utf-8").partition("\n")
        if cached_stamp == stamp and cached.strip() and os.path.isfile(cached.strip()):
            return Path(cached.strip())
    except (OSError, UnicodeDecodeError):
        pass
    found: Path | None = next((c for c in iter_which("claude") if not _is_shim(c)), None)
    if found is None:
        found = next((fb for fb in _native_fallbacks() if fb.is_file()), None)
    if found is not None:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(f"{stamp}\n{found}\n", encoding="utf-8")
        except OSError:
            pass
    return found


def run(
    cmd: list[str],
    timeout: float | None = 60,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
    input_text: str | None = None,
):
    """Run a command capturing text output. Never raises for missing exe / timeout: returns rc 127 / 126 / 124."""
    import subprocess

    extra: dict = {}
    if paths.IS_WINDOWS:
        extra["creationflags"] = (
            0x08000000  # CREATE_NO_WINDOW: pythonw-run jobs would otherwise flash a console per call
        )
    if cmd:
        exe = resolve_exe(str(cmd[0]), env)  # never from the current directory (see resolve_exe)
        if exe is None:
            return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found on PATH")
        cmd = [exe, *cmd[1:]]
    try:
        return subprocess.run(
            cmd,
            **extra,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            cwd=cwd,
            input=input_text,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        out = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        return subprocess.CompletedProcess(cmd, 124, out, f"timeout after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 126, "", str(exc))


def http_json(url: str, timeout: float = 0.6, loopback: bool = True) -> tuple[int, Any]:
    """GET url -> (status, parsed-json-or-text). Status 0 on connection failure.

    loopback=True ignores HTTP(S)_PROXY (health checks must hit the local proxy directly); loopback=False honours it,
    which update checks against PyPI need behind a corporate proxy.
    """
    import json
    import urllib.error
    import urllib.request

    opener = _opener() if loopback else urllib.request.build_opener()
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None


def port_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_port(preferred: int | None = None, host: str = "127.0.0.1") -> int:
    import socket

    if preferred and not port_listening(host, preferred):
        return preferred
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def cmd_exists(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None
