import os
import re

import pytest

from claude_hr import config, resources, sysinfo


def test_session_limit_scales_with_ram_and_is_capped(sandbox, monkeypatch):
    monkeypatch.setattr(sysinfo, "cpu_count", lambda: 20)
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 40.0))
    assert resources.session_limit(config.load()) == 6  # session_cap
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (16.0, 10.0))
    assert resources.session_limit(config.load()) == 4  # (16-4)/3
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (4.0, 2.0))
    assert resources.session_limit(config.load()) == 1


def test_session_limit_explicit_wins(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MAX_SESSIONS", "3")
    assert resources.session_limit(config.load()) == 3


def test_memory_limits_auto(sandbox, monkeypatch):
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 40.0))
    mx, hi = resources.memory_limits_gb(config.load())
    assert mx == 16.0  # 40% of 64 GB, capped
    assert abs(hi - 12.8) < 1e-6
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (8.0, 5.0))
    assert resources.memory_limits_gb(config.load())[0] == 6.0  # floor
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MEMORY_MAX_GB", "0")
    assert resources.memory_limits_gb(config.load()) == (0.0, 0.0)


def test_admission_refuses_when_full_or_low_on_ram(sandbox, monkeypatch):
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 40.0))
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MAX_SESSIONS", "1")
    resources.register_session(os.getpid(), "/x")
    a = resources.admit(config.load())
    assert not a.allowed
    assert "limit 1" in a.reason
    resources.unregister_session(os.getpid())
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 1.0))
    assert "RAM free" in resources.admit(config.load()).reason


def test_stale_sessions_are_pruned(sandbox):
    resources.register_session(999999, "/gone")
    assert resources.live_sessions() == []


def test_child_env_caps_fanout_and_unattended_retry(sandbox, monkeypatch):
    monkeypatch.setattr(sysinfo, "cpu_count", lambda: 8)
    env = resources.child_env(config.load(), {}, "headless")
    assert env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "4"
    assert env["CLAUDE_CODE_RETRY_WATCHDOG"] == "1"
    assert env["CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS"] == "1800000"
    assert "CLAUDE_CODE_RETRY_WATCHDOG" not in resources.child_env(config.load(), {}, "session")
    keep = resources.child_env(
        config.load(), {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2"}, "session"
    )
    assert (
        keep["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "2"
    )  # a user's explicit value is never overridden


def test_posix_prefix_noop_without_systemd(sandbox, monkeypatch):
    monkeypatch.setattr(resources, "_systemd_scope_available", lambda: False)
    assert resources.wrap_command_posix(config.load(), ["claude", "x"]) == ["claude", "x"]


def test_posix_prefix_with_systemd(sandbox, monkeypatch):
    monkeypatch.setattr(resources, "_systemd_scope_available", lambda: True)
    monkeypatch.setattr(resources.paths, "IS_LINUX", True)
    monkeypatch.setattr(resources, "_own_cgroup", lambda: "0::/user.slice/x.service")
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 40.0))
    cmd = resources.wrap_command_posix(config.load(), ["claude"])
    assert cmd[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
    assert "MemoryMax=16384M" in cmd
    assert cmd[-1] == "claude"
    monkeypatch.setattr(resources, "_own_cgroup", lambda: "0::/user.slice/claude-hr-1.scope")
    assert resources.wrap_command_posix(config.load(), ["claude"]) == ["claude"]  # no nested scopes


def test_wrapper_script_contract(sandbox, monkeypatch):
    monkeypatch.setattr(resources.paths, "IS_LINUX", True)
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (64.0, 40.0))
    script = resources.render_wrapper(config.load())
    assert script.startswith("#!/bin/sh")
    assert script.rstrip().endswith('-- "$@"')
    code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert sum('exec "$@"' in ln for ln in code) == 3  # every escape hatch ends in exec
    assert not any("echo" in ln for ln in code)  # must print nothing before exec


