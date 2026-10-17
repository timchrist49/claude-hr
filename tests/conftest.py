import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# tests/windows/*.py are scripts for a Windows machine (ctypes.WinDLL at import time), run by hand or by verify-windows.ps1:
# pytest must never import them, whatever they are called
collect_ignore_glob = ["windows/*.py"]


@pytest.fixture(scope="session")
def _empty_gitconfig(tmp_path_factory):
    """One empty git config file for the whole run, outside every test's own folder (a test may list its tmp_path)."""
    path = tmp_path_factory.mktemp("git-config") / "gitconfig"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch, _empty_gitconfig):
    """Isolated home / Claude config / claude-hr home so tests never touch the real machine."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HR_USER_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CLAUDE_HR_HOME", str(home / ".claude-hr"))
    # Every location the code may delete from must resolve inside tmp_path, never the real profile.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    # git reads its global ignore list from $XDG_CONFIG_HOME/git/ignore (else ~/.config/git/ignore), and Claude Code writes
    # `**/.claude/settings.local.json` there the first time a local plugin setting is saved: tests that ask git whether a
    # file is ignored must not depend on the machine they run on
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    # ... and the same for the rest of git's configuration: a `core.excludesFile` in the developer's ~/.gitconfig that
    # lists `.claude/` made "the recipe is kept out of git status" pass whatever the code did. An empty global file (not
    # /dev/null: some gits want a real path), no system file, and no configuration injected through the environment.
    # A test that wants a git setting sets it itself, after this, in its own repository or environment.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_empty_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k in list(os.environ):
        if k in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM"} or k.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    for k in list(os.environ):
        if k.startswith("CLAUDE_HR_") and k not in {"CLAUDE_HR_USER_HOME", "CLAUDE_HR_HOME"}:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("CLAUDE_HR_SESSION", raising=False)
    return home


@pytest.fixture(autouse=True)
def _every_test_is_sandboxed(sandbox):
    """No test can reach the real profile by forgetting the `sandbox` fixture (a hook test once wrote a crash log into
    the real ~/.claude-hr). Tests that need the path ask for `sandbox` as before and get this same instance."""


@pytest.fixture(autouse=True)
def _never_start_real_update_helpers(request, monkeypatch):
    """No test may register a scheduled task or spawn a reinstall on the machine running the suite (on Windows the real
    code path does exactly that). Tests that exercise the real functions are marked `real_helpers` and stub what they call."""
    if request.node.get_closest_marker("real_helpers"):
        return
    from claude_hr import updater

    def refuse(*args, **kwargs):
        raise AssertionError(
            "test tried to start a real self-update helper: monkeypatch it or mark the test real_helpers"
        )

    monkeypatch.setattr(updater, "_start_helper_task", refuse)
    monkeypatch.setattr(updater, "_spawn_detached", refuse)
    monkeypatch.setattr(updater, "_start_systemd_service", refuse)


# ---- guard: the suite must never touch the machine's real Claude / claude-hr profile ---------------------------------
_REAL_HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
_WATCHED = [
    _REAL_HOME
    / ".claude-hr",  # its own entries only: a directory's mtime does not see a write below it
    _REAL_HOME / ".claude" / "settings.json",
    _REAL_HOME / ".claude" / "CLAUDE.md",
    _REAL_HOME / ".claude.json",
]
# Files below ~/.claude-hr that only a person (or a leaking test) writes. NOT the files the machine's own running claude-hr
# keeps writing while the suite runs (state/gates.json after every turn, state/headroom-proxy.json, the audit trail, the
# scheduled jobs' health and install records): watching those would fail a green run whenever a session ends a turn.
_WATCHED_BELOW = [
    _REAL_HOME / ".claude-hr" / "config.toml",
    _REAL_HOME / ".claude-hr" / "state" / "verify-offer.json",
    _REAL_HOME / ".claude-hr" / "state" / "verify-trust.json",
    _REAL_HOME / ".claude-hr" / "logs" / "crash.log",
]


def _fingerprint(watched=None):
    out = {}
    for p in (_WATCHED + _WATCHED_BELOW) if watched is None else watched:
        try:
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size if p.is_file() else None)
        except OSError:
            out[str(p)] = None
    return out


def _changed(before, after):
    return {k: (before[k], after[k]) for k in before if before[k] != after[k]}


_BEFORE = _fingerprint()


def pytest_sessionfinish(session, exitstatus):
    changed = _changed(_BEFORE, _fingerprint())
    # ~/.claude.json is rewritten by any concurrently running Claude Code; only the paths claude-hr owns are a hard failure
    changed.pop(str(_REAL_HOME / ".claude.json"), None)
    if changed:
        print(f"\nTEST ISOLATION BREACH - the suite touched the real profile: {changed}")
        session.exitstatus = 1
