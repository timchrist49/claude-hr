import json
import subprocess
from pathlib import Path

import pytest

from claude_hr import config, resources, updater


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
    monkeypatch.setattr(updater.procutil, "run", lambda *a, **k: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    u = make(sandbox, monkeypatch, dry=True)
    res = u.update_claude()
    assert res.action == "available"
    assert calls == []


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
    monkeypatch.setattr(updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    u = make(sandbox, monkeypatch, force=True)
    seq = iter(["2.1.292", "2.1.300"])
    monkeypatch.setattr(u, "claude_version", lambda: next(seq))
    assert u.update_claude().action == "updated"
    resources.unregister_session(__import__("os").getpid())


def test_failed_install_rolls_back(sandbox, monkeypatch):
    log = []

    def fake_run(cmd, **k):
        log.append(cmd[1:3])
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == "install" and cmd[2] == "2.1.300" else 0, "", "boom")

    monkeypatch.setattr(updater.procutil, "run", fake_run)
    u = make(sandbox, monkeypatch)
    res = u.update_claude()
    assert res.action == "rolled-back"
    assert ["install", "2.1.292"] in log  # previous version restored


def test_wrong_version_after_install_rolls_back(sandbox, monkeypatch):
    monkeypatch.setattr(updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))
    u = make(sandbox, monkeypatch)  # claude_version keeps reporting the old version
    assert u.update_claude().action == "rolled-back"


def test_unmanaged_updates_are_left_alone(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__MANAGE_UPDATES", "false")
    res = make(sandbox, monkeypatch).update_claude()
    assert res.action == "none"


def test_component_failure_does_not_block_others(sandbox, monkeypatch):
    u = make(sandbox, monkeypatch, dry=True)
    monkeypatch.setattr(u, "update_headroom", lambda: (_ for _ in ()).throw(RuntimeError("pypi down")))
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

    ts = dt.datetime.fromtimestamp(now - age_days * DAY, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
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

    monkeypatch.setattr(Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: "0.40.1")
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
        Headroom, "latest_version", lambda self, timeout=15.0, cooldown_days=0.0: seen.setdefault("cd", cooldown_days) or "0.41.0"
    )
    monkeypatch.setattr(Headroom, "install", lambda self, version=None, upgrade=True, exclude_newer=None: subprocess.CompletedProcess([], 1, "", "x"))
    updater.Updater(config.load(), dry=True, force=True).update_headroom()
    assert seen["cd"] == 0.0


def test_non_native_installs_are_not_touched(sandbox, monkeypatch):
    u = updater.Updater(config.load())  # real is_native_install() on a path that is not the native layout
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
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


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
    subprocess.run(["git", "clone", "-q", str(origin), str(managed)], check=True, capture_output=True)
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

    monkeypatch.setattr(launcher, "_daemon_status", lambda claude: {"running": True, "pid": 1, "starter_pid": 2, "workers": 2})
    res = make(sandbox, monkeypatch).update_claude()
    assert res.action == "deferred"
    assert "background" in res.detail


def test_update_does_not_treat_unknown_daemon_state_as_idle(sandbox, monkeypatch):
    from claude_hr import launcher

    monkeypatch.setattr(launcher, "_daemon_status", lambda claude: {"running": True, "pid": 1, "starter_pid": 2, "workers": -1})
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
    monkeypatch.setattr(updater, "_spawn_detached", lambda cmd, log=None: spawned.append(cmd))
    u = updater.Updater(config.load())
    res = u.update_self()
    assert res.action == "updated" and res.target == "v0.2.0"
    assert spawned == [], "update_self started the reinstall itself"
    assert u._pending_reinstall and u._pending_reinstall[1:3] == ["tool", "install"]
    assert _git(managed, "describe", "--tags", "--always") == "v0.2.0"  # the checkout (and so the plugin source) is already new


def test_run_starts_the_reinstall_after_plugins_were_updated(sandbox, monkeypatch):
    """Windows regression: `self` then `plugins` in one process used to leave the tool environment empty."""
    events = []
    u = updater.Updater(config.load())

    def fake_self():
        u._pending_reinstall = ["uv", "tool", "install", "--force", "--reinstall", "/repo"]
        events.append("self")
        return updater.Result("self", action="updated")

    monkeypatch.setattr(u, "update_self", fake_self)
    monkeypatch.setattr(u, "update_plugins", lambda: events.append("plugins") or updater.Result("plugins"))
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)  # this test is about ordering, not the Windows helper
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log: False)  # fall through to the plain spawn
    monkeypatch.setattr(updater, "_spawn_detached", lambda cmd, log=None: events.append("reinstall"))
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
    u._pending_reinstall = [r"C:\Users\O'Neil\bin\uv.exe", "tool", "install", "--force", "--reinstall", r"C:\Users\O'Neil\.claude-hr\repo"]
    __import__("shutil").rmtree(updater.paths.hr_home(), ignore_errors=True)  # the updater must create state/ and logs/ itself
    return u


