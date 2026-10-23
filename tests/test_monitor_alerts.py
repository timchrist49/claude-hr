"""The ~/.claude size alert says when the size is a lower bound and when cleanup frees little."""

from __future__ import annotations

from claude_hr import config, monitor


# ---- the ~/.claude size alert ---------------------------------------------------------------------------------------------
def snap(**over):
    base = {
        "disk_free_gb": 100,
        "ram_total_gb": 64,
        "ram_free_gb": 30,
        "sessions": 0,
        "claude_dir_gb": 21.11,
        "proxy": {"enabled": False, "ok": True, "status": "off"},
    }
    return {**base, **over}


def test_the_size_alert_says_when_the_size_is_a_lower_bound_and_when_cleanup_frees_little(sandbox):
    cfg = config.load()
    complete = [
        a for a in monitor.evaluate(cfg, snap(claude_dir_complete=True)) if a.key == "claude-dir"
    ][0]
    assert "21.11 GB." in complete.body and "at least" not in complete.body
    cut_short = [
        a for a in monitor.evaluate(cfg, snap(claude_dir_complete=False)) if a.key == "claude-dir"
    ][0]
    assert "at least 21.11 GB" in cut_short.body and "ran out of time" in cut_short.body
    for alert in (complete, cut_short):
        assert (
            "14 days" in alert.body
            and "jobs_retention_days" in alert.body
            and "may free little" in alert.body
        )
    # a snapshot written before this field existed still evaluates as a complete scan
    assert [a for a in monitor.evaluate(cfg, snap()) if a.key == "claude-dir"]


def test_the_size_info_remembers_whether_the_scan_finished(sandbox, monkeypatch):
    monkeypatch.setattr(
        monitor.sysinfo, "dir_size_info", lambda root, budget_s=20.0: (21.11, False)
    )
    assert monitor._claude_dir_info() == (21.11, False)
    monkeypatch.setattr(
        monitor.sysinfo, "dir_size_info", lambda root, budget_s=20.0: (1.0, True)
    )  # cached for 6 hours
    assert monitor._claude_dir_info() == (21.11, False) and monitor._claude_dir_gb() == 21.11


def test_a_snapshot_without_the_scan_flag_reads_as_a_complete_scan(sandbox):
    alert = [a for a in monitor.evaluate(config.load(), snap()) if a.key == "claude-dir"][0]
    assert "at least" not in alert.body and "21.11 GB." in alert.body


# ---- Claude's shared cgroup scope: alerts, per-tick deltas, self-heal (Linux) -----------------------------------------
import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402

from claude_hr import cgroups, resources, sysinfo  # noqa: E402


def row(**over):
    base = {
        "name": "claude-hr-4242.scope",
        "current_gb": 1.0,
        "used_gb": 1.0,
        "high_gb": 4.0,
        "max_gb": 5.0,
        "pids": 40,
        "events_high": 0,
        "events_max": 0,
        "oom_kill": 0,
        "high_new": 0,
        "max_new": 0,
        "oom_new": 0,
    }
    return {**base, **over}


def keys(alerts):
    return {a.key for a in alerts}


def test_a_scope_near_its_soft_limit_is_reported_with_what_it_holds(sandbox):
    cfg = config.load()
    assert "scope-full" not in keys(
        monitor.evaluate(cfg, snap(scopes=[row(used_gb=3.5)]))
    )  # 87% of 4 GB
    assert "scope-full" in keys(
        monitor.evaluate(cfg, snap(scopes=[row(used_gb=3.6)]))
    )  # exactly 90%
    (alert,) = [
        a for a in monitor.evaluate(cfg, snap(scopes=[row(used_gb=3.7)])) if a.key == "scope-full"
    ]
    assert "claude-hr-4242.scope 3.7 of 4.0 GB in use, 40 tasks" in alert.body
    assert "throttles everything in the scope" in alert.body and "maxWorkers" in alert.body
    # without a soft limit the hard cap is the ceiling (and what happens there is a kill); with neither there is nothing to be near
    (hard,) = [
        a
        for a in monitor.evaluate(cfg, snap(scopes=[row(used_gb=4.8, high_gb=0)]))
        if a.key == "scope-full"
    ]
    assert "kills a process" in hard.body and "throttles" not in hard.body
    assert "scope-full" not in keys(
        monitor.evaluate(cfg, snap(scopes=[row(used_gb=99, high_gb=0, max_gb=0)]))
    )


