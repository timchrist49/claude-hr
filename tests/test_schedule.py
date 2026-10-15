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
    assert 'Environment="PATH=/mnt/c/Program Files/x:/bin"' in spaced  # quoted so systemd keeps the whole value
    assert "OnUnitActiveSec=15min" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    _, daily = schedule.render_systemd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/bin")
    assert "OnCalendar=*-*-* 03:17:00" in daily
    _, weekly = schedule.render_systemd(schedule.Spec("maintain", "weekly", at="04:41", weekday=0), ARGV, "/bin")
    assert "OnCalendar=Sun *-*-* 04:41:00" in weekly


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
def test_systemd_units_verify(tmp_path):
    for spec in [schedule.Spec("monitor", "interval", minutes=15), schedule.Spec("update", "daily", at="03:17")]:
        service, timer = schedule.render_systemd(spec, ["/bin/true"], "/usr/bin:/bin")
        (tmp_path / f"claude-hr-{spec.name}.service").write_text(service)
        (tmp_path / f"claude-hr-{spec.name}.timer").write_text(timer)
    files = [str(p) for p in tmp_path.iterdir()]
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
    data = plistlib.loads(schedule.render_launchd(schedule.Spec("update", "daily", at="03:17"), ARGV, "/usr/bin"))
    assert data["Label"] == "io.github.timchrist49.claude-hr.update"
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 17}
    assert data["ProgramArguments"][-1] == "update"
    interval = plistlib.loads(schedule.render_launchd(schedule.Spec("monitor", "interval", minutes=15), ARGV, "/usr/bin"))
    assert interval["StartInterval"] == 900


def test_windows_script_registers_every_job():
    sp = [
        schedule.Spec("warm", "logon"),
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("update", "daily", at="03:17"),
    ]
    script = schedule.render_windows_script(sp, [r"C:\py\pythonw.exe", "-m", "claude_hr.ctl", "job"])
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
