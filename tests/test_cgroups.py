"""Claude's shared cgroup scope as the kernel reports it: parsed from a fake tree, raised only upwards, never an exception."""

from __future__ import annotations

import subprocess

import pytest

from claude_hr import cgroups

GIB = 1024**3


def make_scope(
    root,
    name="claude-hr-4242.scope",
    uid=1000,
    *,
    current=2 * GIB,
    high=str(4 * GIB),
    max_=str(6 * GIB),
    pids="12",
    events="low 0\nhigh 7\nmax 1\noom 0\noom_kill 0\n",
    cpu=None,
    stat=None,
    layout="app",
):
    """The files of one transient user scope, laid out as systemd 249 does (verified on a real one)."""
    base = root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
    d = base / "app.slice" / name if layout == "app" else base / name
    d.mkdir(parents=True)
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.high").write_text(f"{high}\n")
    (d / "memory.max").write_text(f"{max_}\n")
    (d / "pids.current").write_text(f"{pids}\n")
    (d / "memory.events").write_text(events)
    if cpu is not None:
        (d / "cpu.max").write_text(f"{cpu}\n")
    if stat is not None:
        (d / "memory.stat").write_text(stat)
    return d


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(cgroups.paths, "IS_LINUX", True)


def test_a_scope_is_read_back_the_way_the_kernel_reports_it(tmp_path, linux):
    make_scope(tmp_path)
    (sc,) = cgroups.scopes(tmp_path, uid=1000)
    assert sc.name == "claude-hr-4242.scope"
    assert (sc.current_gb, sc.high_gb, sc.max_gb, sc.pids) == (2.0, 4.0, 6.0, 12)
    assert sc.events == {"low": 0, "high": 7, "max": 1, "oom": 0, "oom_kill": 0}
    assert sc.ceiling_gb == 4.0  # the soft limit is what slows things down first
    assert (
        sc.cpu_max == "" and not sc.cpu_limited
    )  # no cpu.max file: the cpu controller is not delegated


def test_no_limit_is_zero_and_the_ceiling_falls_back_to_the_hard_cap(tmp_path, linux):
    make_scope(tmp_path, high="max", max_=str(6 * GIB))
    (sc,) = cgroups.scopes(tmp_path, uid=1000)
    assert sc.high_gb == 0.0 and sc.max_gb == 6.0 and sc.ceiling_gb == 6.0
    make_scope(tmp_path, name="claude-hr-1.scope", high="max", max_="max")
    both = {s.name: s for s in cgroups.scopes(tmp_path, uid=1000)}
    assert both["claude-hr-1.scope"].ceiling_gb == 0.0  # nothing to be near


def test_only_our_scopes_of_this_user_are_listed_in_either_layout(tmp_path, linux):
    make_scope(tmp_path, name="claude-hr-1.scope")
    make_scope(
        tmp_path, name="claude-hr-2.scope", layout="flat"
    )  # older systemd: straight under user@<uid>.service
    make_scope(tmp_path, name="run-r123.scope")  # a scope of something else
    make_scope(tmp_path, name="claude-hr-3.scope", uid=1001)  # somebody else's
    assert [s.name for s in cgroups.scopes(tmp_path, uid=1000)] == [
        "claude-hr-1.scope",
        "claude-hr-2.scope",
    ]


def test_cpu_max_tells_whether_a_cpu_limit_is_in_force(tmp_path, linux):
    make_scope(tmp_path, name="claude-hr-1.scope", cpu="200000 100000")
    make_scope(tmp_path, name="claude-hr-2.scope", cpu="max 100000")
    got = {s.name: s.cpu_limited for s in cgroups.scopes(tmp_path, uid=1000)}
    assert got == {"claude-hr-1.scope": True, "claude-hr-2.scope": False}


def test_unreadable_or_garbled_files_never_raise(tmp_path, linux):
    d = make_scope(tmp_path, current="garbage", pids="", events="not a table\nhigh x\n")
    (d / "memory.max").unlink()
    (sc,) = cgroups.scopes(tmp_path, uid=1000)
    assert (sc.current_gb, sc.max_gb, sc.pids, sc.events) == (0.0, 0.0, 0, {})
    assert cgroups.scopes(tmp_path / "missing", uid=1000) == []


def test_other_systems_have_no_scopes(tmp_path, monkeypatch):
    make_scope(tmp_path)
    monkeypatch.setattr(cgroups.paths, "IS_LINUX", False)
    assert cgroups.scopes(tmp_path, uid=1000) == []


def test_pressure_is_the_share_of_the_last_minute_and_the_time_stalled_since_boot(tmp_path):
    (tmp_path / "memory").write_text(
        "some avg10=1.00 avg60=34.50 avg300=3.00 total=99\nfull avg10=0.00 avg60=12.25 avg300=1.00 total=5\n"
    )
    (tmp_path / "cpu").write_text(
        "some avg10=0.00 avg60=2.00 avg300=0.00 total=1\n"
    )  # older kernels: no `full` for cpu
    assert cgroups.pressure(tmp_path) == {
        "memory": {"some": 34.5, "some_us": 99.0, "full": 12.25, "full_us": 5.0},
        "cpu": {"some": 2.0, "some_us": 1.0},
    }
    assert cgroups.pressure(tmp_path / "no-psi") == {}  # kernel without PSI


