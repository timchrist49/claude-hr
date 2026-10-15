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
