"""Filesystem locations. Everything claude-hr owns lives under one directory (CLAUDE_HR_HOME)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def home() -> Path:
    return Path(os.environ.get("CLAUDE_HR_USER_HOME") or Path.home())


def hr_home() -> Path:
    """Root of everything claude-hr manages. Override with CLAUDE_HR_HOME (used by tests and sandboxes)."""
    return Path(os.environ.get("CLAUDE_HR_HOME") or (home() / ".claude-hr"))


def state_dir() -> Path:
    return hr_home() / "state"


def logs_dir() -> Path:
    return hr_home() / "logs"


def backups_dir() -> Path:
    return hr_home() / "backups"


def cache_dir() -> Path:
    return hr_home() / "cache"


def bin_dir() -> Path:
    """Where optional shims (e.g. a `claude` shim) are placed."""
    return hr_home() / "bin"


def repo_dir() -> Path:
    """Checkout that the installer clones; source of the plugin marketplace and templates."""
    env = os.environ.get("CLAUDE_HR_REPO")
    if env:
        return Path(env)
    managed = hr_home() / "repo"
    if managed.exists():
        return managed
    # Dev checkout: src/claude_hr/paths.py -> repo root is three levels up.
    here = Path(__file__).resolve()
    candidate = here.parents[2]
    if (candidate / ".claude-plugin" / "marketplace.json").exists():
        return candidate
    return managed


def package_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def claude_home() -> Path:
    """Claude Code's config directory (honours CLAUDE_CONFIG_DIR)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else home() / ".claude"


def claude_global_json() -> Path:
    """Claude Code's global state file (~/.claude.json, or inside CLAUDE_CONFIG_DIR when set)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env).expanduser() / ".claude.json") if env else home() / ".claude.json"


def claude_settings() -> Path:
    return claude_home() / "settings.json"


def claude_user_md() -> Path:
    return claude_home() / "CLAUDE.md"


def system_config_file() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "claude-hr" / "config.toml"
    return Path("/etc/claude-hr/config.toml")


def user_config_file() -> Path:
    return hr_home() / "config.toml"


def _fold(path: Path | str) -> str:
    """The real location of `path`, compared without regard to case on Windows."""
    return os.path.normcase(os.path.realpath(path))


def _within(path: str, tree: str) -> bool:
    try:
        return os.path.commonpath([path, tree]) == tree
    except ValueError:  # another drive: not inside
        return False


def state_inside(root: Path | str, cwd: Path | str | None = None) -> bool:
    """Is the state directory (it holds the trust record) inside the repository `root` or the folder `cwd` (default: the
    folder this process runs in), the folder Claude works in?

    CLAUDE_HR_HOME decides where it is, and a hook inherits the environment Claude Code built from the project's own
    .claude/settings.json: a repository could point it at a folder it ships, with a trust record already in it, and have
    its own recipe run unprompted. The hooks trust only a record the repository cannot have written, so they do nothing
    here (and write nothing: the audit directory moves with the home, so it would be a folder the repository chose). The CLI
    keeps honouring CLAUDE_HR_HOME. Not being able to tell counts as inside."""
    try:
        state = _fold(state_dir())
        trees = (_fold(root), _fold(os.getcwd() if cwd is None else cwd))
    except (OSError, ValueError):
        return True
    return any(_within(state, tree) for tree in trees)


def inside_cwd(path: Path | str, *cwds: Path | str) -> bool:
    """Is `path` inside the folder this process runs in, or inside one of `cwds` (the folders a hook payload names)?

    A hook never writes there: the folder is the repository's, and the environment that placed `path` in it may be the
    repository's too. Not being able to tell counts as inside."""
    try:
        target = _fold(path)
        trees = [_fold(os.getcwd())] + [_fold(c) for c in cwds if c]
    except (OSError, ValueError):
        return True
    return any(_within(target, tree) for tree in trees)


def hook_write(path: Path, text: str, *cwds: Path | str, append: bool = False) -> bool:
    """Write `text` to `path`, a file directly below a folder of claude-hr's home (a crash log, the status line's cache),
    for a hook process; True when it was written. Hooks run on the project's environment, which can move the home, so:

    * nothing is written when `path` is inside the working folder or one of `cwds` (see inside_cwd);
    * a link at `path` is never followed (O_NOFOLLOW where the platform has it, a look at the file where it has not);
    * only the home and the one folder below it are created, never the parents above the home.

    Best effort: any failure is False, never an exception."""
    try:
        if inside_cwd(path, *cwds) or os.path.islink(path):
            return False
        # not parents=True: a missing parent means this is not a home claude-hr made
        for d in (hr_home(), path.parent):
            d.mkdir(exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8", errors="replace"))
        return True
    except (OSError, ValueError):
        return False


def ensure_dirs() -> None:
    for d in (state_dir(), logs_dir(), backups_dir(), cache_dir(), bin_dir()):
        d.mkdir(parents=True, exist_ok=True)
    tighten_existing()


def tighten_existing() -> None:
    """Make claude-hr's private folders owner-only without creating anything. Called on every entry point so an install
    upgraded from a release that created them world-readable is fixed by the first command that runs, not the first
    scheduled job."""
    make_private(hr_home(), state_dir(), logs_dir(), backups_dir(), cache_dir())


def make_private(*dirs: Path) -> None:
    """Owner-only (0700) on POSIX. State, logs and backups hold transcript-derived reports, proxy logs and copies of
    settings.json, which other local users on a shared VM must not read. Best effort; Windows profiles are already
    private by ACL. Never touches the user's real home directory if CLAUDE_HR_HOME was pointed at it."""
    if IS_WINDOWS:
        return
    try:
        user_home = home().resolve()
    except OSError:
        user_home = None
    for d in dirs:
        try:
            if user_home is not None and d.resolve() == user_home:
                continue
            d.chmod(0o700)
        except OSError:
            pass