def test_reclaimable_cache_does_not_make_a_scope_look_full(sandbox):
    """A scope that lives for days keeps clean page cache up to its soft limit: 4.0 of 4.0 GB by memory.current, nothing in use."""
    cfg = config.load()
    cached = row(current_gb=4.0, used_gb=0.2, high_new=2189)
    assert not keys(monitor.evaluate(cfg, snap(scopes=[cached]))) & {
        "scope-full",
        "scope-capped",
        "scope-oom",
    }
    old_snapshot = row(current_gb=3.9)  # written by a release that did not know the working set
    del old_snapshot["used_gb"]
    assert "scope-full" in keys(monitor.evaluate(cfg, snap(scopes=[old_snapshot])))


def test_the_warning_share_is_configurable(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_MONITOR__SCOPE_WARN_PCT", "50")
    assert "scope-full" in keys(monitor.evaluate(config.load(), snap(scopes=[row(used_gb=2.1)])))


def test_a_kill_is_told_apart_from_reaching_the_cap_and_going_over_the_soft_limit_alone_is_not_news(
    sandbox,
):
    cfg = config.load()
    killed = monitor.evaluate(cfg, snap(scopes=[row(oom_new=1, max_new=3)]))
    assert "scope-oom" in keys(killed) and "scope-capped" not in keys(killed)
    (capped,) = [
        a for a in monitor.evaluate(cfg, snap(scopes=[row(max_new=2)])) if a.key == "scope-capped"
    ]
    assert "reached its hard cap 2 time(s)" in capped.body
    # the soft limit's counter includes cache pressure: on its own it is not news
    assert not keys(monitor.evaluate(cfg, snap(scopes=[row(high_new=5000)]))) & {
        "scope-full",
        "scope-capped",
        "scope-oom",
    }


def test_the_machine_stalling_for_memory_or_io_is_reported_but_cpu_alone_is_not(sandbox):
    cfg = config.load()
    (alert,) = [
        a
        for a in monitor.evaluate(
            cfg, snap(pressure={"memory": {"some": 34.0}, "io": {"some": 2.0}})
        )
        if a.key == "pressure"
    ]
    assert "memory 34% of the last minute" in alert.body and "io 2%" not in alert.body
    assert "pressure" in keys(monitor.evaluate(cfg, snap(pressure={"io": {"some": 25.0}})))
    assert "pressure" not in keys(
        monitor.evaluate(cfg, snap(pressure={"cpu": {"some": 90.0}, "memory": {"some": 5.0}}))
    )
    assert "pressure" not in keys(
        monitor.evaluate(cfg, snap())
    )  # a snapshot from another OS or an older release


def test_a_stall_in_the_middle_of_the_interval_is_caught_by_the_share_since_the_last_tick(sandbox):
    cfg = config.load()
    quiet_now = {"memory": {"some": 0.0}}  # the 60 second average has long since decayed
    (alert,) = [
        a
        for a in monitor.evaluate(cfg, snap(pressure=quiet_now, stall_pct={"memory": 33.0}))
        if a.key == "pressure"
    ]
    assert "memory 33% of the time since the last check" in alert.body
    assert "pressure" not in keys(
        monitor.evaluate(cfg, snap(pressure=quiet_now, stall_pct={"memory": 3.0}))
    )


def test_the_stall_threshold_is_configurable_and_exact(sandbox, monkeypatch):
    cfg = config.load()
    assert "pressure" in keys(
        monitor.evaluate(cfg, snap(pressure={"memory": {"some": 20.0}}))
    )  # at the limit
    assert "pressure" not in keys(monitor.evaluate(cfg, snap(pressure={"memory": {"some": 19.9}})))
    monkeypatch.setenv("CLAUDE_HR_MONITOR__PSI_WARN_PCT", "50")
    assert "pressure" not in keys(
        monitor.evaluate(config.load(), snap(pressure={"memory": {"some": 34.0}}))
    )
    assert "pressure" in keys(monitor.evaluate(config.load(), snap(stall_pct={"io": 50.0})))


def test_the_stall_share_comes_from_the_counters_and_survives_resets(sandbox):
    prev = {"ts": 1000.0, "stall_us": {"memory": 10e6, "io": 0.0}}
    share = monitor._stall_share(prev, 1100.0, {"memory": 50e6, "io": 0.0})  # 40 s stalled in 100 s
    assert share == {"memory": 40.0, "io": 0.0}
    assert (
        monitor._stall_share(prev, 1100.0, {"memory": 5e6}) == {}
    )  # counters restarted (reboot): no figure
    assert (
        monitor._stall_share(prev, 1100.0, {"memory": 900e6})["memory"] == 100.0
    )  # never above 100
    assert monitor._stall_share({}, 1100.0, {"memory": 1.0}) == {}  # first tick
    assert monitor._stall_share({"ts": 1000.0}, 1100.0, {"memory": 1.0}) == {}
    assert (
        monitor._stall_share(prev, 1000.0 + 7 * 3600, {"memory": 50e6}) == {}
    )  # the last reading is stale
    assert monitor._stall_share({"ts": "x", "stall_us": {}}, 1100.0, {"memory": 1.0}) == {}


def test_the_snapshot_turns_the_counters_into_a_share_of_the_time_since_the_last_one(
    sandbox, monkeypatch
):
    before = time.time() - 100
    monitor.write_json(
        monitor.paths.state_dir() / "health.json", {"ts": before, "stall_us": {"memory": 0.0}}
    )
    monkeypatch.setattr(cgroups, "scopes", lambda: [])
    monkeypatch.setattr(
        cgroups,
        "pressure",
        lambda: {"memory": {"some": 1.0, "some_us": 40e6, "full": 0.5, "full_us": 1e6}},
    )
    out = monitor.snapshot(config.load())
    assert 38.0 <= out["stall_pct"]["memory"] <= 41.0
    assert out["pressure"] == {
        "memory": {"some": 1.0, "full": 0.5}
    }  # the averages, without the raw counters
    assert out["stall_us"] == {"memory": 40e6}


# ---- the per-tick event counts ----------------------------------------------------------------------------------------
def scope_at(name="claude-hr-4242.scope", high=0, mx=0, oom=0):
    return cgroups.Scope(name, 1.0, 4.0, 5.0, 3, {"high": high, "max": mx, "oom_kill": oom})


def test_events_are_counted_since_the_previous_tick(sandbox, monkeypatch):
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope_at(high=120, mx=10, oom=2)])
    first = monitor._scope_rows({})
    assert (first[0]["high_new"], first[0]["max_new"], first[0]["oom_new"]) == (
        0,
        0,
        0,
    )  # no previous reading: history
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope_at(high=150, mx=14, oom=3)])
    second = monitor._scope_rows({"scopes": first})
    assert (second[0]["high_new"], second[0]["max_new"], second[0]["oom_new"]) == (30, 4, 1)
    assert second[0]["events_high"] == 150
    # a counter that went DOWN (a restarted scope under the same name) is not a negative count
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope_at(high=3, mx=1, oom=0)])
    third = monitor._scope_rows({"scopes": second})
    assert (third[0]["high_new"], third[0]["max_new"], third[0]["oom_new"]) == (0, 0, 0)


