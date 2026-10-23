import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from claude_hr import config, resources, updater
from claude_hr.checks import FAIL, INFO, WARN, Check


@pytest.fixture(autouse=True)
def _hermetic_report(monkeypatch, tmp_path):
    """A run's report asks the machine two things no test here is about: which processes run (a Claude Code session on the
    developer's machine would add an INFO line) and what doctor says. Tests that are about them replace these again."""
    from claude_hr import doctor

    # the helper script waits for everything that runs from the tool environment: the pytest interpreter does, so a script
    # run for real by a test must look at a folder nothing runs from
    monkeypatch.setattr(updater, "_tool_env", lambda: str(tmp_path / "no-tool-env"))
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {})
    monkeypatch.setattr(doctor, "run", lambda cfg, cwd, deep=False: [])
    # ... nor may the machine's routing decide a test: a shell inside a claude-hr session has ANTHROPIC_BASE_URL pointing at
    # the proxy, a test run by a systemd unit has INVOCATION_ID
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    # ... and no run may ask the machine's real claude whether its background service is busy
    from claude_hr import launcher

    monkeypatch.setattr(
        launcher,
        "_daemon_status",
        lambda claude: {"running": False, "pid": 0, "starter_pid": 0, "workers": 0},
    )


def make(sandbox, monkeypatch, current="2.1.292", target="2.1.300", **kw):
    cfg = config.load()
    u = updater.Updater(cfg, **kw)
    u.claude = Path("/fake/claude")
    monkeypatch.setattr(u, "claude_version", lambda: current)
    monkeypatch.setattr(u, "claude_target", lambda: target)
    monkeypatch.setattr(u, "is_native_install", lambda: True)
    return u


def test_vtuple():
    assert updater.vtuple("2.1.300 (Claude Code)") == (2, 1, 300)
    assert updater.vtuple("v0.41.0") == (0, 41, 0)
    assert updater.vtuple(None) == ()
    assert updater.vtuple("2.1.9") < updater.vtuple("2.1.10")


def test_dry_run_reports_available_without_acting(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(
        updater.procutil,
        "run",
        lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    u = make(sandbox, monkeypatch, dry=True)
    res = u.update_claude()
    assert res.action == "available"
    # a preview asks claude nothing at all (the daemon probe is stubbed by the autouse fixture): not one command
    # that could install, update or refresh anything
    assert calls == []
    assert res.detail == "Claude Code 2.1.292 -> channel stable offers 2.1.300"


def test_up_to_date_is_a_noop(sandbox, monkeypatch):
    assert make(sandbox, monkeypatch, current="2.1.300").update_claude().action == "none"


def test_never_downgrades_a_newer_install(sandbox, monkeypatch):
    res = make(sandbox, monkeypatch, current="2.1.300", target="2.1.292").update_claude()
    assert res.action == "none"
    assert "not downgrading" in res.detail


def test_pin_may_downgrade(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__PIN", "2.1.292")
    installed = []

    def fake_run(cmd, **k):
        if cmd[1] == "install":
            installed.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(updater.procutil, "run", fake_run)
    u = make(sandbox, monkeypatch, current="2.1.300", target="2.1.292")
    state = {"v": "2.1.300"}
    monkeypatch.setattr(u, "claude_version", lambda: state["v"] if not installed else "2.1.292")
    assert u.update_claude().action == "updated"
    assert installed == ["2.1.292"]


def test_defers_while_sessions_run(sandbox, monkeypatch):
    resources.register_session(__import__("os").getpid(), "/x")
    res = make(sandbox, monkeypatch).update_claude()
    assert res.action == "deferred"
    assert "session" in res.detail
    resources.unregister_session(__import__("os").getpid())


def test_force_overrides_deferral(sandbox, monkeypatch):
    resources.register_session(__import__("os").getpid(), "/x")
    monkeypatch.setattr(
        updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    u = make(sandbox, monkeypatch, force=True)
    seq = iter(["2.1.292", "2.1.300"])
    monkeypatch.setattr(u, "claude_version", lambda: next(seq))
    assert u.update_claude().action == "updated"
    resources.unregister_session(__import__("os").getpid())


def test_failed_install_rolls_back(sandbox, monkeypatch):
    log = []

    def fake_run(cmd, **k):
        log.append(cmd[1:3])
        return subprocess.CompletedProcess(
            cmd, 1 if cmd[1] == "install" and cmd[2] == "2.1.300" else 0, "", "boom"
        )

    monkeypatch.setattr(updater.procutil, "run", fake_run)
    u = make(sandbox, monkeypatch)
    res = u.update_claude()
    assert res.action == "rolled-back"
    assert ["install", "2.1.292"] in log  # previous version restored


def test_wrong_version_after_install_rolls_back(sandbox, monkeypatch):
    monkeypatch.setattr(
        updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", "")
    )
    u = make(sandbox, monkeypatch)  # claude_version keeps reporting the old version
    assert u.update_claude().action == "rolled-back"


def test_unmanaged_updates_are_left_alone(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__MANAGE_UPDATES", "false")
    res = make(sandbox, monkeypatch).update_claude()
    assert res.action == "none"


def test_component_failure_does_not_block_others(sandbox, monkeypatch):
    u = make(sandbox, monkeypatch, dry=True)
    monkeypatch.setattr(
        u, "update_headroom", lambda: (_ for _ in ()).throw(RuntimeError("pypi down"))
    )
    monkeypatch.setattr(u, "update_plugins", lambda: updater.Result("plugins"))
    monkeypatch.setattr(u, "update_self", lambda: updater.Result("self"))
    rep = u.run()
    by = {r.component: r for r in rep.results}
    assert by["headroom"].action == "failed"
    assert "pypi down" in by["headroom"].detail
    assert by["claude"].action == "available"
    assert not rep.ok


DAY = 86400


def rel(age_days, now, yanked=False):
    import datetime as dt

    ts = dt.datetime.fromtimestamp(now - age_days * DAY, dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z"
    )
    return [{"upload_time_iso_8601": ts, "yanked": yanked}]


def test_pick_release_respects_cooldown_and_skips_bad_builds():
    from claude_hr.headroom import pick_release

    now = 1_800_000_000
    releases = {
        "0.38.0": rel(40, now),
        "0.40.0": rel(8, now),
        "0.40.1": rel(6, now),
        "0.41.0": rel(1, now),  # too new for a 5-day cooldown
        "0.42.0rc1": rel(7, now),  # pre-release
        "0.39.9": rel(20, now, yanked=True),
        "0.39.0": [],  # no files
    }
    assert pick_release(releases, 5, now=now) == "0.40.1"
    assert pick_release(releases, 0, now=now) == "0.41.0"
    assert pick_release(releases, 90, now=now) is None
    assert pick_release(releases, 7, now=now) == "0.40.0"
    assert pick_release(releases, 10, now=now) == "0.38.0"


def _patch_headroom(monkeypatch, version_state, health_ok=True):
    from claude_hr.headroom import Headroom, Health

    monkeypatch.setattr(Headroom, "version", lambda self: version_state["v"])
    monkeypatch.setattr(Headroom, "health", lambda self, timeout=0.6: Health(health_ok))
    monkeypatch.setattr(Headroom, "stop", lambda self: True)
    monkeypatch.setattr(Headroom, "ensure", lambda self, wait_s=0.0, announce=None: Health(True))


def test_headroom_update_uses_cooldown_and_exclude_newer(sandbox, monkeypatch):
    from claude_hr.headroom import Headroom

    state = {"v": "0.40.0"}
    _patch_headroom(monkeypatch, state)
    seen = {}

    def latest(self, timeout=15.0, cooldown_days=0.0):
        seen["cooldown"] = cooldown_days
        return "0.40.1"

    installs = []

    def fake_install(self, version=None, upgrade=True, exclude_newer=None):
        installs.append((version, exclude_newer))
        state["v"] = version
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(Headroom, "latest_version", latest)
    monkeypatch.setattr(Headroom, "install", fake_install)
    monkeypatch.setattr(updater.Updater, "smoke_headroom", lambda self, wait_s=150.0: True)
    res = updater.Updater(config.load()).update_headroom()
    assert res.action == "updated"
    assert seen["cooldown"] == 5.0
    assert installs[0][0] == "0.40.1"
    assert installs[0][1] and installs[0][1].endswith("Z")


def test_headroom_smoke_failure_reinstalls_previous(sandbox, monkeypatch):
    from claude_hr.headroom import Headroom

    state = {"v": "0.40.0"}
    _patch_headroom(monkeypatch, state)
    installs = []

    def fake_install(self, version=None, upgrade=True, exclude_newer=None):
        installs.append(version)
        state["v"] = version
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: "0.40.1"
    )
    monkeypatch.setattr(Headroom, "install", fake_install)
    monkeypatch.setattr(updater.Updater, "smoke_headroom", lambda self, wait_s=150.0: False)
    res = updater.Updater(config.load()).update_headroom()
    assert res.action == "rolled-back"
    assert installs == ["0.40.1", "0.40.0"]


def test_force_skips_the_cooldown(sandbox, monkeypatch):
    from claude_hr.headroom import Headroom

    state = {"v": "0.40.0"}
    _patch_headroom(monkeypatch, state)
    seen = {}
    monkeypatch.setattr(
        Headroom,
        "latest_version",
        lambda self, timeout=15.0, cooldown_days=0.0: (
            seen.setdefault("cd", cooldown_days) or "0.41.0"
        ),
    )
    monkeypatch.setattr(
        Headroom,
        "install",
        lambda self, version=None, upgrade=True, exclude_newer=None: subprocess.CompletedProcess(
            [], 1, "", "x"
        ),
    )
    updater.Updater(config.load(), dry=True, force=True).update_headroom()
    assert seen["cd"] == 0.0


def test_non_native_installs_are_not_touched(sandbox, monkeypatch):
    u = updater.Updater(
        config.load()
    )  # real is_native_install() on a path that is not the native layout
    u.claude = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js")
    monkeypatch.setattr(u, "claude_version", lambda: "2.1.292")
    monkeypatch.setattr(u, "claude_target", lambda: "2.1.300")
    res = u.update_claude()
    assert res.action == "none"
    assert "package manager" in res.detail


def test_native_layout_detection(sandbox):
    u = updater.Updater(config.load())
    home = sandbox
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / ("claude.exe" if __import__("os").name == "nt" else "claude")
    exe.write_text("x")
    u.claude = exe
    assert u.is_native_install()


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@__import__("pytest").fixture
def managed_repo(sandbox, tmp_path):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    for c in (work,):
        _git(c, "config", "user.email", "t@example.com")
        _git(c, "config", "user.name", "t")
    (work / "f").write_text("1")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "c1")
    _git(work, "tag", "v0.1.0")
    _git(work, "push", "-q", "origin", "main", "--tags")
    managed = sandbox / ".claude-hr" / "repo"
    managed.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(managed)], check=True, capture_output=True
    )
    return origin, work, managed


def test_self_update_reports_newer_tag_and_never_rewinds(managed_repo):
    origin, work, managed = managed_repo
    u = updater.Updater(config.load(), dry=True)
    assert u.update_self().detail == ""  # at the newest tag: nothing to do
    (work / "f").write_text("2")
    _git(work, "commit", "-q", "-am", "c2")
    _git(work, "tag", "v0.2.0")
    _git(work, "push", "-q", "origin", "main", "--tags")
    res = u.update_self()
    assert res.action == "available"
    assert res.target == "v0.2.0"
    # installed checkout sits on main AHEAD of the newest tag -> must not be moved back to an older tag
    _git(work, "commit", "-q", "--allow-empty", "-m", "c3")
    _git(work, "push", "-q", "origin", "main")
    _git(managed, "fetch", "-q", "--tags")
    _git(managed, "checkout", "-q", "--detach", "origin/main")
    res = u.update_self()
    assert res.action == "none"
    assert "not moving" in res.detail


def test_self_update_surfaces_fetch_failure(managed_repo, tmp_path):
    origin, work, managed = managed_repo
    _git(managed, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    res = updater.Updater(config.load(), dry=True).update_self()
    assert res.action == "failed"
    assert "fetch failed" in res.detail


def test_update_defers_when_daemon_workers_run(sandbox, monkeypatch):
    from claude_hr import launcher

    monkeypatch.setattr(
        launcher,
        "_daemon_status",
        lambda claude: {"running": True, "pid": 1, "starter_pid": 2, "workers": 2},
    )
    res = make(sandbox, monkeypatch).update_claude()
    assert res.action == "deferred"
    assert "background" in res.detail


def test_update_does_not_treat_unknown_daemon_state_as_idle(sandbox, monkeypatch):
    from claude_hr import launcher

    monkeypatch.setattr(
        launcher,
        "_daemon_status",
        lambda claude: {"running": True, "pid": 1, "starter_pid": 2, "workers": -1},
    )
    assert make(sandbox, monkeypatch).update_claude().action == "deferred"


def test_self_update_defers_while_sessions_run(managed_repo, monkeypatch):
    origin, work, managed = managed_repo
    (work / "f").write_text("2")
    _git(work, "commit", "-q", "-am", "c2")
    _git(work, "tag", "v0.2.0")
    _git(work, "tag", "v0.3.0-rc1")  # pre-release tags are never picked
    _git(work, "push", "-q", "origin", "main", "--tags")
    resources.register_session(__import__("os").getpid(), "/x")
    try:
        res = updater.Updater(config.load()).update_self()
    finally:
        resources.unregister_session(__import__("os").getpid())
    assert res.action == "deferred"
    assert res.target == "v0.2.0"
    assert _git(managed, "describe", "--tags", "--always") == "v0.1.0"  # nothing was checked out


def _release_v020(work):
    (work / "f").write_text("2")
    _git(work, "commit", "-q", "-am", "c2")
    _git(work, "tag", "v0.2.0")
    _git(work, "push", "-q", "origin", "main", "--tags")


def test_update_self_only_queues_the_reinstall(managed_repo, monkeypatch):
    """The reinstall replaces the running tool environment: update_self must not start it, run() starts it last."""
    origin, work, managed = managed_repo
    _release_v020(work)
    spawned = []
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: spawned.append(cmd)
    )
    u = updater.Updater(config.load())
    res = u.update_self()
    assert res.action == "updated" and res.target == "v0.2.0"
    assert spawned == [], "update_self started the reinstall itself"
    assert u._pending_reinstall and u._pending_reinstall[1:3] == ["tool", "install"]
    assert (
        _git(managed, "describe", "--tags", "--always") == "v0.2.0"
    )  # the checkout (and so the plugin source) is already new


def test_run_starts_the_reinstall_after_plugins_were_updated(sandbox, monkeypatch):
    """Windows regression: `self` then `plugins` in one process used to leave the tool environment empty."""
    events = []
    u = updater.Updater(config.load())

    def fake_self():
        u._pending_reinstall = ["uv", "tool", "install", "--force", "--reinstall", "/repo"]
        events.append("self")
        return updater.Result("self", action="updated")

    monkeypatch.setattr(u, "update_self", fake_self)
    monkeypatch.setattr(
        u, "update_plugins", lambda: events.append("plugins") or updater.Result("plugins")
    )
    monkeypatch.setattr(
        updater.paths, "IS_WINDOWS", False
    )  # this test is about ordering, not the Windows helper
    monkeypatch.setattr(
        updater, "_start_systemd_service", lambda cmd, log, cwd=None: False
    )  # fall through to the plain spawn
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: events.append("reinstall")
    )
    u.run(["self", "plugins"])
    assert events == ["self", "plugins", "reinstall"]
    assert u._pending_reinstall is None
    events.clear()
    u.run(["plugins"])  # nothing queued -> nothing started
    assert events == ["plugins"]


def _windows_updater(monkeypatch):
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(updater.procutil, "which_all", lambda name: [r"C:\Windows\powershell.exe"])
    u = updater.Updater(config.load())
    u._pending_reinstall = [
        r"C:\Users\O'Neil\bin\uv.exe",
        "tool",
        "install",
        "--force",
        "--reinstall",
        r"C:\Users\O'Neil\.claude-hr\repo",
    ]
    __import__("shutil").rmtree(
        updater.paths.hr_home(), ignore_errors=True
    )  # the updater must create state/ and logs/ itself
    return u


def test_windows_reinstall_runs_from_a_scheduled_task_not_as_our_child(sandbox, monkeypatch):
    """uv's claude-hr-ctl.exe trampoline kills everything in its job when it exits, so a child of ours never reinstalls."""
    started, spawned = [], []
    monkeypatch.setattr(
        updater, "_start_helper_task", lambda shell, script: started.append((shell, script)) or True
    )
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: spawned.append(cmd)
    )
    u = _windows_updater(monkeypatch)
    u._launch_reinstall()
    assert spawned == []
    shell, script_path = started[0]
    assert shell.endswith("powershell.exe") and script_path.name == "self-update.ps1"
    raw = script_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, or PowerShell 5.1 reads the file as ANSI
    text = raw.decode("utf-8-sig")
    assert text.index("Wait-Process") < text.index(
        "'tool'"
    )  # waits for this process first, then reinstalls
    assert (
        "claude-hr*" in text
    )  # also waits for the claude-hr-ctl.exe trampoline that started this process


