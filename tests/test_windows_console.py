"""A background process claude-hr starts on Windows must not open a window.

The Headroom proxy is started by `headroom.exe`, a uv trampoline that starts `python.exe`: a console program starting a
console program. Started with DETACHED_PROCESS the trampoline has no console, so Windows gives the python.exe it starts a
NEW visible one: an empty terminal window (a Windows Terminal tab titled with the path of headroom.exe) that kept popping up,
and that killed the proxy (exit code 0xC000013A) when a person closed it. This test starts the same two-hop chain with the
flags claude-hr uses and looks at the windows of the desktop session.

A window counts only when it appeared while the chain ran AND is gone again once the chain has ended (a terminal a person
opens meanwhile stays open, so it cannot fail the test). The chain must also have run to the end, so a start that failed
cannot pass for "no window".

Windows only. The control (the old flags DO open a window) opens a window for about a second, so it only runs when
CLAUDE_HR_TEST_WINDOW_CONTROL=1; without it the test cannot tell "no window" from "this session cannot show one", and the
unit tests of the flags (tests/test_background_flags.py) carry the guarantee.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from claude_hr import procutil

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows console windows")

CONSOLE_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass", "PseudoConsoleWindow"}
DETACHED_PROCESS = 0x00000008
HOP_SECONDS = 1.5

# python.exe starts python.exe and waits for it: the shape of uv's headroom.exe -> python.exe
TWO_HOPS = (
    "import subprocess, sys; "
    f"subprocess.run([sys.executable, '-c', 'import time; time.sleep({HOP_SECONDS})'])"
)


def _visible_console_windows() -> dict:
    import ctypes
    from ctypes import wintypes as wt

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


def _windows_opened_by_the_chain(flags: int) -> dict:
    """Windows that appeared while the chain ran and were gone after it ended."""
    before = _visible_console_windows()
    t0 = time.monotonic()
    p = subprocess.Popen(
        [sys.executable, "-c", TWO_HOPS],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    seen: dict = {}
    deadline = t0 + 15
    while p.poll() is None and time.monotonic() < deadline:
        for hwnd, info in _visible_console_windows().items():
            if hwnd not in before:
                seen[hwnd] = info
        time.sleep(0.1)
    if p.poll() is None:
        p.kill()
        pytest.fail("the two-hop chain did not finish in 15 s")
    elapsed = time.monotonic() - t0
    assert p.returncode == 0, (
        f"the chain failed with {p.returncode}: a start that fails shows no window either"
    )
    assert elapsed >= HOP_SECONDS * 0.9, (
        f"the chain ended after {elapsed:.2f}s: the inner process did not run"
    )
    time.sleep(1.0)  # a console window closes with its last process
    after = _visible_console_windows()
    return {h: info for h, info in seen.items() if h not in after}


def test_the_flags_claude_hr_uses_for_a_background_child_open_no_window():
    flags = procutil.windows_background_flags()
    assert not flags & DETACHED_PROCESS
    assert _windows_opened_by_the_chain(flags) == {}


@pytest.mark.skipif(
    os.environ.get("CLAUDE_HR_TEST_WINDOW_CONTROL") != "1",
    reason="the control opens a window for a second; set CLAUDE_HR_TEST_WINDOW_CONTROL=1",
)
def test_control_the_old_flags_do_open_a_window():
    """If this fails the machine cannot show a window to this test, and the test above proves nothing here."""
    old = DETACHED_PROCESS | 0x00000200 | 0x08000000
    assert _windows_opened_by_the_chain(old), (
        "the old flags opened no window: this session cannot show console windows"
    )
