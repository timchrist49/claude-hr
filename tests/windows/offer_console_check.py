"""Real Windows console test of the launch-time offer, against the INSTALLED claude-hr (0.2.1), in a throwaway sandbox.

A hidden console is created for the launcher (CREATE_NEW_CONSOLE + SW_HIDE) and keystrokes are injected into it with
WriteConsoleInput, exactly what a person's keyboard does. Nothing in the real profile is touched: every claude-hr and
Claude Code location points into the sandbox, and a stand-in `claude.cmd` replaces Claude Code.
Checks: a stray Enter typed before the question is discarded; y turns the gate on (recipe written, kept out of git status,
trusted, recorded) and Claude starts; n declines and Claude starts; a second launch asks nothing.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes as wt
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

CREATE_NEW_CONSOLE = 0x00000010
STARTF_USESHOWWINDOW = 1
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 1, 2
OPEN_EXISTING = 3
KEY_EVENT = 1


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", wt.WCHAR),
        ("dwControlKeyState", wt.DWORD),
    ]


class _EV(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EV)]


k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [
    wt.LPCWSTR,
    wt.DWORD,
    wt.DWORD,
    ctypes.c_void_p,
    wt.DWORD,
    wt.DWORD,
    wt.HANDLE,
]
k32.WriteConsoleInputW.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(INPUT_RECORD),
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]
k32.CloseHandle.argtypes = [wt.HANDLE]
INVALID = wt.HANDLE(-1).value


def type_into_console(pid: int, text: str) -> None:
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        raise OSError(f"AttachConsole({pid}) failed: {ctypes.get_last_error()}")
    try:
        h = k32.CreateFileW(
            "CONIN$",
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if h in (None, INVALID):
            raise OSError("cannot open CONIN$")
        recs = []
        for ch in text:
            vk = 0x0D if ch == "\r" else (user32.VkKeyScanW(ord(ch)) & 0xFF)
            sc = user32.MapVirtualKeyW(vk, 0)
            for down in (1, 0):
                r = INPUT_RECORD()
                r.EventType = KEY_EVENT
                r.Event.KeyEvent.bKeyDown = down
                r.Event.KeyEvent.wRepeatCount = 1
                r.Event.KeyEvent.wVirtualKeyCode = vk
                r.Event.KeyEvent.wVirtualScanCode = sc
                r.Event.KeyEvent.uChar = ch
                r.Event.KeyEvent.dwControlKeyState = 0
                recs.append(r)
        arr = (INPUT_RECORD * len(recs))(*recs)
        n = wt.DWORD(0)
        if not k32.WriteConsoleInputW(h, arr, len(recs), ctypes.byref(n)):
            raise OSError(f"WriteConsoleInput failed: {ctypes.get_last_error()}")
        k32.CloseHandle(h)
    finally:
        k32.FreeConsole()


def sandbox(tag: str):
    root = Path(tempfile.gettempdir()) / f"hrcon-{tag}"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    (root / "h" / ".claude").mkdir(parents=True)
    (root / "fake").mkdir()
    repo = root / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "package.json").write_text(
        json.dumps(
            {"name": "x", "version": "1.0.0", "scripts": {"test": 'node -e "process.exit(0)"'}}
        ),
        encoding="utf-8",
    )
    (root / "fake" / "claude.cmd").write_text(
        "@echo off\r\n"
        'if "%~1"=="--version" (echo 9.9.9 ^(Claude Code^) & exit /b 0)\r\n'
        'if "%~1"=="--help" (echo Usage: claude [options] [command] [prompt] & echo. & echo Options: & echo   -p, --print  print & echo. & echo Commands: & echo   mcp  manage & exit /b 0)\r\n'
        'echo started %*> "%~dp0started.txt"\r\n'
        "exit /b 0\r\n",
        encoding="ascii",
    )
    key = os.path.realpath(repo).replace("\\", "/")
    (root / "h" / ".claude" / ".claude.json").write_text(
        json.dumps({"projects": {key: {"hasTrustDialogAccepted": True}}}), encoding="utf-8"
    )
    return root, repo


def env_for(root: Path):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CI", "ANTHROPIC_BASE_URL")
    }
    env.update(
        CLAUDE_HR_HOME=str(root / "h" / ".claude-hr"),
        CLAUDE_HR_USER_HOME=str(root / "h"),
        CLAUDE_CONFIG_DIR=str(root / "h" / ".claude"),
        XDG_CONFIG_HOME=str(root / "h" / "xdg"),
        CLAUDE_HR_CLAUDE_BIN=str(root / "fake" / "claude.cmd"),
        CLAUDE_HR_HEADROOM__ENABLED="false",
    )
    return env


def launch(root: Path, repo: Path, exe: str):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    (root / "fake" / "started.txt").unlink(missing_ok=True)
    return subprocess.Popen(
        [exe], cwd=repo, env=env_for(root), creationflags=CREATE_NEW_CONSOLE, startupinfo=si
    )


def wait_exit(p, secs):
    try:
        p.wait(secs)
        return True
    except subprocess.TimeoutExpired:
        return False


def state(root, repo):
    sdir = root / "h" / ".claude-hr" / "state"
    trust = (
        json.loads((sdir / "verify-trust.json").read_text())
        if (sdir / "verify-trust.json").exists()
        else {}
    )
    offer = (
        json.loads((sdir / "verify-offer.json").read_text())
        if (sdir / "verify-offer.json").exists()
        else {}
    )
    return {
        "recipe": (repo / ".claude" / "hr-verify.toml").exists(),
        "trusted": bool(trust),
        "offer": sorted({e.get("decision") for e in offer.values()}),
        "started": (root / "fake" / "started.txt").exists(),
        "excluded": ".claude/hr-verify.toml"
        in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8", errors="replace")
        if (repo / ".git" / "info" / "exclude").exists()
        else False,
    }


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(
        ("  PASS " if ok else "  FAIL ") + name + (f"   {detail}" if detail and not ok else ""),
        flush=True,
    )


def main(exe: str) -> int:
    print(f"launcher under test: {exe}")
    # ---- scenario A: type-ahead is discarded, y turns it on ------------------------------------------------------
    root, repo = sandbox("a")
    p = launch(root, repo, exe)
    time.sleep(0.15)
    type_into_console(p.pid, "\r")  # a habitual Enter, typed before the question exists
    time.sleep(8)
    alive = p.poll() is None
    s = state(root, repo)
    check(
        "A1 a stray Enter before the question did not answer it (still waiting, nothing written)",
        alive and not s["recipe"] and not s["started"],
        str(s),
    )
    type_into_console(p.pid, "y\r")
    done = wait_exit(p, 120)
    s = state(root, repo)
    check(
        "A2 typing y turned the gate on: recipe written, trusted, recorded, Claude started",
        done and s["recipe"] and s["trusted"] and s["offer"] == ["enabled"] and s["started"],
        f"exited={done} {s}",
    )
    check("A3 the recipe is kept out of git status", s["excluded"], str(s))
    # ---- scenario C: second launch in the same sandbox asks nothing ----------------------------------------------
    p2 = launch(root, repo, exe)
    done2 = wait_exit(p2, 40)
    s2 = state(root, repo)
    check(
        "C1 a second launch asks nothing and starts Claude",
        done2 and s2["started"],
        f"exited={done2} {s2}",
    )
    if not done2:
        p2.kill()
    # ---- scenario B: n declines for good ------------------------------------------------------------------------
    root, repo = sandbox("b")
    p3 = launch(root, repo, exe)
    time.sleep(8)
    type_into_console(p3.pid, "n\r")
    done3 = wait_exit(p3, 60)
    s3 = state(root, repo)
    check(
        "B1 typing n declined: no recipe, remembered, Claude started",
        done3 and not s3["recipe"] and s3["offer"] == ["declined"] and s3["started"],
        f"exited={done3} {s3}",
    )
    p4 = launch(root, repo, exe)
    done4 = wait_exit(p4, 40)
    s4 = state(root, repo)
    check(
        "B2 and a no stays a no: the next launch asks nothing",
        done4 and s4["started"] and not s4["recipe"],
        f"exited={done4} {s4}",
    )
    if not done4:
        p4.kill()
    for tag in ("a", "b"):
        shutil.rmtree(Path(tempfile.gettempdir()) / f"hrcon-{tag}", ignore_errors=True)
    print(f"CONSOLE_RESULT: {sum(results)} passed, {len(results) - sum(results)} failed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