def test_windows_reinstall_falls_back_to_a_direct_helper_if_the_scheduler_refuses(
    sandbox, monkeypatch
):
    import base64

    spawned = []
    monkeypatch.setattr(updater, "_start_helper_task", lambda shell, script: False)
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: spawned.append(cmd)
    )
    _windows_updater(monkeypatch)._launch_reinstall()
    cmd = spawned[0]
    assert cmd[0].endswith("powershell.exe") and "-EncodedCommand" in cmd
    assert "Wait-Process" in base64.b64decode(cmd[-1]).decode("utf-16-le")


@pytest.mark.real_helpers
def test_helper_task_is_registered_and_started_synchronously(sandbox, monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"], seen["kw"] = cmd, k
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(updater.procutil, "run", fake_run)
    assert updater._start_helper_task("powershell.exe", tmp_path / "self-update.ps1") is True
    ps = seen["cmd"][-1]
    assert "Register-ScheduledTask -TaskName 'selfupdate' -TaskPath '\\claude-hr\\'" in ps
    assert ps.index("Register-ScheduledTask") < ps.index("Start-ScheduledTask")
    assert "-File" in ps and "self-update.ps1" in ps and "-ExecutionPolicy Bypass" in ps
    monkeypatch.setattr(
        updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "denied")
    )
    assert updater._start_helper_task("powershell.exe", tmp_path / "x.ps1") is False


@pytest.mark.real_helpers
def test_detached_helper_breaks_away_from_the_trampoline_job_and_logs(
    sandbox, monkeypatch, tmp_path
):
    """uv's claude-hr-ctl.exe trampoline kills its job on exit; the reinstall helper must survive it and say what it did."""
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            raise OSError(
                "breakaway not permitted"
            )  # the parent job forbids it: fall back to a plain detached start

    monkeypatch.setattr(updater.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    log = tmp_path / "self-update.log"
    updater._spawn_detached(["powershell", "-File", "x"], log)
    assert calls[0][1]["creationflags"] & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB first
    for call in calls:
        assert call[1]["creationflags"] & 0x08000000  # CREATE_NO_WINDOW
        assert not call[1]["creationflags"] & 0x00000008, (
            "DETACHED_PROCESS overrides CREATE_NO_WINDOW: the shell the helper starts would open a visible console"
        )
    assert not calls[1][1]["creationflags"] & 0x01000000  # fallback without it
    assert (
        log.exists() and calls[1][1]["stdout"] is not subprocess.DEVNULL
    )  # output goes to the log, not nowhere


def test_reinstall_script_recreates_the_uv_environment(sandbox):
    """Regression: a task started by Task Scheduler does not inherit UV_TOOL_DIR, so the helper installed claude-hr into the
    default tool directory instead of the one the updater was running from."""
    env = {
        "UV_TOOL_DIR": r"D:\x'y\tools",
        "UV_TOOL_BIN_DIR": r"D:\bin",
        "Path": r"C:\a;C:\b",
        "UNRELATED": "no",
        "CLAUDE_CONFIG_DIR": "no",
    }
    lines = updater._windows_reinstall_script(
        ["uv", "tool", "install"], 1, 2, "log.txt", env=env
    ).splitlines()
    assert r"$env:UV_TOOL_DIR = 'D:\x''y\tools'" in lines
    assert r"$env:UV_TOOL_BIN_DIR = 'D:\bin'" in lines
    assert r"$env:Path = 'C:\a;C:\b'" in lines
    assert not any("UNRELATED" in ln or "CLAUDE_CONFIG_DIR" in ln for ln in lines)
    last_env = max(i for i, ln in enumerate(lines) if ln.startswith("$env:"))
    assert last_env < next(
        i for i, ln in enumerate(lines) if ln.lstrip().startswith("& ")
    )  # set before uv runs


def test_reinstall_script_logs_each_step_and_quotes_awkward_paths():
    script = updater._windows_reinstall_script(
        [r"C:\Users\O'Neil\uv.exe", "tool", "install", r"D:\a b\repo"],
        111,
        222,
        r"C:\Users\O'Neil\logs\self-update.log",
        env={},
    )
    lines = script.splitlines()
    assert "$ids = @(111)" in lines and any("Get-Process -Id 222" in ln for ln in lines)
    assert any(ln.startswith("Wait-Process -Id $ids") for ln in lines)
    uv_line = next(ln for ln in lines if ln.lstrip().startswith("& ")).lstrip()
    assert uv_line.startswith(
        r"& 'C:\Users\O''Neil\uv.exe' 'tool' 'install' 'D:\a b\repo' 2>&1 | ForEach-Object"
    )
    assert (
        "Add-Content -LiteralPath $log -Encoding UTF8" in uv_line
    )  # ONE encoding in the log (5.1: `*>>` wrote UTF-16)
    assert "-Encoding UTF8" in next(ln for ln in lines if ln.startswith("function Log"))
    assert "$log = 'C:\\Users\\O''Neil\\logs\\self-update.log'" in lines
    assert any("reinstall exit code" in ln for ln in lines)
    # it removes its own script (it may hold the user's PATH) and then the one-shot task that started it
    assert any("Remove-Item -LiteralPath $PSCommandPath" in ln for ln in lines)
    assert lines[-1].startswith(
        "Unregister-ScheduledTask -TaskName 'selfupdate' -TaskPath '\\claude-hr\\'"
    )


def test_helper_environment_never_carries_credentials():
    """The helper script is a file on disk: no tokens, passwords or URLs with embedded credentials may be written into it."""
    env = {
        "UV_TOOL_DIR": r"D:\tools",
        "UV_CACHE_DIR": r"D:\cache",
        "Path": r"C:\a;C:\b",
        "UV_INDEX_URL": "https://user:pw@mirror.example/simple",
        "UV_INDEX_CORP_PASSWORD": "pw",
        "UV_PUBLISH_TOKEN": "t",
        "UV_GITHUB_TOKEN": "t",
        "UV_INDEX_CORP_USERNAME": "u",
        "UV_KEYRING_PROVIDER": "subprocess",
        "UV_INDEX": "https://ok.example/simple",
        "GITHUB_TOKEN": "t",
        "HOME": "x",
    }
    kept = updater._helper_env(env)
    assert kept == {
        "UV_TOOL_DIR": r"D:\tools",
        "UV_CACHE_DIR": r"D:\cache",
        "Path": r"C:\a;C:\b",
        "UV_INDEX": "https://ok.example/simple",
    }
    script = updater._windows_reinstall_script(["uv"], 1, 2, "log.txt", env=env)
    for secret in (
        "user:pw",
        "PASSWORD",
        "UV_PUBLISH_TOKEN",
        "GITHUB_TOKEN",
        "USERNAME",
        "KEYRING",
    ):
        assert secret not in script


def test_powershell_quoting_doubles_every_kind_of_single_quote():
    """PowerShell also closes a string at U+2018/2019/201A/201B: one in a path would end the literal early (and could inject)."""
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        quoted = updater._ps_quote(f"C:\\Users\\Tim{q}s\\bin")
        assert quoted == f"'C:\\Users\\Tim{q}{q}s\\bin'"
    assert updater._ps_quote("plain") == "'plain'"


@pytest.mark.skipif(
    __import__("shutil").which("powershell") is None and __import__("shutil").which("pwsh") is None,
    reason="needs PowerShell",
)
def test_generated_helper_script_parses_with_hostile_values(tmp_path):
    """Render the script with awkward values and let the real PowerShell parser judge it (parse only, nothing is run)."""
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    env = {
        "UV_TOOL_DIR": "C:\\Users\\Tim\u2019s\\t'ools",
        "Path": "C:\\a b;C:\\\u201bq;$(calc);`n",
        "UV_X": "line1\nline2",
    }
    script = updater._windows_reinstall_script(
        ["C:\\Users\\Tim\u2019s\\uv.exe", "tool", "install", "D:\\a $(calc) b\\repo"],
        1,
        2,
        "C:\\l\u2018og\\x.log",
        env=env,
    )
    ps1 = tmp_path / "helper.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))
    check = (
        "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{ps1}', [ref]$t, [ref]$e); if ($e.Count -eq 0) {{ 'PARSE_OK' }} else {{ $e | ForEach-Object {{ $_.Message }} }}"
    )
    out = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "PARSE_OK" in out, out


@pytest.mark.real_helpers
def test_systemd_service_survives_the_timer_job_and_gets_the_uv_environment(sandbox, monkeypatch):
    from claude_hr import resources

    seen = {}
    monkeypatch.setattr(updater.paths, "IS_LINUX", True)
    monkeypatch.setattr(resources, "caps_available", lambda: True)
    monkeypatch.setattr(
        updater.procutil,
        "run",
        lambda cmd, **k: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setenv("UV_TOOL_DIR", "/srv/tools")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "secret")
    assert updater._start_systemd_service(["uv", "tool", "install"], "/tmp/self-update.log") is True
    cmd = seen["cmd"]
    assert cmd[:3] == ["systemd-run", "--user", "--quiet"]
    assert any(a.startswith("--unit=claude-hr-selfupdate-") for a in cmd)
    assert (
        "StandardOutput=append:/tmp/self-update.log" in cmd
        and "--setenv=UV_TOOL_DIR=/srv/tools" in cmd
    )
    assert not any("secret" in a or "PUBLISH_TOKEN" in a for a in cmd)
    assert cmd[-4:] == ["--", "uv", "tool", "install"] or cmd[cmd.index("--") + 1 :] == [
        "uv",
        "tool",
        "install",
    ]
    monkeypatch.setattr(resources, "caps_available", lambda: False)
    assert (
        updater._start_systemd_service(["uv"], "/tmp/x.log") is False
    )  # no user manager: caller falls back to a plain spawn


def test_posix_reinstall_prefers_a_systemd_unit_and_falls_back_to_a_plain_spawn(
    sandbox, monkeypatch
):
    events = []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    u = updater.Updater(config.load())
    monkeypatch.setattr(
        updater, "_start_systemd_service", lambda cmd, log, cwd=None: events.append("unit") or True
    )
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: events.append("spawn")
    )
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall()
    assert events == ["unit"]
    monkeypatch.setattr(
        updater, "_start_systemd_service", lambda cmd, log, cwd=None: events.append("unit") or False
    )
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall()
    assert events == ["unit", "unit", "spawn"]


def _stub_uv(monkeypatch, found=True):
    real = updater.procutil.which_all
    monkeypatch.setattr(
        updater.procutil,
        "which_all",
        lambda name: ([r"/usr/bin/uv"] if found else []) if name == "uv" else real(name),
    )


def test_uv_missing_does_not_move_the_checkout(managed_repo, monkeypatch):
    """The checkout used to move first; with uv missing the tool then stayed old and `head == want` hid the problem forever."""
    origin, work, managed = managed_repo
    _release_v020(work)
    _stub_uv(monkeypatch, found=False)
    res = updater.Updater(config.load()).update_self()
    assert res.action == "failed" and "uv missing" in res.detail
    assert _git(managed, "describe", "--tags", "--always") == "v0.1.0"


def _fake_checkout_version(managed, version):
    d = managed / "src" / "claude_hr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def test_a_reinstall_that_never_finished_is_retried(managed_repo, monkeypatch, sandbox):
    """Checkout at the newest tag but the installed tool is older: the earlier reinstall failed/was killed -> try again."""
    origin, work, managed = managed_repo
    _release_v020(work)
    _git(managed, "fetch", "-q", "--tags")
    _git(managed, "checkout", "-q", "--detach", "v0.2.0")
    _stub_uv(monkeypatch)
    monkeypatch.setattr(updater, "__version__", "0.1.0", raising=False)
    monkeypatch.setattr("claude_hr.__version__", "0.1.0")
    _fake_checkout_version(managed, "0.2.0")
    u = updater.Updater(config.load())
    paths_state = updater.paths.state_dir()
    paths_state.mkdir(parents=True, exist_ok=True)
    (paths_state / "self-update.json").write_text('{"ts": 1}', encoding="utf-8")  # long ago
    res = u.update_self()
    assert res.action == "updated" and "retrying" in res.detail
    assert u._pending_reinstall and u._pending_reinstall[1:3] == ["tool", "install"]
    # ... but not while a reinstall started a moment ago may still be running
    u2 = updater.Updater(config.load())
    (paths_state / "self-update.json").write_text(
        json.dumps({"ts": __import__("time").time()}), encoding="utf-8"
    )
    res2 = u2.update_self()
    assert res2.action == "none" and "waiting" in res2.detail and u2._pending_reinstall is None
    # an up-to-date tool is left alone
    monkeypatch.setattr("claude_hr.__version__", "0.2.0")
    assert updater.Updater(config.load()).update_self().detail == ""


# ---- after an unattended self-update the new release's baseline must be applied (wiring review) ------------------------
# What the unattended job may re-apply is the settings baseline and the CLAUDE.md block, and nothing else: no plugin install,
# no marketplace, no `-y` (SECURITY.md), and only for a step `setup` recorded, in the Claude config dir it recorded.
@pytest.fixture
def upgraded_machine(sandbox, monkeypatch):
    """A machine `setup --only settings claude-md` ran on under an older release (0.1.9, whose stop-review default was
    different). The claude CLI is faked at the one place ClaudeSetup calls it, and nothing that installs Headroom or touches
    the scheduler may run."""
    from claude_hr import claude_setup, ctl, schedule
    from claude_hr.headroom import Headroom

    calls = []

    def fake_claude(self, *args, timeout=120):
        calls.append(args)
        listing = args[:3] == ("plugin", "list", "--json") or args[:3] == (
            "plugin",
            "marketplace",
            "list",
        )
        return subprocess.CompletedProcess(args, 0, "[]" if listing else "", "")

    def forbidden(*a, **k):
        raise AssertionError(
            "re-applying the baseline must not install Headroom or touch the scheduler"
        )

    monkeypatch.setattr(claude_setup.ClaudeSetup, "_claude", fake_claude)
    monkeypatch.setattr(Headroom, "install", forbidden)
    monkeypatch.setattr(schedule, "install", forbidden)
    monkeypatch.setattr(updater.procutil, "find_claude", lambda *a, **k: Path("/fake/claude"))
    with monkeypatch.context() as old:
        old.setattr("claude_hr.__version__", "0.1.9")
        old.setenv("CLAUDE_HR_SECURITY__STOP_REVIEW", "plugin-default")
        assert ctl._run(["setup", "--only", "settings", "claude-md"]) == 0
    calls.clear()
    u = updater.Updater(config.load())
    u.claude = Path("/fake/claude")
    u.calls = calls
    return u


def test_reapply_baseline_applies_settings_and_claude_md_once_and_never_touches_plugins(
    upgraded_machine,
):
    from claude_hr import __version__, claude_setup, paths

    u = upgraded_machine
    assert claude_setup.baseline_is_stale() == (True, "0.1.9")
    assert "ENABLE_STOP_REVIEW" not in json.loads(paths.claude_settings().read_text("utf-8"))["env"]
    res = u.reapply_baseline()
    assert res.component == "baseline" and res.action == "updated", res.detail
    assert "settings ok" in res.detail and "claude-md ok" in res.detail
    assert "plugins" not in res.detail
    doc = json.loads(paths.claude_settings().read_text(encoding="utf-8"))
    assert doc["env"]["ENABLE_STOP_REVIEW"] == "0"  # what this release's baseline adds
    assert "CLAUDE-HR:BEGIN" in paths.claude_user_md().read_text(encoding="utf-8")
    # unattended: no plugin install, no marketplace, no -y, not even a `claude` call
    assert u.calls == []
    assert claude_setup.applied_version("settings") == __version__
    assert claude_setup.applied_version("claude-md") == __version__
    assert claude_setup.baseline_is_stale() == (False, __version__)
    assert u.reapply_baseline() is None  # the next run finds nothing to do
    assert u.calls == []


def test_reapply_baseline_does_not_need_claude_code_installed(upgraded_machine):
    u = upgraded_machine
    u.claude = None  # plugins are not part of it, so a missing claude is not a failure
    res = u.reapply_baseline()
    assert res.action == "updated", res.detail


def test_the_full_update_run_never_installs_a_plugin_or_passes_y(upgraded_machine, monkeypatch):
    """SECURITY.md: unattended updates never pass `-y` and never add a marketplace or install a plugin."""
    u = upgraded_machine
    for name in ("update_claude", "update_headroom", "update_self", "update_proxy"):
        comp = name.split("_", 1)[1]
        monkeypatch.setattr(u, name, lambda comp=comp: updater.Result(comp))
    rep = u.run()  # the real update_plugins runs too
    by = {r.component: r for r in rep.results}
    assert by["baseline"].action == "updated"
    assert u.calls, "update_plugins should have asked the fake claude something"
    assert ("plugin", "marketplace", "update") in [c[:3] for c in u.calls]
    for call in u.calls:
        assert "-y" not in call and "--yes" not in call, call
        assert call[:2] != ("plugin", "install") and call[:3] != ("plugin", "marketplace", "add")


def test_reapply_baseline_only_says_what_it_would_do_on_a_dry_run_and_skips_a_machine_setup_never_ran_on(
    upgraded_machine, sandbox
):
    from claude_hr import claude_setup, paths

    u = upgraded_machine
    before = paths.claude_settings().read_text(encoding="utf-8")
    u.dry = True
    preview = u.reapply_baseline()
    assert (
        preview.action == "available" and preview.detail == "would re-apply settings and claude-md"
    )
    assert (preview.current, preview.target) == ("0.1.9", claude_setup.running_version())
    assert paths.claude_settings().read_text(encoding="utf-8") == before
    assert claude_setup.applied_version("settings") == "0.1.9"
    u.dry = False
    (updater.paths.state_dir() / "install.json").unlink()
    assert (
        u.reapply_baseline() is None
    )  # not set up (never, or uninstalled): this is not where the baseline is applied
    assert paths.claude_settings().read_text(encoding="utf-8") == before