def test_windows_reinstall_runs_from_a_scheduled_task_not_as_our_child(sandbox, monkeypatch):
    """uv's claude-hr-ctl.exe trampoline kills everything in its job when it exits, so a child of ours never reinstalls."""
    started, spawned = [], []
    monkeypatch.setattr(updater, "_start_helper_task", lambda shell, script: started.append((shell, script)) or True)
    monkeypatch.setattr(updater, "_spawn_detached", lambda cmd, log=None: spawned.append(cmd))
    u = _windows_updater(monkeypatch)
    u._launch_reinstall()
    assert spawned == []
    shell, script_path = started[0]
    assert shell.endswith("powershell.exe") and script_path.name == "self-update.ps1"
    raw = script_path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, or PowerShell 5.1 reads the file as ANSI
    text = raw.decode("utf-8-sig")
    assert text.index("Wait-Process") < text.index("'tool'")  # waits for this process first, then reinstalls
    assert "claude-hr*" in text  # also waits for the claude-hr-ctl.exe trampoline that started this process


def test_windows_reinstall_falls_back_to_a_direct_helper_if_the_scheduler_refuses(sandbox, monkeypatch):
    import base64

    spawned = []
    monkeypatch.setattr(updater, "_start_helper_task", lambda shell, script: False)
    monkeypatch.setattr(updater, "_spawn_detached", lambda cmd, log=None: spawned.append(cmd))
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
    monkeypatch.setattr(updater.procutil, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "denied"))
    assert updater._start_helper_task("powershell.exe", tmp_path / "x.ps1") is False


