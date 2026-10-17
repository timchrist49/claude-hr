"""Watches the desktop for console windows that open and close again, and writes what it saw to a JSON file.

    python window_watch.py <seconds> <out.json>

A window counts when it appeared after the watch began and is gone again at the end (a terminal a person opens and keeps is
not counted). Used by verify-windows-selfupdate.ps1 to prove the self-update helper (a task started by Task Scheduler) opens
no console window. Windows only.
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes as wt
from pathlib import Path

CONSOLE_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "PseudoConsoleWindow"}
user32 = ctypes.WinDLL("user32", use_last_error=True)


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
                found[hwnd] = [cls.value, title.value]
        return True

    user32.EnumWindows(proc_type(cb), 0)
    return found


def main() -> int:
    seconds, out = float(sys.argv[1]), Path(sys.argv[2])
    before = visible_console_windows()
    seen: dict = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for hwnd, info in visible_console_windows().items():
            if hwnd not in before:
                seen[hwnd] = info
        time.sleep(0.2)
    time.sleep(1.0)
    after = visible_console_windows()
    opened_and_closed = [info for hwnd, info in seen.items() if hwnd not in after]
    out.write_text(json.dumps({"opened_and_closed": opened_and_closed}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
