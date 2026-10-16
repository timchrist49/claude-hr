import plistlib
import shutil
import subprocess

import pytest

from claude_hr import config, schedule

ARGV = ["/opt/claude-hr/bin/python", "-m", "claude_hr.ctl", "job"]


def test_specs_follow_config(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_MONITOR__INTERVAL_MINUTES", "10")
    monkeypatch.setenv("CLAUDE_HR_UPDATES__AT", "02:05")
    sp = {s.name: s for s in schedule.specs(config.load())}
    assert sp["monitor"].minutes == 10
    assert sp["update"].at == "02:05"
    assert set(sp) == set(schedule.JOBS)


def test_updates_can_be_disabled(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_UPDATES__ENABLED", "false")
    assert "update" not in {s.name for s in schedule.specs(config.load())}


def test_systemd_units():
    spec = schedule.Spec("monitor", "interval", minutes=15)
    service, timer = schedule.render_systemd(spec, ARGV, "/usr/bin:/bin")
    assert "ExecStart=/opt/claude-hr/bin/python -m claude_hr.ctl job monitor" in service
    assert "Type=oneshot" in service
    assert 'Environment="PATH=/usr/bin:/bin"' in service
    spaced = schedule.render_systemd(spec, ARGV, "/mnt/c/Program Files/x:/bin")[0]
    assert (
        'Environment="PATH=/mnt/c/Program Files/x:/bin"' in spaced
    )  # quoted so systemd keeps the whole value
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    _, daily = schedule.render_systemd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/bin")
    assert "OnCalendar=*-*-* 03:17:00" in daily
    _, weekly = schedule.render_systemd(
        schedule.Spec("maintain", "weekly", at="04:41", weekday=0), ARGV, "/bin"
    )
    assert "OnCalendar=Sun *-*-* 04:41:00" in weekly


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
def test_systemd_units_verify(tmp_path):
    units = tmp_path / "units"  # tmp_path also holds the sandbox profile (every test is sandboxed)
    units.mkdir()
    for spec in [
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("update", "daily", at="03:17"),
    ]:
        service, timer = schedule.render_systemd(spec, ["/bin/true"], "/usr/bin:/bin")
        (units / f"claude-hr-{spec.name}.service").write_text(service)
        (units / f"claude-hr-{spec.name}.timer").write_text(timer)
    files = [str(p) for p in units.iterdir()]
    r = subprocess.run(["systemd-analyze", "verify", *files], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_cron_block_is_replaceable():
    sp = [
        schedule.Spec("warm", "logon"),
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("update", "daily", at="03:17"),
        schedule.Spec("maintain", "weekly", at="04:41", weekday=0),
    ]
    block = schedule.render_cron(sp, ARGV, "/usr/bin")
    assert block.startswith(schedule.CRON_BEGIN)
    assert block.strip().endswith(schedule.CRON_END)
    for needle in ("*/15 * * * *", "17 3 * * *", "41 4 * * 0", "@reboot"):
        assert needle in block


def test_launchd_plist_round_trips():
    data = plistlib.loads(
        schedule.render_launchd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/usr/bin")
    )
    assert data["Label"] == "io.github.timchrist49.claude-hr.update"
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}
    assert data["ProgramArguments"][-1] == "update"
    interval = plistlib.loads(
        schedule.render_launchd(schedule.Spec("monitor", "interval", minutes=15), ARGV, "/usr/bin")
    )
    assert interval["StartInterval"] == 900


def test_windows_script_registers_every_job():
    sp = [
        schedule.Spec("warm", "logon"),
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("update", "daily", at="03:17"),
    ]
    script = schedule.render_windows_script(
        sp, [r"C:\py\pythonw.exe", "-m", "claude_hr.ctl", "job"]
    )
    for name in ("warm", "monitor", "update"):
        assert f"claude-hr\\{name}" in script
    for needle in ("-AtLogOn", "-RepetitionInterval", "-MultipleInstances IgnoreNew"):
        assert needle in script


def test_cron_install_never_overwrites_an_unreadable_crontab(sandbox, monkeypatch):
    import subprocess as sp

    from claude_hr import procutil

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:2] == ["crontab", "-l"]:
            return sp.CompletedProcess(cmd, 1, "", "crontab: timeout while reading")
        return sp.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(procutil, "run", fake_run)
    monkeypatch.setattr(schedule.paths, "IS_WINDOWS", False)
    monkeypatch.setattr(schedule.paths, "IS_MAC", False)
    monkeypatch.setattr(schedule, "_systemd_user_ok", lambda: False)
    msgs = schedule.install(config.load())
    assert "not touching it" in msgs[0]
    assert ["crontab", "-"] not in calls


def _windows_remove(monkeypatch, returncode, stdout="", stderr=""):
    from claude_hr import procutil

    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(procutil, "run", fake_run)
    monkeypatch.setattr(schedule.paths, "IS_WINDOWS", True)
    return schedule.remove(), seen["cmd"]


def test_windows_remove_with_no_tasks_is_not_a_failure(sandbox, monkeypatch):
    """Regression: uninstall printed '[ok ] schedule: FAILED' when nothing had ever been registered (-SkipSchedule)."""
    msgs, cmd = _windows_remove(monkeypatch, 0, stdout="0\n")
    assert msgs == ["Task Scheduler: nothing to remove"]
    assert "Count -gt 0" in cmd[-1] and "exit 1" in cmd[-1]


def test_windows_remove_only_touches_this_installs_tasks(sandbox, monkeypatch):
    """Regression: uninstall in a sandbox (or a second profile) removed EVERY task under \\claude-hr\\ on the machine."""
    msgs, cmd = _windows_remove(monkeypatch, 0, stdout="0\n")
    script = cmd[-1]
    mine = str(schedule.Path(schedule.job_argv()[0]).parent)
    assert f"$mine = '{mine}'" in script
    assert (
        "StartsWith($mine" in script
    )  # only tasks whose action runs from this install's environment
    assert (
        "'selfupdate'" in script and "Test-Path -LiteralPath $_.Execute" in script
    )  # + the one-shot helper and orphans
    # the real PowerShell parser must accept it (parse only; nothing is run)
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    if shell:
        check = (
            "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($args[0], [ref]$t, [ref]$e); "
            "if ($e.Count -eq 0) { 'PARSE_OK' } else { $e | ForEach-Object { $_.Message } }"
        )
        out = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", check, script],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        assert "PARSE_OK" in out, out


def test_windows_remove_reports_how_many_tasks_went_and_real_failures(sandbox, monkeypatch):
    assert _windows_remove(monkeypatch, 0, stdout="4\n")[0] == ["Task Scheduler: removed 4 task(s)"]
    msgs = _windows_remove(monkeypatch, 1, stderr="Access is denied")[0]
    assert msgs and msgs[0].startswith("FAILED") and "Access is denied" in msgs[0]


def test_update_job_is_allowed_to_leave_its_reinstall_running(sandbox):
    """The update job ends by starting the reinstall as a child; systemd (control-group) and launchd would kill it with the job."""
    service, _ = schedule.render_systemd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/bin")
    assert "KillMode=process" in service
    plist = plistlib.loads(
        schedule.render_launchd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/bin")
    )
    assert plist["AbandonProcessGroup"] is True
