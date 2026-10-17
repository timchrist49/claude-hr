"""Staged upgrades: check -> snapshot -> upgrade -> smoke test -> keep or roll back.

Components: claude (native build), headroom (uv tool), plugins (marketplace refresh), self (this repo).
Rules that keep it safe to run unattended:
  * never downgrade unless a pin says so; never upgrade the shared proxy or Claude Code while sessions run
  * every upgrade is smoke-tested; a failed smoke test restores the previous version
  * every decision is appended to logs/update.jsonl
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import paths, procutil, resources
from .claude_setup import (
    ClaudeSetup,
    Step,
    applied_version,
    record_applied_version,
    unattended_reapply,
)
from .config import Config
from .headroom import Headroom
from .jsonio import read_json, write_json
from .schedule import ps_quote

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"


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

    @property
    def ok(self) -> bool:
        return not any(r.action in {"failed", "rolled-back"} for r in self.results)


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


def _windows_reinstall_script(cmd: list[str], pid: int, ppid: int, log, env=None) -> str:
    """PowerShell source for the helper that reinstalls claude-hr after the updater process (and the claude-hr-ctl.exe
    trampoline that started it) has exited. Everything it does is appended to logs/self-update.log (UTF-8), and it deletes
    its own script file and the one-shot scheduled task that started it."""
    lines = [
        "$ErrorActionPreference = 'Continue'",
        *_helper_env_lines(os.environ if env is None else env),
        f"$log = {_ps_quote(str(log))}",
        "function Log($m) { Add-Content -LiteralPath $log -Encoding UTF8 -Value ('[' + (Get-Date -Format s) + '] ' + $m) }",
        f"$ids = @({pid})",
        f"$parent = Get-Process -Id {ppid} -ErrorAction SilentlyContinue",
        "if ($parent -and $parent.ProcessName -like 'claude-hr*') { $ids += $parent.Id }",
        "Log ('waiting for pid(s) ' + ($ids -join ',') + ' to exit before reinstalling')",
        "Wait-Process -Id $ids -Timeout 120 -ErrorAction SilentlyContinue",
        "Log ('running: ' + " + _ps_quote(" ".join(cmd)) + ")",
        "& " + " ".join(_ps_quote(part) for part in cmd) + " 2>&1 | ForEach-Object { "
        'if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" } } | '
        "Add-Content -LiteralPath $log -Encoding UTF8",
        "Log ('reinstall exit code ' + $LASTEXITCODE)",
        "if ($PSCommandPath) { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue }",
        f"Unregister-ScheduledTask -TaskName {_ps_quote(HELPER_TASK_NAME)} -TaskPath {_ps_quote(HELPER_TASK_FOLDER)} "
        "-Confirm:$false -ErrorAction SilentlyContinue",
    ]
    return "\n".join(lines) + "\n"


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


def _start_systemd_service(cmd: list[str], log) -> bool:
    """Start `cmd` as a transient unit of the systemd USER manager. The daily update runs as a timer's oneshot service whose
    default KillMode=control-group kills every process of its cgroup (even a setsid child) the moment the job ends; a
    reinstall started as the job's last act would be killed half-way. A unit created through the manager lives in its own
    cgroup and survives. Returns True if the unit was started."""
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
    argv += [f"--setenv={k}={v}" for k, v in sorted(_helper_env(os.environ).items())]
    argv += ["--", *cmd]
    return procutil.run(argv, timeout=30).returncode == 0


def _spawn_detached(cmd: list[str], log_path=None) -> None:
    """Start `cmd` detached from this process (own session / no console); output goes to `log_path` if given.

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
        self, cfg: Config, dry: bool = False, force: bool = False, allow_downgrade: bool = False
    ):
        self.cfg, self.dry, self.force, self.allow_downgrade = cfg, dry, force, allow_downgrade
        self.hr = Headroom(cfg)
        self.claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
        self._pending_reinstall: list[str] | None = (
            None  # set by update_self, started by run() once everything else is done
        )

    # ---- guards ---------------------------------------------------------------------------
    def _busy(self) -> str:
        """Why an upgrade must wait. On Windows replacing the tool env under a running launcher fails and can leave a
        half-installed package, so this guards claude-hr's own update as well."""
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

    def claude_target(self) -> str:
        pin = str(self.cfg.get("claude.pin", "") or "")
        if pin:
            return pin
        channel = str(self.cfg.get("claude.channel", "stable"))
        try:
            import urllib.request

            with urllib.request.urlopen(f"{CLAUDE_RELEASES}/{channel}", timeout=10) as resp:  # noqa: S310
                return resp.read().decode().strip()
        except Exception:
            return ""

    def update_claude(self) -> Result:
        cur, tgt = self.claude_version(), self.claude_target()
        res = Result("claude", cur, tgt)
        if not self.claude:
            res.action, res.detail = "failed", "claude not installed"
            return res
        if not tgt:
            res.action, res.detail = "failed", "could not read release channel"
            return res
        if not self.cfg.flag("claude.manage_updates", True):
            res.detail = "managed updates disabled in config"
            return res
        if not self.is_native_install():
            res.detail = (
                "not the native build (npm/Homebrew/WinGet): update it with that package manager"
            )
            return res
        if vtuple(tgt) == vtuple(cur):
            return res
        if (
            vtuple(tgt) < vtuple(cur)
            and not self.allow_downgrade
            and not self.cfg.get("claude.pin")
        ):
            res.detail = f"installed version is newer than channel '{self.cfg.get('claude.channel')}'; not downgrading"
            return res
        if self.dry:
            res.action = "available"
            return res
        busy = self._busy()
        if busy:
            res.action, res.detail = "deferred", busy
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
            res.detail = "disabled"
            return res
        if not tgt:
            res.action, res.detail = "failed", "could not read PyPI"
            return res
        if cur and vtuple(tgt) == vtuple(cur):
            return res
        if cur and vtuple(tgt) < vtuple(cur) and not (self.allow_downgrade or pin):
            res.detail = "installed version is newer than target; not downgrading"
            return res
        if self.dry:
            res.action = "available"
            return res
        busy = self._busy()
        if busy:
            res.action, res.detail = "deferred", busy
            return res
        was_running = self.hr.health().ok
        self.hr.stop()
        # a new release may need a newer model revision: allow the network for this one start
        online_h = {**self.hr.h, "offline_models": False}
        self.hr.h, saved_h = online_h, self.hr.h
        cutoff = None
        if cooldown > 0:  # cool the whole dependency tree, not just headroom-ai itself
            cutoff = (datetime.now(UTC) - timedelta(days=cooldown)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        else:
            res.action = "rolled-back" if cur else "failed"
            res.detail = (inst.stderr or "smoke test failed").strip()[-160:]
            if cur:
                self.hr.stop()
                # restore with the SAME dependency cutoff (an unconstrained reinstall would pull today's newest
                # transitive packages - possibly the very breakage being rolled back) and prove it works again
                back = self.hr.install(cur, exclude_newer=cutoff)
                if back.returncode != 0 or not self.smoke_headroom():
                    res.detail += " | WARNING: rollback could not be verified, run `claude-hr-ctl headroom bench`"
        self.hr.h = saved_h
        if was_running or res.action == "updated":
            self.hr.stop()  # restart so the next start uses the normal (possibly offline) settings
            self.hr.ensure(wait_s=0)
        return res

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
        if self.dry:
            res.detail = "dry run"
            return res
        setup = ClaudeSetup(self.cfg, self.hr, self.claude)
        before = {p["id"]: p.get("version") for p in setup._installed_plugins()}
        step = setup.update_marketplaces()
        after = {p["id"]: p.get("version") for p in setup._installed_plugins()}
        changed = [f"{k} {before.get(k)}->{v}" for k, v in after.items() if before.get(k) != v]
        res.action = "updated" if changed else "none"
        res.detail = "; ".join(changed) or "already current"
        if not step.ok:
            res.action, res.detail = "failed", "; ".join(step.lines)[-160:]
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
        run. Nothing happens on a dry run. Never raises."""
        if self.dry:
            return None
        from . import __version__

        res = Result("baseline")
        try:
            steps, why = unattended_reapply()
            if why:
                return Result("baseline", target=__version__, action="deferred", detail=why)
            if not steps:
                return None
            res.current, res.target = applied_version(steps[0]) or "?", __version__
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

    # ---- self (claude-hr itself) ----------------------------------------------------------------------
    def update_self(self) -> Result:
        repo = paths.hr_home() / "repo"
        res = Result("self")
        git = procutil.which_all("git")
        if not (repo / ".git").exists() or not git:
            res.detail = "not a managed checkout (dev install?)"
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
            res.detail = "no release tag yet"
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
            res.detail = f"installed commit is not behind {tgt}; not moving"
            return res
        if self.dry:
            res.action = "available"
            res.detail = "reinstall of the checked-out release is pending" if retry else ""
            return res
        busy = self._busy()
        if busy:  # replacing the tool env under a running launcher fails on Windows and can leave a broken install
            res.action, res.detail = "deferred", busy
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
            (
                "retrying the reinstall of the checked-out release (the last one did not finish); "
                if retry
                else ""
            )
            + "reinstalling in the background (log: logs/self-update.log); run `claude-hr-ctl setup` afterwards if config changed"
        )
        return res

    def _launch_reinstall(self) -> None:
        """Start the detached `uv tool install --reinstall` for a self-update. Called once, after every component has run.

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
        try:
            paths.ensure_dirs()  # the helper script lives in state/, its log in logs/
            if paths.IS_WINDOWS:
                shell = procutil.which_all("powershell") or procutil.which_all("pwsh")
                if shell:
                    import base64

                    script = _windows_reinstall_script(cmd, os.getpid(), os.getppid(), log)
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
            if not paths.IS_WINDOWS and _start_systemd_service(cmd, log):
                return
            _spawn_detached(cmd, log)
        except OSError as exc:
            _log(
                {
                    "component": "self",
                    "action": "failed",
                    "detail": f"could not start the reinstall: {exc}",
                }
            )

    # ---- orchestration --------------------------------------------------------------------
    def run(self, components: list[str] | None = None) -> Report:
        order = components or [
            "claude",
            "headroom",
            "self",
            "plugins",
        ]  # self before plugins: the plugin comes from the repo
        fns = {
            "claude": self.update_claude,
            "headroom": self.update_headroom,
            "plugins": self.update_plugins,
            "self": self.update_self,
        }
        report = Report()
        # a full update starts by bringing the machine up to the release that is already installed (see reapply_baseline)
        steps = [("baseline", self.reapply_baseline)] if components is None else []
        steps += [(name, fns[name]) for name in order]
        for name, fn in steps:
            try:
                res = fn()
            except Exception as exc:  # one broken component must not block the others
                res = Result(name, action="failed", detail=f"{type(exc).__name__}: {exc}")
            if res is None:  # nothing to do for this step: no row, no log line
                continue
            report.results.append(res)
            _log(
                {
                    "component": res.component,
                    "from": res.current,
                    "to": res.target,
                    "action": res.action,
                    "detail": res.detail,
                }
            )
        st = read_json(paths.state_dir() / "update-state.json", {}) or {}
        st["last_run"] = time.time()
        st["last_ok"] = report.ok
        write_json(paths.state_dir() / "update-state.json", st)
        self._launch_reinstall()  # strictly last: nothing may run in this process while the tool environment is replaced
        return report
