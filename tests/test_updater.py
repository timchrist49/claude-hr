import subprocess
from pathlib import Path

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