def test_a_machine_set_up_before_the_per_step_record_waits_for_one_setup(upgraded_machine):
    """0.1.x left install.json and settings-merge.json but no applied-version record: the job cannot know which Claude
    config directory that setup went to, so it leaves everything alone (doctor says to run setup)."""
    from claude_hr import claude_setup, paths

    u = upgraded_machine
    (paths.state_dir() / "applied-version.json").unlink()
    before = paths.claude_settings().read_text(encoding="utf-8")
    assert claude_setup.baseline_is_stale() == (True, None)
    assert u.reapply_baseline() is None
    assert paths.claude_settings().read_text(encoding="utf-8") == before


def test_reapply_baseline_respects_what_setup_skipped(sandbox, monkeypatch):
    """`setup --only claude-md` (settings skipped) must not get its settings baseline from the update job."""
    from claude_hr import claude_setup, ctl, paths

    with monkeypatch.context() as old:
        old.setattr("claude_hr.__version__", "0.1.9")
        assert ctl._run(["setup", "--only", "claude-md"]) == 0
    u = updater.Updater(config.load())
    assert claude_setup.unattended_reapply() == (["claude-md"], "")
    res = u.reapply_baseline()
    assert res.action == "updated" and "claude-md ok" in res.detail and "settings" not in res.detail
    assert not paths.claude_settings().exists()  # still skipped
    assert u.reapply_baseline() is None
    # a machine on which setup skipped everything has no record at all
    (paths.state_dir() / "applied-version.json").unlink()
    assert claude_setup.unattended_reapply() == ([], "")
    assert u.reapply_baseline() is None


def test_the_job_leaves_another_claude_config_dir_alone(upgraded_machine, monkeypatch, sandbox):
    """A scheduled job does not inherit the CLAUDE_CONFIG_DIR exported in the user's shell: it must not write the baseline
    into ~/.claude, nor record the version as applied for the profile the user really runs."""
    from claude_hr import claude_setup, ctl, paths

    u = upgraded_machine
    custom = sandbox / "custom"
    custom.mkdir()
    default = paths.claude_home()
    with monkeypatch.context() as old:  # `setup` was run in a shell with CLAUDE_CONFIG_DIR=custom
        old.setattr("claude_hr.__version__", "0.1.9")
        old.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert ctl._run(["setup", "--only", "settings", "claude-md"]) == 0
    assert (custom / "CLAUDE.md").exists()
    (default / "CLAUDE.md").unlink(missing_ok=True)
    before = (default / "settings.json").read_text(encoding="utf-8")
    # the job: same state dir, no CLAUDE_CONFIG_DIR variable of the user's shell -> the default profile
    assert claude_setup.applied_elsewhere() == str(custom / "settings.json")
    res = u.reapply_baseline()
    assert res.action == "deferred" and "claude-hr-ctl setup" in res.detail, res.detail
    assert str(custom) in res.detail and res.line().startswith("baseline")
    assert not (default / "CLAUDE.md").exists()
    assert (default / "settings.json").read_text(encoding="utf-8") == before
    assert (
        claude_setup.applied_version("settings") == "0.1.9"
    )  # not recorded: the used profile is still stale
    # ... while a run from the shell the user really uses (same config dir as the record) does apply it
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    res = updater.Updater(config.load()).reapply_baseline()
    assert res.action == "updated", res.detail
    assert claude_setup.applied_version("settings") == claude_setup.running_version()
    assert "ENABLE_STOP_REVIEW" in json.loads((custom / "settings.json").read_text("utf-8"))["env"]


def test_reapply_baseline_that_fails_is_reported_retried_and_never_raises(
    upgraded_machine, monkeypatch
):
    from claude_hr import claude_setup, paths

    u = upgraded_machine
    paths.claude_settings().write_text("{ not json", encoding="utf-8")
    res = u.reapply_baseline()
    assert res.action == "failed" and "settings FAILED" in res.detail and "retry" in res.detail
    # claude-md succeeded and is recorded; the failed settings step is not: the next update run tries it again
    assert claude_setup.applied_version("settings") == "0.1.9"
    assert claude_setup.applied_version("claude-md") == claude_setup.running_version()
    assert paths.claude_settings().read_text(encoding="utf-8") == "{ not json"
    paths.claude_settings().write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        claude_setup.ClaudeSetup,
        "sync_claude_md",
        lambda self: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    (paths.state_dir() / "applied-version.json").write_text(
        json.dumps(
            {
                "settings": "0.1.9",
                "claude-md": "0.1.9",
                "config_dir": str(paths.claude_settings()),
            }
        ),
        encoding="utf-8",
    )
    res = u.reapply_baseline()
    assert res.action == "failed" and "RuntimeError" in res.detail and "disk full" in res.detail


def _stub_components(monkeypatch, target, names=("claude", "headroom", "self", "plugins", "proxy")):
    for comp in names:
        monkeypatch.setattr(target, f"update_{comp}", lambda *a, comp=comp: updater.Result(comp))


def test_a_full_update_run_re_applies_the_baseline_after_the_components_and_a_partial_one_does_not(
    upgraded_machine, monkeypatch
):
    from claude_hr import claude_setup

    u = upgraded_machine
    _stub_components(monkeypatch, u)
    rep = u.run(["plugins"])
    assert [r.component for r in rep.results] == [
        "plugins"
    ]  # `--only plugins` is not an upgrade of the config
    rep = u.run()
    assert [r.component for r in rep.results] == [
        "self",
        "claude",
        "headroom",
        "plugins",
        "baseline",
        "proxy",
    ]
    assert rep.results[4].action == "updated" and rep.ok
    rep = (
        u.run()
    )  # applied now: the row stays (the report shows every component) but says nothing was due
    baseline = next(r for r in rep.results if r.component == "baseline")
    assert baseline.action == "none" and baseline.current == claude_setup.running_version()
    log = (updater.paths.logs_dir() / "update.jsonl").read_text(encoding="utf-8")
    assert '"component": "baseline", "from": "0.1.9"' in log


def test_a_failed_baseline_makes_the_update_run_fail_but_does_not_stop_it(
    upgraded_machine, monkeypatch
):
    from claude_hr import paths

    u = upgraded_machine
    paths.claude_settings().write_text("{ not json", encoding="utf-8")
    _stub_components(monkeypatch, u)
    rep = u.run()
    assert [r.component for r in rep.results] == [
        "self",
        "claude",
        "headroom",
        "plugins",
        "baseline",
        "proxy",
    ]  # the step after the failed one still ran
    assert next(r for r in rep.results if r.component == "baseline").action == "failed"
    assert not rep.ok and rep.exit_code() == updater.EXIT_FAILED


def test_the_scheduled_update_job_reapplies_the_baseline_after_the_components(
    upgraded_machine, monkeypatch
):
    from claude_hr import claude_setup, ctl, paths

    _stub_components(monkeypatch, updater.Updater)
    assert ctl._run(["job", "update"]) == 0
    line = (paths.logs_dir() / "job-update.log").read_text(encoding="utf-8")
    assert "rc=0" in line and line.index("plugins") < line.index("baseline")
    assert claude_setup.baseline_is_stale()[0] is False
    # auto_apply = false only reports: nothing may be written
    (paths.state_dir() / "applied-version.json").write_text(
        json.dumps(
            {
                "settings": "0.1.9",
                "claude-md": "0.1.9",
                "config_dir": str(paths.claude_settings()),
            }
        ),
        encoding="utf-8",
    )
    paths.claude_settings().unlink()
    monkeypatch.setenv("CLAUDE_HR_UPDATES__AUTO_APPLY", "false")
    assert ctl._run(["job", "update"]) == 0
    assert not paths.claude_settings().exists()
    # ... and a report-only job whose check FAILED is a failed job, like the applying one (a nightly check that cannot
    # read PyPI, git or claude must not show as a green last result)
    monkeypatch.setattr(
        updater.Updater,
        "update_claude",
        lambda self: updater.Result("claude", action="failed", detail="claude not installed"),
    )
    assert ctl._run(["job", "update"]) == 1
    assert "claude" in (paths.logs_dir() / "job-update.log").read_text(encoding="utf-8")


# ---- `update` is one command: its order, its report, its exit codes ---------------------------------------------------
def _spied(monkeypatch, events, **rows):
    """An Updater whose components only record that they ran (and return the row given for them, else an untouched one)."""
    u = updater.Updater(config.load())
    for comp in ("self", "claude", "headroom", "plugins", "proxy"):

        def fn(comp=comp):
            events.append(comp)
            return rows.get(comp) or updater.Result(comp)

        monkeypatch.setattr(u, f"update_{comp}", fn)
    monkeypatch.setattr(u, "reapply_baseline", lambda: events.append("baseline"))
    return u


def _patch_class(monkeypatch, rows=None):
    """Every Updater of this test (the ones ctl creates too) answers with `rows[component]` (else an untouched row)."""
    rows = rows or {}
    for comp in ("self", "claude", "headroom", "plugins", "proxy"):
        monkeypatch.setattr(
            updater.Updater,
            f"update_{comp}",
            lambda self, comp=comp: rows.get(comp) or updater.Result(comp),
        )
    monkeypatch.setattr(updater.Updater, "reapply_baseline", lambda self: rows.get("baseline"))


def test_a_full_run_goes_self_claude_headroom_plugins_then_baseline_and_proxy(sandbox, monkeypatch):
    events = []
    rep = _spied(monkeypatch, events).run()
    assert events == ["self", "claude", "headroom", "plugins", "baseline", "proxy"]
    assert [r.component for r in rep.results] == events


def test_only_means_just_these_and_keeps_the_run_order(sandbox, monkeypatch):
    events = []
    u = _spied(monkeypatch, events)
    u.run(["proxy", "claude", "self"])
    assert events == ["self", "claude", "proxy"]
    events.clear()
    u.run(["baseline"])
    assert events == ["baseline"]


def test_the_report_of_a_machine_that_is_up_to_date(sandbox, monkeypatch, capsys):
    from claude_hr import claude_setup, ctl, paths

    (paths.state_dir()).mkdir(parents=True, exist_ok=True)
    (paths.state_dir() / "install.json").write_text(
        "{}", encoding="utf-8"
    )  # a machine `setup` ran on
    rows = {
        "self": updater.Result("self", "v0.2.3", "v0.2.3"),
        "claude": updater.Result("claude", "2.1.302", "2.1.302"),
        "headroom": updater.Result("headroom", "0.41.0", "0.41.0"),
    }
    _patch_class(monkeypatch, rows)
    assert ctl._run(["update"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["component", "before", "->", "after", "result"]
    table = [" ".join(ln.split()) for ln in lines[2:8]]
    assert table == [
        "self v0.2.3 up to date",
        "claude 2.1.302 up to date",
        "headroom 0.41.0 up to date",
        "plugins - up to date",
        f"baseline {claude_setup.running_version()} up to date",
        "proxy - up to date",
    ]
    assert lines[-2:] == ["Everything is up to date.", "doctor: no warnings, no failures"]
    assert "Pending" not in "\n".join(lines)
    assert lines[-3] == ""  # the verdict stands apart from the table


def test_the_report_lists_what_is_pending_and_why_and_names_the_way_out_of_a_deferral(
    sandbox, monkeypatch, capsys
):
    from claude_hr import ctl

    waiting = updater.Result(
        "claude",
        "2.1.301",
        "2.1.302",
        action="deferred",
        detail="2 claude-hr session(s) running",
        busy=True,
    )
    _patch_class(monkeypatch, {"claude": waiting})
    assert ctl._run(["update"]) == 75
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert next(ln for ln in lines if ln.startswith("claude ")).split()[1:] == [
        "2.1.301",
        "->",
        "2.1.302",
        "deferred",
    ]
    assert "Pending:" in lines and "  claude: 2 claude-hr session(s) running" in lines
    assert "Everything is up to date." not in text
    assert (
        "Nothing is broken. Close the claude-hr sessions and run `claude-hr update` again" in text
    )
    assert "--force" in text


@pytest.mark.parametrize(
    "rows,code",
    [
        ({}, 0),
        ({"claude": updater.Result("claude", "2.1.1", "2.1.2", action="updated")}, 0),
        ({"claude": updater.Result("claude", action="failed", detail="boom")}, 1),
        ({"headroom": updater.Result("headroom", action="rolled-back", detail="smoke")}, 1),
        (
            {
                "claude": updater.Result(
                    "claude", action="deferred", detail="1 claude-hr session(s) running", busy=True
                )
            },
            75,
        ),
        # deferred for another reason (the baseline went to another Claude config directory): sessions are not why, so 0
        ({"baseline": updater.Result("baseline", action="deferred", detail="elsewhere")}, 0),
        (
            {
                "claude": updater.Result("claude", action="deferred", busy=True, detail="x"),
                "headroom": updater.Result("headroom", action="failed", detail="boom"),
            },
            1,
        ),
    ],
)
def test_the_update_command_exits_0_ok_1_failed_75_deferred(sandbox, monkeypatch, rows, code):
    from claude_hr import ctl

    _patch_class(monkeypatch, rows)
    assert ctl._run(["update"]) == code


def test_a_usage_error_exits_2_and_a_preview_exits_0_unless_a_component_could_not_be_checked(
    sandbox, monkeypatch, capsys
):
    from claude_hr import ctl, paths

    assert ctl.update_main(["--bogus"]) == 2
    assert ctl.update_main(["--only", "bogus"]) == 2
    assert ctl.update_main(["--post", "--check"]) == 2  # the helper's half takes no preview
    with pytest.raises(SystemExit) as exc:
        ctl._run(["update", "--bogus"])
    assert exc.value.code == 2
    capsys.readouterr()
    available = updater.Result("claude", "1", "2", action="available", detail="Claude Code 1 -> 2")
    _patch_class(monkeypatch, {"claude": available})
    assert ctl._run(["update", "--check"]) == 0  # something to apply is no failure ...
    out = capsys.readouterr().out
    assert "Available:" in out and "Run `claude-hr update` to apply it." in out
    _patch_class(
        monkeypatch, {"claude": updater.Result("claude", action="failed", detail="not installed")}
    )
    assert ctl._run(["update", "--check"]) == 1  # ... something that could not be checked is
    out = capsys.readouterr().out
    assert "Could not check:" in out and "  claude: not installed" in out
    assert "Available:" not in out and "Run `claude-hr update`" not in out
    # ... and a preview writes nothing of its own: no log line, no record of a run
    assert not (paths.logs_dir() / "update.jsonl").exists()
    assert not (paths.state_dir() / "update-last.json").exists()
    assert not (paths.state_dir() / "update-state.json").exists()


def test_the_scheduled_job_does_not_call_a_deferral_a_failure(sandbox, monkeypatch):
    from claude_hr import ctl, paths

    waiting = updater.Result("claude", action="deferred", detail="1 session", busy=True)
    _patch_class(monkeypatch, {"claude": waiting})
    assert ctl._run(["update"]) == 75  # the command says "deferred" ...
    assert ctl._run(["job", "update"]) == 0  # ... the scheduler hears "fine"
    assert " rc=0 " in (paths.logs_dir() / "job-update.log").read_text(encoding="utf-8")
    _patch_class(monkeypatch, {"claude": updater.Result("claude", action="failed", detail="boom")})
    assert ctl._run(["job", "update"]) == 1


def test_update_claude_only_runs_claude_codes_own_updater(sandbox, monkeypatch):
    from claude_hr import ctl

    seen = []
    monkeypatch.setattr(ctl.procutil, "find_claude", lambda *a, **k: Path("/fake/claude"))
    monkeypatch.setattr(ctl.subprocess, "call", lambda argv, *a, **k: seen.append(argv) or 7)
    monkeypatch.setattr(
        updater.Updater, "run", lambda *a, **k: pytest.fail("claude-hr's own update must not run")
    )
    assert ctl._run(["update", "--claude-only"]) == 7
    assert seen == [[str(Path("/fake/claude")), "update"]]


def test_an_update_run_from_a_claude_code_shell_works_but_never_installs_a_plugin(
    upgraded_machine, monkeypatch, capsys
):
    """It is non-interactive and asks nobody anything; installing a missing profile plugin is `plugins sync`, never this."""
    from claude_hr import ctl, human

    monkeypatch.setenv("CLAUDECODE", "1")
    assert human.not_a_person()
    u = upgraded_machine
    for comp in ("self", "claude", "headroom", "proxy"):
        monkeypatch.setattr(
            updater.Updater, f"update_{comp}", lambda self, c=comp: updater.Result(c)
        )
    assert ctl._run(["update"]) == 0
    assert "Everything is up to date." in capsys.readouterr().out
    assert u.calls, "the plugin step should have asked the fake claude something"
    for call in u.calls:
        assert "-y" not in call and "--yes" not in call
        assert call[:2] != ("plugin", "install") and call[:3] != ("plugin", "marketplace", "add")


# ---- self-update: the post phase is chained behind the reinstall ------------------------------------------------------
POST = [r"C:\Program Files\tool env\python.exe", "-m", "claude_hr.ctl", "update", "--post"]


def test_the_windows_helper_runs_the_post_phase_only_after_a_reinstall_that_exited_with_0():
    script = updater._windows_reinstall_script(
        ["uv", "tool", "install"], 1, 2, "log.txt", env={}, post=POST
    )
    lines = script.splitlines()
    rc = lines.index("  $rc = $LASTEXITCODE")
    guard = lines.index("if ($rc -eq 0) {")
    post = next(i for i, ln in enumerate(lines) if ln.startswith("    & ") and "'--post'" in ln)
    assert next(i for i, ln in enumerate(lines) if "reinstall exit code" in ln) > rc
    assert rc < guard < post < next(i for i, ln in enumerate(lines) if ln.startswith("} else {"))
    # quoted as one literal per argument (a space in the path), and its output goes to the same log
    assert lines[post].startswith(
        "    & 'C:\\Program Files\\tool env\\python.exe' '-m' 'claude_hr.ctl' 'update' '--post' 2>&1 | ForEach-Object"
    )
    # the reinstall's exit code must not pass for the post command's: it is cleared first, and a post command that could
    # not be started is caught and logged
    assert lines[post - 2] == "  $global:LASTEXITCODE = $null" and lines[post - 1] == "  try {"
    assert any("could not start the post phase" in ln for ln in lines[post:])
    assert any("$null -eq $LASTEXITCODE" in ln for ln in lines[post:])
    assert "Add-Content -LiteralPath $log" in lines[post]
    assert "the reinstall did not succeed: the post phase was not run" in script
    # without a post phase the script is what it always was
    plain = updater._windows_reinstall_script(["uv", "tool", "install"], 1, 2, "log.txt", env={})
    assert "--post" not in plain and "if ($rc -eq 0)" not in plain
    # ... except that a reinstall that did not happen always clears the retry mark, with or without a post phase
    assert "if ($rc -ne 0) { Remove-Item" in plain and "if ($rc -ne 0) { Remove-Item" in script


def test_the_post_phase_of_the_helper_works_on_the_profile_the_update_resolved():
    env = {"CLAUDE_CONFIG_DIR": r"D:\Tim's profile", "CLAUDE_HR_HOME": r"D:\hr", "UNRELATED": "x"}
    lines = updater._windows_reinstall_script(
        ["uv"], 1, 2, "log.txt", env=env, post=POST
    ).splitlines()
    assert r"$env:CLAUDE_CONFIG_DIR = 'D:\Tim''s profile'" in lines
    assert r"$env:CLAUDE_HR_HOME = 'D:\hr'" in lines
    assert not any("UNRELATED" in ln for ln in lines)
    first_command = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("& "))
    assert max(i for i, ln in enumerate(lines) if ln.startswith("$env:")) < first_command


def test_the_post_phase_script_parses_with_the_real_powershell_parser(tmp_path):
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    if shell is None:
        pytest.skip("needs PowerShell")
    script = updater._windows_reinstall_script(
        ["C:\\Users\\Tim\u2019s\\uv.exe", "tool", "install"],
        1,
        2,
        "C:\\l\u2018og\\x.log",
        env={"CLAUDE_CONFIG_DIR": "C:\\a $(calc) b"},
        post=["C:\\Users\\Tim\u2019s\\py thon.exe", "-m", "claude_hr.ctl", "update", "--post"],
    )
    ps1 = tmp_path / "helper.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))
    check = (
        "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{ps1}', [ref]$t, [ref]$e); if ($e.Count -eq 0) {{ 'PARSE_OK' }} else {{ $e | ForEach-Object {{ $_.Message }} }}"
    )
    out = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "PARSE_OK" in out, out


def test_the_posix_wrapper_quotes_every_argument_and_runs_post_second():
    cmd = ["/opt/my tools/uv", "tool", "install", "/home/o'neil/${HOME}/$$/.claude-hr/repo"]
    post = ["/usr/bin/python 3", "-m", "claude_hr.ctl", "update", "--post"]
    chained = updater._posix_chain(cmd, post)
    assert chained[:2] == ["/bin/sh", "-c"] and len(chained) == 3
    script = chained[2]
    assert shlex.join(cmd) in script and shlex.join(post) in script
    assert script.index(shlex.join(cmd)) < script.index(shlex.join(post))
    # it says how each part ended and leaves with the code of the one that failed (the unit must not look clean)
    assert 'exit "$rc"' in script and script.rstrip().endswith("the post phase finished'")
    assert script.index("the post phase finished") > script.index("the post phase did not finish")


def test_systemd_gets_every_dollar_doubled_and_every_percent_left_alone():
    """systemd expands $VAR and ${VAR} in the words of a unit's command line and turns $$ into $; in what systemd-run passes
    to a transient unit % is not special (checked on systemd 249: %h and %% reached the program unchanged), so it must not
    be doubled."""
    words = ["/bin/sh", "-c", 'x=$?; echo "$x" ${HOME} $$ 100%h %%', "/p/${HOME}/a$b"]
    assert updater._systemd_words(words) == [
        "/bin/sh",
        "-c",
        'x=$$?; echo "$$x" $${HOME} $$$$ 100%h %%',
        "/p/$${HOME}/a$$b",
    ]
    assert updater._systemd_words(["plain"]) == ["plain"]


@pytest.mark.skipif(os.name == "nt" or not os.path.exists("/bin/sh"), reason="needs a POSIX sh")
def test_the_posix_wrapper_really_runs_post_only_when_the_reinstall_succeeded(tmp_path):
    work = tmp_path / "o'neil dir"
    work.mkdir()
    out = work / "out.txt"

    def run(code, post_code=0):
        out.unlink(missing_ok=True)
        reinstall = [
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'a').write('reinstall ' + sys.argv[2] + chr(10)); sys.exit(int(sys.argv[2]))",
            str(out),
            str(code),
        ]
        post = [
            sys.executable,
            "-c",
            "import sys; open(sys.argv[1], 'a').write('post' + chr(10)); sys.exit(int(sys.argv[2]))",
            str(out),
            str(post_code),
        ]
        proc = subprocess.run(updater._posix_chain(reinstall, post), capture_output=True, text=True)
        ran = out.read_text(encoding="utf-8").split(chr(10))[:-1]
        return proc.returncode, ran, proc.stdout

    rc, ran, said = run(0)
    assert (rc, ran) == (0, ["reinstall 0", "post"]) and "the post phase finished" in said
    rc, ran, said = run(3)
    assert rc == 3 and ran == [
        "reinstall 3"
    ]  # the post phase never ran, and the unit reports the failure
    assert "the reinstall did not succeed (exit code 3)" in said and "finished" not in said
    # a post phase that failed or deferred (75) is not a clean run: not "finished", not exit 0
    for code in (1, 75):
        rc, ran, said = run(0, code)
        assert rc == code and ran == ["reinstall 0", "post"]
        assert f"the post phase did not finish (exit code {code})" in said
        assert "the post phase finished" not in said


