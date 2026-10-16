"""Cross-platform scheduled jobs: Windows Task Scheduler, systemd --user timers (cron fallback), launchd.

Jobs (all run `python -m claude_hr.ctl job <name>`, with the absolute interpreter of the claude-hr env):
  warm      at login / boot: start the shared Headroom proxy so the first session doesn't wait ~1 min
  monitor   every N minutes: health snapshot, self-heal, alerts
  update    daily: staged upgrades with smoke tests and rollback
  maintain  weekly: disk hygiene, usage snapshot, CLAUDE.md audit
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from . import paths, procutil
from .config import Config

JOBS = ("warm", "monitor", "update", "maintain")
PREFIX = "claude-hr"
CRON_BEGIN = "# >>> claude-hr managed block >>>"
CRON_END = "# <<< claude-hr managed block <<<"


@dataclass
class Spec:
    name: str
    kind: str  # logon | interval | daily | weekly
    minutes: int = 0
    at: str = "03:17"
    weekday: int = 0  # 0 = Sunday


def specs(cfg: Config) -> list[Spec]:
    out = [
        Spec("warm", "logon"),
        Spec("monitor", "interval", minutes=int(cfg.get("monitor.interval_minutes", 15))),
    ]
    if cfg.flag("updates.enabled", True):
        out.append(Spec("update", "daily", at=str(cfg.get("updates.at", "03:17"))))
    out.append(Spec("maintain", "weekly", at="04:41", weekday=0))
    return out


def job_argv() -> list[str]:
    py = sys.executable
    if paths.IS_WINDOWS:  # pythonw: no console window flashing every N minutes
        pyw = Path(py).with_name("pythonw.exe")
        if pyw.exists():
            py = str(pyw)
    return [py, "-m", "claude_hr.ctl", "job"]


# ---------------------------------------------------------------------------------------------
# rendering (pure functions: unit-tested without touching the OS)
# ---------------------------------------------------------------------------------------------
def render_systemd(spec: Spec, argv: list[str], path_env: str) -> tuple[str, str]:
    cmd = " ".join(shlex.quote(a) for a in [*argv, spec.name])
    service = (
        f"[Unit]\nDescription=claude-hr {spec.name}\n\n"
        # quoted: systemd splits an unquoted Environment= value on whitespace ("Program Files" in a WSL PATH would be cut)
        # KillMode=process: the daily update ends by starting its own reinstall as a child; the default (control-group) would
        # kill that child together with the job the moment the job exits
        f'[Service]\nType=oneshot\nKillMode=process\nNice=10\nEnvironment="PATH={path_env.replace(chr(34), "")}"\nExecStart={cmd}\n'
    )
    if spec.kind == "logon":
        when = "OnBootSec=45s\nOnStartupSec=45s"
    elif spec.kind == "interval":
        when = f"OnBootSec=3min\nOnUnitActiveSec={spec.minutes}min"
    elif spec.kind == "daily":
        when = f"OnCalendar=*-*-* {spec.at}:00\nRandomizedDelaySec=900"
    else:
        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][spec.weekday]
        when = f"OnCalendar={days} *-*-* {spec.at}:00\nRandomizedDelaySec=900"
    timer = (
        f"[Unit]\nDescription=claude-hr {spec.name} timer\n\n[Timer]\n{when}\nPersistent=true\n"
        f"Unit={PREFIX}-{spec.name}.service\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def render_cron(all_specs: list[Spec], argv: list[str], path_env: str) -> str:
    lines = [CRON_BEGIN, f"PATH={path_env}"]
    for s in all_specs:
        cmd = " ".join(shlex.quote(a) for a in [*argv, s.name])
        hh, mm = (s.at.split(":") + ["0"])[:2]
        if s.kind == "logon":
            lines.append(f"@reboot sleep 30 && {cmd}")
        elif s.kind == "interval":
            lines.append(f"*/{max(1, s.minutes)} * * * * {cmd}")
        elif s.kind == "daily":
            lines.append(f"{int(mm)} {int(hh)} * * * {cmd}")
        else:
            lines.append(f"{int(mm)} {int(hh)} * * {s.weekday} {cmd}")
    lines.append(CRON_END)
    return "\n".join(lines) + "\n"


def render_launchd(spec: Spec, argv: list[str], path_env: str) -> bytes:
    d: dict = {
        "Label": f"io.github.timchrist49.{PREFIX}.{spec.name}",
        "ProgramArguments": [*argv, spec.name],
        "EnvironmentVariables": {"PATH": path_env},
        "ProcessType": "Background",
        "AbandonProcessGroup": True,  # launchd would otherwise kill the reinstall the update job starts as its last act
        "Nice": 10,
        "StandardOutPath": str(paths.logs_dir() / f"{spec.name}.out.log"),
        "StandardErrorPath": str(paths.logs_dir() / f"{spec.name}.err.log"),
    }
    if spec.kind == "logon":
        d["RunAtLoad"] = True
    elif spec.kind == "interval":
        d["StartInterval"] = spec.minutes * 60
        d["RunAtLoad"] = True
    else:
        hh, mm = (spec.at.split(":") + ["0"])[:2]
        cal = {"Hour": int(hh), "Minute": int(mm)}
        if spec.kind == "weekly":
            cal["Weekday"] = spec.weekday
        d["StartCalendarInterval"] = cal
    return plistlib.dumps(d)


_PS_QUOTES = "'\u2018\u2019\u201a\u201b"  # PowerShell treats all four typographic single quotes like an ASCII one


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted string literal. PowerShell also accepts U+2018/2019/201A/201B as quote characters, so every
    one of them is doubled: a typographic apostrophe in a path or environment value must not end the string early. Nothing
    else is special inside single quotes (no `$` expansion, no escapes)."""
    return "'" + "".join(c * 2 if c in _PS_QUOTES else c for c in str(value)) + "'"


