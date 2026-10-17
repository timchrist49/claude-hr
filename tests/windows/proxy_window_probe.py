"""Starts the Headroom proxy through claude-hr's own Headroom.start() and watches the desktop for a new console window.

    python proxy_window_probe.py [--keep] [--src <path to a claude-hr src folder>]

Prints one line per new visible console window (an empty terminal window or Windows Terminal tab titled with the path of
headroom.exe is exactly what 0.2.1 opened) and the final verdict; exit code 1 when a window appeared or the proxy did not
come up. The proxy is stopped afterwards unless --keep. Windows only; run it with the claude-hr tool's python, with the
CLAUDE_HR_* variables of the profile you want to test (the e2e script points them at its sandbox).
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes as wt

CONSOLE_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "PseudoConsoleWindow"}


def visible_console_windows() -> dict:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: dict = {}
    proc_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in CONSOLE_CLASSES:
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                found[hwnd] = (cls.value, title.value)
        return True

    user32.EnumWindows(proc_type(cb), 0)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--src", default="")
    ap.add_argument(
        "--watch",
        type=float,
        default=30.0,
        help="seconds to watch (the proxy needs up to ~1 min to be healthy)",
    )
    a = ap.parse_args()
    if a.src:
        sys.path.insert(0, a.src)
    from claude_hr import config, headroom

    hr = headroom.Headroom(config.load())
    if hr.health().ok:
        print("a proxy is already healthy on the port: stop it first (claude-hr-ctl headroom stop)")
        return 2
    before = visible_console_windows()
    proc = hr.start()
    if proc is None:
        print("could not start the proxy (is Headroom installed?)")
        return 2
    seen: dict = {}
    t0 = time.monotonic()
    healthy = False
    while time.monotonic() - t0 < a.watch:
        for hwnd, info in visible_console_windows().items():
            if hwnd not in before:
                seen[hwnd] = info
        if not healthy and hr.health().ok:
            healthy = True
        if (
            healthy and time.monotonic() - t0 > 15
        ):  # a window opens in the first seconds, long before the proxy is healthy
            break
        time.sleep(0.4)
    for cls, title in seen.values():
        print(f"NEW WINDOW: class={cls} title={title!r}")
    print(f"new console windows: {len(seen)}; proxy healthy within {a.watch:.0f}s: {healthy}")
    if not a.keep:
        hr.stop()
    return 0 if (not seen and healthy) else 1


if __name__ == "__main__":
    sys.exit(main())