def test_the_working_set_leaves_out_the_cache_the_kernel_gives_back(tmp_path, linux):
    """Reproduced on a real cgroup: a scope that only read files sat at its soft limit with 99.5% clean cache and no pressure."""
    make_scope(
        tmp_path,
        current=4 * GIB,
        stat=f"anon 1000\nfile {4 * GIB}\ninactive_file {4 * GIB - 5_000_000}\n",
    )
    (sc,) = cgroups.scopes(tmp_path, uid=1000)
    assert (
        sc.current_gb == 4.0 and sc.used_gb < 0.01
    )  # 'full' by memory.current, empty by what it really holds
    assert sc.ceiling_gb == 4.0


def test_without_memory_stat_the_raw_figure_is_used(tmp_path, linux):
    make_scope(tmp_path, current=3 * GIB)  # no memory.stat file
    (sc,) = cgroups.scopes(tmp_path, uid=1000)
    assert sc.working_gb is None and sc.used_gb == 3.0
    make_scope(
        tmp_path, name="claude-hr-2.scope", current=3 * GIB, stat="anon 1\nfile 2\n"
    )  # no inactive_file line
    make_scope(
        tmp_path, name="claude-hr-3.scope", current=GIB, stat=f"inactive_file {2 * GIB}\n"
    )  # more cache than usage
    used = {s.name: s.used_gb for s in cgroups.scopes(tmp_path, uid=1000)}
    assert used["claude-hr-2.scope"] == 3.0 and used["claude-hr-3.scope"] == 0.0


class FakeRun:
    def __init__(self, rc=0):
        self.rc, self.calls = rc, []

    def __call__(self, cmd, timeout=60, env=None, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.rc, "", "")


def scope(**over):
    base = {
        "name": "claude-hr-4242.scope",
        "current_gb": 1.0,
        "high_gb": 4.0,
        "max_gb": 5.0,
        "pids": 3,
    }
    return cgroups.Scope(**{**base, **over})


def test_a_too_small_running_scope_is_raised_with_a_runtime_property(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(cgroups.procutil, "run", run)
    note = cgroups.raise_caps(scope(), max_mb=22937, high_mb=17203)
    assert run.calls == [
        [
            "systemctl",
            "--user",
            "set-property",
            "--runtime",
            "claude-hr-4242.scope",
            "MemoryMax=22937M",
            "MemoryHigh=17203M",
        ]
    ]
    assert "claude-hr-4242.scope" in note and "MemoryMax=22937M" in note


def test_caps_are_never_lowered_and_a_cap_that_is_not_there_is_not_invented(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(cgroups.procutil, "run", run)
    assert cgroups.raise_caps(scope(), max_mb=2048, high_mb=1536) == ""  # smaller machine: leave it
    assert (
        cgroups.raise_caps(scope(max_gb=5.0), max_mb=5200, high_mb=4096) == ""
    )  # within 5%: not a resize
    assert (
        cgroups.raise_caps(scope(high_gb=0.0, max_gb=0.0), max_mb=22937, high_mb=17203) == ""
    )  # unlimited stays so
    assert run.calls == []
    # only the cap that is too small is touched
    cgroups.raise_caps(scope(high_gb=20.0), max_mb=22937, high_mb=17203)
    assert run.calls[0][-1:] == ["MemoryMax=22937M"]


def test_a_soft_limit_the_config_no_longer_wants_is_lifted_but_none_is_invented(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(cgroups.procutil, "run", run)
    assert "MemoryHigh=infinity" in cgroups.raise_caps(scope(), max_mb=2048, high_mb=0)
    assert (
        run.calls[0][-1] == "MemoryHigh=infinity"
    )  # only that: the smaller hard cap is not lowered
    run.calls.clear()
    assert (
        cgroups.raise_caps(scope(high_gb=0.0), max_mb=2048, high_mb=0) == ""
    )  # it has none: nothing to lift
    assert run.calls == []


def test_the_soft_limit_alone_can_be_raised_and_has_its_own_five_percent(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(cgroups.procutil, "run", run)
    assert (
        cgroups.raise_caps(scope(max_gb=22.4, high_gb=4.0), max_mb=22937, high_mb=4096 + 100) == ""
    )  # +2.4%: noise
    cgroups.raise_caps(scope(max_gb=22.4, high_gb=4.0), max_mb=22937, high_mb=17203)
    assert run.calls[0][-1] == "MemoryHigh=17203M" and "MemoryMax=22937M" not in run.calls[0]


def test_systemctl_is_run_with_the_users_environment(monkeypatch):
    seen = {}

    def run(cmd, timeout=60, env=None, **kw):
        seen["env"] = env
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cgroups.procutil, "run", run)
    monkeypatch.setattr(cgroups, "user_env", lambda: {"XDG_RUNTIME_DIR": "/run/user/4242"})
    cgroups.raise_caps(scope(), max_mb=22937, high_mb=17203)
    assert seen["env"] == {
        "XDG_RUNTIME_DIR": "/run/user/4242"
    }  # cron has none, and systemctl --user needs it


def test_a_refusal_by_systemd_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(cgroups.procutil, "run", FakeRun(rc=1))
    assert cgroups.raise_caps(scope(), 22937, 17203).startswith("failed:")


def test_systemctl_gets_the_runtime_dir_cron_does_not_have(monkeypatch):
    monkeypatch.setattr(cgroups, "_uid", lambda: 4242)
    monkeypatch.setattr(cgroups.os.path, "exists", lambda p: p == "/run/user/4242/bus")
    assert cgroups.user_env({"PATH": "x"})["XDG_RUNTIME_DIR"] == "/run/user/4242"
    assert cgroups.user_env({"XDG_RUNTIME_DIR": "/elsewhere"})["XDG_RUNTIME_DIR"] == "/elsewhere"
    monkeypatch.setattr(cgroups.os.path, "exists", lambda p: False)
    assert "XDG_RUNTIME_DIR" not in cgroups.user_env({"PATH": "x"})