def test_the_reinstall_carries_the_post_phase_on_both_posix_routes_and_in_the_windows_script(
    sandbox, monkeypatch
):
    started, spawned = [], []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    cwds = []
    monkeypatch.setattr(
        updater,
        "_start_systemd_service",
        lambda cmd, log, cwd=None: started.append(cmd) or cwds.append(cwd) or True,
    )
    monkeypatch.setattr(
        updater,
        "_spawn_detached",
        lambda cmd, log=None, cwd=None: spawned.append(cmd) or cwds.append(cwd),
    )
    u = updater.Updater(config.load())
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall(post=POST)
    assert started == [updater._posix_chain(["uv", "tool", "install"], POST)] and spawned == []
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log, cwd=None: False)
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall(post=POST)
    assert spawned == [updater._posix_chain(["uv", "tool", "install"], POST)]
    # both routes start in the claude-hr state directory, never in the directory `update` was typed in
    assert cwds == [str(updater.paths.state_dir())] * 2
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall()  # no post phase asked for: the reinstall alone, as before
    assert spawned[-1] == ["uv", "tool", "install"]
    # Windows: the scheduled-task helper's script
    scripts = []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(updater.procutil, "which_all", lambda name: [r"C:\Windows\powershell.exe"])
    monkeypatch.setattr(
        updater,
        "_start_helper_task",
        lambda shell, ps1: scripts.append(ps1.read_text("utf-8-sig")) or True,
    )
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall(post=POST)
    assert "'--post'" in scripts[0] and scripts[0].index("$rc -eq 0") < scripts[0].index("'--post'")
    assert f"Set-Location -LiteralPath '{updater.paths.state_dir()}'" in scripts[0].splitlines()


def test_the_post_command_is_the_console_interpreter_of_this_env_and_carries_the_flags(
    sandbox, monkeypatch
):
    cfg = config.load()
    cmd = updater.Updater(cfg)._post_command()
    # -I: the working directory and the PYTHON* variables stay out of the import path (a claude_hr, json or types in the
    # directory `update` was typed in must not be what the post phase imports)
    assert cmd[1:] == ["-I", "-m", "claude_hr.ctl", "update", "--post"]
    cmd = updater.Updater(cfg, restart=False, force=True)._post_command()
    assert cmd[-2:] == ["--no-restart", "--force"]
    # the scheduled jobs run under pythonw.exe, whose output would vanish: the helper gets python.exe instead
    exe_dir = Path(sandbox) / "env"
    exe_dir.mkdir()
    (exe_dir / "python.exe").write_bytes(b"")
    monkeypatch.setattr(updater.sys, "executable", str(exe_dir / "pythonw.exe"))
    assert updater._console_python() == str(exe_dir / "python.exe")


def test_a_queued_self_update_leaves_the_post_steps_to_the_helper_and_says_so(sandbox, monkeypatch):
    from claude_hr import paths

    events, launched = [], []
    u = _spied(monkeypatch, events)

    def queue():
        events.append("self")
        u._pending_reinstall = ["uv", "tool", "install"]
        return updater.Result("self", "v0.2.2", "v0.2.3", action="updated")

    monkeypatch.setattr(u, "update_self", queue)
    monkeypatch.setattr(u, "_launch_reinstall", lambda post=None: launched.append(post))
    rep = u.run()
    assert events == [
        "self",
        "claude",
        "headroom",
        "plugins",
    ]  # baseline and proxy need the NEW code
    assert launched and launched[0][-3:] == ["claude_hr.ctl", "update", "--post"]
    rec = updater.read_last()
    assert rec["phase"] == "pending" and rec["post"] == {
        "steps": ["baseline", "proxy"],
        "doctor": True,
    }
    assert [r["component"] for r in rec["results"]] == ["self", "claude", "headroom", "plugins"]
    log = str(paths.logs_dir() / "self-update.log")
    assert rep.background == log
    text = "\n".join(updater.format_report(rep))
    assert (
        f"claude-hr itself is updating in the background; the rest finishes there: see {log}; "
        "claude-hr-ctl status shows the result" in text
    )
    assert "Everything is up to date." not in text
    row = next(ln for ln in text.splitlines() if ln.startswith("self "))
    assert "updated, finishing in the background" in row
    # `--only self` still chains the helper (to finish the record) with no post steps and no doctor run
    events.clear()
    u.run(["self"])
    assert updater.read_last()["post"] == {"steps": [], "doctor": False}


def test_the_post_phase_continues_the_run_that_queued_the_reinstall(sandbox, monkeypatch):
    from claude_hr import __version__

    events, launched = [], []
    u = _spied(monkeypatch, events)
    monkeypatch.setattr(
        u,
        "update_self",
        lambda: (
            setattr(u, "_pending_reinstall", ["uv"])
            or updater.Result("self", "v0.2.2", "v0.2.3", action="updated")
        ),
    )
    monkeypatch.setattr(u, "_launch_reinstall", lambda post=None: launched.append(post))
    u.run()
    # ... in the new process, after the reinstall: `update --post`
    from claude_hr import doctor

    monkeypatch.setattr(
        doctor,
        "run",
        lambda cfg, cwd, deep=False: [
            Check("a", INFO, "fine"),
            Check("b", WARN, "slow"),
            Check("c", FAIL, "broken"),
        ],
    )
    events2 = []
    u2 = _spied(monkeypatch, events2)
    rep = u2.run_post()
    assert events2 == ["baseline", "proxy"]  # exactly the post steps, nothing of phase A again
    assert [r.component for r in rep.results] == [
        "self",
        "claude",
        "headroom",
        "plugins",
        "baseline",
        "proxy",
    ]
    assert rep.results[0].detail == f"reinstalled: claude-hr {__version__} is running"
    rec = updater.read_last()
    assert rec["phase"] == "complete" and rec["versions"]["claude-hr"] == __version__
    assert rec["doctor"] == {"warn": 1, "fail": 1, "first": ["FAIL c: broken", "WARN b: slow"]}
    assert [r["component"] for r in rec["results"]][-2:] == ["baseline", "proxy"]
    assert "doctor: 1 warning(s), 1 failure(s)" in "\n".join(updater.format_report(rep))
    assert "post" not in rec
    assert "claude-hr itself is updating" not in "\n".join(updater.format_report(rep))


def test_update_post_runs_the_post_steps_through_the_command_and_survives_a_missing_record(
    sandbox, monkeypatch, capsys
):
    from claude_hr import ctl

    events = []
    monkeypatch.setattr(updater.Updater, "reapply_baseline", lambda self: events.append("baseline"))
    monkeypatch.setattr(
        updater.Updater,
        "update_proxy",
        lambda self: events.append("proxy") or updater.Result("proxy"),
    )
    assert ctl._run(["update", "--post"]) == 0
    assert events == ["baseline", "proxy"]  # no record of a pending run: the full post phase
    rec = updater.read_last()
    assert rec["phase"] == "complete" and rec["doctor"] == {"warn": 0, "fail": 0, "first": []}
    assert "doctor: no warnings, no failures" in capsys.readouterr().out
    # a record of a run that never finished long ago is not continued
    rec.update(
        phase="pending",
        ts=1.0,
        post={"steps": [], "doctor": False},
        results=[{"component": "self"}],
    )
    updater.write_json(updater.paths.state_dir() / "update-last.json", rec)
    events.clear()
    assert ctl._run(["update", "--post"]) == 0
    assert events == ["baseline", "proxy"]
    # ... nor one whose time somebody edited into something that is not a number
    rec.update(phase="pending", ts="yesterday", post={"steps": [], "doctor": False})
    updater.write_json(updater.paths.state_dir() / "update-last.json", rec)
    events.clear()
    assert ctl._run(["update", "--post"]) == 0
    assert events == ["baseline", "proxy"]


def test_status_reads_the_record_of_the_last_run(sandbox, monkeypatch):
    import claude_hr

    events = []
    u = _spied(monkeypatch, events)
    assert updater.last_update_summary() is None
    u.run()
    line = updater.last_update_summary()
    assert line and "nothing to update" in line and "doctor 0 warning(s), 0 failure(s)" in line
    u = _spied(
        monkeypatch,
        events,
        claude=updater.Result("claude", "2.1.1", "2.1.2", action="updated"),
        headroom=updater.Result(
            "headroom", "0.41.0", "0.40.0", action="none"
        ),  # held: stays on 0.41.0
    )
    u.run()
    assert "updated claude 2.1.1 -> 2.1.2" in updater.last_update_summary()
    versions = updater.read_last()["versions"]
    assert versions == {"claude-hr": claude_hr.__version__, "claude": "2.1.2", "headroom": "0.41.0"}
    # a pending record: still going, then given up on
    rec = updater.read_last()
    rec.update(phase="pending", ts=__import__("time").time())
    updater.write_json(updater.paths.state_dir() / "update-last.json", rec)
    assert "updating in the background" in updater.last_update_summary()
    rec["ts"] = 1.0
    updater.write_json(updater.paths.state_dir() / "update-last.json", rec)
    assert "did not finish" in updater.last_update_summary()


# ---- the proxy step ----------------------------------------------------------------------------------------------------
class _ProxyWorld:
    state = "changed"
    running = True
    unowned = ""  # why the proxy is not one claude-hr started ("" = it is: Updater._unowned_proxy has its own tests)

    def __init__(self):
        self.events = []


@pytest.fixture
def proxy(sandbox, monkeypatch):
    """A Headroom whose proxy runs with the settings state a test sets; stop and ensure only record what was asked."""
    from claude_hr.headroom import Headroom, Health

    w = _ProxyWorld()
    monkeypatch.setattr(Headroom, "binary", lambda self: Path("/fake/headroom"))
    monkeypatch.setattr(Headroom, "settings_state", lambda self: w.state)
    monkeypatch.setattr(
        Headroom,
        "health_patient",
        lambda self, patience_s=4.0: Health(
            w.running, "healthy" if w.running else "down", "0.41.0"
        ),
    )
    monkeypatch.setattr(Headroom, "stop", lambda self: w.events.append("stop") or True)
    monkeypatch.setattr(
        Headroom,
        "ensure",
        lambda self, wait_s=0.0, announce=None: (
            w.events.append(f"ensure:{wait_s:g}") or Health(True, "healthy", "0.41.0")
        ),
    )
    monkeypatch.setattr(updater.Updater, "_unowned_proxy", lambda self: w.unowned)
    return w


