"""Stdlib-only system probes: RAM, CPU, disk, process liveness and memory. Windows via ctypes."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import paths

GB = 1024**3


# ---------------------------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------------------------
def _win_memstatus() -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
    return int(stat.ullTotalPhys), int(stat.ullAvailPhys)


def _linux_memstatus() -> tuple[int, int]:
    total = avail = 0
    # Inside a container the cgroup limit is the real ceiling, not /proc/meminfo.
    cg_limit = None
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(p).read_text().strip()
            if raw.isdigit() and int(raw) < 1 << 60:
                cg_limit = int(raw)
                break
        except OSError:
            pass
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1]) * 1024
    if cg_limit and cg_limit < total:
        used = 0
        for p in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            try:
                used = int(Path(p).read_text().strip())
                break
            except (OSError, ValueError):
                pass
        total, avail = cg_limit, max(0, cg_limit - used)
    return total, avail


def _mac_memstatus() -> tuple[int, int]:
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip() or 0)
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = 4096
    free = inactive = spec = 0
    for line in out.splitlines():
        if "page size of" in line:
            page = int("".join(c for c in line.split("page size of")[1] if c.isdigit()) or 4096)
        key, _, val = line.partition(":")
        digits = "".join(c for c in val if c.isdigit())
        if not digits:
            continue
        if key == "Pages free":
            free = int(digits)
        elif key == "Pages inactive":
            inactive = int(digits)
        elif key == "Pages speculative":
            spec = int(digits)
    return total, (free + inactive + spec) * page


def memory_gb() -> tuple[float, float]:
    """(total_gb, available_gb). Returns (0, 0) when it cannot be determined."""
    try:
        if paths.IS_WINDOWS:
            total, avail = _win_memstatus()
        elif paths.IS_MAC:
            total, avail = _mac_memstatus()
        else:
            total, avail = _linux_memstatus()
        return total / GB, avail / GB
    except Exception:
        return 0.0, 0.0


def cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # respects cgroup/cpuset pinning on Linux
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def disk_free_gb(path: Path | str) -> float:
    try:
        probe = Path(path)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe).free / GB
    except OSError:
        return 0.0


def dir_size_info(path: Path, budget_s: float = 5.0) -> tuple[float, bool]:
    """(size_gb, complete). Iterative scandir walk that stops after budget_s seconds: ~/.claude can hold
    millions of tiny files (job scratch dirs with node_modules), so an unbounded walk would hang a scheduled job."""
    total = 0
    deadline = time.monotonic() + budget_s
    stack = [str(path)]
    complete = True
    while stack:
        if time.monotonic() > deadline:
            complete = False
            break
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total / GB, complete


def dir_size_gb(path: Path, budget_s: float = 5.0) -> float:
    return dir_size_info(path, budget_s)[0]


# ---------------------------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------------------------

def _k32():
    """kernel32 with correct 64-bit prototypes (default ctypes int args truncate HANDLEs)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32

def pid_alive(pid: int) -> bool:
    """Safe liveness check. NEVER uses os.kill on Windows (signal 0 there terminates the process)."""
    if pid <= 0:
        return False
    if paths.IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        k32 = _k32()
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_rss_mb(pid: int) -> float:
    """Resident set size of one process in MB (0 if unknown)."""
    try:
        if paths.IS_LINUX:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
        elif paths.IS_MAC:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
            return int(out.strip() or 0) / 1024
        else:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            k32 = _k32()
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            handle = k32.OpenProcess(0x1000 | 0x0010, False, pid)
            if not handle:
                return 0.0
            try:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    return pmc.WorkingSetSize / 1024 / 1024
            finally:
                k32.CloseHandle(handle)
    except Exception:
        pass
    return 0.0
