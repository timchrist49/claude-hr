"""Staged upgrades: check -> snapshot -> upgrade -> smoke test -> keep or roll back.

One run updates the whole setup, in this order: self (claude-hr), claude (native build), headroom (uv tool), plugins
(marketplace refresh), then the post steps baseline (settings + CLAUDE.md block) and proxy (a restart, only when it is idle
and running stale settings), and a doctor verdict. Self comes first and its reinstall last: the reinstall replaces the code
that is running, so it is handed to a detached helper, and the post steps (they need the NEW code) are chained after it
(`update --post`); with no self-update queued they run inline at the end of the same command.
Rules that keep it safe to run unattended:
  * never downgrade unless a pin says so; never upgrade the shared proxy or Claude Code while sessions run
  * every upgrade is smoke-tested; a failed smoke test restores the previous version
  * every decision is appended to logs/update.jsonl; the outcome of the last run is state/update-last.json
  * one update at a time: a run that writes (not a preview) holds state/update.lock; a second one exits 75
  * never `-y`, never a marketplace added, never a plugin installed: that is `plugins sync`, a person's decision
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shlex
import sys
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta

from . import paths, procutil, resources, sysinfo
from .claude_setup import (
    ClaudeSetup,
    Step,
    applied_version,
    record_applied_version,
    resolve_channel,
    unattended_reapply,
    was_set_up,
)
from .config import Config
from .filelock import LockTimeout, locked
from .headroom import Headroom
from .jsonio import read_json, write_json
from .schedule import ps_quote

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"

# Exit codes of the update command (claude-hr update / claude-hr-ctl update). The scheduled job treats DEFERRED as success.
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_DEFERRED = 0, 1, 2, 75
# The order of a run. Self first: it only checks out the new release and queues the reinstall, which runs after everything
# else (see _launch_reinstall); the rest of PHASE_A runs on the code that is installed now. POST needs the NEW code after a
# self-update, so it is chained behind the reinstall.
PHASE_A = ("self", "claude", "headroom", "plugins")
POST = ("baseline", "proxy")
COMPONENTS = PHASE_A + POST
LAST_FILE = "update-last.json"
# an unfinished record older than this is not what `update --post` should continue
PENDING_MAX_AGE_S = 6 * 3600
UPDATE_LOCK = "update.lock"
# a full run can take a long time (a Claude Code install alone may run 15 minutes): a lock is only stale when its owner is gone
UPDATE_LOCK_STALE_S = 4 * 3600
# how long `update --post` waits for a run that holds the lock (the post phase of a self-update must not be lost to it)
POST_LOCK_WAIT_S = 120.0


class UpdateInProgress(RuntimeError):
    """Another update holds state/update.lock: this one does not run (the command exits 75)."""


def installed_is_behind(repo) -> tuple[str, str] | None:
    """(checkout version, installed version) if the managed checkout holds a newer claude-hr than the one that is installed
    (a reinstall failed or was killed), else None."""
    try:
        text = (pathlib.Path(repo) / "src" / "claude_hr" / "__init__.py").read_text(
            encoding="utf-8"
        )
    except OSError:
        return None
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        return None
    from . import __version__

    return (m.group(1), __version__) if vtuple(m.group(1)) > vtuple(__version__) else None


def vtuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else ()


@dataclass
class Result:
    component: str
    current: str = ""
    target: str = ""
    action: str = "none"  # none | updated | deferred | rolled-back | failed | available
    detail: str = ""
    busy: bool = False  # deferred because claude-hr sessions (or Claude background sessions) run: the command exits 75
    state: str = ""  # the report's word for an action "none" that is more than "up to date" (skipped, held ...)
    pending: str = ""  # what a person still has to do, whatever the action

    def line(self) -> str:
        arrow = (
            f"{self.current or '?'} -> {self.target}"
            if self.target and self.target != self.current
            else self.current
        )
        return f"{self.component:<9} {self.action:<11} {arrow}  {self.detail}".rstrip()


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    # lines above the table (the Claude Code channel in force) and INFO lines below it
    header: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # {"warn": n, "fail": n, "first": [up to three lines]} of the closing doctor run
    doctor: dict | None = None
    # the log file when claude-hr's own reinstall (and the post steps) continue there
    background: str = ""
    # a preview: nothing was written
    check: bool = False

    @property
    def ok(self) -> bool:
        return not any(r.action in {"failed", "rolled-back"} for r in self.results)

    def exit_code(self) -> int:
        """0 ok or already current | 1 a component failed or was rolled back (in a preview: could not be checked) | 75
        something waits for running sessions. (2, a refusal, is decided before a run starts.) A preview that found no
        failure exits 0 whatever it found, and never defers."""
        if not self.ok:
            return EXIT_FAILED
        if self.check:
            return EXIT_OK
        return EXIT_DEFERRED if any(r.busy for r in self.results) else EXIT_OK


_LABELS = {
    "none": "up to date",
    "updated": "updated",
    "deferred": "deferred",
    "rolled-back": "ROLLED BACK",
    "failed": "FAILED",
    "available": "update available",
}


def _label(r: Result, rep: Report) -> str:
    if r.action == "none":
        return r.state or ("pending" if r.pending else "up to date")
    if r.component == "self" and r.action == "updated" and rep.background:
        return "updated, finishing in the background"
    return _LABELS.get(r.action, r.action)


def _before_after(r: Result) -> str:
    if r.action != "none" and r.target and r.target != r.current:
        return f"{r.current or '?'} -> {r.target}"
    return r.current or "-"


def _failed(r: Result) -> bool:
    return r.action in {"failed", "rolled-back"}


def _pending(r: Result) -> str:
    """What is left to do for this component, in one phrase, or "" when nothing is."""
    if r.pending:
        return r.pending
    if r.state == "waiting":  # a reinstall started moments ago is still to finish
        return r.detail
    if r.action in {"available", "deferred", "failed", "rolled-back"}:
        return r.detail or _LABELS[r.action]
    return ""


# What --force really does, for the hint under a deferral: _busy() answers "" for every component, claude-hr's own reinstall
# included, and the helper that runs that reinstall waits for this process, never for the sessions.
_FORCE_SELF = (
    "--force also reinstalls claude-hr itself without waiting for the sessions; on Windows a running claude-hr session "
    "can make that reinstall fail. To update the rest now and leave claude-hr itself for later: "
    "`claude-hr update --force --only claude headroom plugins`."
)


def format_report(rep: Report) -> list[str]:
    """The one report at the end of an update run (and of a preview): the channel in force, a table of component |
    before -> after | result, what each row has to say, then what failed (or could not be checked), what is pending (or
    available) and why, or "Everything is up to date." when there is nothing to say."""
    lines = [*rep.header, ""] if rep.header else []
    rows = [("component", "before -> after", "result")] + [
        (r.component, _before_after(r), _label(r, rep)) for r in rep.results
    ]
    w0 = max(len(x[0]) for x in rows)
    w1 = max(len(x[1]) for x in rows)
    for i, (a, b, c) in enumerate(rows):
        lines.append(f"{a:<{w0}}  {b:<{w1}}  {c}".rstrip())
        if i == 0:
            lines.append(f"{'-' * w0}  {'-' * w1}  {'-' * len('result')}")
    for r in rep.results:
        if r.detail:
            lines.append(f"  {r.component}: {r.detail}")
    lines += [f"INFO: {n}" for n in rep.notes]
    failed = [(r.component, _pending(r)) for r in rep.results if _failed(r)]
    todo = [(r.component, _pending(r)) for r in rep.results if not _failed(r) and _pending(r)]
    lines.append("")
    if rep.background:
        lines.append(
            f"claude-hr itself is updating in the background; the rest finishes there: see {rep.background}; "
            "claude-hr-ctl status shows the result"
        )
    if failed:
        lines.append("Could not check:" if rep.check else "Failed:")
        lines += [f"  {c}: {why}" for c, why in failed]
    if todo:
        lines.append("Available:" if rep.check else "Pending:")
        lines += [f"  {c}: {why}" for c, why in todo]
        if rep.check and any(r.action == "available" for r in rep.results):
            lines.append("Run `claude-hr update` to apply it.")
    busy = [r for r in rep.results if r.busy]
    if busy and not rep.check:
        lines.append(
            ("" if failed else "Nothing is broken. ")
            + "Close the claude-hr sessions and run `claude-hr update` again, "
            + "or use `claude-hr update --force` to update now."
        )
        if any(r.component == "self" for r in busy):
            lines.append(_FORCE_SELF)
    if not (failed or todo or rep.background):
        previewed_all = not any(r.state == "not previewed" for r in rep.results)
        lines.append(
            "Everything is up to date."
            if previewed_all
            else "Nothing to update among what a preview can see."
        )
    if rep.doctor is not None:
        d = rep.doctor
        if d["warn"] or d["fail"]:
            lines.append(f"doctor: {d['warn']} warning(s), {d['fail']} failure(s)")
            lines += [f"  {x}" for x in d["first"]]
        else:
            lines.append("doctor: no warnings, no failures")
    return lines


def unmanaged_claude_processes() -> list[int]:
    """Process ids of Claude Code processes that claude-hr did not start: named claude, and neither registered as a session
    nor above or below one (Windows runs a thin launcher above the registered claude.exe). Neither this process and
    what it started, nor the `claude` shim that started it (a copy of the launcher named claude.exe, the parent of this
    interpreter), is one. Empty when that cannot be told."""
    try:
        table = sysinfo.process_table()
        mine = {int(s.get("pid", 0)) for s in resources.live_sessions()} - {0}
    except Exception:  # noqa: BLE001 - a note for the report must never fail the update
        return []
    me = os.getpid()
    managed: set[int] = set()
    for pid in mine:  # the sessions, their ancestors and (below) their descendants
        cur, hops = pid, 0
        while cur and cur not in managed and hops < 64:
            managed.add(cur)
            cur, hops = table.get(cur, (0, ""))[0], hops + 1
    # the shim: the unbroken run of `claude` processes directly above this one. A Claude Code further up (this command
    # was started from its Bash tool) is a real, unmanaged session and stays in the count.
    cur, hops = table.get(me, (0, ""))[0], 0
    while cur and hops < 64 and table.get(cur, (0, ""))[1].lower() in {"claude", "claude.exe"}:
        managed.add(cur)
        cur, hops = table.get(cur, (0, ""))[0], hops + 1
    found = []
    for pid, (_ppid, name) in table.items():
        if name.lower() not in {"claude", "claude.exe"} or pid == me or pid in managed:
            continue
        cur, hops, below = table.get(pid, (0, ""))[0], 0, False
        while cur and hops < 64 and not below:
            below = cur in mine or cur == me
            cur, hops = table.get(cur, (0, ""))[0], hops + 1
        if not below:
            found.append(pid)
    return sorted(found)


def plain_claude_uses_proxy(hr: Headroom) -> bool:
    """Does the configuration route a Claude Code that claude-hr did not start through claude-hr's own proxy? That is the
    case when ANTHROPIC_BASE_URL in ~/.claude/settings.json `env` (doctor accepts that state) or in this process's
    environment names the proxy: such a process is a consumer of the proxy like a session is. Never raises."""
    try:
        from .doctor import _is_own_proxy

        urls = [os.environ.get("ANTHROPIC_BASE_URL") or ""]
        env = (read_json(paths.claude_settings(), {}) or {}).get("env")
        if isinstance(env, dict):
            urls.append(str(env.get("ANTHROPIC_BASE_URL") or ""))
        return any(u and _is_own_proxy(u, hr.port) for u in urls)
    except Exception:  # noqa: BLE001 - an unreadable settings.json (any encoding) is "no"; doctor reports it
        return False


def _own_cgroup() -> str | None:
    try:
        return pathlib.Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def in_systemd_service() -> bool:
    """Is this process part of a systemd service unit, so that whatever it starts is killed when the unit ends? Needs
    INVOCATION_ID, and where the control group can be read, a group that is a service: a terminal that is itself a
    service hands the variable on to every shell it starts, and a shell is not a unit that ends under what it started."""
    if not os.environ.get("INVOCATION_ID"):
        return False
    for line in (_own_cgroup() or "").splitlines():
        _id, _sep, rest = line.partition(":")
        controllers, _sep, group = rest.partition(":")
        if controllers in {"", "name=systemd"} and group:
            return group.rstrip("/").rsplit("/", 1)[-1].endswith(".service")
    return True


def _result_from(row: dict) -> Result:
    known = {f.name for f in fields(Result)}
    return Result(**{k: v for k, v in row.items() if k in known})


def read_last() -> dict:
    """state/update-last.json: the outcome of the last update run ({} when there is none or it cannot be read)."""
    try:
        rec = read_json(paths.state_dir() / LAST_FILE, {}) or {}
    except Exception:  # noqa: BLE001
        return {}
    return rec if isinstance(rec, dict) else {}


def _rows(rec: dict) -> list[dict]:
    """The result rows of a record: dicts only, whatever else somebody (or another release) put there."""
    rows = rec.get("results")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def last_update_summary() -> str | None:
    """One line about the last update run for `claude-hr-ctl status`, or None when there was none. A record of the wrong
    shape (hand-edited, another release's) is no news, never an error."""
    try:
        return _last_update_summary(read_last())
    except (OSError, OverflowError, ValueError, TypeError, AttributeError):
        return None


def _last_update_summary(rec: dict) -> str | None:
    ts = rec.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    if rec.get("phase") == "pending":
        if time.time() - ts < 1800:
            return f"{when}: claude-hr itself is updating in the background (log: {paths.logs_dir() / 'self-update.log'})"
        return (
            f"{when}: the self-update did not finish (log: {paths.logs_dir() / 'self-update.log'}); "
            "run `claude-hr update` again"
        )
    rows = _rows(rec)
    done = [
        f"{r.get('component')} {r.get('current') or '?'} -> {r.get('target')}"
        for r in rows
        if r.get("action") == "updated"
    ]
    bad = [str(r.get("component")) for r in rows if r.get("action") in {"failed", "rolled-back"}]
    left = [str(r.get("component")) for r in rows if r.get("action") in {"deferred", "available"}]
    text = "updated " + ", ".join(done) if done else "nothing to update"
    if bad:
        text += f"; FAILED: {', '.join(bad)}"
    if left:
        text += f"; pending: {', '.join(left)}"
    doc = rec.get("doctor")
    if isinstance(doc, dict):
        text += f"; doctor {doc.get('warn', 0)} warning(s), {doc.get('fail', 0)} failure(s)"
    return f"{when}: {text}"


def _log(entry: dict) -> None:
    try:
        paths.ensure_dirs()
        with open(paths.logs_dir() / "update.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}) + "\n")
    except OSError:
        pass


# one implementation (schedule.py) for every PowerShell literal this package generates
_ps_quote = ps_quote

HELPER_TASK_FOLDER = "\\claude-hr\\"
HELPER_TASK_NAME = "selfupdate"
_SECRET_NAME = re.compile(r"(?i)(TOKEN|PASSWORD|PASSWD|SECRET|KEY|USERNAME|CREDENTIAL|AUTH)")
_URL_WITH_USERINFO = re.compile(r"://[^/@\s]*@")


def _helper_env(env) -> dict[str, str]:
    """The environment the reinstall helper needs: PATH and the UV_* settings (tool directory, cache, Python selection ...).
    A process started by Task Scheduler / the systemd user manager does NOT inherit ours, so without UV_TOOL_DIR and
    UV_TOOL_BIN_DIR `uv tool install` would use the default tool directory. Anything that looks like a credential is NOT
    copied (the helper script is a file on disk, and reinstalling a local checkout needs no package-index credentials)."""
    out: dict[str, str] = {}
    for k, v in env.items():
        if not (k.upper().startswith("UV_") or k.upper() == "PATH"):
            continue
        if _SECRET_NAME.search(k) or _URL_WITH_USERINFO.search(str(v)):
            continue
        out[k] = str(v)
    return out


def _helper_env_lines(env) -> list[str]:
    return [f"$env:{k} = {_ps_quote(v)}" for k, v in sorted(_helper_env(env).items())]


_PS_TO_LOG = (
    " 2>&1 | ForEach-Object { "
    'if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" } } | '
    "Add-Content -LiteralPath $log -Encoding UTF8"
)
# The variables that tell a process which Claude / claude-hr profile it works on. A task started by Task Scheduler does not
# inherit them, and the post phase must write the baseline into the same profile the update that queued it resolved.
_PROFILE_VARS = ("CLAUDE_CONFIG_DIR", "CLAUDE_HR_HOME", "CLAUDE_HR_USER_HOME")


def _profile_env(env) -> dict[str, str]:
    return {k: str(env[k]) for k in _PROFILE_VARS if env.get(k)}


def _windows_reinstall_script(
    cmd: list[str], pid: int, ppid: int, log, env=None, post: list[str] | None = None, cwd=None
) -> str:
    """PowerShell source for the helper that reinstalls claude-hr after the updater process (and the claude-hr-ctl.exe
    trampoline that started it) has exited. Everything it does is appended to logs/self-update.log (UTF-8), and it deletes
    its own script file and the one-shot scheduled task that started it.

    `post` is the command of the post phase (`claude-hr-ctl update --post`): it needs the NEW code, so it runs here, after
    the reinstall, and only when the reinstall exited with 0. `cwd` is the directory everything runs in (never the
    directory a person typed `update` in)."""
    env = os.environ if env is None else env
    lines = [
        "$ErrorActionPreference = 'Continue'",
        *_helper_env_lines(env),
        *(
            [f"$env:{k} = {_ps_quote(v)}" for k, v in sorted(_profile_env(env).items())]
            if post
            else []
        ),
        *([f"Set-Location -LiteralPath {_ps_quote(str(cwd))}"] if cwd else []),
        f"$log = {_ps_quote(str(log))}",
        "function Log($m) { Add-Content -LiteralPath $log -Encoding UTF8 -Value ('[' + (Get-Date -Format s) + '] ' + $m) }",
        f"$ids = @({pid})",
        f"$parent = Get-Process -Id {ppid} -ErrorAction SilentlyContinue",
        "if ($parent -and $parent.ProcessName -like 'claude-hr*') { $ids += $parent.Id }",
        "Log ('waiting for pid(s) ' + ($ids -join ',') + ' to exit before reinstalling')",
        "Wait-Process -Id $ids -Timeout 120 -ErrorAction SilentlyContinue",
        "Log ('running: ' + " + _ps_quote(" ".join(cmd)) + ")",
        "& " + " ".join(_ps_quote(part) for part in cmd) + _PS_TO_LOG,
        "$rc = $LASTEXITCODE",
        "Log ('reinstall exit code ' + $rc)",
    ]
    if post:
        lines += [
            "if ($rc -eq 0) {",
            "  Log ('running the post phase: ' + " + _ps_quote(" ".join(post)) + ")",
            # $LASTEXITCODE still holds the reinstall's code: a post command that cannot start must not report that as its own
            "  $global:LASTEXITCODE = $null",
            "  try {",
            "    & " + " ".join(_ps_quote(part) for part in post) + _PS_TO_LOG,
            "  } catch { Log ('could not start the post phase: ' + $_.Exception.Message) }",
            "  if ($null -eq $LASTEXITCODE) { Log 'the post phase did not run (no exit code)' } "
            "else { Log ('post phase exit code ' + $LASTEXITCODE) }",
            "} else { Log 'the reinstall did not succeed: the post phase was not run' }",
        ]
    lines += [
        "if ($PSCommandPath) { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue }",
        f"Unregister-ScheduledTask -TaskName {_ps_quote(HELPER_TASK_NAME)} -TaskPath {_ps_quote(HELPER_TASK_FOLDER)} "
        "-Confirm:$false -ErrorAction SilentlyContinue",
    ]
    return "\n".join(lines) + "\n"


def _posix_chain(cmd: list[str], post: list[str]) -> list[str]:
    """`cmd` (the reinstall) followed by `post` (the post phase) when - and only when - the reinstall succeeded, as one
    command for systemd-run or a detached spawn. Both are quoted with shlex.join. The chain says how each part ended and
    exits with the code of the part that failed, so a unit or a log never reads as a clean run after a failed post phase.
    Run under systemd it must pass through `_systemd_words`: the script has `$`, and so may the paths."""
    script = (
        f"{shlex.join(cmd)}; rc=$?; "
        'if [ "$rc" -ne 0 ]; then echo "claude-hr: the reinstall did not succeed (exit code $rc), '
        'the post phase was not run"; exit "$rc"; fi; '
        "echo 'claude-hr: the reinstall succeeded, running the post phase'; "
        f"{shlex.join(post)}; rc=$?; "
        'if [ "$rc" -ne 0 ]; then echo "claude-hr: the post phase did not finish (exit code $rc)"; exit "$rc"; fi; '
        "echo 'claude-hr: the post phase finished'"
    )
    return ["/bin/sh", "-c", script]


def _console_python() -> str:
    """The interpreter of this tool environment, with a console: the scheduled jobs run under pythonw.exe, whose output
    would vanish, and the helper logs what the post phase prints."""
    exe = pathlib.Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


_HEADLESS_OK: dict[str, bool] = {}


def _conhost_headless() -> str | None:
    """Absolute path of conhost.exe when it understands --headless (Windows 10 1809+ / 11), else None.

    A scheduled task whose action is a console program (powershell.exe) gets no console from Task Scheduler, so Windows
    Terminal (the default host on Windows 11) opens a window for it and keeps it, minimised, for the whole run; -WindowStyle
    Hidden does not stop that, and closing it kills the helper in the middle of the reinstall. `conhost.exe --headless <program>`
    gives the program a console without a window. One cheap run of a no-op tells whether this machine supports it."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    conhost = os.path.join(root, "System32", "conhost.exe")
    if not os.path.isfile(conhost):
        return None
    if conhost not in _HEADLESS_OK:
        cmd_exe = os.path.join(root, "System32", "cmd.exe")
        r = procutil.run([conhost, "--headless", cmd_exe, "/c", "exit 0"], timeout=10)
        _HEADLESS_OK[conhost] = r.returncode == 0
    return conhost if _HEADLESS_OK[conhost] else None


def _helper_task_action(args: str) -> tuple[str, str]:
    """(Execute, Argument) of the helper task: through conhost --headless when the machine supports it, else the shell itself."""
    conhost = _conhost_headless()
    if conhost is None:
        return "powershell.exe", args
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    ps_exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.isfile(ps_exe):
        return "powershell.exe", args
    return conhost, f'--headless "{ps_exe}" {args}'


def _start_helper_task(shell: str, script_path) -> bool:
    """Run the helper script from Windows Task Scheduler instead of as our child. uv's claude-hr-ctl.exe trampoline puts the
    updater in a Job Object that is killed when the trampoline exits (and refuses breakaway), so any child we spawn dies
    with us before it can reinstall. A task started by the scheduler lives outside that job. Registering and starting it is
    synchronous and finishes before we exit. Returns True if the task was started."""
    args = f'-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}"'
    execute, argument = _helper_task_action(args)
    ps = (
        "$ErrorActionPreference = 'Stop'; "
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(execute)} -Argument {_ps_quote(argument)}; "
        "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30); "
        f"Register-ScheduledTask -TaskName {_ps_quote(HELPER_TASK_NAME)} -TaskPath {_ps_quote(HELPER_TASK_FOLDER)} -Force "
        "-Action $a -Settings $s -Description 'claude-hr self-update helper (removes itself)' | Out-Null; "
        f"Start-ScheduledTask -TaskName {_ps_quote(HELPER_TASK_NAME)} -TaskPath {_ps_quote(HELPER_TASK_FOLDER)}"
    )
    r = procutil.run([shell, "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60)
    return r.returncode == 0


def _systemd_words(cmd: list[str]) -> list[str]:
    """`cmd` as systemd must be given it. systemd expands `${VAR}` / `$VAR` in the words of a unit's command line before
    the program starts and turns `$$` into `$`, so a path or a script with a `$` in it is only correct with every `$`
    doubled. `%` needs nothing: checked with systemd 249, a transient unit hands `%h` and `%%` to the program unchanged
    (specifiers are expanded in unit files, not in what systemd-run passes), so doubling it would corrupt the word."""
    return [w.replace("$", "$$") for w in cmd]


def _start_systemd_service(cmd: list[str], log, cwd=None) -> bool:
    """Start `cmd` as a transient unit of the systemd USER manager. The daily update runs as a timer's oneshot service whose
    default KillMode=control-group kills every process of its cgroup (even a setsid child) the moment the job ends; a
    reinstall started as the job's last act would be killed half-way. A unit created through the manager lives in its own
    cgroup and survives, but what it started dies with it: the post phase therefore never starts the proxy from here (see
    Updater.update_proxy). `cwd` is its working directory. Returns True if the unit was started."""
    if not (paths.IS_LINUX and resources.caps_available()):
        return False
    argv = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit=claude-hr-selfupdate-{int(time.time())}",
        "-p",
        f"StandardOutput=append:{log}",
        "-p",
        f"StandardError=append:{log}",
    ]
    if cwd:
        argv += ["-p", f"WorkingDirectory={cwd}"]
    env = {**_helper_env(os.environ), **_profile_env(os.environ)}
    argv += [f"--setenv={k}={v}" for k, v in sorted(env.items())]
    argv += ["--", *_systemd_words(cmd)]
    return procutil.run(argv, timeout=30).returncode == 0


def _spawn_detached(cmd: list[str], log_path=None, cwd=None) -> None:
    """Start `cmd` detached from this process (own session / no console); output goes to `log_path` if given; it runs in
    `cwd` when that directory exists (the caller's own directory may be an untrusted repository).

    On Windows the child also tries to break away from any Job Object (this is only a best-effort fallback: uv's trampoline
    job usually refuses breakaway, which is why _start_helper_task exists)."""
    import subprocess

    out = subprocess.DEVNULL
    if log_path is not None:
        try:
            out = open(log_path, "ab")  # noqa: SIM115
        except OSError:
            out = subprocess.DEVNULL
    kwargs: dict = {
        "stdout": out,
        "stderr": subprocess.STDOUT if out is not subprocess.DEVNULL else subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if cwd and os.path.isdir(cwd):
        kwargs["cwd"] = str(cwd)
    try:
        if paths.IS_WINDOWS:
            flags = (
                procutil.windows_background_flags()
            )  # no window: DETACHED_PROCESS would make the shell open one
            try:
                subprocess.Popen(
                    cmd, creationflags=flags | procutil.CREATE_BREAKAWAY_FROM_JOB, **kwargs
                )
            except OSError:  # the parent job does not allow breakaway
                subprocess.Popen(cmd, creationflags=flags, **kwargs)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kwargs)
    finally:
        if out is not subprocess.DEVNULL:
            out.close()


class Updater:
    def __init__(
        self,
        cfg: Config,
        dry: bool = False,
        force: bool = False,
        allow_downgrade: bool = False,
        restart: bool = True,
        record: bool | None = None,
    ):
        """dry: a preview (nothing is installed or restarted). restart=False: never restart the proxy (--no-restart).
        record: leave traces of the run (logs/update.jsonl, state/update-last.json); default: unless dry. The scheduled job
        in report-only mode is dry AND records."""
        self.cfg, self.dry, self.force, self.allow_downgrade = cfg, dry, force, allow_downgrade
        self.restart = restart
        self.record = (not dry) if record is None else record
        self.hr = Headroom(cfg)
        self.claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
        self._pending_reinstall: list[str] | None = (
            None  # set by update_self, started by run() once everything else is done
        )
        self.header: list[str] = []  # report lines above the table
        self.notes: list[str] = []  # INFO lines below it
        self.headroom_upgraded = False  # this run installed a new Headroom ...
        self.proxy_restarted = (
            False  # ... and (or) restarted the proxy, which then runs the current settings
        )
        self._channel = None
        self._pypi: dict | None = None

    # ---- guards ---------------------------------------------------------------------------
    def _busy(self, proxy: bool = False) -> str:
        """Why an upgrade must wait. On Windows replacing the tool env under a running launcher fails and can leave a
        half-installed package, so this guards claude-hr's own update as well. `proxy`: the step stops the Headroom
        proxy, so a Claude Code that claude-hr did not start counts too when the configuration routes it through the
        proxy (plain_claude_uses_proxy); for the Claude binary or claude-hr itself it never does."""
        if self.force:
            return ""
        n = len(resources.live_sessions())
        if n:
            return f"{n} claude-hr session(s) running"
        if self.claude:  # daemon-hosted `agents` / `--bg` workers are not in claude-hr's registry
            from . import launcher

            st = launcher._best_effort(launcher._daemon_status, self.claude) or {}
            if st.get("running") and st.get("workers", 0) != 0:
                return "Claude background sessions are running"
        if proxy and plain_claude_uses_proxy(self.hr):
            n = len(unmanaged_claude_processes())
            if n:
                return f"{n} Claude Code process(es) not started by claude-hr use the proxy (ANTHROPIC_BASE_URL)"
        return ""

    # ---- claude ---------------------------------------------------------------------------
    def claude_version(self) -> str:
        if not self.claude:
            return ""
        r = procutil.run([str(self.claude), "--version"], timeout=30)
        m = re.search(r"\d+\.\d+\.\d+\S*", r.stdout or "")
        return m.group(0) if m else ""

    def is_native_install(self) -> bool:
        """`claude install <version>` only manages the native build (~/.local/bin/claude -> ~/.local/share/claude/...)."""
        if not self.claude:
            return False
        try:
            real = str(self.claude.resolve()).replace("\\", "/").lower()
        except OSError:
            return False
        home = str(paths.home()).replace("\\", "/").lower()
        return (
            "/.local/share/claude/" in real
            or real.startswith(f"{home}/.local/bin/")
            or "/.claude/local/" in real
        )

    def channel_info(self):
        """The Claude Code release channel in force (claude_setup.resolve_channel: the same answer the baseline writes)."""
        if self._channel is None:
            self._channel = resolve_channel(self.cfg)
        return self._channel

    def claude_target(self) -> str:
        pin = str(self.cfg.get("claude.pin", "") or "")
        if pin:
            return pin
        channel = self.channel_info().effective
        try:
            import urllib.request

            with urllib.request.urlopen(f"{CLAUDE_RELEASES}/{channel}", timeout=10) as resp:  # noqa: S310
                return resp.read().decode().strip()
        except Exception:
            return ""

    def update_claude(self) -> Result:
        cur, tgt = self.claude_version(), self.claude_target()
        res = Result("claude", cur, tgt)
        pin = str(self.cfg.get("claude.pin", "") or "")
        ch = self.channel_info()
        if self.cfg.flag("claude.manage_updates", True):
            self.header.append(
                f"Claude Code is pinned to {pin} (claude.pin)"
                if pin
                else f"Claude Code channel: {ch.describe()}"
            )
        if not self.claude:
            res.action, res.detail = "failed", "claude not installed"
            return res
        if not tgt:
            res.action, res.detail = (
                "failed",
                "could not read the release channel"
                if not pin
                else "claude.pin is set but could not be read",
            )
            return res
        if not self.cfg.flag("claude.manage_updates", True):
            res.state, res.detail = "skipped", "managed updates disabled in config"
            return res
        if not self.is_native_install():
            res.state = "skipped"
            res.detail = (
                "not the native build (npm/Homebrew/WinGet): update it with that package manager, "
                "or run `claude-hr update --claude-only`"
            )
            return res
        if vtuple(tgt) == vtuple(cur):
            return res
        if vtuple(tgt) < vtuple(cur) and not self.allow_downgrade and not pin:
            res.state = "held"
            res.detail = (
                f"installed {cur} is newer than channel {ch.effective} ({tgt}): not downgrading"
                + (
                    '; set claude.channel = "latest" to follow the newest'
                    if ch.effective == "stable"
                    else ""
                )
            )
            return res
        offer = f"claude.pin holds {tgt}" if pin else f"channel {ch.effective} offers {tgt}"
        if self.dry:
            res.action = "available"
            res.detail = f"Claude Code {cur} -> {offer}"
            busy = self._busy()
            if busy:
                res.detail += f"; it would wait now: {busy}"
            return res
        busy = self._busy()
        if busy:
            res.action, res.detail, res.busy = "deferred", busy, True
            return res
        r = procutil.run([str(self.claude), "install", tgt, "--force"], timeout=900)
        new = self.claude_version()
        smoke = procutil.run([str(self.claude), "plugin", "list", "--json"], timeout=60)
        if r.returncode == 0 and vtuple(new) == vtuple(tgt) and smoke.returncode == 0:
            res.action = "updated"
        else:
            procutil.run([str(self.claude), "install", cur, "--force"], timeout=900)
            res.action = "rolled-back"
            res.detail = (r.stderr or r.stdout or "smoke test failed").strip()[-160:]
        return res

    # ---- headroom -------------------------------------------------------------------------
    def update_headroom(self) -> Result:
        cur = self.hr.version() or ""
        pin = str(self.cfg.get("headroom.version", "") or "")
        cooldown = 0.0 if (self.force or pin) else float(self.cfg.get("updates.cooldown_days", 5))
        tgt = pin or self.hr.latest_version(cooldown_days=cooldown) or ""
        res = Result("headroom", cur, tgt)
        if not self.cfg.flag("headroom.enabled", True):
            res.state, res.detail = "skipped", "disabled"
            return res
        if not tgt:
            res.action, res.detail = "failed", "could not read PyPI"
            return res
        if cur and vtuple(tgt) == vtuple(cur):
            if self.dry and cooldown > 0:
                res.detail = self._cooldown_note(cooldown, tgt)
            return res
        if cur and vtuple(tgt) < vtuple(cur) and not (self.allow_downgrade or pin):
            res.state = "held"
            what = "the cooldown-eligible" if cooldown > 0 else "the target"
            res.detail = f"installed {cur} is newer than {what} {tgt}: not downgrading" + (
                f" ({self._cooldown_note(cooldown, tgt)})" if cooldown > 0 else ""
            )
            return res
        if self.dry:
            res.action = "available"
            res.detail = f"Headroom {cur or 'not installed'} -> {tgt}" + (
                " (headroom.version)"
                if pin
                else " (newest release past the cooldown)"
                if cooldown > 0
                else ""
            )
            if not self.restart and self.hr.health_patient().ok:
                res.detail += (
                    "; it would be skipped now: it restarts the proxy and --no-restart is set"
                )
                return res
            busy = self._busy(proxy=True)
            if busy:
                res.detail += f"; it would wait now: {busy}"
            return res
        was_running = self.hr.health_patient().ok
        if was_running and not self.restart:
            # the upgrade stops the proxy and starts it again: --no-restart means never, --force or not (--force is about
            # waiting for sessions). Checked before anything is stopped.
            res.action, res.detail = "deferred", "needs a proxy restart, skipped: --no-restart"
            return res
        busy = self._busy(proxy=True)
        if busy:
            res.action, res.detail, res.busy = "deferred", busy, True
            return res
        self.hr.stop()
        # a new release may need a newer model revision: allow the network for this one start
        saved_h = self.hr.h
        self.hr.h = {**saved_h, "offline_models": False}
        cutoff = None
        if cooldown > 0:  # cool the whole dependency tree, not just headroom-ai itself
            cutoff = (datetime.now(UTC) - timedelta(days=cooldown)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            self._upgrade_headroom(res, cur, tgt, cutoff)
        except Exception as exc:  # noqa: BLE001 - the proxy is stopped and a release is half-installed: put things back
            res.action, res.detail = "failed", f"{type(exc).__name__}: {exc}"
            if cur:
                res.detail += self._restore_headroom(cur, cutoff)
        finally:
            self.hr.h = saved_h
        if res.action == "updated":
            self.headroom_upgraded = True
        if was_running or res.action == "updated":
            res.detail += self._restart_after_upgrade()
        return res

    def _upgrade_headroom(self, res: Result, cur: str, tgt: str, cutoff: str | None) -> None:
        """Install `tgt`, smoke-test it, and put `cur` back when that fails. Sets res.action and res.detail."""
        inst = self.hr.install(tgt, exclude_newer=cutoff)
        ok = (
            inst.returncode == 0
            and self.hr.version() is not None
            and vtuple(self.hr.version()) == vtuple(tgt)
        )
        if ok:
            ok = self.smoke_headroom()
        if ok:
            res.action = "updated"
            return
        res.action = "rolled-back" if cur else "failed"
        res.detail = (inst.stderr or "smoke test failed").strip()[-160:]
        if cur:
            res.detail += self._restore_headroom(cur, cutoff)

    def _restore_headroom(self, cur: str, cutoff: str | None) -> str:
        """Reinstall `cur` with the SAME dependency cutoff (an unconstrained reinstall would pull today's newest transitive
        packages - possibly the very breakage being rolled back) and prove it works again. "" or a WARNING to append.
        Never raises."""
        warning = " | WARNING: rollback could not be verified, run `claude-hr-ctl headroom bench`"
        try:
            self.hr.stop()
            back = self.hr.install(cur, exclude_newer=cutoff)
            return "" if back.returncode == 0 and self.smoke_headroom() else warning
        except Exception as exc:  # noqa: BLE001
            return f" | WARNING: rollback failed ({type(exc).__name__}: {exc}), run `claude-hr-ctl headroom bench`"

    def _restart_after_upgrade(self) -> str:
        """Stop the smoke-test proxy and start one that uses the normal (possibly offline) settings. "" or a note to
        append to the row. Never raises."""
        try:
            self.hr.stop()
            self.hr.ensure(wait_s=0)
        except Exception as exc:  # noqa: BLE001
            return f" | the proxy could not be started again: {type(exc).__name__}: {exc}"
        self.proxy_restarted = (
            True  # with the current settings: the proxy step has nothing left to do
        )
        return ""

    def _pypi_headroom(self) -> dict | None:
        """PyPI's record of headroom-ai, fetched once per run; None when PyPI cannot be read."""
        if self._pypi is None:
            status, body = procutil.http_json(
                "https://pypi.org/pypi/headroom-ai/json", timeout=15.0, loopback=False
            )
            self._pypi = body if status == 200 and isinstance(body, dict) else {}
        return self._pypi or None

    def _cooldown_note(self, cooldown: float, tgt: str) -> str:
        """For the newest stable release that is newer than `tgt` and still inside the cooldown: when it came out and when
        the cooldown ends ("0.41.0 came out 2026-10-14; the 5-day cooldown ends 2026-10-19"), else "". Never raises."""
        try:
            releases = (self._pypi_headroom() or {}).get("releases") or {}
            newest = None
            for ver, files in releases.items():
                parts = ver.split(".")
                if len(parts) != 3 or not all(p.isdigit() for p in parts):
                    continue
                if not files or all(f.get("yanked") for f in files):
                    continue
                if newest is None or vtuple(ver) > vtuple(newest):
                    newest = ver
            if newest is None or vtuple(newest) <= vtuple(tgt):
                return ""
            times = []
            for f in releases[newest]:
                raw = f.get("upload_time_iso_8601") or f.get("upload_time")
                times.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
            came = min(times)
            ends = came + cooldown * 86400
            if ends <= time.time():
                return ""
            day = lambda ts: datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")  # noqa: E731
            return f"{newest} came out {day(came)}; the {cooldown:g}-day cooldown ends {day(ends)}"
        except Exception:  # noqa: BLE001 - an explanation, never a reason to fail
            return ""

    def smoke_headroom(self, wait_s: float = 150.0) -> bool:
        """Start the proxy, require healthy, then push a repetitive tool result through /v1/compress."""
        health = self.hr.ensure(wait_s=wait_s)
        if not health.ok:
            return False
        import urllib.request

        rows = [
            {"id": i, "status": "ok", "message": "heartbeat received from node-a", "latency_ms": 12}
            for i in range(300)
        ]
        body = json.dumps(
            {
                "model": "claude-sonnet-5-5",
                "messages": [
                    {"role": "user", "content": "check the nodes"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Bash",
                                "input": {"command": "nodes"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": json.dumps(rows),
                            }
                        ],
                    },
                ],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.hr.url}/v1/compress", data=body, headers={"content-type": "application/json"}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
            return resp.status == 200 and int(data.get("tokens_saved", 0)) > 0
        except Exception:
            return False

    # ---- plugins --------------------------------------------------------------------------
    def update_plugins(self) -> Result:
        res = Result("plugins")
        if not self.claude:
            res.action, res.detail = "failed", "claude not installed"
            return res
        setup = ClaudeSetup(self.cfg, self.hr, self.claude)
        if self.dry:
            # A preview cannot see plugin updates: `claude plugin list --available` names what can be installed, not what
            # is newer, and it refreshes claude's catalog cache (a write). The update itself asks claude about each
            # plugin, so the preview says that, and what is installed now.
            res.state = "not previewed"
            res.detail = "not previewed (the update asks Claude Code for each plugin)"
            try:
                have = [p for p in setup._installed_plugins() if p.get("scope") == "user"]
            except Exception:  # noqa: BLE001 - a preview is never worth a traceback
                have = []
            if have:
                shown = ", ".join(f"{p['id']} {p.get('version') or '?'}" for p in have[:12])
                more = f" (+{len(have) - 12} more)" if len(have) > 12 else ""
                res.detail += f"; installed, user scope: {shown}{more}"
            return res
        before, why = setup._plugin_list()
        if why:
            res.action, res.detail = "failed", f"could not check plugins: {why}"
            return res
        step = setup.update_marketplaces()
        if not step.ok:
            res.action = "failed"
            res.detail = step.lines[-1] if step.lines else "could not check plugins"
            return res
        after, why = setup._plugin_list()
        if why:
            res.action, res.detail = "failed", f"could not check plugins: {why}"
            return res
        was = {p["id"]: p.get("version") for p in before}
        now = {p["id"]: p.get("version") for p in after}
        changed = [f"{k} {was.get(k)}->{v}" for k, v in now.items() if was.get(k) != v]
        res.action = "updated" if changed else "none"
        if changed:
            res.detail = "; ".join(changed)
        elif step.stale:
            res.detail = f"marketplace refresh failed ({step.stale}); nothing new in the catalogs claude already had"
        else:
            res.detail = "nothing was updated" if step.needs else "already current"
        res.pending = "; ".join(
            [
                *(
                    [
                        f"could not check plugins for newer versions: marketplace refresh failed ({step.stale})"
                    ]
                    if step.stale
                    else []
                ),
                *step.needs,
            ]
        )
        return res

    # ---- baseline (what the new claude-hr release wants in settings.json and the CLAUDE.md block) ------------------------
    def reapply_baseline(self) -> Result | None:
        """After claude-hr itself was upgraded, make the machine match the new release: re-run the idempotent `setup` steps
        `settings` and `claude-md` (the ownership-tracked, reversible merges; no Headroom install, no scheduler change). The
        unattended self-update only replaces the code, so without this a new release's permission rules and env defaults
        would wait for a person to run `setup`.

        NEVER plugins or marketplaces: nothing in an unattended run installs code or passes `-y` (SECURITY.md). A plugin the
        new release adds (hr-gates after an upgrade from 0.1.x) is reported by doctor with the fix `plugins sync`.

        Keyed on the per-step record `setup` writes (state/applied-version.json): only a step `setup` applied, for an older
        release, is re-applied (a skipped step stays skipped; after `uninstall` there is no record), and only when this run
        resolves the same Claude config directory the record names (a scheduled job lacks the shell's CLAUDE_CONFIG_DIR).
        A step's version is recorded when that step succeeded, so a failure (unparsable settings.json) is retried by the next
        run. A dry run only says what it would re-apply. Never raises."""
        from . import __version__

        res = Result("baseline")
        try:
            steps, why = unattended_reapply()
            if why:
                return Result("baseline", target=__version__, action="deferred", detail=why)
            if not steps:
                return None
            res.current, res.target = applied_version(steps[0]) or "?", __version__
            if self.dry:
                res.action, res.detail = "available", "would re-apply " + " and ".join(steps)
                return res
            setup = ClaudeSetup(self.cfg, self.hr, self.claude)
            appliers = {"settings": setup.apply_settings, "claude-md": setup.sync_claude_md}
            parts, done = [], []
            for name in steps:
                try:
                    st = appliers[name]()
                except Exception as exc:  # noqa: BLE001 - one broken step must not cost the other its record
                    st = Step(name, ok=False, lines=[f"{type(exc).__name__}: {exc}"])
                part = f"{name} {'ok' if st.ok else 'FAILED'}{' (changed)' if st.changed else ''}"
                if not st.ok and st.lines:
                    part += f": {st.lines[0][:120]}"
                parts.append(part)
                if st.ok:
                    done.append(name)
            if done:
                record_applied_version(*done)
            if len(done) == len(steps):
                res.action = "updated"
            else:
                res.action = "failed"
                parts.append("will retry at the next update")
            res.detail = "re-applied the managed baseline after the upgrade: " + ", ".join(parts)
        except Exception as exc:  # noqa: BLE001 - fail open: the update job must go on
            res.action, res.detail = "failed", f"{type(exc).__name__}: {exc}"
        return res

    def _baseline_row(self) -> Result:
        """reapply_baseline for the report: a row even when there was nothing to re-apply."""
        res = self.reapply_baseline()
        if res is not None:
            return res
        from . import __version__

        if not was_set_up():
            return Result(
                "baseline",
                state="skipped",
                detail="this machine was never set up (run `claude-hr-ctl setup`)",
            )
        return Result("baseline", current=applied_version("settings") or __version__)

    # ---- proxy (a restart, only when it matters and nobody is using it) ------------------------------------
    def _unowned_proxy(self) -> str:
        """ "" when the proxy that answers on the port is one claude-hr started and can identify, else why it is not: the
        record of its start (state/headroom-proxy.json, which only stop() removes, so a crash or a reboot leaves it behind)
        must name a live Headroom process that listens on the port or is the parent of the one that does (on Windows the
        uv shim above the listener). Anything else may be somebody else's proxy; Headroom.stop() ends every Headroom
        process on the port, so it must not be asked to."""
        try:
            pid = int((read_json(self.hr._state_file(), {}) or {}).get("pid") or 0)
        except Exception:  # noqa: BLE001 - a record that cannot be read identifies nothing
            return "the record of its start cannot be read"
        if pid <= 0 or not sysinfo.pid_alive(pid):
            return "the record of its start names a process that is no longer running"
        if not self.hr._is_headroom_process(pid):
            return "the recorded process is not a Headroom process"
        listeners = self.hr._listener_pids()
        if pid in listeners:
            return ""
        table = sysinfo.process_table()
        for listener in listeners:
            cur, hops = listener, 0
            while cur and hops < 16:
                cur, hops = table.get(cur, (0, ""))[0], hops + 1
                if cur == pid:
                    return ""
        return f"the recorded process does not listen on port {self.hr.port}"

    def update_proxy(self) -> Result:
        """Restart the shared Headroom proxy when it runs other settings than the configuration asks for now (or this run
        upgraded Headroom without restarting it), and only while nothing uses it: no claude-hr session, and no Claude Code
        that the configuration routes through the proxy (plain_claude_uses_proxy). A proxy claude-hr cannot show it started
        (no record of a start, or a record of a process that is gone or is not the listener: Headroom.settings_state,
        _unowned_proxy) is not judged and never stopped, and one that is not running has nothing to restart: its next start
        uses the current settings. Inside a systemd service it is stopped but not started again: what a unit starts dies
        with it (the transient unit of the post phase), so the next claude-hr launch or the monitor job starts it fresh."""
        res = Result("proxy")
        mode = str(self.cfg.get("headroom.mode", "persistent"))
        if not self.cfg.flag("headroom.enabled", True) or mode == "off":
            res.state, res.detail = "skipped", "Headroom is off"
            return res
        if self.hr.binary() is None:
            res.state, res.detail = "skipped", "Headroom is not installed"
            return res
        state = self.hr.settings_state()
        why = ""
        if state == "changed":
            why = "it runs other settings than the configuration asks for now"
        elif state == "older":
            why = "it was started by an older claude-hr, which did not record its settings"
        elif self.headroom_upgraded and not self.proxy_restarted:
            why = "Headroom was upgraded"
        if not why:
            res.detail = (
                "runs the current settings"
                if state == "current" or self.proxy_restarted
                else "nothing to restart"
            )
            return res
        if not self.hr.health_patient().ok:
            res.detail = "not running; its next start uses the current settings"
            return res
        foreign = self._unowned_proxy()
        if foreign:
            res.state, res.detail = "not judged", "left alone: claude-hr did not start this proxy"
            self.notes.append(
                f"the Headroom proxy on port {self.hr.port} is not one claude-hr started ({foreign}): "
                "it was not judged, stopped or restarted"
            )
            return res
        if self.dry:
            res.action = "available"
            res.detail = f"would restart: {why}"
            busy = self._busy(proxy=True) if self.restart else ""
            if busy:
                res.detail += f" (it would wait now: {busy})"
            return res
        if not self.restart:
            res.action, res.detail = "deferred", f"needs a restart ({why}), skipped: --no-restart"
            return res
        busy = self._busy(proxy=True)
        if busy:
            res.action, res.detail, res.busy = "deferred", f"needs a restart ({why}); {busy}", True
            return res
        self.hr.stop()
        self.proxy_restarted = True
        if in_systemd_service():
            res.action = "updated"
            res.detail = (
                f"stopped ({why}); it is not started from inside a systemd unit (it would die with the unit): "
                "the next claude-hr launch or the monitor job starts it with the current settings"
            )
            return res
        h = self.hr.ensure(wait_s=float(self.cfg.get("headroom.start_wait_s", 90)))
        if h.ok:
            res.action, res.detail = "updated", f"restarted ({why}); healthy v{h.version}"
        elif h.status in {"crashed", "port-busy", "not-installed"}:
            res.action = "failed"
            res.detail = f"restarted ({why}) but it is not up: {h.status} {h.detail}".rstrip()
        else:  # a cold start can take longer than we wait; the next launch finishes the wait
            res.action = "updated"
            res.detail = (
                f"restarted ({why}); still starting ({h.status}), the next launch waits for it"
            )
        return res

    # ---- self (claude-hr itself) ----------------------------------------------------------------------
    def update_self(self) -> Result:
        repo = paths.hr_home() / "repo"
        res = Result("self")
        git = procutil.which_all("git")
        if not (repo / ".git").exists() or not git:
            res.state, res.detail = "skipped", "not a managed checkout (dev install?)"
            return res
        g = str(git[0])
        cur = procutil.run(
            [g, "-C", str(repo), "describe", "--tags", "--always"], timeout=30
        ).stdout.strip()
        fetched = procutil.run(
            [g, "-C", str(repo), "fetch", "--tags", "--prune", "origin"], timeout=120
        )
        if fetched.returncode != 0:
            # surfaces the usual private-repo problem (no credentials on this VM) instead of silently never updating
            res.action, res.detail = (
                "failed",
                "git fetch failed: " + (fetched.stderr or fetched.stdout).strip()[-160:],
            )
            return res
        ref = str(self.cfg.get("self.update_ref", "stable"))
        if ref == "stable":
            tags = procutil.run(
                [g, "-C", str(repo), "tag", "--list", "v*"], timeout=30
            ).stdout.split()
            # release tags only: vX.Y.Z (a v1.3.0-rc1 must never outrank v1.3.0, nor be installed unattended)
            tags = sorted((t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)), key=vtuple)
            tgt = tags[-1] if tags else ""
        else:
            tgt = f"origin/{ref}"
        res.current, res.target = cur, tgt
        if not tgt:
            res.state, res.detail = "skipped", "no release tag yet"
            return res
        head = procutil.run([g, "-C", str(repo), "rev-parse", "HEAD"], timeout=30).stdout.strip()
        want = procutil.run(
            [g, "-C", str(repo), "rev-parse", tgt + "^{commit}"], timeout=30
        ).stdout.strip()
        if not want:
            res.target = res.current
            return res
        retry = False
        if head == want:
            # Already checked out. If the installed tool is still OLDER than this checkout, an earlier reinstall failed or
            # was killed (the checkout is moved before the reinstall runs): try again, but not while one was started
            # moments ago.
            if not installed_is_behind(repo):
                res.target = res.current
                return res
            started = (read_json(paths.state_dir() / "self-update.json", {}) or {}).get("ts", 0)
            if time.time() - float(started) < 900:
                res.target = res.current
                res.state = "waiting"
                res.detail = "a reinstall was started in the last 15 minutes; waiting for it"
                return res
            retry = True
        # only ever move forward: a checkout of `main` that is ahead of the newest tag must not be rewound to it
        elif (
            procutil.run(
                [g, "-C", str(repo), "merge-base", "--is-ancestor", head, want], timeout=30
            ).returncode
            != 0
        ):
            res.target = res.current
            res.state = "held"
            res.detail = f"installed commit is not behind {tgt}; not moving"
            return res
        if self.dry:
            res.action = "available"
            res.detail = (
                "reinstall of the checked-out release is pending"
                if retry
                else f"claude-hr {cur} -> the newest release {tgt}"
            )
            busy = self._busy()
            if busy:
                res.detail += f"; it would wait now: {busy}"
            return res
        busy = self._busy()
        if busy:  # replacing the tool env under a running launcher fails on Windows and can leave a broken install
            res.action, res.detail, res.busy = "deferred", busy, True
            return res
        uv = procutil.which_all(
            "uv"
        )  # resolved BEFORE the checkout moves, so "uv missing" never leaves it ahead of the tool
        if not uv:
            res.action, res.detail = "failed", "uv missing"
            return res
        if not retry:
            co = procutil.run(
                [g, "-C", str(repo), "checkout", "--detach", "--quiet", tgt], timeout=60
            )
            if co.returncode != 0:
                res.action, res.detail = (
                    "failed",
                    "git checkout failed: " + (co.stderr or co.stdout).strip()[-160:],
                )
                return res
        # Re-installing replaces the files this process runs from, so it is handed to a detached helper that starts
        # only at the very END of run() (see _launch_reinstall): the helper must never overlap with later components.
        self._pending_reinstall = [
            str(uv[0]),
            "tool",
            "install",
            "--python",
            "3.13",
            "--force",
            "--reinstall",
            str(repo),
        ]
        write_json(
            paths.state_dir() / "self-update.json", {"from": head, "to": want, "ts": time.time()}
        )
        res.action = "updated"
        res.detail = (
            "retrying the reinstall of the checked-out release (the last one did not finish); "
            if retry
            else ""
        ) + "the reinstall runs after this command ends (log: logs/self-update.log)"
        return res

    def _launch_reinstall(self, post: list[str] | None = None) -> None:
        """Start the detached `uv tool install --reinstall` for a self-update. Called once, after every component has run.
        `post` (the command of `update --post`) is chained behind it and runs only when the reinstall succeeded.

        On Windows the reinstall must not run while this process (or the claude-hr-ctl.exe trampoline that started it)
        still holds the tool environment: doing so alongside `plugins update` left the environment without site-packages
        (every later claude-hr call failed with ModuleNotFoundError). The trampoline also kills every child we spawn the
        moment it exits, so a detached child never gets to reinstall. So on Windows a one-shot scheduled task runs a
        PowerShell helper that waits for those processes to exit and then runs uv (see _start_helper_task)."""
        cmd = self._pending_reinstall
        if not cmd:
            return
        self._pending_reinstall = None
        log = paths.logs_dir() / "self-update.log"
        cwd = str(
            paths.state_dir()
        )  # never the directory a person typed `update` in: it may be an untrusted repository
        try:
            paths.ensure_dirs()  # the helper script lives in state/, its log in logs/
            if paths.IS_WINDOWS:
                shell = procutil.which_all("powershell") or procutil.which_all("pwsh")
                if shell:
                    import base64

                    script = _windows_reinstall_script(
                        cmd, os.getpid(), os.getppid(), log, post=post, cwd=cwd
                    )
                    ps1 = paths.state_dir() / "self-update.ps1"
                    ps1.write_bytes(
                        b"\xef\xbb\xbf" + script.encode("utf-8")
                    )  # BOM: PowerShell 5.1 reads ANSI otherwise
                    if _start_helper_task(str(shell[0]), ps1):
                        return
                    _log(
                        {
                            "component": "self",
                            "action": "note",
                            "detail": "scheduler refused the helper task; starting the helper directly",
                        }
                    )
                    ps1.unlink(
                        missing_ok=True
                    )  # the fallback carries the script itself; do not leave a copy on disk
                    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                    cmd = [
                        str(shell[0]),
                        "-NoProfile",
                        "-NonInteractive",
                        "-WindowStyle",
                        "Hidden",
                        "-EncodedCommand",
                        encoded,
                    ]
            if not paths.IS_WINDOWS and post:
                cmd = _posix_chain(cmd, post)
            if not paths.IS_WINDOWS and _start_systemd_service(cmd, log, cwd=cwd):
                return
            _spawn_detached(cmd, log, cwd=cwd)
        except OSError as exc:
            _log(
                {
                    "component": "self",
                    "action": "failed",
                    "detail": f"could not start the reinstall: {exc}",
                }
            )

    # ---- orchestration --------------------------------------------------------------------
    def _post_command(self) -> list[str]:
        """`claude-hr-ctl update --post`, as the helper starts it after the reinstall (the interpreter of this tool
        environment: the path survives the reinstall, and the new code is what it imports). `-I` keeps the working
        directory and PYTHON* variables out of the import path: `-m` would otherwise import a claude_hr, json or
        types from whatever directory the person typed `update` in."""
        cmd = [_console_python(), "-I", "-m", "claude_hr.ctl", "update", "--post"]
        if not self.restart:
            cmd.append("--no-restart")
        if self.force:
            cmd.append("--force")
        return cmd

    def _step(self, report: Report, name: str) -> None:
        fns = {
            "self": self.update_self,
            "claude": self.update_claude,
            "headroom": self.update_headroom,
            "plugins": self.update_plugins,
            "baseline": self._baseline_row,
            "proxy": self.update_proxy,
        }
        try:
            res = fns[name]()
        except Exception as exc:  # one broken component must not block the others
            res = Result(name, action="failed", detail=f"{type(exc).__name__}: {exc}")
        report.results.append(res)
        if self.record:
            _log(
                {
                    "component": res.component,
                    "from": res.current,
                    "to": res.target,
                    "action": res.action,
                    "detail": res.detail,
                }
            )

    def _doctor_verdict(self) -> dict | None:
        """A compact doctor run: how many warnings and failures, and the first three (failures first)."""
        try:
            from . import doctor
            from .checks import FAIL, WARN

            checks = doctor.run(self.cfg, paths.home())
        except Exception:  # noqa: BLE001 - the verdict is a courtesy; the update stands without it
            return None
        bad = sorted(
            (c for c in checks if c.status in {FAIL, WARN}), key=lambda c: c.status != FAIL
        )
        return {
            "warn": sum(c.status == WARN for c in checks),
            "fail": sum(c.status == FAIL for c in checks),
            "first": [f"{c.status.upper()} {c.name}: {c.detail}"[:160] for c in bad[:3]],
        }

    def _attach(self, report: Report) -> None:
        """The lines around the table: what the components said (the channel), and the INFO lines: what they noted, and one
        about Claude Code processes claude-hr did not start (the session guard only sees sessions it registered, and a plain
        claude is a reason to say so, not to wait - unless the configuration routes it through the proxy, which then waits
        for it: someone who keeps a session open for days would otherwise never be updated)."""
        report.header = [*dict.fromkeys([*report.header, *self.header])]
        report.notes = [*dict.fromkeys([*report.notes, *self.notes])]
        n = len(unmanaged_claude_processes())
        if n:
            if plain_claude_uses_proxy(self.hr):
                report.notes.append(
                    f"{n} Claude Code process(es) not started by claude-hr are running and use the Headroom proxy "
                    "(ANTHROPIC_BASE_URL points at it): a proxy restart waits for them, and the updated claude takes "
                    "effect when they restart"
                )
            else:
                report.notes.append(
                    f"{n} Claude Code process(es) not started by claude-hr are running; they are unaffected by a proxy "
                    "restart (they do not use it) and the updated claude takes effect when they restart"
                )

    def _save_last(
        self, report: Report, phase: str, post: dict | None = None, post_only: bool = False
    ) -> None:
        """state/update-last.json: what this run did. phase "pending" = the post steps are still to run (in the helper,
        after the reinstall); "complete" = nothing is left. `post_only`: this is a post phase that continues no run (a second
        `update --post`, or one typed by hand): it is added to the record of the run before it, under "post_only", and
        leaves that run's rows alone. Fail open: a record that cannot be written is not an error."""
        from . import __version__

        # what is installed after this run; claude-hr is the code that wrote the record, so the NEW version once the
        # helper has finished
        versions = {"claude-hr": __version__}
        for r in report.results:
            v = r.target if r.action == "updated" and r.target else r.current
            if r.component in {"claude", "headroom"} and v:
                versions[r.component] = v
        rec = {
            "ts": time.time(),
            "phase": phase,
            "versions": versions,
            "exit_code": report.exit_code(),
            "header": report.header,
            "results": [asdict(r) for r in report.results],
            "doctor": report.doctor,
        }
        if post is not None:
            rec["post"] = post
        try:
            paths.ensure_dirs()
            target = paths.state_dir() / LAST_FILE
            if post_only:
                prior = read_last()
                if isinstance(prior.get("ts"), (int, float)) and _rows(prior):
                    earlier = prior.get("post_only")
                    earlier = (
                        [e for e in earlier if isinstance(e, dict)]
                        if isinstance(earlier, list)
                        else []
                    )
                    entry = {
                        "label": "post phase only: it continued no pending update, so the run above is untouched",
                        **{k: rec[k] for k in ("ts", "exit_code", "results", "doctor")},
                    }
                    write_json(target, {**prior, "post_only": [*earlier, entry][-5:]})
                    return
                rec["label"] = "post phase only"
            write_json(target, rec)
            if phase == "complete":
                st = read_json(paths.state_dir() / "update-state.json", {}) or {}
                st["last_run"], st["last_ok"] = rec["ts"], report.ok
                write_json(paths.state_dir() / "update-state.json", st)
        except Exception:  # noqa: BLE001
            pass

    def _finish(
        self, report: Report, steps: list[str], doctor: bool, post_only: bool = False
    ) -> None:
        """The post steps (they need the code of the release that is installed NOW), the doctor verdict and the record."""
        for name in steps:
            self._step(report, name)
        if doctor and not self.dry:
            report.doctor = self._doctor_verdict()
        self._attach(report)
        if self.record:
            self._save_last(report, "complete", post_only=post_only)

    @contextlib.contextmanager
    def _exclusive(self, wait_s: float = 0.0):
        """One update at a time: a run that writes (not a preview that leaves no trace) holds state/update.lock. A second
        one raises UpdateInProgress after `wait_s`; a lock whose owner is gone is taken over (filelock.locked). A lock file
        that cannot be made is no reason to skip the update: it runs unguarded."""
        if self.dry and not self.record:
            yield
            return
        lock = locked(
            paths.state_dir() / UPDATE_LOCK, timeout=wait_s, stale_after=UPDATE_LOCK_STALE_S
        )
        try:
            lock.__enter__()
        except LockTimeout as exc:
            raise UpdateInProgress("another update is running") from exc
        except OSError:
            yield
            return
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def _cancel_reinstall(self, report: Report, busy: str) -> None:
        """Sessions started while the other components ran: claude-hr's own reinstall must not replace the tool environment
        under them (see _busy). The checkout stays on the new release; the next update finds the installed tool behind it and
        reinstalls (the 15-minute `started` mark is removed, or that retry would wait for it)."""
        self._pending_reinstall = None
        try:
            (paths.state_dir() / "self-update.json").unlink(missing_ok=True)
        except OSError:
            pass
        for r in report.results:
            if r.component == "self" and r.action == "updated":
                r.action, r.busy = "deferred", True
                r.detail = (
                    f"{busy} (started while this ran); the new release is checked out and "
                    "the next update reinstalls it"
                )

    def run(self, components: list[str] | None = None) -> Report:
        """Update what `components` names (default: everything) in the order of COMPONENTS. Without a queued self-update the
        post steps run here; with one they are chained behind its reinstall (see _launch_reinstall). Raises UpdateInProgress
        when another update holds the lock."""
        with self._exclusive():
            return self._run(components)

    def _run(self, components: list[str] | None) -> Report:
        chosen = [c for c in COMPONENTS if components is None or c in components]
        report = Report(check=self.dry)
        for name in (c for c in chosen if c in PHASE_A):
            self._step(report, name)
        steps = [c for c in chosen if c in POST]
        if self._pending_reinstall:
            # the guard of update_self was taken minutes ago (a Claude Code install alone may run 15): ask again
            busy = self._busy()
            if busy:
                self._cancel_reinstall(report, busy)
        if self._pending_reinstall:
            report.background = str(paths.logs_dir() / "self-update.log")
            self._attach(report)
            if self.record:
                self._save_last(
                    report, "pending", post={"steps": steps, "doctor": components is None}
                )
            # strictly last: nothing may run in this process while the tool environment is replaced
            self._launch_reinstall(post=self._post_command())
        else:
            self._finish(report, steps, doctor=components is None)
        return report

    def run_post(self) -> Report:
        """`update --post`: the steps that needed the new code. Continues the run that queued the self-update (its rows, its
        post steps) when that record is recent, else runs the full post phase. The helper calls this after the reinstall.
        Waits a while for a run that holds the lock, then raises UpdateInProgress."""
        with self._exclusive(wait_s=POST_LOCK_WAIT_S):
            return self._run_post()

    def _run_post(self) -> Report:
        from . import __version__

        prior = read_last()
        try:
            age = time.time() - float(prior.get("ts") or 0)
        except (TypeError, ValueError):  # a record somebody edited by hand
            age = PENDING_MAX_AGE_S
        pending = prior.get("phase") == "pending"
        fresh = pending and age < PENDING_MAX_AGE_S
        report = Report()
        steps, doctor = list(POST), True
        if fresh:
            header = prior.get("header")
            report.header = [str(x) for x in header] if isinstance(header, list) else []
            report.results = [_result_from(r) for r in _rows(prior)]
            post = prior.get("post") if isinstance(prior.get("post"), dict) else {}
            listed = post.get("steps")
            steps = [s for s in listed if s in POST] if isinstance(listed, list) else []
            doctor = bool(post.get("doctor"))
            for r in report.results:
                if r.component == "self" and r.action == "updated":
                    r.detail = f"reinstalled: claude-hr {__version__} is running"
        # a post phase that continues no pending run (a second one, or one typed by hand) must not replace the rows of the
        # run before it; an abandoned pending record (older than PENDING_MAX_AGE_S) is replaced
        self._finish(report, steps, doctor, post_only=not pending)
        return report