def test_the_proxy_is_restarted_when_it_runs_stale_settings_and_no_session_runs(proxy):
    res = updater.Updater(config.load()).update_proxy()
    assert res.action == "updated" and "restarted" in res.detail
    assert "other settings than the configuration asks for now" in res.detail
    assert proxy.events == [
        "stop",
        "ensure:90",
    ]  # waits for it: a restart that never came up must be seen
    proxy.state, proxy.events[:] = "older", []
    res = updater.Updater(config.load()).update_proxy()
    assert res.action == "updated" and "older claude-hr" in res.detail


@pytest.mark.parametrize("state", ["current", "unknown"])
def test_a_proxy_with_current_or_unrecorded_settings_is_left_alone(proxy, state):
    proxy.state = state
    res = updater.Updater(config.load()).update_proxy()
    assert (
        res.action == "none" and proxy.events == []
    )  # a proxy somebody else started is not judged


def test_the_proxy_waits_while_a_claude_hr_session_runs_unless_forced(proxy):
    resources.register_session(os.getpid(), "/x")
    try:
        res = updater.Updater(config.load()).update_proxy()
        assert (
            res.action == "deferred" and res.busy and "1 claude-hr session(s) running" in res.detail
        )
        assert proxy.events == []
        assert updater.Report(results=[res]).exit_code() == updater.EXIT_DEFERRED
        res = updater.Updater(config.load(), force=True).update_proxy()
        assert res.action == "updated" and proxy.events == ["stop", "ensure:90"]
    finally:
        resources.unregister_session(os.getpid())


def test_no_restart_keeps_the_proxy_running_and_says_what_is_left(proxy):
    res = updater.Updater(config.load(), restart=False).update_proxy()
    assert res.action == "deferred" and not res.busy and "--no-restart" in res.detail
    assert proxy.events == []
    rep = updater.Report(results=[res])
    assert rep.exit_code() == 0 and "needs a restart" in "\n".join(updater.format_report(rep))


def test_a_proxy_that_is_not_running_or_a_preview_is_never_restarted(proxy):
    proxy.running = False
    res = updater.Updater(config.load()).update_proxy()
    assert res.action == "none" and "not running" in res.detail and proxy.events == []
    proxy.running = True
    res = updater.Updater(config.load(), dry=True).update_proxy()
    assert res.action == "available" and res.detail.startswith("would restart: ")
    assert proxy.events == []


def test_a_headroom_upgrade_counts_unless_that_run_already_restarted_the_proxy(proxy):
    proxy.state = "current"
    u = updater.Updater(config.load())
    u.headroom_upgraded = True
    res = u.update_proxy()
    assert res.action == "updated" and "Headroom was upgraded" in res.detail
    u = updater.Updater(config.load())
    u.headroom_upgraded = u.proxy_restarted = True
    proxy.events.clear()
    res = u.update_proxy()
    assert res.action == "none" and proxy.events == []


def test_update_headroom_remembers_that_it_upgraded_and_restarted_the_proxy(sandbox, monkeypatch):
    from claude_hr.headroom import Headroom

    state = {"v": "0.40.0"}
    _patch_headroom(monkeypatch, state)
    monkeypatch.setattr(
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: "0.40.1"
    )

    def fake_install(self, version=None, upgrade=True, exclude_newer=None):
        state["v"] = version
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(Headroom, "install", fake_install)
    monkeypatch.setattr(updater.Updater, "smoke_headroom", lambda self, wait_s=150.0: True)
    u = updater.Updater(config.load())
    assert u.update_headroom().action == "updated"
    assert u.headroom_upgraded and u.proxy_restarted


def test_a_disabled_headroom_has_no_proxy_step(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    res = updater.Updater(config.load()).update_proxy()
    assert res.action == "none" and res.state == "skipped"


# ---- channel, versions and reasons in the report ----------------------------------------------------------------------
def test_a_newer_install_than_the_channel_offers_names_the_channel_and_the_way_to_follow_the_newest(
    sandbox, monkeypatch
):
    u = make(sandbox, monkeypatch, current="2.1.301", target="2.1.292")
    res = u.update_claude()
    assert res.action == "none" and res.state == "held"
    assert res.detail == (
        "installed 2.1.301 is newer than channel stable (2.1.292): not downgrading; "
        'set claude.channel = "latest" to follow the newest'
    )
    monkeypatch.setenv(
        "CLAUDE_HR_CLAUDE__CHANNEL", "latest"
    )  # on latest there is nothing to switch to
    res = make(sandbox, monkeypatch, current="2.1.301", target="2.1.292").update_claude()
    assert res.detail == "installed 2.1.301 is newer than channel latest (2.1.292): not downgrading"


def test_a_preview_names_the_channel_what_it_offers_and_what_would_hold_it_up(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__CHANNEL", "latest")
    u = make(sandbox, monkeypatch, current="2.1.301", target="2.1.302", dry=True)
    res = u.update_claude()
    assert res.action == "available"
    assert res.detail == "Claude Code 2.1.301 -> channel latest offers 2.1.302"
    assert u.header == ['Claude Code channel: latest (claude.channel = "latest")']
    resources.register_session(os.getpid(), "/x")
    try:
        res = make(
            sandbox, monkeypatch, current="2.1.301", target="2.1.302", dry=True
        ).update_claude()
    finally:
        resources.unregister_session(os.getpid())
    assert res.detail.endswith("; it would wait now: 1 claude-hr session(s) running")


def test_a_pinned_claude_is_reported_as_pinned_not_as_a_channel(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__PIN", "2.1.292")
    u = make(sandbox, monkeypatch, current="2.1.301", target="2.1.292", dry=True)
    res = u.update_claude()
    assert res.detail == "Claude Code 2.1.301 -> claude.pin holds 2.1.292"
    assert u.header == ["Claude Code is pinned to 2.1.292 (claude.pin)"]


@pytest.mark.parametrize(
    "settings,configured,channel",
    [
        (None, None, "stable"),  # auto, nothing set: stable
        ({"autoUpdatesChannel": "latest"}, None, "latest"),  # auto follows the user's own setting
        (
            {"autoUpdatesChannel": "latest"},
            "stable",
            "stable",
        ),  # an explicit channel is used as written
        ({"autoUpdatesChannel": "beta"}, None, "stable"),  # not a channel claude-hr knows
        ({"autoUpdatesChannel": "stable"}, "latest", "latest"),
    ],
)
def test_claude_target_reads_the_release_file_of_the_channel_in_force(
    sandbox, monkeypatch, settings, configured, channel
):
    import urllib.request

    from claude_hr import paths

    if settings is not None:
        paths.claude_settings().write_text(json.dumps(settings), encoding="utf-8")
    if configured:
        monkeypatch.setenv("CLAUDE_HR_CLAUDE__CHANNEL", configured)
    urls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"2.1.302\n"

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=0: urls.append(url) or Resp()
    )
    assert updater.Updater(config.load()).claude_target() == "2.1.302"
    assert urls == [f"{updater.CLAUDE_RELEASES}/{channel}"]


def _pypi(monkeypatch, releases):
    monkeypatch.setattr(
        updater.procutil,
        "http_json",
        lambda *a, **k: (200, {"releases": releases, "info": {"version": "0.41.0"}}),
    )


def test_a_newer_headroom_than_the_cooldown_allows_says_so_and_when_the_cooldown_ends(
    sandbox, monkeypatch
):
    from claude_hr.headroom import Headroom

    now = __import__("time").time()
    _pypi(monkeypatch, {"0.40.0": rel(8, now), "0.41.0": rel(2.5, now)})
    _patch_headroom(monkeypatch, {"v": "0.41.0"})
    monkeypatch.setattr(
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: "0.40.0"
    )
    res = updater.Updater(config.load()).update_headroom()
    assert res.action == "none" and res.state == "held"
    assert res.detail.startswith(
        "installed 0.41.0 is newer than the cooldown-eligible 0.40.0: not downgrading (0.41.0 came out "
    )
    assert "; the 5-day cooldown ends " in res.detail
    # a preview of an up-to-date Headroom still says what is held back by the cooldown
    _pypi(monkeypatch, {"0.40.0": rel(8, now), "0.42.0": rel(1, now)})
    _patch_headroom(monkeypatch, {"v": "0.40.0"})
    res = updater.Updater(config.load(), dry=True).update_headroom()
    assert res.action == "none" and res.detail.startswith("0.42.0 came out ")
    assert "the 5-day cooldown ends " in res.detail
    # ... and says nothing once the cooldown is over
    _pypi(monkeypatch, {"0.40.0": rel(8, now), "0.42.0": rel(6, now)})
    assert updater.Updater(config.load(), dry=True).update_headroom().detail == ""


def test_a_headroom_preview_names_the_release_it_would_install(sandbox, monkeypatch):
    from claude_hr.headroom import Headroom

    _patch_headroom(monkeypatch, {"v": "0.40.0"})
    monkeypatch.setattr(
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: "0.40.1"
    )
    res = updater.Updater(config.load(), dry=True).update_headroom()
    assert res.action == "available"
    assert res.detail == "Headroom 0.40.0 -> 0.40.1 (newest release past the cooldown)"


# ---- the plugins step -------------------------------------------------------------------------------------------------
INSTALLED = [
    {"id": "a@m", "scope": "user", "version": "1.0.0"},
    {"id": "b@m", "scope": "user", "version": "2.0.0"},
    {"id": "c@m", "scope": "project", "version": "0.1.0"},
    {"id": "d@m", "scope": "user", "version": "ea10b213686c"},
    {"id": "e@m", "scope": "user", "version": "6.4.2-8ca22dba9a94"},
]


def _fake_plugin_cli(
    monkeypatch, installed=None, update_rc=0, refresh_rc=0, list_rc=0, list_out=None
):
    """A fake `claude plugin ...`; every call is recorded. `installed` is what `plugin list --json` answers."""
    from claude_hr import claude_setup

    calls = []

    def fake(self, *args, timeout=120):
        calls.append(args)
        if args[:3] == ("plugin", "list", "--json"):
            body = json.dumps(installed or []) if list_out is None else list_out
            return subprocess.CompletedProcess(args, list_rc, body, "list broke" if list_rc else "")
        if args[:2] == ("plugin", "update"):
            return subprocess.CompletedProcess(
                args, update_rc, "", "needs confirmation" if update_rc else ""
            )
        if args[:3] == ("plugin", "marketplace", "update"):
            return subprocess.CompletedProcess(
                args,
                refresh_rc,
                "refreshed" if not refresh_rc else "",
                "getaddrinfo ENOTFOUND github.com" if refresh_rc else "",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(claude_setup.ClaudeSetup, "_claude", fake)
    return calls


def _plugins(monkeypatch, **kw):
    dry = kw.pop("dry", False)
    calls = _fake_plugin_cli(monkeypatch, **kw)
    u = updater.Updater(config.load(), dry=dry)
    u.claude = Path("/fake/claude")
    return u, calls


def test_the_plugin_preview_says_it_cannot_preview_and_lists_what_is_installed(
    sandbox, monkeypatch
):
    """`claude plugin list --available` names what can be installed, never what is newer, and it refreshes claude's
    catalog cache: the preview must neither ask it nor claim "up to date" from it."""
    u, calls = _plugins(monkeypatch, installed=INSTALLED, dry=True)
    res = u.update_plugins()
    assert res.action == "none" and res.state == "not previewed" and not res.pending
    assert res.detail == (
        "not previewed (the update asks Claude Code for each plugin); installed, user scope: "
        "a@m 1.0.0, b@m 2.0.0, d@m ea10b213686c, e@m 6.4.2-8ca22dba9a94"
    )
    assert calls == [("plugin", "list", "--json")]  # no --available, no refresh, no update
    text = "\n".join(updater.format_report(updater.Report(results=[res], check=True)))
    assert "  plugins: not previewed (the update asks Claude Code for each plugin)" in text
    assert "up to date" not in text  # not on the row, not in the closing line


def test_the_plugin_preview_survives_a_claude_that_cannot_list(sandbox, monkeypatch):
    u, _ = _plugins(monkeypatch, installed=INSTALLED, list_rc=1, dry=True)
    res = u.update_plugins()
    assert res.state == "not previewed" and res.action == "none"
    assert res.detail == "not previewed (the update asks Claude Code for each plugin)"


def test_a_plugin_that_wants_a_confirmation_is_reported_for_the_person_not_answered(
    sandbox, monkeypatch
):
    u, calls = _plugins(monkeypatch, installed=INSTALLED[:1], update_rc=1)
    res = u.update_plugins()
    assert ("plugin", "update", "a@m", "--scope", "user") in calls
    assert all("-y" not in c and "--yes" not in c for c in calls)
    assert res.detail == "nothing was updated"
    assert "run `claude plugin update a@m --scope user` yourself" in res.pending
    rep = updater.Report(results=[res])
    text = "\n".join(updater.format_report(rep))
    assert "Pending:" in text and "  plugins: a@m was not updated (needs confirmation)" in text
    assert "Everything is up to date." not in text
    assert rep.exit_code() == 0  # asking the person is not a failure
    row = next(ln for ln in text.splitlines() if ln.startswith("plugins "))
    assert "up to date" not in row  # a row with something left to do is not "up to date"


def test_a_failed_marketplace_refresh_is_pending_never_up_to_date(sandbox, monkeypatch):
    u, calls = _plugins(monkeypatch, installed=INSTALLED[:1], refresh_rc=1)
    res = u.update_plugins()
    assert res.action == "none" and ("plugin", "update", "a@m", "--scope", "user") in calls
    assert "marketplace refresh failed (getaddrinfo ENOTFOUND github.com)" in res.detail
    assert res.pending.startswith(
        "could not check plugins for newer versions: marketplace refresh failed"
    )
    rep = updater.Report(results=[res])
    text = "\n".join(updater.format_report(rep))
    assert "Pending:" in text and "Everything is up to date." not in text
    assert "up to date" not in next(ln for ln in text.splitlines() if ln.startswith("plugins "))
    assert rep.exit_code() == 0
    # what the refresh could not look for does not hide what an update did change
    u, _ = _plugins(monkeypatch, installed=INSTALLED[:1], refresh_rc=1)
    seq = iter([INSTALLED[:1], INSTALLED[:1], [{**INSTALLED[0], "version": "1.1.0"}]])
    monkeypatch.setattr(
        updater.ClaudeSetup, "_plugin_list", lambda self: (next(seq, INSTALLED[:1]), "")
    )
    res = u.update_plugins()
    assert res.action == "updated" and res.detail == "a@m 1.0.0->1.1.0" and res.pending


def test_a_plugin_list_that_failed_is_a_failure_not_an_empty_list(sandbox, monkeypatch):
    for kw, why in (
        ({"list_rc": 1}, "`claude plugin list` exited 1: list broke"),
        ({"list_out": "not json at all"}, "did not print JSON"),
        ({"list_out": '"just a string"'}, "unexpected shape"),
    ):
        u, calls = _plugins(monkeypatch, installed=INSTALLED[:1], **kw)
        res = u.update_plugins()
        assert res.action == "failed", kw
        assert res.detail.startswith("could not check plugins: ") and why in res.detail
        rep = updater.Report(results=[res])
        text = "\n".join(updater.format_report(rep))
        assert rep.exit_code() == updater.EXIT_FAILED and "up to date" not in text.lower()
        assert not [c for c in calls if c[:2] == ("plugin", "update")]


# ---- Claude Code processes claude-hr did not start --------------------------------------------------------------------
def test_the_report_says_when_claude_code_processes_run_that_claude_hr_did_not_start(
    sandbox, monkeypatch
):
    me = os.getpid()
    table = {
        8000: (1, "claude.exe"),  # a plain `claude` somebody typed
        8001: (1, "claude"),
        me: (7000, "claude.exe"),  # a claude-hr session (registered) ...
        7000: (6000, "claude.exe"),  # ... under the thin launcher Windows keeps above it
        7001: (me, "claude.exe"),  # ... and one it started itself
        9000: (1, "node.exe"),  # not Claude Code
    }
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: table)
    resources.register_session(me, "/x")
    try:
        assert updater.unmanaged_claude_processes() == [8000, 8001]
        rep = _spied(monkeypatch, []).run(["proxy"])
    finally:
        resources.unregister_session(me)
    assert rep.notes == [
        "2 Claude Code process(es) not started by claude-hr are running; they are unaffected by a proxy restart "
        "(they do not use it) and the updated claude takes effect when they restart"
    ]
    assert "INFO: 2 Claude Code process(es) not started by claude-hr" in "\n".join(
        updater.format_report(rep)
    )
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {})
    assert _spied(monkeypatch, []).run(["proxy"]).notes == []


def test_a_process_scan_that_fails_is_no_news_and_no_error(sandbox, monkeypatch):
    def boom():
        raise OSError("no snapshot")

    monkeypatch.setattr(updater.sysinfo, "process_table", boom)
    assert updater.unmanaged_claude_processes() == []


def test_the_session_guard_itself_still_counts_only_registered_sessions(sandbox, monkeypatch):
    """A plain claude is reported (INFO), not a reason to wait: otherwise a person who keeps a session open for days would
    never be updated."""
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {8000: (1, "claude.exe")})
    u = updater.Updater(config.load())
    u.claude = None
    assert u._busy() == ""


