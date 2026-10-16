"""Is a person at the keyboard? (and one way to ask them a yes/no question)

The two decisions that let a repository's own commands run unattended after every turn - `claude-hr-ctl verify trust`
/ `verify enable`, and the one-time offer the launcher makes at the start of an interactive session - are only ever
taken by a person. There is no --yes flag and no environment variable that answers for them. This module is the single
place that decides "a person plausibly is here"; everything that approves something calls it.

Kept tiny and import-light (no re, json, subprocess, ctypes at the top): the launcher imports it on every interactive
session.
"""

from __future__ import annotations

import os
import sys

# How long a question waits for a person. After that Claude starts without an answer and nothing is recorded: a
# terminal nobody is sitting at (a tmux pane an orchestrator started, `ssh -tt`, a CI job with a pty) must not hold
# `claude` back. Tests lower it.
ANSWER_TIMEOUT_S = 300
TIMEOUT_NOTE = (
    "No answer, so Claude starts without asking; run claude-hr-ctl verify enable any time."
)


def is_console(fd: int) -> bool:
    """Is file descriptor `fd` a real terminal?

    os.isatty() alone says True for the NUL device on Windows, so a process whose streams all go to NUL (a spawn with
    stdio ignored, `claude-hr < NUL > NUL`) looked like a person. A real console is told apart by GetConsoleMode, which
    fails for NUL, a pipe and a file. Anything unexpected is False: no terminal, no question."""
    try:
        if not os.isatty(fd):
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(fd), ctypes.byref(mode)))
    except Exception:  # noqa: BLE001 - cannot tell: not a terminal
        return False


def is_terminal(stream) -> bool:
    """Is this stream (sys.stdin, sys.stdout, sys.stderr) a real terminal? A closed or detached one is not."""
    try:
        return bool(stream and stream.isatty() and is_console(stream.fileno()))
    except Exception:  # noqa: BLE001
        return False


def not_a_person() -> str | None:
    """Why nobody can be assumed to be at the keyboard (None = a person plausibly is).

    An agent that was talked into running an approval must not be able to approve itself: a shell that belongs to a
    Claude Code session (CLAUDECODE / CLAUDE_CODE_ENTRYPOINT) and a process without an interactive terminal on both
    stdin and stdout (a pipe, a redirect, NUL, a scheduled job, `claude -p`) are refused. This is a speed bump, not a
    boundary (a determined agent can unset the variables and allocate a pty); the permission prompts and the bash
    guard are the other layers.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "this shell belongs to a Claude Code session"
    if not (is_terminal(sys.stdin) and is_terminal(sys.stdout)):
        return "there is no interactive terminal"
    return None


def _answer(text: str, default: bool) -> bool | None:
    """True / False for an answer we understand, None for anything else."""
    word = text.strip().lower()
    if not word:
        return default
    if word in ("y", "yes"):
        return True
    if word in ("n", "no"):
        return False
    return None


# ---- the live console: what was typed too early, and nobody there -------------------------------------------------
def _live_console() -> bool:
    """Is the answer going to be read from a real console? Only then is there type-ahead to drop and a wait to bound
    (a pipe or a scripted input() has neither; tests replace this)."""
    try:
        return is_console(sys.stdin.fileno())
    except Exception:  # noqa: BLE001
        return False


def _drain_console(kbhit, getwch, limit: int = 65536) -> None:
    """Read and drop every key the Windows console has queued (bounded: a console that never empties is left alone)."""
    drained = 0
    while drained < limit and kbhit():
        getwch()
        drained += 1


def _discard_pending_input() -> None:
    """Drop what is already queued on the terminal: keys pressed while claude-hr was busy (the proxy wait, the scan)
    must not answer a question the person has not seen yet, and a first prompt typed ahead for Claude is not an answer.
    Never raises."""
    try:
        if os.name == "nt":
            import msvcrt

            _drain_console(msvcrt.kbhit, msvcrt.getwch)
        else:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - a flush that cannot happen is not a reason to stop
        pass


def _poll_keys(limit_s: float, kbhit, sleep, now) -> bool:
    """Poll `kbhit` every 0.1 s until it says a key is waiting (True) or `limit_s` seconds are over (False)."""
    end = now() + max(0.0, limit_s)
    while not kbhit():
        if now() >= end:
            return False
        sleep(0.1)
    return True


def _wait_for_answer(limit_s: float) -> bool:
    """Wait until there is something to read or `limit_s` seconds have passed; False = nobody answered in time.

    POSIX: a terminal in line mode is readable once a whole line was typed. Windows: the first key counts (the console
    has no line-level readiness), and from then on the person is at it. When it cannot tell, True: the read that
    follows simply blocks as it always did."""
    try:
        if os.name == "nt":
            import msvcrt
            import time

            return _poll_keys(limit_s, msvcrt.kbhit, time.sleep, time.monotonic)
        import select

        ready, _, _ = select.select([sys.stdin], [], [], max(0.0, limit_s))
        return bool(ready)
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001
        return True


def _show_prompt(prompt: str) -> None:
    """The question's last line, where input() would have put it (stderr, next to the screen that came before)."""
    try:
        sys.stderr.write(prompt)
        sys.stderr.flush()
    except (OSError, ValueError, UnicodeError):
        pass


def ask_yes_no(question: str, default: bool, *, tell=None) -> bool | None:
    """Ask `question` with a [Y/n] / [y/N] suffix. True = yes, False = no, None = "not now": EOF, Ctrl-C, or nobody
    answering within ANSWER_TIMEOUT_S (nothing is decided then).

    Enter gives `default`. An answer that is none of y / yes / n / no is asked once more; a second one is "not now" too
    (the person did not decide; typed-ahead text meant for something else must not become a recorded "no"). Keys typed
    before the question is on the screen are dropped. `tell` prints the "please answer" hint (default: stderr). The
    caller has already checked not_a_person().
    """
    suffix = "[Y/n]" if default else "[y/N]"
    prompt = f"{question} {suffix} "
    deadline = None
    for attempt in range(2):
        try:
            if _live_console():
                import time

                _discard_pending_input()  # after the screen was printed, right before the read
                _show_prompt(prompt)
                if deadline is None:
                    deadline = time.monotonic() + ANSWER_TIMEOUT_S
                if not _wait_for_answer(deadline - time.monotonic()):
                    _stderr("\n" + TIMEOUT_NOTE)  # the prompt line has no newline yet
                    return None
                reply = input()
            else:
                reply = input(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        except (OSError, ValueError):  # a terminal that vanished, or bytes it cannot decode
            return None
        verdict = _answer(reply, default)
        if verdict is not None:
            return verdict
        if attempt == 0:
            (tell or _stderr)("Please answer y or n.")
    return None


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