@pytest.mark.real_helpers
def test_detached_helper_breaks_away_from_the_trampoline_job_and_logs(sandbox, monkeypatch, tmp_path):
    """uv's claude-hr-ctl.exe trampoline kills its job on exit; the reinstall helper must survive it and say what it did."""
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            raise OSError("breakaway not permitted")  # the parent job forbids it: fall back to a plain detached start

    monkeypatch.setattr(updater.paths, "IS_WINDOWS", True)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    log = tmp_path / "self-update.log"
    updater._spawn_detached(["powershell", "-File", "x"], log)
    assert calls[0][1]["creationflags"] & 0x01000000  # CREATE_BREAKAWAY_FROM_JOB first
    assert calls[0][1]["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert not calls[1][1]["creationflags"] & 0x01000000  # fallback without it
    assert log.exists() and calls[1][1]["stdout"] is not subprocess.DEVNULL  # output goes to the log, not nowhere


def test_reinstall_script_recreates_the_uv_environment(sandbox):
    """Regression: a task started by Task Scheduler does not inherit UV_TOOL_DIR, so the helper installed claude-hr into the
    default tool directory instead of the one the updater was running from."""
    env = {"UV_TOOL_DIR": r"D:\x'y\tools", "UV_TOOL_BIN_DIR": r"D:\bin", "Path": r"C:\a;C:\b", "UNRELATED": "no", "CLAUDE_CONFIG_DIR": "no"}
    lines = updater._windows_reinstall_script(["uv", "tool", "install"], 1, 2, "log.txt", env=env).splitlines()
    assert r"$env:UV_TOOL_DIR = 'D:\x''y\tools'" in lines
    assert r"$env:UV_TOOL_BIN_DIR = 'D:\bin'" in lines
    assert r"$env:Path = 'C:\a;C:\b'" in lines
    assert not any("UNRELATED" in ln or "CLAUDE_CONFIG_DIR" in ln for ln in lines)
    last_env = max(i for i, ln in enumerate(lines) if ln.startswith("$env:"))
    assert last_env < next(i for i, ln in enumerate(lines) if ln.startswith("& "))  # set before uv runs


def test_reinstall_script_logs_each_step_and_quotes_awkward_paths():
    script = updater._windows_reinstall_script(
        [r"C:\Users\O'Neil\uv.exe", "tool", "install", r"D:\a b\repo"], 111, 222, r"C:\Users\O'Neil\logs\self-update.log", env={})
    lines = script.splitlines()
    assert "$ids = @(111)" in lines and any("Get-Process -Id 222" in ln for ln in lines)
    assert any(ln.startswith("Wait-Process -Id $ids") for ln in lines)
    uv_line = next(ln for ln in lines if ln.startswith("& "))
    assert uv_line.startswith(r"& 'C:\Users\O''Neil\uv.exe' 'tool' 'install' 'D:\a b\repo' 2>&1 | ForEach-Object")
    assert "Add-Content -LiteralPath $log -Encoding UTF8" in uv_line  # ONE encoding in the log (5.1: `*>>` wrote UTF-16)
    assert "-Encoding UTF8" in next(ln for ln in lines if ln.startswith("function Log"))
    assert "$log = 'C:\\Users\\O''Neil\\logs\\self-update.log'" in lines
    assert any("reinstall exit code" in ln for ln in lines)
    # it removes its own script (it may hold the user's PATH) and then the one-shot task that started it
    assert any("Remove-Item -LiteralPath $PSCommandPath" in ln for ln in lines)
    assert lines[-1].startswith("Unregister-ScheduledTask -TaskName 'selfupdate' -TaskPath '\\claude-hr\\'")


def test_helper_environment_never_carries_credentials():
    """The helper script is a file on disk: no tokens, passwords or URLs with embedded credentials may be written into it."""
    env = {
        "UV_TOOL_DIR": r"D:\tools", "UV_CACHE_DIR": r"D:\cache", "Path": r"C:\a;C:\b",
        "UV_INDEX_URL": "https://user:pw@mirror.example/simple", "UV_INDEX_CORP_PASSWORD": "pw", "UV_PUBLISH_TOKEN": "t",
        "UV_GITHUB_TOKEN": "t", "UV_INDEX_CORP_USERNAME": "u", "UV_KEYRING_PROVIDER": "subprocess", "UV_INDEX": "https://ok.example/simple",
        "GITHUB_TOKEN": "t", "HOME": "x",
    }
    kept = updater._helper_env(env)
    assert kept == {"UV_TOOL_DIR": r"D:\tools", "UV_CACHE_DIR": r"D:\cache", "Path": r"C:\a;C:\b", "UV_INDEX": "https://ok.example/simple"}
    script = updater._windows_reinstall_script(["uv"], 1, 2, "log.txt", env=env)
    for secret in ("user:pw", "PASSWORD", "UV_PUBLISH_TOKEN", "GITHUB_TOKEN", "USERNAME", "KEYRING"):
        assert secret not in script


def test_powershell_quoting_doubles_every_kind_of_single_quote():
    """PowerShell also closes a string at U+2018/2019/201A/201B: one in a path would end the literal early (and could inject)."""
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        quoted = updater._ps_quote(f"C:\\Users\\Tim{q}s\\bin")
        assert quoted == f"'C:\\Users\\Tim{q}{q}s\\bin'"
    assert updater._ps_quote("plain") == "'plain'"


@pytest.mark.skipif(__import__("shutil").which("powershell") is None and __import__("shutil").which("pwsh") is None, reason="needs PowerShell")
def test_generated_helper_script_parses_with_hostile_values(tmp_path):
    """Render the script with awkward values and let the real PowerShell parser judge it (parse only, nothing is run)."""
    shell = __import__("shutil").which("powershell") or __import__("shutil").which("pwsh")
    env = {"UV_TOOL_DIR": "C:\\Users\\Tim\u2019s\\t'ools", "Path": "C:\\a b;C:\\\u201bq;$(calc);`n", "UV_X": "line1\nline2"}
    script = updater._windows_reinstall_script(
        ["C:\\Users\\Tim\u2019s\\uv.exe", "tool", "install", "D:\\a $(calc) b\\repo"], 1, 2, "C:\\l\u2018og\\x.log", env=env)
    ps1 = tmp_path / "helper.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + script.encode("utf-8"))
    check = (
        "$e = $null; $t = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{ps1}', [ref]$t, [ref]$e); if ($e.Count -eq 0) {{ 'PARSE_OK' }} else {{ $e | ForEach-Object {{ $_.Message }} }}"
    )
    out = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check], capture_output=True, text=True, timeout=60).stdout
    assert "PARSE_OK" in out, out