def test_a_scope_that_appeared_between_two_ticks_brings_its_whole_history(sandbox, monkeypatch):
    """After a reboot or a daemon restart the new scope starts at zero: everything it counts happened inside the window."""
    other = row(name="claude-hr-1111.scope")
    monkeypatch.setattr(
        cgroups, "scopes", lambda: [scope_at(name="claude-hr-9999.scope", high=5000, mx=3, oom=1)]
    )
    for known in ({"scopes": [other]}, {"scopes": []}):
        (fresh,) = monitor._scope_rows(known)
        assert (fresh["high_new"], fresh["max_new"], fresh["oom_new"]) == (5000, 3, 1)
    # ... but with no previous reading of the scopes at all, a scope that was already running has only history
    for unknown in ({}, {"scopes": "garbage"}, {"scopes": None}):
        (old,) = monitor._scope_rows(unknown)
        assert (old["high_new"], old["max_new"], old["oom_new"]) == (0, 0, 0)


def test_a_damaged_health_file_is_not_an_error(sandbox, monkeypatch):
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope_at(high=7)])
    for bad in ("x", None, [], {}, 3.5):
        damaged = {"scopes": [{"name": "claude-hr-4242.scope", "events_high": bad}, "junk", 5]}
        (out,) = monitor._scope_rows(damaged)
        assert out["name"] == "claude-hr-4242.scope" and out["high_new"] >= 0