def test_status_shows_the_last_update_line(sandbox, monkeypatch, capsys):
    """`claude-hr-ctl status` is where the background self-update message sends the person to see the result."""
    from claude_hr import ctl
    from claude_hr.headroom import Headroom, Health

    monkeypatch.setattr(
        ctl.monitor,
        "snapshot",
        lambda cfg: {
            "sessions": 0,
            "session_limit": 3,
            "ram_free_gb": 8,
            "ram_total_gb": 16,
            "disk_free_gb": 100,
        },
    )
    monkeypatch.setattr(
        Headroom, "health_patient", lambda self, patience_s=4.0: Health(False, "down")
    )
    assert ctl._run(["status"]) == 0
    assert "Last update" not in capsys.readouterr().out
    _spied(
        monkeypatch, [], claude=updater.Result("claude", "2.1.1", "2.1.2", action="updated")
    ).run()
    assert ctl._run(["status"]) == 0
    out = capsys.readouterr().out
    assert "  Last update: " in out and "updated claude 2.1.1 -> 2.1.2" in out


def test_a_real_self_update_queues_the_reinstall_with_the_post_phase_behind_it(
    managed_repo, monkeypatch
):
    """update_self for real (a throwaway git origin with a newer tag), the helper replaced by a recorder: the reinstall
    first, the post phase behind it, in whatever form this platform starts the helper."""
    origin, work, managed = managed_repo
    _release_v020(work)
    _stub_uv(monkeypatch)
    started = []
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log, cwd=None: False)
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: started.append(" ".join(cmd))
    )
    monkeypatch.setattr(
        updater,
        "_start_helper_task",
        lambda shell, ps1: started.append(ps1.read_text(encoding="utf-8-sig")) or True,
    )
    rep = updater.Updater(config.load()).run(["self"])
    assert [(r.component, r.action, r.target) for r in rep.results] == [
        ("self", "updated", "v0.2.0")
    ]
    assert len(started) == 1
    helper = started[0]
    assert "tool install --python 3.13 --force --reinstall" in helper.replace("'", "").replace(
        '"', ""
    )
    assert helper.index("--reinstall") < helper.index(
        "claude_hr.ctl"
    )  # the post phase comes after the reinstall
    assert "--post" in helper
    assert updater.read_last()["phase"] == "pending"


# ---- --no-restart, an exception in the upgrade, the proxy that is not ours --------------------------------------------
_REAL_UNOWNED_PROXY = updater.Updater._unowned_proxy  # before any test replaces it


def _upgrade_world(monkeypatch, healthy=True, installed="0.40.0", newest="0.40.1"):
    """A Headroom whose newest release is `newest`; every call that stops, installs or starts something is recorded."""
    from claude_hr.headroom import Headroom, Health

    events, state = [], {"v": installed}
    monkeypatch.setattr(updater.procutil, "port_listening", lambda host, port: False)
    monkeypatch.setattr(Headroom, "version", lambda self: state["v"])
    monkeypatch.setattr(
        Headroom,
        "health",
        lambda self, timeout=0.6: Health(healthy, "healthy" if healthy else "down", "0.40.0"),
    )
    monkeypatch.setattr(
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: newest
    )
    monkeypatch.setattr(Headroom, "stop", lambda self: events.append("stop") or True)
    monkeypatch.setattr(
        Headroom,
        "ensure",
        lambda self, wait_s=0.0, announce=None: events.append("ensure") or Health(True),
    )

    def fake_install(self, version=None, upgrade=True, exclude_newer=None):
        events.append(f"install:{version}")
        state["v"] = version
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(Headroom, "install", fake_install)
    monkeypatch.setattr(
        updater.Updater,
        "smoke_headroom",
        lambda self, wait_s=150.0: events.append("smoke") or True,
    )
    return events, state


@pytest.mark.parametrize("force", [False, True])
def test_no_restart_defers_a_headroom_upgrade_before_anything_is_stopped(
    sandbox, monkeypatch, force
):
    """The upgrade stops the proxy and starts it again; --no-restart means never, and --force is about waiting for
    sessions, not about this."""
    events, state = _upgrade_world(monkeypatch)
    res = updater.Updater(config.load(), restart=False, force=force).update_headroom()
    assert res.action == "deferred" and not res.busy
    assert res.detail == "needs a proxy restart, skipped: --no-restart"
    assert events == [] and state["v"] == "0.40.0"  # not stopped, not installed, not started
    rep = updater.Report(results=[res])
    assert rep.exit_code() == 0
    assert "  headroom: needs a proxy restart, skipped: --no-restart" in updater.format_report(rep)


def test_no_restart_holds_nothing_back_when_no_proxy_runs(sandbox, monkeypatch):
    events, state = _upgrade_world(monkeypatch, healthy=False)
    res = updater.Updater(config.load(), restart=False).update_headroom()
    assert res.action == "updated" and state["v"] == "0.40.1"


def test_a_headroom_preview_says_no_restart_would_skip_the_upgrade(sandbox, monkeypatch):
    events, _ = _upgrade_world(monkeypatch)
    res = updater.Updater(config.load(), dry=True, restart=False).update_headroom()
    assert res.action == "available"
    assert res.detail == (
        "Headroom 0.40.0 -> 0.40.1 (newest release past the cooldown); "
        "it would be skipped now: it restarts the proxy and --no-restart is set"
    )
    assert "skipped" not in updater.Updater(config.load(), dry=True).update_headroom().detail
    assert events == []


def test_an_exception_in_the_headroom_upgrade_puts_the_old_release_and_the_proxy_back(
    sandbox, monkeypatch
):
    events, state = _upgrade_world(monkeypatch)
    online = []

    def smoke(self, wait_s=150.0):
        online.append(self.hr.h.get("offline_models"))
        events.append("smoke")
        if (
            len(online) == 1
        ):  # the new release's smoke test: a full disk, an unwritable state directory ...
            raise OSError(28, "No space left on device")
        return True  # the rollback's

    monkeypatch.setattr(updater.Updater, "smoke_headroom", smoke)
    u = updater.Updater(config.load())
    before = dict(u.hr.h)
    res = u.update_headroom()
    assert res.action == "failed" and "No space left on device" in res.detail
    assert res.current == "0.40.0" and res.target == "0.40.1"
    assert "WARNING" not in res.detail  # the rollback was verified
    assert events == [
        "stop",
        "install:0.40.1",
        "smoke",
        "stop",
        "install:0.40.0",
        "smoke",
        "stop",
        "ensure",  # the proxy runs again
    ]
    assert state["v"] == "0.40.0"
    assert (
        online == [False, False] and u.hr.h == before
    )  # allowed online while it ran, restored after
    assert u.proxy_restarted and not u.headroom_upgraded
    rep = updater.Report(results=[res])
    assert rep.exit_code() == updater.EXIT_FAILED


def test_an_exception_while_putting_the_old_release_back_is_a_warning_not_a_second_crash(
    sandbox, monkeypatch
):
    from claude_hr.headroom import Headroom

    events, state = _upgrade_world(monkeypatch)

    def smoke(self, wait_s=150.0):
        raise RuntimeError("smoke exploded")

    def install(self, version=None, upgrade=True, exclude_newer=None):
        events.append(f"install:{version}")
        if version == "0.40.0":
            raise OSError("uv vanished")
        state["v"] = version
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(updater.Updater, "smoke_headroom", smoke)
    monkeypatch.setattr(Headroom, "install", install)
    res = updater.Updater(config.load()).update_headroom()
    assert res.action == "failed" and "RuntimeError: smoke exploded" in res.detail
    assert "WARNING: rollback failed (OSError: uv vanished)" in res.detail
    assert events[-2:] == ["stop", "ensure"]  # the proxy is still put back


def test_a_proxy_that_cannot_be_started_again_after_a_good_upgrade_is_a_note_not_a_failure(
    sandbox, monkeypatch
):
    from claude_hr.headroom import Headroom

    _upgrade_world(monkeypatch)

    def ensure(self, wait_s=0.0, announce=None):
        raise OSError("spawn failed")

    monkeypatch.setattr(Headroom, "ensure", ensure)
    u = updater.Updater(config.load())
    res = u.update_headroom()
    assert res.action == "updated"
    assert "the proxy could not be started again: OSError: spawn failed" in res.detail
    assert (
        u.headroom_upgraded and not u.proxy_restarted
    )  # the proxy step still has something to say


def test_a_proxy_is_only_ours_when_its_record_names_a_live_headroom_process_that_listens(
    sandbox, monkeypatch
):
    from claude_hr.headroom import Headroom

    u = updater.Updater(config.load())
    record = u.hr._state_file()
    record.parent.mkdir(parents=True, exist_ok=True)
    world = {"alive": True, "headroom": True, "listeners": {4242}, "table": {}}
    monkeypatch.setattr(updater.sysinfo, "pid_alive", lambda pid: world["alive"])
    monkeypatch.setattr(
        Headroom, "_is_headroom_process", staticmethod(lambda pid: world["headroom"])
    )
    monkeypatch.setattr(Headroom, "_listener_pids", lambda self: world["listeners"])
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: world["table"])
    check = lambda: _REAL_UNOWNED_PROXY(u)  # noqa: E731
    assert "no longer running" in check()  # no record at all
    record.write_text(json.dumps({"pid": 4242, "port": 8787, "args": "abc"}), encoding="utf-8")
    assert check() == ""  # alive, a Headroom process, and it listens
    world["alive"] = False
    assert "no longer running" in check()  # a crash or a reboot leaves the record behind
    world.update(alive=True, headroom=False)
    assert "not a Headroom process" in check()  # a reused pid
    world.update(headroom=True, listeners={9999})
    assert "does not listen on port" in check()  # somebody else's proxy answers on the port
    world["table"] = {
        9999: (4242, "python.exe")
    }  # the uv shim above the listener is the recorded pid
    assert check() == ""
    world["table"] = {9999: (5000, "python.exe"), 5000: (4242, "headroom.exe")}
    assert check() == ""
    world.update(listeners=set(), table={})  # nothing can be shown to listen: not judged
    assert "does not listen" in check()
    for broken in ("[1,", json.dumps({"pid": "x"}), json.dumps([1, 2])):
        record.write_text(broken, encoding="utf-8")
        assert check()  # never raises, never "ours"


def test_a_stale_record_never_gets_a_proxy_claude_hr_did_not_start_stopped(proxy, monkeypatch):
    """After a reboot or a crash headroom-proxy.json is still there; if the person's own `headroom proxy` listens on the
    port, Headroom.stop() would end it. Nothing is judged, stopped or restarted, and the report says so."""
    monkeypatch.setattr(updater.Updater, "_unowned_proxy", _REAL_UNOWNED_PROXY)
    monkeypatch.setattr(updater.sysinfo, "pid_alive", lambda pid: False)
    proxy.state = "older"  # a record without `args` ...
    u = updater.Updater(config.load())
    u.hr._state_file().parent.mkdir(parents=True, exist_ok=True)
    u.hr._state_file().write_text(json.dumps({"pid": 4242, "port": 8787}), encoding="utf-8")
    for kw in ({}, {"dry": True}):
        u = updater.Updater(config.load(), **kw)
        res = u.update_proxy()
        assert res.action == "none" and res.state == "not judged" and proxy.events == []
        assert len(u.notes) == 1 and "is not one claude-hr started" in u.notes[0]
        assert "no longer running" in u.notes[0]
    rep = updater.Updater(config.load()).run(["proxy"])
    text = "\n".join(updater.format_report(rep))
    assert (
        "INFO: the Headroom proxy on port " in text and "not judged, stopped or restarted" in text
    )
    assert rep.exit_code() == 0 and proxy.events == []
    row = next(ln for ln in text.splitlines() if ln.startswith("proxy "))
    assert row.endswith("not judged")


def test_inside_a_systemd_service_the_proxy_is_stopped_but_not_started_again(proxy, monkeypatch):
    """What a unit starts dies with it (the transient unit of the post phase): the next launch or the monitor job starts
    the proxy, in a context that lasts."""
    monkeypatch.setattr(updater, "in_systemd_service", lambda: True)
    u = updater.Updater(config.load())
    res = u.update_proxy()
    assert proxy.events == ["stop"]  # an ensure() here would start a proxy that dies with the unit
    assert res.action == "updated" and res.detail.startswith("stopped (it runs other settings")
    assert "monitor job" in res.detail and "next claude-hr launch" in res.detail
    assert u.proxy_restarted
    proxy.events.clear()
    monkeypatch.setattr(
        updater, "in_systemd_service", lambda: False
    )  # anywhere else: restart as designed
    assert updater.Updater(config.load()).update_proxy().detail.startswith("restarted")
    assert proxy.events == ["stop", "ensure:90"]


@pytest.mark.parametrize(
    "invocation,cgroup,expected",
    [
        (None, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.service\n", False),
        (
            "abc",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/claude-hr-selfupdate-1.service\n",
            True,
        ),
        (
            "abc",
            "0::/user.slice/user-1000.slice/session-3.scope\n",
            False,
        ),  # a shell that inherited it
        (
            "abc",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-1.scope\n",
            False,
        ),
        ("abc", "12:cpu:/x\n1:name=systemd:/system.slice/foo.service\n", True),  # cgroup v1
        ("abc", "1:name=systemd:/user.slice/user-1000.slice/session-3.scope\n", False),
        ("abc", None, True),  # the group cannot be read: the variable is all there is
    ],
)
def test_a_systemd_service_is_told_from_a_shell_that_inherited_the_variable(
    monkeypatch, invocation, cgroup, expected
):
    if invocation is None:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
    else:
        monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setattr(updater, "_own_cgroup", lambda: cgroup)
    assert updater.in_systemd_service() is expected


# ---- Claude Code processes that the configuration routes through the proxy -------------------------------------------
def _routed(sandbox, monkeypatch, how="settings", url=None):
    from claude_hr import paths
    from claude_hr.headroom import Headroom

    url = url or Headroom(config.load()).url
    if how == "settings":
        paths.claude_settings().write_text(
            json.dumps({"env": {"ANTHROPIC_BASE_URL": url}}), encoding="utf-8"
        )
    else:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {8000: (1, "claude.exe")})


def test_a_plain_claude_is_no_reason_to_wait_for_the_proxy_unless_it_is_routed_through_it(
    proxy, sandbox, monkeypatch
):
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {8000: (1, "claude.exe")})
    res = updater.Updater(config.load()).update_proxy()
    assert res.action == "updated" and proxy.events == [
        "stop",
        "ensure:90",
    ]  # nobody routes it here
    proxy.events.clear()
    _routed(
        sandbox, monkeypatch, "settings", url="http://127.0.0.1:6767"
    )  # another proxy: not ours to wait for
    assert updater.Updater(config.load()).update_proxy().action == "updated"


@pytest.mark.parametrize("how", ["settings", "environment"])
def test_a_plain_claude_routed_through_the_proxy_is_a_consumer_and_the_restart_waits(
    proxy, sandbox, monkeypatch, how
):
    _routed(sandbox, monkeypatch, how)
    u = updater.Updater(config.load())
    res = u.update_proxy()
    assert res.action == "deferred" and res.busy and proxy.events == []
    assert "1 Claude Code process(es) not started by claude-hr use the proxy" in res.detail
    assert updater.Report(results=[res]).exit_code() == updater.EXIT_DEFERRED
    # a preview says it would wait, --force goes ahead
    dry = updater.Updater(config.load(), dry=True).update_proxy()
    assert (
        dry.action == "available" and "it would wait now: 1 Claude Code process(es)" in dry.detail
    )
    assert updater.Updater(config.load(), force=True).update_proxy().action == "updated"
    # ... but updating the Claude binary itself never waits for them: they keep the old build until they restart
    assert u._busy() == ""
    assert u._busy(proxy=True).startswith("1 Claude Code process(es)")


def test_a_headroom_upgrade_stops_the_proxy_too_so_it_waits_for_the_same_consumers(
    sandbox, monkeypatch
):
    events, state = _upgrade_world(monkeypatch)
    _routed(sandbox, monkeypatch)
    res = updater.Updater(config.load()).update_headroom()
    assert res.action == "deferred" and res.busy and "use the proxy" in res.detail
    assert events == [] and state["v"] == "0.40.0"


def test_the_info_line_tells_whether_the_unmanaged_claude_uses_the_proxy(sandbox, monkeypatch):
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: {8000: (1, "claude.exe")})
    assert _spied(monkeypatch, []).run(["proxy"]).notes == [
        "1 Claude Code process(es) not started by claude-hr are running; they are unaffected by a proxy restart "
        "(they do not use it) and the updated claude takes effect when they restart"
    ]
    _routed(sandbox, monkeypatch)
    assert _spied(monkeypatch, []).run(["proxy"]).notes == [
        "1 Claude Code process(es) not started by claude-hr are running and use the Headroom proxy "
        "(ANTHROPIC_BASE_URL points at it): a proxy restart waits for them, and the updated claude takes "
        "effect when they restart"
    ]


@pytest.mark.parametrize(
    "content",
    [
        "\ufeff{ not json",
        "[1, 2]",
        '{"env": "oops"}',
        '{"env": {"ANTHROPIC_BASE_URL": 5}}',
        b'\xff\xfe{\x00"\x00e\x00n\x00v\x00"\x00:\x00{\x00}\x00}\x00',  # UTF-16 from a Windows PowerShell redirect
    ],
)
def test_a_settings_file_that_cannot_be_read_never_breaks_the_routing_question(sandbox, content):
    from claude_hr import paths
    from claude_hr.headroom import Headroom

    f = paths.claude_settings()
    f.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    assert updater.plain_claude_uses_proxy(Headroom(config.load())) is False