def render_windows_script(all_specs: list[Spec], argv: list[str]) -> str:
    """The PowerShell that registers every job. Every interpolated value goes through ps_quote: the interpreter path comes
    from the profile / UV_TOOL_DIR (C:\\Users\\Tim O'Brien ...) and the time of day from config.toml."""
    exe, rest = argv[0], argv[1:]
    args = " ".join(f'"{a}"' if " " in a else a for a in rest)
    lines = ["$ErrorActionPreference = 'Stop'"]
    for s in all_specs:
        name = f"claude-hr\\{s.name}"
        action = f"New-ScheduledTaskAction -Execute {ps_quote(exe)} -Argument {ps_quote(f'{args} {s.name}')}"
        if s.kind == "logon":
            trig = "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME"
        elif s.kind == "interval":
            trig = f"New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes {int(s.minutes)})"
        elif s.kind == "daily":
            trig = f"New-ScheduledTaskTrigger -Daily -At {ps_quote(s.at)}"
        else:
            day = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][
                s.weekday
            ]
            trig = f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day} -At {ps_quote(s.at)}"
        lines.append(
            f"Register-ScheduledTask -TaskName {ps_quote(name)} -Force -Action ({action}) -Trigger ({trig}) "
            "-Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)) "
            "-Description 'Managed by claude-hr-ctl schedule' | Out-Null"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------------------------
def _systemd_user_ok() -> bool:
    if not paths.IS_LINUX or not procutil.cmd_exists("systemctl"):
        return False
    return procutil.run(["systemctl", "--user", "show-environment"], timeout=8).returncode == 0


def install(cfg: Config) -> list[str]:
    paths.ensure_dirs()
    sp = specs(cfg)
    argv = job_argv()
    path_env = os.environ.get("PATH", "")
    msgs: list[str] = []
    if paths.IS_WINDOWS:
        script = render_windows_script(sp, argv)
        r = procutil.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=120,
        )
        msgs.append(
            "Task Scheduler: "
            + (
                "registered " + ", ".join(s.name for s in sp)
                if r.returncode == 0
                else f"FAILED {r.stderr.strip()[:300]}"
            )
        )
    elif paths.IS_MAC:
        d = paths.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        for s in sp:
            f = d / f"io.github.timchrist49.{PREFIX}.{s.name}.plist"
            f.write_bytes(render_launchd(s, argv, path_env))
            procutil.run(["launchctl", "bootout", f"gui/{uid}", str(f)], timeout=20)
            r = procutil.run(["launchctl", "bootstrap", f"gui/{uid}", str(f)], timeout=20)
            msgs.append(
                f"launchd {s.name}: "
                + ("ok" if r.returncode == 0 else f"FAILED {r.stderr.strip()[:200]}")
            )
    elif _systemd_user_ok():
        d = paths.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        for s in sp:
            service, timer = render_systemd(s, argv, path_env)
            (d / f"{PREFIX}-{s.name}.service").write_text(service)
            (d / f"{PREFIX}-{s.name}.timer").write_text(timer)
        procutil.run(["systemctl", "--user", "daemon-reload"], timeout=20)
        for s in sp:
            r = procutil.run(
                ["systemctl", "--user", "enable", "--now", f"{PREFIX}-{s.name}.timer"], timeout=20
            )
            msgs.append(
                f"systemd timer {s.name}: "
                + ("ok" if r.returncode == 0 else f"FAILED {r.stderr.strip()[:200]}")
            )
        msgs.append(
            "headless VM? run once as root: loginctl enable-linger $USER  (keeps timers running without a login)"
        )
    else:
        cur = procutil.run(["crontab", "-l"], timeout=10)
        if cur.returncode != 0 and "no crontab for" not in (cur.stderr + cur.stdout).lower():
            # a timeout / permission error is NOT "empty": writing back would wipe the user's whole crontab
            return [
                f"cron: could not read the current crontab ({(cur.stderr or cur.stdout).strip()[:120]}); not touching it"
            ]
        base = cur.stdout if cur.returncode == 0 else ""
        base = re.sub(
            re.escape(CRON_BEGIN) + r".*?" + re.escape(CRON_END) + r"\n?", "", base, flags=re.S
        )
        new = base.rstrip("\n") + ("\n" if base.strip() else "") + render_cron(sp, argv, path_env)
        r = procutil.run(["crontab", "-"], timeout=10, input_text=new)
        msgs.append(
            "cron: "
            + (
                "installed managed block"
                if r.returncode == 0
                else f"FAILED {r.stderr.strip()[:200]}"
            )
        )
    return msgs