# ---- the process wrapper follows the machine ---------------------------------------------------------------------------
def machine(monkeypatch, ram, cpus):
    monkeypatch.setattr(resources.paths, "IS_LINUX", True)
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (ram, ram * 0.8))
    monkeypatch.setattr(sysinfo, "cpu_count", lambda: cpus)


def install_wrapper(cfg):
    dest = resources.wrapper_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(resources.render_wrapper(cfg), encoding="utf-8")
    return dest


def test_the_wrapper_cap_is_a_share_of_the_machine_it_was_written_for(sandbox, monkeypatch):
    machine(
        monkeypatch, 7.56, 4
    )  # the VM of the incident: one session fits, so the scope is 70% of RAM
    small = resources.wrapper_limits(config.load())
    assert small.max_mb == int(7.56 * 0.7 * 1024) and small.high_mb == int(7.56 * 0.7 * 0.75 * 1024)
    machine(
        monkeypatch, 32.0, 16
    )  # the same VM after a resize: four sessions' worth, still capped at 70% of RAM
    big = resources.wrapper_limits(config.load())
    assert big.max_mb == int(32.0 * 0.7 * 1024) and big.max_mb > 4 * small.max_mb
    assert (big.ram_gb, big.cpus) == (32.0, 16)


def test_the_wrapper_records_what_it_was_sized_for(sandbox, monkeypatch):
    machine(monkeypatch, 32.0, 16)
    script = resources.render_wrapper(config.load())
    assert "# sized for: RAM 32.0 GB, 16 CPUs: MemoryMax 22937M" in script
    assert (
        "-p MemoryMax=22937M -p MemorySwapMax=0 -p MemoryHigh=17203M -p TasksMax=4096 -- " in script
    )
    assert "CPUQuota" not in script  # resources.cpu_quota_pct is 0 by default


def test_the_wrapper_honours_the_cpu_quota_and_the_switched_off_soft_limit(sandbox, monkeypatch):
    machine(monkeypatch, 32.0, 16)
    monkeypatch.setenv(
        "CLAUDE_HR_RESOURCES__CPU_QUOTA_PCT", "200"
    )  # per session: the shared scope holds four
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MEMORY_HIGH_GB", "0")
    lim = resources.wrapper_limits(config.load())
    assert lim.cpu_pct == 800 and lim.high_mb == 0
    script = resources.render_wrapper(config.load())
    assert "-p CPUQuota=800%" in script and "-p MemoryHigh" not in script
    machine(
        monkeypatch, 32.0, 4
    )  # two sessions fit on 4 CPUs: 2 x 200% is exactly what the machine has
    assert resources.wrapper_limits(config.load()).cpu_pct == 400
    monkeypatch.setenv(
        "CLAUDE_HR_RESOURCES__CPU_QUOTA_PCT", "300"
    )  # 2 x 300% would be more than it has
    assert resources.wrapper_limits(config.load()).cpu_pct == 400  # never more than the machine has


def test_no_wrapper_where_there_are_no_caps(sandbox, monkeypatch):
    machine(monkeypatch, 32.0, 16)
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MEMORY_MAX_GB", "0")
    assert (
        resources.wrapper_limits(config.load()) is None
        and resources.render_wrapper(config.load()) is None
    )
    assert resources.wrapper_state(config.load()).state == "off"
    monkeypatch.setattr(resources.paths, "IS_LINUX", False)
    assert resources.wrapper_limits(config.load()) is None


def test_a_wrapper_written_for_a_smaller_machine_is_stale(sandbox, monkeypatch):
    machine(monkeypatch, 7.56, 4)
    cfg = config.load()
    assert resources.wrapper_state(cfg).state == "absent"  # nothing installed: that is setup's job
    install_wrapper(cfg)
    assert resources.wrapper_state(cfg).state == "ok"
    machine(monkeypatch, 32.0, 16)
    st = resources.wrapper_state(config.load())
    assert st.state == "stale"
    assert (
        st.installed["ram_gb"] == 7.6 and st.installed["cpus"] == 4 and st.wanted.ram_gb == 32.0
    )  # RAM is recorded to 0.1 GB


def test_a_few_megabytes_of_memtotal_noise_is_not_a_resize(sandbox, monkeypatch):
    machine(monkeypatch, 7.56, 4)
    install_wrapper(config.load())
    machine(monkeypatch, 7.55, 4)  # the same VM after a reboot
    assert resources.wrapper_state(config.load()).state == "ok"
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__CPU_QUOTA_PCT", "100")  # a changed setting is
    assert resources.wrapper_state(config.load()).state == "stale"


def test_a_wrapper_from_before_the_sizing_line_is_rewritten_once_and_quietly(sandbox, monkeypatch):
    machine(monkeypatch, 7.56, 4)
    dest = install_wrapper(config.load())
    dest.write_text(
        "\n".join(
            ln for ln in dest.read_text(encoding="utf-8").splitlines() if "sized for" not in ln
        )
        + "\n",
        encoding="utf-8",
    )
    st = resources.wrapper_state(config.load())
    assert st.state == "ok" and st.unmarked  # right for this machine: not a resize, not a warning
    note = resources.refresh_wrapper(config.load())
    assert "record what it was sized for" in note and "limits unchanged" in note
    assert not resources.wrapper_state(config.load()).unmarked and "sized for" in dest.read_text(
        encoding="utf-8"
    )
    assert resources.refresh_wrapper(config.load()) == ""  # and then it is left alone


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(("MemoryMax=", 300), id="hard cap"),
        pytest.param(("MemoryHigh=", 300), id="soft limit"),
    ],
)
def test_each_limit_that_differs_makes_the_wrapper_stale(sandbox, monkeypatch, edit):
    machine(monkeypatch, 32.0, 16)
    dest = install_wrapper(config.load())
    key, value = edit
    dest.write_text(
        re.sub(rf"{key}\d+M", f"{key}{value}M", dest.read_text(encoding="utf-8")), encoding="utf-8"
    )
    assert resources.wrapper_state(config.load()).state == "stale"


def test_the_setting_that_adds_or_removes_a_limit_makes_the_wrapper_stale(sandbox, monkeypatch):
    machine(monkeypatch, 32.0, 16)
    install_wrapper(config.load())
    assert resources.wrapper_state(config.load()).state == "ok"
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__LOW_PRIORITY", "true")  # --nice appears
    assert resources.wrapper_state(config.load()).state == "stale"
    monkeypatch.delenv("CLAUDE_HR_RESOURCES__LOW_PRIORITY")
    monkeypatch.setenv("CLAUDE_HR_RESOURCES__MEMORY_HIGH_GB", "0")  # the soft limit disappears
    assert resources.wrapper_state(config.load()).state == "stale"
    assert "--nice=10" in resources.render_wrapper(
        _with(monkeypatch, RESOURCES__LOW_PRIORITY="true")
    )


def _with(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(f"CLAUDE_HR_{k}", v)
    return config.load()


def test_refresh_rewrites_a_stale_wrapper_and_nothing_else(sandbox, monkeypatch):
    machine(monkeypatch, 7.56, 4)
    cfg = config.load()
    assert (
        resources.refresh_wrapper(cfg) == "" and not resources.wrapper_path().exists()
    )  # never installs
    dest = install_wrapper(cfg)
    assert resources.refresh_wrapper(cfg) == ""  # current: not touched
    machine(monkeypatch, 32.0, 16)
    note = resources.refresh_wrapper(config.load())
    assert "MemoryMax" in note and "32.0 GB RAM" in note
    assert "MemoryMax=22937M" in dest.read_text(encoding="utf-8")
    if os.name != "nt":
        assert dest.stat().st_mode & 0o111  # still executable
    foreign = '#!/bin/sh\nexec "$@"\n'  # a wrapper that is not ours is none of our business
    dest.write_text(foreign, encoding="utf-8")
    assert (
        resources.refresh_wrapper(config.load()) == ""
        and dest.read_text(encoding="utf-8") == foreign
    )