# ---- the Claude shim that started the update, and what this process started, are not "other" Claude Code ---------------
def test_the_claude_shim_that_started_the_update_and_its_own_tree_are_not_unmanaged(
    sandbox, monkeypatch
):
    me = os.getpid()
    table = {
        me: (5000, "python.exe"),
        5000: (
            4000,
            "claude.exe",
        ),  # the shim: a copy of the launcher named claude.exe, above this interpreter
        4000: (3000, "cmd.exe"),
        9100: (me, "claude.exe"),  # something this process started
        8000: (1, "claude.exe"),  # a plain `claude` somebody typed
    }
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: table)
    assert updater.unmanaged_claude_processes() == [8000]
    # a real Claude Code higher up (the update was started from its Bash tool) is an unmanaged session: it stays counted
    table = {me: (5000, "python.exe"), 5000: (4000, "bash.exe"), 4000: (1, "claude.exe")}
    monkeypatch.setattr(updater.sysinfo, "process_table", lambda: table)
    assert updater.unmanaged_claude_processes() == [4000]


def test_the_skip_of_a_claude_code_a_package_manager_owns_names_the_way_to_update_it(
    sandbox, monkeypatch
):
    u = updater.Updater(config.load())
    u.claude = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js")
    monkeypatch.setattr(u, "claude_version", lambda: "2.1.292")
    monkeypatch.setattr(u, "claude_target", lambda: "2.1.300")
    res = u.update_claude()
    assert res.action == "none" and res.state == "skipped"
    assert "that package manager" in res.detail and "`claude-hr update --claude-only`" in res.detail


# ---- the report never reads fine next to something that is not ----------------------------------------------------------
def _text(*rows, check=False, background=""):
    return "\n".join(
        updater.format_report(updater.Report(list(rows), check=check, background=background))
    )


def test_the_report_does_not_say_nothing_is_broken_next_to_a_failure():
    waiting = updater.Result(
        "claude", action="deferred", busy=True, detail="1 claude-hr session(s) running"
    )
    failed = updater.Result("baseline", action="failed", detail="settings FAILED: unparsable")
    text = _text(waiting, failed)
    assert "Nothing is broken" not in text and "Everything is up to date." not in text
    assert "Failed:\n  baseline: settings FAILED: unparsable" in text
    assert "Pending:\n  claude: 1 claude-hr session(s) running" in text
    assert (
        "Close the claude-hr sessions and run `claude-hr update` again, or use `claude-hr update --force"
        in text
    )
    assert "Nothing is broken. Close the claude-hr sessions" in _text(waiting)


def test_the_report_does_not_say_everything_is_up_to_date_while_a_reinstall_waits():
    waiting = updater.Result(
        "self",
        "v0.2.2",
        "v0.2.2",
        state="waiting",
        detail="a reinstall was started in the last 15 minutes; waiting for it",
    )
    text = _text(waiting)
    assert "Everything is up to date." not in text
    assert (
        "Pending:\n  self: a reinstall was started in the last 15 minutes; waiting for it" in text
    )
    assert "Everything is up to date." in _text(updater.Result("self", "v0.2.2", "v0.2.2"))
    assert "Everything is up to date." not in _text(updater.Result("self"), background="/log")


def test_a_preview_lists_failures_as_could_not_check_not_as_available():
    failed = updater.Result("claude", action="failed", detail="could not read the release channel")
    text = _text(failed, check=True)
    assert "Could not check:\n  claude: could not read the release channel" in text
    assert "Available:" not in text and "Run `claude-hr update`" not in text
    both = _text(
        failed,
        updater.Result("headroom", "1", "2", action="available", detail="1 -> 2"),
        check=True,
    )
    assert "Could not check:\n  claude: " in both and "Available:\n  headroom: 1 -> 2" in both
    assert "Run `claude-hr update` to apply it." in both
    assert "Pending:" not in both and "Failed:" not in both
    # a real run calls the same rows Failed, not Could not check
    assert "Failed:\n  claude: " in _text(failed) and "Could not check" not in _text(failed)


def test_a_preview_that_cannot_see_plugins_does_not_claim_everything_is_up_to_date():
    rows = [
        updater.Result("claude", "2.1.1", "2.1.1"),
        updater.Result(
            "plugins",
            state="not previewed",
            detail="not previewed (the update asks Claude Code for each plugin)",
        ),
    ]
    text = _text(*rows, check=True)
    assert "Everything is up to date." not in text
    assert "Nothing to update among what a preview can see." in text


def test_the_hint_says_what_force_really_does_for_claude_hrs_own_reinstall(
    managed_repo, monkeypatch
):
    """--force answers "" from the session guard for every component, claude-hr's own reinstall included: the hint says so
    where claude-hr itself is among what waits, and the code behaves that way."""
    self_wait = updater.Result(
        "self", action="deferred", busy=True, detail="1 claude-hr session(s) running"
    )
    claude_wait = updater.Result(
        "claude", action="deferred", busy=True, detail="1 claude-hr session(s) running"
    )
    text = _text(self_wait, claude_wait)
    assert "or use `claude-hr update --force` to update now." in text
    assert "--force also reinstalls claude-hr itself without waiting for the sessions" in text
    assert "`claude-hr update --force --only claude headroom plugins`" in text
    assert "--force also reinstalls" not in _text(claude_wait)
    origin, work, managed = managed_repo
    _release_v020(work)
    _stub_uv(monkeypatch)
    resources.register_session(os.getpid(), "/x")
    try:
        res = updater.Updater(config.load(), force=True).update_self()
    finally:
        resources.unregister_session(os.getpid())
    assert (
        res.action == "updated" and not res.busy
    )  # it queues the reinstall with a session running


# ---- one update at a time; sessions that start while a run goes on ---------------------------------------------------------
def test_a_second_update_while_one_runs_exits_75_and_runs_nothing(sandbox, monkeypatch, capsys):
    from claude_hr import ctl, filelock, paths

    _patch_class(monkeypatch)
    ran = []
    monkeypatch.setattr(
        updater.Updater,
        "update_claude",
        lambda self: ran.append("claude") or updater.Result("claude"),
    )
    with filelock.locked(paths.state_dir() / updater.UPDATE_LOCK, timeout=1):
        assert ctl._run(["update"]) == 75
        out = capsys.readouterr().out
        assert "another update is running" in out and ran == []  # nothing ran
        assert not (paths.state_dir() / "update-last.json").exists()
        # the scheduled job is not a failure for it ...
        assert ctl._run(["job", "update"]) == 0
        assert "another update is running" in (paths.logs_dir() / "job-update.log").read_text(
            encoding="utf-8"
        )
        # ... a preview leaves no trace and needs no lock ...
        assert ctl._run(["update", "--check"]) == 0 and ran == ["claude"]
        # ... and the post phase waits for it a while, then gives up the same way
        monkeypatch.setattr(updater, "POST_LOCK_WAIT_S", 0.3)
        assert ctl._run(["update", "--post"]) == 75
    assert ctl._run(["update"]) == 0 and ran == ["claude", "claude"]  # released: the next one runs
    assert not (paths.state_dir() / updater.UPDATE_LOCK).exists()  # ... and releases it again


def test_a_lock_left_by_a_dead_update_is_taken_over(sandbox, monkeypatch):
    from claude_hr import paths

    _patch_class(monkeypatch)
    lock = paths.state_dir() / updater.UPDATE_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(updater.sysinfo, "pid_alive", lambda pid: False)
    assert updater.Updater(config.load()).run(["claude"]).ok
    assert not lock.exists()


@pytest.mark.parametrize("forced", [False, True])
def test_sessions_that_start_while_the_run_goes_on_cancel_claude_hrs_own_reinstall(
    sandbox, monkeypatch, forced
):
    from claude_hr import paths

    events, launched = [], []
    u = updater.Updater(config.load(), force=forced)
    for comp in ("claude", "headroom", "proxy"):
        monkeypatch.setattr(
            u, f"update_{comp}", lambda comp=comp: events.append(comp) or updater.Result(comp)
        )
    monkeypatch.setattr(u, "reapply_baseline", lambda: events.append("baseline"))

    def queue():
        u._pending_reinstall = ["uv", "tool", "install"]
        updater.write_json(
            paths.state_dir() / "self-update.json", {"ts": __import__("time").time()}
        )
        return updater.Result("self", "v0.2.2", "v0.2.3", action="updated")

    def plugins():
        resources.register_session(
            os.getpid(), "/x"
        )  # somebody starts claude-hr in another terminal
        return updater.Result("plugins")

    monkeypatch.setattr(u, "update_self", queue)
    monkeypatch.setattr(u, "update_plugins", plugins)
    monkeypatch.setattr(u, "_launch_reinstall", lambda post=None: launched.append(post))
    try:
        rep = u.run()
    finally:
        resources.unregister_session(os.getpid())
    if forced:  # --force never waited for sessions
        assert len(launched) == 1 and rep.background
        return
    assert launched == [] and u._pending_reinstall is None and rep.background == ""
    row = rep.results[0]
    assert row.action == "deferred" and row.busy
    assert row.detail.startswith("1 claude-hr session(s) running (started while this ran)")
    assert rep.exit_code() == updater.EXIT_DEFERRED
    assert not (
        paths.state_dir() / "self-update.json"
    ).exists()  # the retry must not wait for its 15-minute mark
    assert events == [
        "claude",
        "headroom",
        "baseline",
        "proxy",
    ]  # the rest finished inline, on the code that runs now
    assert updater.read_last()["phase"] == "complete"


def test_a_second_post_phase_keeps_the_rows_of_the_run_before_it(sandbox, monkeypatch):
    """`update --post` twice (a helper started twice, or the hidden flag typed by hand) must not replace the first run's
    rows with its own two."""
    events, launched = [], []
    u = _spied(monkeypatch, events)
    monkeypatch.setattr(
        u,
        "update_self",
        lambda: (
            setattr(u, "_pending_reinstall", ["uv"])
            or updater.Result("self", "v0.2.2", "v0.2.3", action="updated")
        ),
    )
    monkeypatch.setattr(u, "_launch_reinstall", lambda post=None: launched.append(post))
    monkeypatch.setattr(
        u, "update_claude", lambda: updater.Result("claude", "2.1.1", "2.1.2", action="updated")
    )
    u.run()
    _spied(monkeypatch, []).run_post()  # the first one continues the pending record ...
    first = updater.read_last()
    assert first["phase"] == "complete" and "post_only" not in first
    components = [r["component"] for r in first["results"]]
    assert components == ["self", "claude", "headroom", "plugins", "baseline", "proxy"]
    for n in (1, 2):  # ... a second, and a third, are labelled and appended
        events2 = []
        _spied(monkeypatch, events2).run_post()
        assert events2 == ["baseline", "proxy"]
        rec = updater.read_last()
        assert {k: v for k, v in rec.items() if k != "post_only"} == first
        assert len(rec["post_only"]) == n
        assert rec["post_only"][-1]["label"].startswith("post phase only")
        assert [r["component"] for r in rec["post_only"][-1]["results"]] == ["baseline", "proxy"]
    assert "updated self v0.2.2 -> v0.2.3" in updater.last_update_summary()
    assert "claude 2.1.1 -> 2.1.2" in updater.last_update_summary()


def test_the_post_phase_continues_only_a_recent_pending_record(sandbox, monkeypatch):
    """run_post's phase check: pending and recent = continue it; pending and abandoned = replace it; anything else (a
    finished run, no record) = a post-only entry that leaves the run before it alone."""
    import time

    path = updater.paths.state_dir() / "update-last.json"
    rows = [{"component": "self", "action": "updated", "current": "a", "target": "b"}]

    def run_post(**record):
        updater.paths.ensure_dirs()
        if record:
            updater.write_json(
                path, {"post": {"steps": ["baseline"], "doctor": False}, "results": rows, **record}
            )
        else:
            path.unlink(missing_ok=True)
        rep = _spied(monkeypatch, []).run_post()
        return [r.component for r in rep.results], updater.read_last()

    now = time.time()
    got, rec = run_post(phase="pending", ts=now)
    assert got == ["self", "baseline"] and rec["phase"] == "complete" and "post_only" not in rec
    got, rec = run_post(phase="pending", ts=now - updater.PENDING_MAX_AGE_S - 60)
    assert got == ["baseline", "proxy"] and [r["component"] for r in rec["results"]] == [
        "baseline",
        "proxy",
    ]
    got, rec = run_post(phase="complete", ts=now - 60)
    assert got == ["baseline", "proxy"]
    assert [r["component"] for r in rec["results"]] == ["self"]  # the finished run is untouched ...
    assert len(rec["post_only"]) == 1  # ... and the post phase is on record next to it
    got, rec = run_post()  # no record at all
    assert (
        got == ["baseline", "proxy"]
        and rec["label"] == "post phase only"
        and "post_only" not in rec
    )


def test_a_post_phase_survives_a_pending_record_of_the_wrong_shape(sandbox, monkeypatch):
    import time

    updater.paths.ensure_dirs()
    updater.write_json(
        updater.paths.state_dir() / "update-last.json",
        {
            "phase": "pending",
            "ts": time.time(),
            "header": None,
            "results": None,
            "post": {"steps": None},
        },
    )
    rep = _spied(monkeypatch, []).run_post()
    assert [
        r.component for r in rep.results
    ] == []  # nothing to continue, no post steps listed: nothing crashed


@pytest.mark.parametrize(
    "rec",
    [
        {"ts": -1},
        {"ts": 1e999},
        {"ts": 1e300},
        {"ts": float("nan")},
        {"ts": True},
        {"ts": "yesterday"},
        {"ts": 1700000000, "results": None},
        {"ts": 1700000000, "results": 5},
        {"ts": 1700000000, "results": [1, "x", None, {"component": "a", "action": "updated"}]},
        {"ts": 1700000000, "doctor": [1], "results": {}},
        {"ts": 1700000000, "phase": "pending"},
    ],
)
def test_status_never_dies_on_an_update_record_of_the_wrong_shape(sandbox, rec):
    updater.paths.ensure_dirs()
    (updater.paths.state_dir() / "update-last.json").write_text(json.dumps(rec), encoding="utf-8")
    line = updater.last_update_summary()
    assert line is None or isinstance(line, str)


# ---- --claude-only is exclusive; the post phase starts where it can be trusted ----------------------------------------------
@pytest.mark.parametrize(
    "flags,named",
    [
        (["--check"], "--check"),
        (["--only", "plugins"], "--only"),
        (["--force"], "--force"),
        (["--allow-downgrade"], "--allow-downgrade"),
        (["--no-restart"], "--no-restart"),
        (["--post"], "--post"),
        (["--check", "--force"], "--check, --force"),
    ],
)
def test_claude_only_takes_none_of_the_other_switches(sandbox, monkeypatch, capsys, flags, named):
    from claude_hr import ctl

    monkeypatch.setattr(ctl.procutil, "find_claude", lambda *a, **k: Path("/fake/claude"))
    monkeypatch.setattr(ctl.subprocess, "call", lambda *a, **k: pytest.fail("claude must not run"))
    monkeypatch.setattr(
        updater.Updater, "run", lambda *a, **k: pytest.fail("claude-hr's own update must not run")
    )
    assert ctl._run(["update", "--claude-only", *flags]) == 2
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and f"takes none of {named}:" in out[0]