def remove() -> list[str]:
    msgs: list[str] = []
    if paths.IS_WINDOWS:
        # Get-ScheduledTask finds nothing when no job was ever registered (e.g. installed with -SkipSchedule): that is a
        # successful removal, not an error. The script prints how many tasks existed.
        # Only tasks that belong to THIS install: they run pythonw.exe from this environment. The one-shot self-update helper
        # and orphans whose executable no longer exists go too; another install's jobs (a sandbox, a second profile) stay.
        mine = ps_quote(str(Path(job_argv()[0]).parent))
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"try {{ $mine = {mine}; "
            "$t = @(Get-ScheduledTask -TaskPath '\\claude-hr\\' -ErrorAction SilentlyContinue | Where-Object { "
            "$_.TaskName -eq 'selfupdate' -or @($_.Actions | Where-Object { $_.Execute -and "
            "($_.Execute.StartsWith($mine, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $_.Execute)) }).Count -gt 0 }); "
            "if ($t.Count -gt 0) { $t | Unregister-ScheduledTask -Confirm:$false }; Write-Output $t.Count } "
            "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
        )
        r = procutil.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=60
        )
        if r.returncode != 0:
            msgs.append(f"FAILED {(r.stderr or r.stdout).strip()[:200]}")
        elif (r.stdout or "").strip() in ("", "0"):
            msgs.append("Task Scheduler: nothing to remove")
        else:
            msgs.append(f"Task Scheduler: removed {(r.stdout or '').strip()} task(s)")
    elif paths.IS_MAC:
        uid = os.getuid()
        for s in JOBS:
            f = (
                paths.home()
                / "Library"
                / "LaunchAgents"
                / f"io.github.timchrist49.{PREFIX}.{s}.plist"
            )
            if f.exists():
                procutil.run(["launchctl", "bootout", f"gui/{uid}", str(f)], timeout=20)
                f.unlink()
        msgs.append("launchd: removed")
    else:
        if _systemd_user_ok():
            d = paths.home() / ".config" / "systemd" / "user"
            for s in JOBS:
                procutil.run(
                    ["systemctl", "--user", "disable", "--now", f"{PREFIX}-{s}.timer"], timeout=20
                )
                for ext in ("service", "timer"):
                    (d / f"{PREFIX}-{s}.{ext}").unlink(missing_ok=True)
            procutil.run(["systemctl", "--user", "daemon-reload"], timeout=20)
            msgs.append("systemd timers: removed")
        if procutil.cmd_exists("crontab"):
            cur = procutil.run(["crontab", "-l"], timeout=10)
            if cur.returncode == 0 and CRON_BEGIN in cur.stdout:
                new = re.sub(
                    re.escape(CRON_BEGIN) + r".*?" + re.escape(CRON_END) + r"\n?",
                    "",
                    cur.stdout,
                    flags=re.S,
                )
                procutil.run(["crontab", "-"], timeout=10, input_text=new)
                msgs.append("cron block: removed")
    return msgs


def status() -> list[str]:
    out: list[str] = []
    if paths.IS_WINDOWS:
        r = procutil.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-ScheduledTask -TaskPath '\\claude-hr\\' -ErrorAction SilentlyContinue | "
                "ForEach-Object { $i = $_ | Get-ScheduledTaskInfo; '{0} {1} last={2} next={3}' -f $_.TaskName, $_.State, $i.LastRunTime, $i.NextRunTime }",
            ],
            timeout=60,
        )
        out = [ln for ln in r.stdout.splitlines() if ln.strip()]
    elif paths.IS_MAC:
        for s in JOBS:
            f = (
                paths.home()
                / "Library"
                / "LaunchAgents"
                / f"io.github.timchrist49.{PREFIX}.{s}.plist"
            )
            out.append(f"{s}: {'installed' if f.exists() else 'missing'}")
    else:
        if _systemd_user_ok():
            r = procutil.run(
                ["systemctl", "--user", "list-timers", f"{PREFIX}-*", "--all", "--no-pager"],
                timeout=20,
            )
            out += [ln for ln in r.stdout.splitlines() if PREFIX in ln]
        if procutil.cmd_exists("crontab"):
            cur = procutil.run(["crontab", "-l"], timeout=10)
            if CRON_BEGIN in (cur.stdout or ""):
                out.append("cron: managed block present")
    return out or ["no scheduled jobs installed"]
