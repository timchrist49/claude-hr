"""Background programs claude-hr starts on Windows must not open a window (platform independent unit tests).

The real-window test is tests/test_windows_console.py; these check what the code asks Windows for.
"""

from __future__ import annotations

import subprocess

import pytest

from claude_hr import config, monitor, paths, procutil, updater

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def test_the_shared_flags_are_no_window_and_a_new_group_and_never_detached():
    flags = procutil.windows_background_flags()
    assert flags & CREATE_NO_WINDOW and flags & 0x00000200
    assert not flags & DETACHED_PROCESS
    assert (
        procutil.CREATE_NO_WINDOW == CREATE_NO_WINDOW
        and procutil.CREATE_BREAKAWAY_FROM_JOB == 0x01000000
    )


# ---- the self-update helper task -------------------------------------------------------------------------------------------
@pytest.fixture
def windows_dirs(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(updater.os.path, "isfile", lambda p: True)
    updater._HEADLESS_OK.clear()
    yield
    updater._HEADLESS_OK.clear()


def test_the_helper_task_runs_powershell_through_a_headless_conhost_when_it_can(
    windows_dirs, monkeypatch
):
    """A task action that is a console program gets a visible Windows Terminal window for the whole reinstall (and closing it
    kills the reinstall); `conhost.exe --headless` gives it a console without one."""
    seen = []
    monkeypatch.setattr(
        updater.procutil,
        "run",
        lambda cmd, **k: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    execute, argument = updater._helper_task_action('-NoProfile -File "x.ps1"')
    assert execute.lower().endswith("conhost.exe")
    assert argument.startswith("--headless ") and "powershell.exe" in argument
    assert argument.endswith('-NoProfile -File "x.ps1"')
    assert seen and "--headless" in seen[0], (
        "it asked conhost once whether it understands --headless"
    )
    updater._helper_task_action("x")
    assert len(seen) == 1, "and remembered the answer"


def test_the_helper_task_falls_back_to_plain_powershell_where_conhost_has_no_headless(
    windows_dirs, monkeypatch
):
    monkeypatch.setattr(
        updater.procutil,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "unknown option"),
    )
    assert updater._helper_task_action("-File x") == ("powershell.exe", "-File x")


def test_the_helper_task_falls_back_when_there_is_no_conhost(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(updater.os.path, "isfile", lambda p: False)
    updater._HEADLESS_OK.clear()
    assert updater._helper_task_action("-File x") == ("powershell.exe", "-File x")


@pytest.mark.real_helpers
def test_the_registered_task_uses_the_headless_action(windows_dirs, sandbox, monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(updater.procutil, "run", fake_run)
    assert updater._start_helper_task("powershell.exe", tmp_path / "self-update.ps1") is True
    ps = calls[-1][-1]
    assert "conhost.exe" in ps and "--headless" in ps and "self-update.ps1" in ps
    assert ps.index("Register-ScheduledTask") < ps.index("Start-ScheduledTask")


# ---- the monitor's alert command ------------------------------------------------------------------------------------------
def _deliver(monkeypatch, sandbox, windows: bool):
    paths.ensure_dirs()  # deliver() also appends to alerts.log
    calls = []
    monkeypatch.setenv("CLAUDE_HR_MONITOR__ALERT_COMMAND", "notify-me --now")
    monkeypatch.setattr(monitor.paths, "IS_WINDOWS", windows)
    monkeypatch.setattr(
        monitor.subprocess, "run", lambda argv, **k: calls.append((argv, k)) or None
    )
    monitor.deliver(config.load(), monitor.Alert("proxy", "Headroom down", "it is down"))
    return calls


def test_the_alert_command_gets_no_window_on_windows(monkeypatch, sandbox):
    """The monitor job runs under pythonw.exe (no console): a console alert program started without CREATE_NO_WINDOW opens
    a terminal window every time it fires."""
    (argv, kwargs), *_ = _deliver(monkeypatch, sandbox, windows=True)
    assert argv == ["notify-me", "--now"]
    assert (
        kwargs["creationflags"] & CREATE_NO_WINDOW
        and not kwargs["creationflags"] & DETACHED_PROCESS
    )
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_the_alert_command_is_started_as_before_elsewhere(monkeypatch, sandbox):
    (argv, kwargs), *_ = _deliver(monkeypatch, sandbox, windows=False)
    assert argv == ["notify-me", "--now"] and "creationflags" not in kwargs