@pytest.mark.real_helpers
def test_systemd_service_survives_the_timer_job_and_gets_the_uv_environment(sandbox, monkeypatch):
    from claude_hr import resources

    seen = {}
    monkeypatch.setattr(updater.paths, "IS_LINUX", True)
    monkeypatch.setattr(resources, "caps_available", lambda: True)
    monkeypatch.setattr(updater.procutil, "run", lambda cmd, **k: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setenv("UV_TOOL_DIR", "/srv/tools")
    monkeypatch.setenv("UV_PUBLISH_TOKEN", "secret")
    assert updater._start_systemd_service(["uv", "tool", "install"], "/tmp/self-update.log") is True
    cmd = seen["cmd"]
    assert cmd[:3] == ["systemd-run", "--user", "--quiet"]
    assert any(a.startswith("--unit=claude-hr-selfupdate-") for a in cmd)
    assert "StandardOutput=append:/tmp/self-update.log" in cmd and "--setenv=UV_TOOL_DIR=/srv/tools" in cmd
    assert not any("secret" in a or "PUBLISH_TOKEN" in a for a in cmd)
    assert cmd[-4:] == ["--", "uv", "tool", "install"] or cmd[cmd.index("--") + 1:] == ["uv", "tool", "install"]
    monkeypatch.setattr(resources, "caps_available", lambda: False)
    assert updater._start_systemd_service(["uv"], "/tmp/x.log") is False  # no user manager: caller falls back to a plain spawn


def test_posix_reinstall_prefers_a_systemd_unit_and_falls_back_to_a_plain_spawn(sandbox, monkeypatch):
    events = []
    monkeypatch.setattr(updater.paths, "IS_WINDOWS", False)
    u = updater.Updater(config.load())
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log: events.append("unit") or True)
    monkeypatch.setattr(updater, "_spawn_detached", lambda cmd, log=None: events.append("spawn"))
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall()
    assert events == ["unit"]
    monkeypatch.setattr(updater, "_start_systemd_service", lambda cmd, log: events.append("unit") or False)
    u._pending_reinstall = ["uv", "tool", "install"]
    u._launch_reinstall()
    assert events == ["unit", "unit", "spawn"]


def _stub_uv(monkeypatch, found=True):
    real = updater.procutil.which_all
    monkeypatch.setattr(updater.procutil, "which_all", lambda name: ([r"/usr/bin/uv"] if found else []) if name == "uv" else real(name))


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
    (paths_state / "self-update.json").write_text(json.dumps({"ts": __import__("time").time()}), encoding="utf-8")
    res2 = u2.update_self()
    assert res2.action == "none" and "waiting" in res2.detail and u2._pending_reinstall is None
    # an up-to-date tool is left alone
    monkeypatch.setattr("claude_hr.__version__", "0.2.0")
    assert updater.Updater(config.load()).update_self().detail == ""
