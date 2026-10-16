import os
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


def _powershell_parse(script: str) -> str | None:
    """Run the real PowerShell PARSER over `script` (None when this machine has no PowerShell). The text travels in an
    environment variable and is only parsed, never executed: handed to `-Command` as an argument it would RUN, and these
    scripts register and unregister tasks in the real Task Scheduler."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return None
    check = (
        "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseInput("
        "$env:CLAUDE_HR_TEST_PS_SCRIPT, [ref]$t, [ref]$e); "
        "if ($e.Count -eq 0) { 'PARSE_OK' } else { $e | ForEach-Object { $_.Message } }"
    )
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "CLAUDE_HR_TEST_PS_SCRIPT": script},
    ).stdout


def _ps_literals(text: str) -> list[str]:
    """The single-quoted PowerShell string literals of `text`, decoded (a doubled quote is one quote). Any of the four
    typographic quotes PowerShell accepts opens and closes a literal too; an unterminated one raises."""
    quotes = schedule._PS_QUOTES
    out, i = [], 0
    while i < len(text):
        if text[i] not in quotes:
            i += 1
            continue
        i += 1
        buf = []
        while True:
            if i >= len(text):
                raise ValueError("unterminated PowerShell string literal")
            c = text[i]
            if c in quotes:
                if i + 1 < len(text) and text[i + 1] in quotes:
                    buf.append(c)  # a doubled quote is one quote character
                    i += 2
                    continue
                i += 1
                break
            buf.append(c)
            i += 1
        out.append("".join(buf))
    return out


@pytest.mark.parametrize(
    "exe",
    [
        r"C:\Users\Tim O'Brien\AppData\Roaming\uv\tools\claude-hr\Scripts\pythonw.exe",
        "C:\\Users\\Tim O\u2019Brien\\AppData\\Roaming\\uv\\tools\\claude-hr\\Scripts\\pythonw.exe",
        "C:\\Users\\a'b \u2018c\u201a d\u201be\\pythonw.exe",
    ],
)
def test_windows_script_survives_an_apostrophe_in_the_interpreter_path(exe):
    """Reproduced: the path went into a PowerShell single-quoted literal unescaped, so `Tim O'Brien` cut -Execute to
    `C:\\Users\\Tim O`, threw a positional-parameter error for the first job and registered no task at all."""
    sp = [
        schedule.Spec("warm", "logon"),
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("update", "daily", at="03:17"),
        schedule.Spec("maintain", "weekly", at="04:41", weekday=0),
    ]
    script = schedule.render_windows_script(sp, [exe, "-m", "claude_hr.ctl", "job"])
    for line in script.splitlines():
        _ps_literals(line)  # every line's quotes pair up
    registers = [ln for ln in script.splitlines() if ln.startswith("Register-ScheduledTask")]
    assert len(registers) == 4
    for line, spec in zip(registers, sp, strict=True):
        lits = _ps_literals(line)
        assert lits[0] == f"claude-hr\\{spec.name}"  # -TaskName
        assert lits[1] == exe  # -Execute: the path, exactly
        assert lits[2] == f"-m claude_hr.ctl job {spec.name}"  # -Argument
    assert script.count("Register-ScheduledTask") == 4


def test_windows_script_with_an_apostrophe_parses_in_real_powershell():
    """An odd number of jobs (updates.enabled = false) is what made the unescaped apostrophe a parse error:
    'The string is missing the terminator'."""
    sp = [
        schedule.Spec("warm", "logon"),
        schedule.Spec("monitor", "interval", minutes=15),
        schedule.Spec("maintain", "weekly", at="04:41", weekday=0),
    ]
    exe = r"C:\Users\Tim O'Brien\AppData\Roaming\uv\tools\claude-hr\Scripts\pythonw.exe"
    out = _powershell_parse(schedule.render_windows_script(sp, [exe, "-m", "claude_hr.ctl", "job"]))
    assert out is None or "PARSE_OK" in out, out


def test_windows_script_escapes_a_time_of_day_from_the_config_too():
    script = schedule.render_windows_script(
        [schedule.Spec("update", "daily", at="03:17'; Remove-Item x; '")],
        [r"C:\py\pythonw.exe", "-m", "claude_hr.ctl", "job"],
    )
    assert "-At '03:17''; Remove-Item x; '''" in script
    assert "03:17'; Remove-Item" not in script


def test_ps_quote_doubles_every_quote_character_powershell_accepts():
    assert schedule.ps_quote("plain") == "'plain'"
    assert schedule.ps_quote("it's") == "'it''s'"
    assert schedule.ps_quote("a\u2018b\u2019c\u201ad\u201be") == (
        "'a\u2018\u2018b\u2019\u2019c\u201a\u201ad\u201b\u201be'"
    )
    assert (
        schedule.ps_quote("$env:X `n") == "'$env:X `n'"
    )  # nothing else is special inside single quotes


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
    out = _powershell_parse(
        script
    )  # the real PowerShell parser must accept it (None: no PowerShell here)
    assert out is None or "PARSE_OK" in out, out


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