@pytest.mark.real_helpers
def test_the_systemd_unit_starts_in_the_state_directory_and_gets_every_dollar_doubled(
    sandbox, monkeypatch
):
    from claude_hr import resources

    seen = {}
    monkeypatch.setattr(updater.paths, "IS_LINUX", True)
    monkeypatch.setattr(resources, "caps_available", lambda: True)
    monkeypatch.setattr(
        updater.procutil,
        "run",
        lambda cmd, **k: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    chain = updater._posix_chain(["/opt/${HOME}/uv", "tool", "install"], ["/p/$$/python", "-I"])
    assert (
        updater._start_systemd_service(chain, "/tmp/self-update.log", cwd="/srv/hr state") is True
    )
    cmd = seen["cmd"]
    assert "WorkingDirectory=/srv/hr state" in cmd
    words = cmd[cmd.index("--") + 1 :]
    assert words[:2] == ["/bin/sh", "-c"] and len(words) == 3
    script = words[2]
    assert "$$" in script and "$${HOME}" in script  # the path's own ${HOME} and $$, doubled
    assert script.replace("$$", "").count("$") == 0  # every `$` that reaches systemd is doubled
    # what the shell sees after systemd has turned $$ back into $ is the original chain
    assert script.replace("$$", "$") == chain[2]


@pytest.mark.real_helpers
def test_the_detached_helper_starts_in_the_given_directory_when_there_is_one(
    sandbox, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append(kw))
    updater._spawn_detached(["sh", "-c", "true"], tmp_path / "x.log", cwd=tmp_path)
    updater._spawn_detached(["sh", "-c", "true"], tmp_path / "x.log", cwd=tmp_path / "missing")
    updater._spawn_detached(["sh", "-c", "true"], None)
    assert calls[0]["cwd"] == str(tmp_path)
    assert (
        "cwd" not in calls[1] and "cwd" not in calls[2]
    )  # a directory that is not there must not stop the reinstall


@pytest.mark.skipif(
    __import__("shutil").which("powershell") is None and __import__("shutil").which("pwsh") is None,
    reason="needs PowerShell",
)
def test_the_helper_logs_the_real_outcome_of_a_post_command_that_cannot_start(tmp_path):
    """$LASTEXITCODE still held the reinstall's 0 after a post command that never ran: the log said 'exit code 0' and the
    error went to a stream nobody reads. The generated script runs under the real shell (never the scheduler: the lines that
    remove the script and unregister the task are cut)."""
    if os.name != "nt":
        pytest.skip("the commands below are Windows commands")
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    log = tmp_path / "self-update.log"
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

    def run(post):
        log.unlink(missing_ok=True)
        script = _isolated(
            updater._windows_reinstall_script(
                [cmd_exe, "/c", "exit 0"], 999999, 999998, log, env={}, post=post, cwd=tmp_path
            )
        )
        keep = [
            ln
            for ln in script.splitlines()
            if not ln.startswith("Unregister-ScheduledTask") and "Remove-Item" not in ln
        ]
        ps1 = tmp_path / "helper.ps1"
        ps1.write_bytes(b"\xef\xbb\xbf" + ("\n".join(keep) + "\n").encode("utf-8"))
        subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return log.read_text(encoding="utf-8-sig")

    text = run(
        [str(tmp_path / "no-such-dir" / "python.exe"), "-m", "claude_hr.ctl", "update", "--post"]
    )
    assert "reinstall exit code 0" in text
    assert "could not start the post phase: " in text and "no-such-dir" in text
    assert (
        "post phase exit code 0" not in text and "the post phase did not run (no exit code)" in text
    )
    assert "post phase exit code 3" in run([cmd_exe, "/c", "exit 3"])
    assert "post phase exit code 0" in run([cmd_exe, "/c", "exit 0"])


# ---- the Windows helper waits for whatever still runs from the tool environment, and retries a failed reinstall --------
def test_the_helper_script_waits_for_the_tool_environment_to_be_free_and_retries(tmp_path):
    """uv failed with 'Access is denied' removing the Scripts folder when the venv's python.exe stub or an entry-point trampoline was
    still shutting down, and left the tool environment half-deleted (claude-hr unusable until reinstalled)."""
    script = updater._windows_reinstall_script(["uv", "tool", "install"], 1, 2, "log.txt", env={})
    assert f"$toolEnv = '{tmp_path / 'no-tool-env'}'" in script
    for needle in (
        "function Get-Holders",
        "function Wait-ForHolders",
        "claude-hr.exe",
        "claude-hr-ctl.exe",
        "claude-hr-hook.exe",
        "retrying the reinstall (attempt ",
        "$attempt -lt 3",
    ):
        assert needle in script, needle
    lines = script.splitlines()
    wait = next(i for i, ln in enumerate(lines) if ln.startswith("if (-not (Wait-ForHolders 120))"))
    run = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("& 'uv'"))
    assert wait < run, "the wait comes before the first reinstall"
    assert "Log ('reinstall exit code ' + $rc)" in script


_NAMES = "$exeNames = @('claude-hr.exe', 'claude-hr-ctl.exe', 'claude-hr-hook.exe')"


def _isolated(script, fast=False):
    """The helper script as a test may run it: it looks for programs by name on the whole machine (a claude-hr session of the
    person running the tests would hold it for minutes), so those names are swapped for one that never runs; `fast` skips the
    pauses between attempts."""
    assert _NAMES in script, "the helper's program-name list changed: update this test helper"
    script = script.replace(_NAMES, "$exeNames = @('no-such-program.exe')")
    return script.replace("Start-Sleep -Seconds 10", "Start-Sleep -Seconds 0") if fast else script


def _run_helper(tmp_path, cmd, tool_env, post=None, fast=False):
    """The generated helper under the real shell (the lines that remove the script and unregister the task are cut)."""
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    log = tmp_path / "self-update.log"
    log.unlink(missing_ok=True)
    script = _isolated(
        updater._windows_reinstall_script(
            cmd, 999999, 999998, log, env={}, post=post, cwd=tmp_path
        ),
        fast,
    )
    keep = [
        ln
        for ln in script.splitlines()
        if not ln.startswith("Unregister-ScheduledTask") and "$PSCommandPath" not in ln
    ]
    ps1 = tmp_path / "helper.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + ("\n".join(keep) + "\n").encode("utf-8"))
    subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return log.read_text(encoding="utf-8-sig")


@pytest.mark.skipif(os.name != "nt", reason="the helper is a Windows PowerShell script")
def test_the_real_helper_retries_a_reinstall_that_fails_and_stops_when_it_works(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(updater, "_tool_env", lambda: str(tmp_path / "no-tool-env"))
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    flag = tmp_path / "second.flag"
    # fails the first time, succeeds the second: a batch line with a marker file
    bat = tmp_path / "flaky.cmd"
    bat.write_text(
        f'@echo off\r\nif exist "{flag}" (exit /b 0)\r\ntype nul > "{flag}"\r\nexit /b 5\r\n',
        encoding="ascii",
    )
    text = _run_helper(tmp_path, [cmd_exe, "/c", str(bat)], tmp_path / "no-tool-env")
    assert "reinstall exit code 5" in text
    assert "retrying the reinstall (attempt 2 of 3)" in text
    assert "reinstall exit code 0" in text and "attempt 3" not in text


@pytest.mark.skipif(os.name != "nt", reason="the helper is a Windows PowerShell script")
def test_the_real_helper_waits_for_a_process_that_still_runs_from_the_tool_environment(
    tmp_path, monkeypatch
):
    """A holder (a copy of cmd.exe started from inside the tool environment folder) that lives 6 s: the reinstall may not
    start before it is gone."""
    import shutil
    import time

    env_dir = tmp_path / "tool-env" / "Scripts"
    env_dir.mkdir(parents=True)
    holder_exe = (
        env_dir / "cmd.exe"
    )  # a copy of a program that runs on its own, from inside the tool environment folder
    shutil.copy2(
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), holder_exe
    )
    monkeypatch.setattr(updater, "_tool_env", lambda: str(tmp_path / "tool-env"))
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    stamp = tmp_path / "reinstall-started.txt"
    holder = subprocess.Popen([str(holder_exe), "/c", "ping -n 7 127.0.0.1 > nul"])
    t0 = time.monotonic()
    try:
        text = _run_helper(
            tmp_path, [cmd_exe, "/c", f"echo started > {stamp}"], tmp_path / "tool-env"
        )
    finally:
        holder.kill()
    assert stamp.exists() and "reinstall exit code 0" in text
    assert time.monotonic() - t0 >= 5, "the helper did not wait for the holder"


# ---- 0.3.6: a run that ends before the helper starts must not lock out its own retry; the run says what it is doing ---------
def _spawn_stubs(monkeypatch):
    """Launching goes down the plain POSIX route and records the command instead of running it."""
    spawned = []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log, cwd=None: False)
    monkeypatch.setattr(
        updater, "_spawn_detached", lambda cmd, log=None, cwd=None: spawned.append(cmd)
    )
    return spawned


def test_queueing_a_reinstall_leaves_no_mark_but_launching_it_does(managed_repo, monkeypatch):
    origin, work, managed = managed_repo
    _release_v020(work)
    mark = updater.paths.state_dir() / "self-update.json"
    u = updater.Updater(config.load())
    assert u.update_self().action == "updated"
    assert not mark.exists(), "update_self marked a reinstall that has not been launched"
    spawned = _spawn_stubs(monkeypatch)
    u._launch_reinstall()
    data = json.loads(mark.read_text(encoding="utf-8"))
    assert spawned and data["ts"] > 0
    assert data["from"] != data["to"] and len(data["to"]) == 40  # the commits it moves between


def test_a_run_that_ends_before_launching_can_be_retried_at_once(managed_repo, monkeypatch):
    """The incident: the checkout moved to the new release, the process ended before it launched the reinstall, and the next
    update then waited 15 minutes for a reinstall that had never started."""
    origin, work, managed = managed_repo
    _release_v020(work)
    _stub_uv(monkeypatch)
    monkeypatch.setattr("claude_hr.__version__", "0.1.0")
    first = updater.Updater(config.load())
    assert (
        first.update_self().action == "updated"
    )  # ... and the process ends here: nothing launched
    _fake_checkout_version(managed, "0.2.0")
    second = updater.Updater(config.load())
    res = second.update_self()
    assert res.action == "updated" and "retrying" in res.detail
    assert second._pending_reinstall and second._pending_reinstall[1:3] == ["tool", "install"]


def test_a_reinstall_that_was_launched_still_makes_the_next_run_wait(managed_repo, monkeypatch):
    origin, work, managed = managed_repo
    _release_v020(work)
    _stub_uv(monkeypatch)
    monkeypatch.setattr("claude_hr.__version__", "0.1.0")
    first = updater.Updater(config.load())
    first.update_self()
    real_os = updater.paths.IS_WINDOWS
    _spawn_stubs(monkeypatch)
    first._launch_reinstall()  # the helper is on its way
    monkeypatch.setattr(
        updater.paths, "IS_WINDOWS", real_os
    )  # `git` is only found with the host's own rules
    _fake_checkout_version(managed, "0.2.0")
    res = updater.Updater(config.load()).update_self()
    assert res.state == "waiting" and "15 minutes" in res.detail, (res.state, res.detail)


def test_launching_that_fails_leaves_no_mark_behind(managed_repo, monkeypatch):
    origin, work, managed = managed_repo
    _release_v020(work)
    u = updater.Updater(config.load())
    u.update_self()
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log, cwd=None: False)

    def cannot_start(cmd, log=None, cwd=None):
        raise OSError("no such program")

    monkeypatch.setattr(updater, "_spawn_detached", cannot_start)
    u._launch_reinstall()
    assert not (updater.paths.state_dir() / "self-update.json").exists()
    rows = _update_rows()
    assert rows[-1]["action"] == "failed" and "could not start the reinstall" in rows[-1]["detail"]


def _update_rows():
    f = updater.paths.logs_dir() / "update.jsonl"
    return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_the_log_says_when_the_helper_was_launched_and_how(managed_repo, monkeypatch):
    origin, work, managed = managed_repo
    _release_v020(work)
    u = updater.Updater(config.load())
    u.update_self()
    _spawn_stubs(monkeypatch)
    u._launch_reinstall()
    row = _update_rows()[-1]
    assert (row["component"], row["action"]) == ("self", "launched")
    assert "detached process" in row["detail"] and "self-update.log" in row["detail"]
    # the Windows route: the scheduler started it
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(updater.procutil, "which_all", lambda name: [r"C:\Windows\powershell.exe"])
    monkeypatch.setattr(updater, "_start_helper_task", lambda shell, script: True)
    u2 = updater.Updater(config.load())
    u2._pending_reinstall = ["uv", "tool", "install"]
    u2._launch_reinstall()
    assert "task scheduler" in _update_rows()[-1]["detail"]


def test_each_component_is_announced_before_it_runs(sandbox, monkeypatch):
    events, said = [], []
    u = _spied(monkeypatch, events)
    u.progress = said.append
    u.run(["claude", "headroom"])
    assert said == [
        "  updating Claude Code (a new version takes about a minute) ...",
        "  updating Headroom ...",
    ]
    assert events == ["claude", "headroom"]  # announced, then run, in the same order


def test_a_preview_says_checking_and_every_component_has_a_label(sandbox, monkeypatch):
    said = []
    u = updater.Updater(config.load(), dry=True)
    u.progress = said.append
    monkeypatch.setattr(u, "update_proxy", lambda: updater.Result("proxy"))
    u._step(updater.Report(check=True), "proxy")
    assert said == ["  checking the Headroom proxy ..."]
    assert set(updater.STEP_LABELS) == {
        "self",
        "claude",
        "headroom",
        "plugins",
        "baseline",
        "proxy",
    }


def test_a_progress_printer_that_fails_never_stops_the_update(sandbox, monkeypatch):
    events = []
    u = _spied(monkeypatch, events)

    def closed_terminal(line):
        raise ValueError("I/O operation on closed file")

    u.progress = closed_terminal
    u.run(["claude", "headroom"])
    assert (
        events == ["claude", "headroom"] and u.progress is None
    )  # silenced after the first failure


def test_nothing_is_announced_unless_asked_for(sandbox, monkeypatch):
    events = []
    u = _spied(monkeypatch, events)
    assert u.progress is None
    u.run(["claude"])  # no callback: the scheduled job and every test run like this
    assert events == ["claude"]


class _Stream:
    def __init__(self, tty):
        self._tty, self.lines = tty, []

    def isatty(self):
        return self._tty

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass


def test_the_command_shows_progress_only_to_a_person_at_a_terminal(monkeypatch):
    from claude_hr import ctl

    monkeypatch.setattr(ctl.sys, "stderr", _Stream(tty=False))
    assert ctl._update_progress() is None  # the scheduled job, a pipe
    monkeypatch.setattr(ctl.sys, "stderr", None)  # pythonw.exe
    assert ctl._update_progress() is None
    tty = _Stream(tty=True)
    monkeypatch.setattr(ctl.sys, "stderr", tty)
    say = ctl._update_progress()
    say("hello")
    assert "hello" in "".join(tty.lines)


def test_update_wires_the_progress_printer_into_the_run(sandbox, monkeypatch):
    from claude_hr import ctl

    got = {}

    class Fake(updater.Updater):
        def run(self, components=None):
            got["progress"] = self.progress
            return updater.Report()

    monkeypatch.setattr(updater, "Updater", Fake)
    tty = _Stream(tty=True)
    monkeypatch.setattr(ctl.sys, "stderr", tty)
    ctl.update_main(["--only", "proxy"])
    assert got["progress"] is not None
    assert "report follows at the end" in "".join(tty.lines)
    monkeypatch.setattr(ctl.sys, "stderr", _Stream(tty=False))
    ctl.update_main(["--only", "proxy"])
    assert got["progress"] is None


# ---- the helper does not reinstall over a tool environment that is in use ----------------------------------------------------
def test_the_helper_script_refuses_to_reinstall_over_a_running_tool_environment(sandbox):
    script = updater._windows_reinstall_script(["uv", "tool", "install"], 1, 2, "log.txt", env={})
    lines = script.splitlines()
    guard = next(i for i, ln in enumerate(lines) if ln.startswith("if ($inEnv.Count -gt 0)"))
    loop = next(i for i, ln in enumerate(lines) if ln.startswith("while ($rc -ne 0"))
    assert guard < loop, "the refusal comes before the first attempt"
    assert "NOT reinstalling" in lines[guard] and "$attempt = 3" in lines[guard]
    mark = str(updater.paths.state_dir() / "self-update.json")
    cleanup = next(ln for ln in lines if ln.startswith("if ($rc -ne 0) { Remove-Item"))
    assert updater._ps_quote(mark) in cleanup  # a reinstall that did not happen leaves no mark


@pytest.mark.skipif(os.name != "nt", reason="the helper is a Windows PowerShell script")
def test_the_real_helper_leaves_claude_hr_alone_while_the_tool_environment_is_in_use(
    tmp_path, monkeypatch, sandbox
):
    """A copy of cmd.exe running from inside the tool environment folder for far longer than the (shortened) wait: uv would
    fail and could leave the environment half-deleted, so the helper does not call it, and clears the retry mark."""
    import shutil

    env_dir = tmp_path / "tool-env" / "Scripts"
    env_dir.mkdir(parents=True)
    holder_exe = env_dir / "cmd.exe"
    shutil.copy2(
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), holder_exe
    )
    monkeypatch.setattr(updater, "_tool_env", lambda: str(tmp_path / "tool-env"))
    monkeypatch.setattr(updater, "_HOLDER_WAIT_S", 3)
    mark = updater.paths.state_dir() / "self-update.json"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text('{"ts": 1}', encoding="utf-8")
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    stamp = tmp_path / "reinstall-started.txt"
    holder = subprocess.Popen([str(holder_exe), "/c", "ping -n 40 127.0.0.1 > nul"])
    try:
        text = _run_helper(
            tmp_path, [cmd_exe, "/c", f"echo started > {stamp}"], tmp_path / "tool-env"
        )
    finally:
        holder.kill()
    assert not stamp.exists(), "the reinstall ran over a tool environment that was in use"
    assert "NOT reinstalling" in text and "cmd:" in text and "reinstall exit code" not in text
    assert not mark.exists()


@pytest.mark.skipif(os.name != "nt", reason="the helper is a Windows PowerShell script")
def test_the_real_helper_clears_the_retry_mark_after_a_reinstall_that_kept_failing(
    tmp_path, monkeypatch, sandbox
):
    monkeypatch.setattr(updater, "_tool_env", lambda: str(tmp_path / "no-tool-env"))
    mark = updater.paths.state_dir() / "self-update.json"
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text('{"ts": 1}', encoding="utf-8")
    cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    text = _run_helper(tmp_path, [cmd_exe, "/c", "exit 5"], tmp_path / "no-tool-env", fast=True)
    assert "retrying the reinstall (attempt 3 of 3)" in text and "NOT reinstalling" not in text
    assert not mark.exists()
    mark.write_text('{"ts": 1}', encoding="utf-8")
    _run_helper(tmp_path, [cmd_exe, "/c", "exit 0"], tmp_path / "no-tool-env", fast=True)
    assert mark.exists(), "a reinstall that worked is not the helper's to clear (the update does)"
