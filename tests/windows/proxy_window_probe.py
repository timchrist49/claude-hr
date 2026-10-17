"""Starts the Headroom proxy through claude-hr's own Headroom.start() and watches the desktop for a new console window.

    python proxy_window_probe.py [--keep] [--restart] [--src <path to a claude-hr src folder>] [--watch SECONDS]

Prints one line per new visible console window that belongs to the proxy (an empty terminal window or Windows Terminal tab
titled with the path of headroom.exe is exactly what 0.2.1 opened) and the final verdict. Exit code 0: no window and the proxy
came up healthy; 1: a window appeared or the proxy did not come up; 2: the probe did not run (a proxy was already healthy and
--restart was not given, or Headroom is not installed). The proxy is stopped afterwards unless --keep and the verdict is 0.
Windows only; run it with the claude-hr tool's python and the CLAUDE_HR_* variables of the profile you want to test (the e2e
script points them at its sandbox).

A window belongs to the proxy when its owner process is in the proxy's process tree, or it is a terminal window titled with the
path of the program that was started: a terminal a person opens meanwhile is not counted.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

CONSOLE_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "PseudoConsoleWindow"}
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def process_tree(root_pid: int) -> set[int]:
    """root_pid and every descendant that is alive now (a toolhelp snapshot)."""
    kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    pairs: list[tuple[int, int]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        pairs.append((entry.th32ProcessID, entry.th32ParentProcessID))
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    tree = {root_pid}
    grew = True
    while grew:
        grew = False
        for pid, parent in pairs:
            if parent in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return tree


def visible_console_windows() -> dict:
    found: dict = {}
    proc_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in CONSOLE_CLASSES:
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found[hwnd] = (cls.value, title.value, pid.value)
        return True

    user32.EnumWindows(proc_type(cb), 0)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument(
        "--restart", action="store_true", help="stop a proxy that is already running first"
    )
    ap.add_argument("--src", default="")
    ap.add_argument(
        "--watch",
        type=float,
        default=60.0,
        help="seconds to watch at most (the proxy needs up to ~1 min to be healthy)",
    )
    a = ap.parse_args()
    if a.src:
        sys.path.insert(0, a.src)
    from claude_hr import config, headroom

    hr = headroom.Headroom(config.load())
    if hr.health().ok:
        if not a.restart:
            print(
                "a proxy is already healthy on the port: stop it first (claude-hr-ctl headroom stop) or pass --restart"
            )
            return 2
        hr.stop()
        for _ in range(40):
            if not hr.health().ok:
                break
            time.sleep(0.5)
    before = visible_console_windows()
    verdict = 1
    proc = None
    try:
        proc = hr.start()
        if proc is None:
            print("could not start the proxy (is Headroom installed?)")
            return 2
        seen: dict = {}
        t0 = time.monotonic()
        healthy = False
        launched = os.path.basename(str(hr.binary())).lower()
        while time.monotonic() - t0 < a.watch:
            tree = process_tree(proc.pid)
            for hwnd, (cls, title, pid) in visible_console_windows().items():
                if hwnd in before:
                    continue
                # a window of the proxy: owned by a process of its tree, or a terminal titled with the program we started
                if pid in tree or title.lower().endswith(launched):
                    seen[hwnd] = (cls, title)
            if not healthy and hr.health().ok:
                healthy = True
            if (
                healthy and time.monotonic() - t0 > 15
            ):  # a window opens in the first seconds, long before the proxy is healthy
                break
            time.sleep(0.4)
        for cls, title in seen.values():
            print(f"NEW WINDOW: class={cls} title={title!r}")
        print(
            f"new console windows of the proxy: {len(seen)}; proxy healthy within {a.watch:.0f}s: {healthy}"
        )
        verdict = 0 if (not seen and healthy) else 1
        return verdict
    finally:
        if proc is not None and not (a.keep and verdict == 0):
            hr.stop()


if __name__ == "__main__":
    sys.exit(main())
