#!/usr/bin/env python3
"""Run a command under a real pseudo-terminal, answer its yes/no questions from a script, print everything it showed.

    pty_launch.py --cwd DIR [--answers y,n] [--env K=V ...] [--timeout S] -- command args...

A "[Y/n]" or "[y/N]" at the end of the output is a question: the next answer is typed (a missing answer is "n", so a
question nobody expected never hangs the run, and the transcript shows it). The transcript goes to stdout; the last
line is `EXIT=<code>`. Used by run-linux-e2e.sh to prove what a person sees when starting `claude-hr` in a terminal.
Stdlib only, POSIX only (there is no pty on Windows; the unit tests cover that side).
"""

from __future__ import annotations

import argparse
import os
import pty
import re
import select
import signal
import sys
import time

QUESTION = re.compile(rb"\[[Yy]/[Nn]\] $")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", required=True)
    ap.add_argument(
        "--answers", default="", help="comma separated, one per question; a missing answer is n"
    )
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE added to the environment")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("no command")
    answers = [x for x in a.answers.split(",") if x != ""]

    env = dict(os.environ)
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CI"):
        env.pop(name, None)  # a person's terminal, not a Claude Code shell or a CI job
    env["TERM"] = "xterm"
    for item in a.env:
        key, _, value = item.partition("=")
        env[key] = value

    pid, fd = pty.fork()
    if pid == 0:  # child: its stdin, stdout and stderr are the pty
        try:
            os.chdir(a.cwd)
            os.execvpe(cmd[0], cmd, env)
        except OSError as exc:
            print(f"cannot start {cmd[0]}: {exc}", file=sys.stderr)
        os._exit(127)

    out = b""
    asked_up_to = 0
    deadline = time.monotonic() + a.timeout
    timed_out = False
    while True:
        if time.monotonic() > deadline:
            timed_out = True
            os.kill(pid, signal.SIGKILL)
            break
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:  # Linux: EIO once the child side is closed
            break
        if not data:
            break
        out += data
        if QUESTION.search(out[asked_up_to:]):
            asked_up_to = len(out)
            reply = answers.pop(0) if answers else "n"
            os.write(fd, reply.encode() + b"\r")
    try:
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        code = -1
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    if timed_out:
        sys.stdout.write("TIMED-OUT\n")
    sys.stdout.write(f"EXIT={code}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