def test_the_counts_survive_from_one_tick_to_the_next_through_health_json(sandbox, monkeypatch):
    """The whole chain: tick() writes health.json, the next snapshot reads it back, the alert fires on the difference."""
    from claude_hr.headroom import Headroom

    monkeypatch.setattr(Headroom, "binary", lambda self: None)
    monkeypatch.setattr(monitor, "deliver", lambda cfg, alert: None)
    events = {"oom": 0, "mx": 0}
    monkeypatch.setattr(
        cgroups, "scopes", lambda: [scope_at(high=900, mx=events["mx"], oom=events["oom"])]
    )
    monkeypatch.setattr(cgroups, "pressure", lambda: {})
    first = monitor.tick(config.load())
    assert not set(first["alerts"]) & {
        "scope-oom",
        "scope-capped",
    }  # 900 old throttles, nothing new
    events.update(oom=1, mx=2)
    second = monitor.tick(config.load())
    assert "scope-oom" in second["alerts"]
    third = monitor.tick(config.load())  # nothing happened since: the condition clears and re-arms
    assert "scope-oom" not in third["alerts"]


def test_the_snapshot_carries_the_scopes_for_the_next_tick(sandbox, monkeypatch):
    scope = cgroups.Scope("claude-hr-1.scope", 2.0, 4.0, 5.0, 7, {"high": 4}, working_gb=0.5)
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope])
    monkeypatch.setattr(cgroups, "pressure", lambda: {"memory": {"some": 1.5}})
    out = monitor.snapshot(config.load())
    assert out["scopes"][0]["name"] == "claude-hr-1.scope"
    assert (out["scopes"][0]["current_gb"], out["scopes"][0]["used_gb"]) == (2.0, 0.5)
    assert out["pressure"] == {"memory": {"some": 1.5}}


# ---- the self-heal: a stale wrapper is rewritten and the scope that is already running is raised, never lowered ----------
def grown_vm(monkeypatch):
    monkeypatch.setattr(resources.paths, "IS_LINUX", True)
    monkeypatch.setattr(monitor.paths, "IS_LINUX", True)
    monkeypatch.setattr(sysinfo, "cpu_count", lambda: 4)
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (7.56, 6.0))
    resources.wrapper_path().parent.mkdir(parents=True, exist_ok=True)
    resources.wrapper_path().write_text(resources.render_wrapper(config.load()), encoding="utf-8")
    monkeypatch.setattr(sysinfo, "cpu_count", lambda: 16)  # the resize
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (32.0, 28.0))


def fake_systemctl(monkeypatch, live):
    calls = []

    def systemctl(cmd, **k):
        calls.append(cmd)
        name = cmd[4]
        for i, sc in enumerate(live):
            if sc.name == name:  # what systemd does with the property
                live[i] = cgroups.Scope(name, sc.current_gb, 16.8, 22.4, sc.pids)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cgroups, "scopes", lambda: list(live))
    monkeypatch.setattr(cgroups.procutil, "run", systemctl)
    return calls


def test_the_monitor_follows_a_resized_vm(sandbox, monkeypatch):
    grown_vm(monkeypatch)
    live = [
        cgroups.Scope("claude-hr-4242.scope", 3.9, 3.97, 5.29, 40)
    ]  # the incident: a 4 GB soft limit under 15 jest workers
    calls = fake_systemctl(monkeypatch, live)
    note = monitor.heal_caps(config.load())
    assert (
        "process wrapper rewritten" in note
        and "claude-hr-4242.scope: MemoryMax=22937M MemoryHigh=17203M" in note
    )
    assert "-p MemoryMax=22937M" in resources.wrapper_path().read_text(encoding="utf-8")
    assert calls[0][:5] == [
        "systemctl",
        "--user",
        "set-property",
        "--runtime",
        "claude-hr-4242.scope",
    ]
    calls.clear()
    assert (
        monitor.heal_caps(config.load()) == "" and calls == []
    )  # now everything is current: nothing more to do


