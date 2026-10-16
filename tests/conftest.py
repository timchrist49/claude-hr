import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
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
    _REAL_HOME / ".claude-hr",
    _REAL_HOME / ".claude" / "settings.json",
    _REAL_HOME / ".claude" / "CLAUDE.md",
    _REAL_HOME / ".claude.json",
]


def _fingerprint():
    out = {}
    for p in _WATCHED:
        try:
            out[str(p)] = p.stat().st_mtime_ns if p.exists() else None
        except OSError:
            out[str(p)] = None
    return out


_BEFORE = _fingerprint()


def pytest_sessionfinish(session, exitstatus):
    after = _fingerprint()
    changed = {k: (_BEFORE[k], after[k]) for k in _BEFORE if _BEFORE[k] != after[k]}
    # ~/.claude.json is rewritten by any concurrently running Claude Code; only the paths claude-hr owns are a hard failure
    changed.pop(str(_REAL_HOME / ".claude.json"), None)
    if changed:
        print(f"\nTEST ISOLATION BREACH - the suite touched the real profile: {changed}")
        session.exitstatus = 1
