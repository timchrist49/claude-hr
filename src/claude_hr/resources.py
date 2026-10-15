"""Resource governor: session registry, admission control, and hard limits per Claude session.

  * Admission: refuse (or warn) when too many claude-hr sessions run or free RAM is low.
  * Limits: Linux -> systemd-run scope (MemoryMax/CPUQuota/TasksMax) when available;
            Windows -> Job Object (memory cap + KILL_ON_JOB_CLOSE so orphaned children die);
            macOS -> nice only (the OS offers no per-process memory cap).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths, procutil, sysinfo
from .config import Config
from .jsonio import atomic_write_text, read_json


# ---------------------------------------------------------------------------------------------
# session registry
# ---------------------------------------------------------------------------------------------
def _sessions_dir() -> Path:
    d = paths.state_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_session(pid: int, cwd: str, kind: str = "interactive") -> Path:
    f = _sessions_dir() / f"{pid}.json"
    atomic_write_text(f, json.dumps({"pid": pid, "cwd": cwd, "kind": kind, "started": time.time()}) + "\n")
    return f


def unregister_session(pid: int) -> None:
    try:
        (_sessions_dir() / f"{pid}.json").unlink()
    except OSError:
        pass


def live_sessions() -> list[dict]:
    out = []
    try:
        files = list(_sessions_dir().glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            rec = read_json(f, None)
            alive = isinstance(rec, dict) and sysinfo.pid_alive(int(rec.get("pid", 0)))
        except Exception:  # a corrupt / half-written registry file must never block a launch
            alive, rec = False, None
        if alive:
            out.append(rec)
        else:
            try:
                f.unlink()
            except OSError:
                pass
    return out


# ---------------------------------------------------------------------------------------------
# admission control
# ---------------------------------------------------------------------------------------------
@dataclass
class Admission:
    allowed: bool
    limit: int
    running: int
    free_gb: float
    reason: str = ""
    reason_kind: str = ""  # "sessions" | "ram" | ""


def session_limit(cfg: Config) -> int:
    r = cfg.section("resources")
    raw = r.get("max_sessions", "auto")
    if isinstance(raw, int) or (isinstance(raw, str) and raw.isdigit()):
        return max(1, int(raw))
    total, _avail = sysinfo.memory_gb()
    cores = sysinfo.cpu_count()
    cap = int(r.get("session_cap", 6))
    if total <= 0:
        return max(1, min(cap, cores // 2))
    per = float(r.get("gb_per_session", 3.0))
    reserve = float(r.get("reserve_gb", 4.0))
    by_ram = int(max(0.0, total - reserve) // per)
    return max(1, min(cap, max(1, cores // 2), by_ram))


def _gb(value, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def memory_limits_gb(cfg: Config) -> tuple[float, float]:
    """(memory_max, memory_high) in GB per session tree. 0 = unlimited. 'auto' scales with RAM."""
    r = cfg.section("resources")
    total, _ = sysinfo.memory_gb()
    raw_max = r.get("memory_max_gb", "auto")
    if raw_max == "auto":
        # generous on purpose: the cap covers the whole tree (MCP servers, language servers, builds run by tools)
        mx = min(16.0, max(6.0, total * 0.40)) if total > 0 else 0.0
    else:
        mx = _gb(raw_max, 0.0)
    raw_high = r.get("memory_high_gb", "auto")
    hi = mx * 0.8 if raw_high == "auto" else _gb(raw_high, 0.0)
    return mx, (hi if hi < mx or mx == 0 else mx)


def admit(cfg: Config) -> Admission:
    r = cfg.section("resources")
    limit = session_limit(cfg)
    running = len(live_sessions())
    _, avail = sysinfo.memory_gb()
    min_free = float(r.get("min_free_gb", 2.0))
    if not bool(r.get("enabled", True)):
        return Admission(True, limit, running, avail)
    if running >= limit:
        return Admission(False, limit, running, avail, f"{running} sessions already running (limit {limit})", "sessions")
    if 0 < avail < min_free:
        return Admission(False, limit, running, avail, f"only {avail:.1f} GB RAM free (minimum {min_free:.1f} GB)", "ram")
    return Admission(True, limit, running, avail)


def child_env(cfg: Config, env: dict[str, str], kind: str = "session") -> dict[str, str]:
    """Env tweaks: cap subagent / tool fan-out; make unattended runs wait out rate limits."""
    r = cfg.section("resources")
    cores = sysinfo.cpu_count()
    cap = int(r.get("max_subagents", 0) or 0) or max(2, min(8, cores // 2))
    env.setdefault("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", str(cap))
    env.setdefault("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", str(max(2, min(10, cores))))
    if kind in {"headless", "daemon", "agents"} and bool(r.get("unattended_retry", True)):
        env.setdefault("CLAUDE_CODE_RETRY_WATCHDOG", "1")
        env.setdefault("CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS", str(int(r.get("retry_max_wait_min", 30)) * 60_000))
    return env


# ---------------------------------------------------------------------------------------------
# launching with hard limits
# ---------------------------------------------------------------------------------------------
def _user_bus_present() -> bool:
    """`systemd-run --user` talks to the user D-Bus session bus (package dbus-user-session). Its absence is
    common on minimal images and WSL; `systemctl --user` can still work there, so it is not a good probe."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return bool(rt) and os.path.exists(os.path.join(rt, "bus"))


