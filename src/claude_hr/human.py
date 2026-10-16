"""Is a person at the keyboard? (and one way to ask them a yes/no question)

The two decisions that let a repository's own commands run unattended after every turn - `claude-hr-ctl verify trust`
/ `verify enable`, and the one-time offer the launcher makes at the start of an interactive session - are only ever
taken by a person. There is no --yes flag and no environment variable that answers for them. This module is the single
place that decides "a person plausibly is here"; everything that approves something calls it.

Kept tiny and import-light (no re, json, subprocess): the launcher imports it on every interactive session.
"""

from __future__ import annotations

import os
import sys


def not_a_person() -> str | None:
    """Why nobody can be assumed to be at the keyboard (None = a person plausibly is).

    An agent that was talked into running an approval must not be able to approve itself: a shell that belongs to a
    Claude Code session (CLAUDECODE / CLAUDE_CODE_ENTRYPOINT) and a process without an interactive terminal on both
    stdin and stdout (a pipe, a redirect, a scheduled job, `claude -p`) are refused. This is a speed bump, not a
    boundary (a determined agent can unset the variables and allocate a pty); the permission prompts and the bash
    guard are the other layers.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "this shell belongs to a Claude Code session"
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (OSError, ValueError):  # a closed or detached stream is not a terminal
        interactive = False
    if not interactive:
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


def ask_yes_no(question: str, default: bool, *, tell=None) -> bool | None:
    """Ask `question` with a [Y/n] / [y/N] suffix. True = yes, False = no, None = "not now" (EOF or Ctrl-C).

    Enter gives `default`. An answer that is none of y / yes / n / no is asked once more; a second one counts as no.
    `tell` prints the "please answer" hint (default: stderr). The caller has already checked not_a_person().
    """
    suffix = "[Y/n]" if default else "[y/N]"
    prompt = f"{question} {suffix} "
    for attempt in range(2):
        try:
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
    return False


def _stderr(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass
