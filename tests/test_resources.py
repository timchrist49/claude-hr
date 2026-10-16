import os

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