def _systemd_scope_available() -> bool:
    """True only if a transient scope can really be created. Probed (not assumed) and cached for an hour,
    because the launcher exec()s systemd-run: a wrong guess would mean Claude never starts."""
    if not paths.IS_LINUX or not procutil.cmd_exists("systemd-run") or not _user_bus_present():
        return False
    cache = paths.state_dir() / "systemd-scope.json"
    c = read_json(cache, {}) or {}
    if time.time() - float(c.get("ts", 0)) < 3600 and "ok" in c:
        return bool(c["ok"])
    ok = procutil.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"], timeout=10).returncode == 0
    try:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        atomic_write_text(cache, json.dumps({"ts": time.time(), "ok": ok}))
    except OSError:
        pass
    return ok


def wrap_command_posix(cfg: Config, cmd: list[str]) -> list[str]:
    """Prefix `cmd` with the strongest available limiter on POSIX. Returns cmd unchanged if none."""
    r = cfg.section("resources")
    mx, hi = memory_limits_gb(cfg)
    cpu = int(r.get("cpu_quota_pct", 0) or 0)
    prefix: list[str] = []
    in_scope = paths.IS_LINUX and "claude-hr-" in _own_cgroup()
    if (mx > 0 or cpu > 0) and not in_scope and _systemd_scope_available():
        prefix = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        if mx > 0:
            prefix += ["-p", f"MemoryMax={int(mx * 1024)}M", "-p", "MemorySwapMax=0"]
            if hi > 0:
                prefix += ["-p", f"MemoryHigh={int(hi * 1024)}M"]
        if cpu > 0:
            prefix += ["-p", f"CPUQuota={cpu}%"]
        prefix += ["-p", "TasksMax=4096"]
        if bool(r.get("low_priority", False)):
            prefix += ["--nice=10"]
    elif bool(r.get("low_priority", False)) and procutil.cmd_exists("nice"):
        prefix += ["nice", "-n", "10"]
    return prefix + cmd


def _own_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text()
    except OSError:
        return ""


WRAPPER_TEMPLATE = """#!/bin/sh
# claude-hr process wrapper for Claude Code's own spawns (CLAUDE_CODE_PROCESS_WRAPPER): the agent-view
# background service and every session it hosts. Generated by `claude-hr-ctl setup`; do not edit.
# Contract (code.claude.com/docs/en/corporate-launcher): print nothing, pass env through, end with exec "$@".
case "$(cat /proc/self/cgroup 2>/dev/null)" in
  *claude-hr-*.scope) exec "$@" ;;   # already inside one of our scopes (nested self-spawn)
esac
command -v systemd-run >/dev/null 2>&1 || exec "$@"
[ -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ] || exec "$@"   # no user D-Bus session: run uncapped rather than fail
exec systemd-run --user --scope --quiet --collect --unit="claude-hr-$$" %(extra)s   -p MemoryMax=%(max)sM -p MemorySwapMax=0 %(high)s -p TasksMax=4096 -- "$@"
"""


def render_wrapper(cfg: Config) -> str | None:
    """Wrapper script for CLAUDE_CODE_PROCESS_WRAPPER (Linux only; ignored on Windows, no cgroups on macOS)."""
    if not paths.IS_LINUX:
        return None
    mx, hi = memory_limits_gb(cfg)
    if mx <= 0:
        return None
    # The wrapper wraps the background service too, and its sessions share that scope: size it for all of them.
    total, _ = sysinfo.memory_gb()
    agg = mx * min(session_limit(cfg), 4)
    if total > 0:
        agg = min(agg, total * 0.7)
    extra = "--nice=10" if bool(cfg.section("resources").get("low_priority", False)) else ""
    high = f"-p MemoryHigh={int(agg * 0.75 * 1024)}M"
    return WRAPPER_TEMPLATE % {"max": int(agg * 1024), "high": high, "extra": extra}


class WindowsJob:
    """Job Object that caps memory and kills every process in it when closed."""

    def __init__(self, memory_gb: float = 0.0, kill_on_close: bool = True):
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IOC),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._handle = k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("CreateJobObject failed")
        info = EXT()
        flags = 0
        if kill_on_close:
            flags |= 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_gb > 0:
            flags |= 0x0200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = int(memory_gb * 1024**3)
        info.BasicLimitInformation.LimitFlags = flags
        if not k32.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(f"SetInformationJobObject failed ({ctypes.get_last_error()})")

    def assign(self, pid: int) -> bool:
        h = self._k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not h:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(self._handle, h))
        finally:
            self._k32.CloseHandle(h)

    def close(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def set_windows_priority_below_normal(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x0200, False, pid)  # PROCESS_SET_INFORMATION
    if h:
        k32.SetPriorityClass(h, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        k32.CloseHandle(h)


def run_limited(cfg: Config, cmd: list[str], env: dict[str, str]) -> int:
    """Run `cmd` attached to this terminal with limits applied; return its exit code."""
    r = cfg.section("resources")
    enabled = bool(r.get("enabled", True))
    if paths.IS_WINDOWS:
        proc = subprocess.Popen(cmd, env=env)
        job = None
        if enabled:
            try:
                job = WindowsJob(memory_limits_gb(cfg)[0], bool(r.get("kill_on_exit", True)))
                job.assign(proc.pid)
            except OSError:
                job = None
            if bool(r.get("low_priority", False)):
                set_windows_priority_below_normal(proc.pid)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                return 130
        finally:
            if job:
                job.close()
    full = wrap_command_posix(cfg, cmd) if enabled else cmd
    proc = subprocess.Popen(full, env=env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue  # Ctrl-C is delivered to the child as well; keep waiting for it to exit