def test_every_running_scope_is_raised_not_just_the_first(sandbox, monkeypatch):
    grown_vm(monkeypatch)
    live = [
        cgroups.Scope("claude-hr-1.scope", 1.0, 3.97, 5.29, 4),
        cgroups.Scope("claude-hr-2.scope", 1.0, 3.97, 5.29, 4),
        cgroups.Scope("claude-hr-3.scope", 1.0, 16.8, 22.4, 4),  # already right
    ]
    calls = fake_systemctl(monkeypatch, live)
    monitor.heal_caps(config.load())
    assert [c[4] for c in calls] == ["claude-hr-1.scope", "claude-hr-2.scope"]


def test_a_wrapper_that_cannot_be_written_does_not_keep_the_running_scope_from_being_raised(
    sandbox, monkeypatch
):
    grown_vm(monkeypatch)
    live = [cgroups.Scope("claude-hr-4242.scope", 3.9, 3.97, 5.29, 40)]
    calls = fake_systemctl(monkeypatch, live)

    def read_only(cfg):
        raise PermissionError("~/.claude-hr/bin belongs to root")

    monkeypatch.setattr(resources, "refresh_wrapper", read_only)
    note = monitor.heal_caps(config.load())
    assert (
        "wrapper refresh failed: PermissionError" in note
        and "claude-hr-4242.scope: MemoryMax=22937M" in note
    )
    assert len(calls) == 1


def test_nothing_is_raised_where_there_is_no_wrapper_config(sandbox, monkeypatch):
    grown_vm(monkeypatch)
    calls = fake_systemctl(monkeypatch, [cgroups.Scope("claude-hr-1.scope", 1.0, 3.97, 5.29, 4)])
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MEMORY_MAX_GB", "0")  # caps switched off
    assert monitor.heal_caps(config.load()) == "" and calls == []


def test_the_cap_heal_can_be_switched_off_and_never_breaks_the_tick(sandbox, monkeypatch):
    grown_vm(monkeypatch)
    before = resources.wrapper_path().read_text(encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HR_MONITOR__HEAL_RESOURCE_CAPS", "false")
    assert (
        monitor.heal_caps(config.load()) == ""
        and resources.wrapper_path().read_text(encoding="utf-8") == before
    )
    monkeypatch.delenv("CLAUDE_HR_MONITOR__HEAL_RESOURCE_CAPS")
    monkeypatch.setattr(monitor.paths, "IS_LINUX", False)  # Windows and macOS have no scopes at all
    assert monitor.heal_caps(config.load()) == ""


def test_a_tick_heals_the_caps_before_it_looks_at_them(sandbox, monkeypatch):
    order = []
    monkeypatch.setattr(
        monitor, "heal_caps", lambda cfg: order.append("heal") or "wrapper rewritten"
    )
    monkeypatch.setattr(monitor, "snapshot", lambda cfg: order.append("snapshot") or snap(ts=1.0))
    out = monitor.tick(config.load())
    assert order == ["heal", "snapshot"] and "wrapper rewritten" in out["note"]
    health = json.loads((monitor.paths.state_dir() / "health.json").read_text(encoding="utf-8"))
    assert health["note"] == "wrapper rewritten"


def test_a_damaged_health_file_does_not_stop_the_monitor(sandbox, monkeypatch):
    """health.json is only rewritten at the END of a tick: if reading it raised, a damaged file would stop every later tick."""
    monkeypatch.setattr(cgroups, "scopes", lambda: [scope_at(high=7)])
    monkeypatch.setattr(cgroups, "pressure", lambda: {})
    target = monitor.paths.state_dir() / "health.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    for damaged in ("{not json", "[]", "5", '"x"', "null"):
        target.write_text(damaged, encoding="utf-8")
        out = monitor.snapshot(config.load())
        assert out["scopes"][0]["high_new"] == 0 and out["stall_pct"] == {}
    target.write_text("{not json", encoding="utf-8")
    from claude_hr.headroom import Headroom

    monkeypatch.setattr(Headroom, "binary", lambda self: None)
    monitor.tick(config.load())
    assert isinstance(
        json.loads(target.read_text(encoding="utf-8")), dict
    )  # and the tick repaired it
