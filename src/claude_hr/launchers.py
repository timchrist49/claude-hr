"""Launcher programs that Windows refuses to start, and the stand-ins that replace them (Windows only).

`uv tool install` writes claude-hr.exe, claude-hr-ctl.exe and claude-hr-hook.exe into its bin folder: small launcher programs,
new files that nobody has ever run, each different from the same file on another PC. Application control and endpoint
security can refuse such a file ("Access is denied", Windows error 5) while they let the Python interpreter run, and then
every command of claude-hr is out of reach although the code is installed.

`ensure` asks Windows whether it starts each launcher (by starting it with an argument that only prints something) and, for
one it refuses, moves the file aside (`<name>.exe.refused`) and writes two stand-ins next to it:
  * `<name>.cmd`  for cmd.exe and PowerShell (an .exe in the same folder would win the lookup, hence the move), and
  * `<name>`      a shell script for Git Bash, where Claude's hooks and the plugins' skills run their commands.
Both start the same entry point through the Python of the tool environment: `python -I -m claude_hr.<module>`. `-I` matters:
`-m` puts the current directory first on the import path, and a launcher is started in whatever repository the person is in.

This is a way round, not a cure: whatever refuses the launcher is still there, headroom.exe (made by the same tool) is
refused the same way, and the .cmd stand-in hands its arguments to cmd.exe, which reads percent signs and carets in them.
Every run of `ensure` looks again, so a launcher that is allowed later (or recreated by an update) gets its place back.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .filelock import LockTimeout, locked
from .jsonio import atomic_write_text, read_json, write_json

# The text on the first lines of everything written here. Only a file that carries it is ours to replace or delete.
MARKER = "claude-hr launcher shim"

# command -> (module that holds main(), arguments that start it without doing anything: the launcher forwards `ctl`, the hook
# prints its usage and exits)
LAUNCHERS: dict[str, tuple[str, list[str]]] = {
    "claude-hr": ("claude_hr.launcher", ["ctl", "--version"]),
    "claude-hr-ctl": ("claude_hr.ctl", ["--version"]),
    "claude-hr-hook": ("claude_hr.hooks", []),
}

# Windows errors by which CreateProcess says "this file will not be started" (policy, antivirus, application control).
# Anything else (not found, bad format, a sharing violation) is some other trouble and no reason to move a file.
REFUSED: dict[int, str] = {
    5: "Access is denied",
    225: "the file contains a virus or potentially unwanted software",
    1260: "this program is blocked by group policy",
    1261: "this program is blocked by group policy",
    4551: "an application control policy has blocked this file",
}

# How long to wait, between looks, before believing a refusal: a scanner may hold a file that was written a moment ago.
WAITS: tuple[float, ...] = (2.0, 4.0, 6.0)

# What a .cmd stand-in costs, in the words every place that mentions it uses (setup output, doctor, installer, docs).
CMD_WARNING = (
    "The .cmd stand-ins hand their arguments to cmd.exe, which acts on & | < > and % ^ ! in them and drops everything after "
    "a line break"
)

_CREATE_NO_WINDOW = 0x08000000


@dataclass
class Probe:
    ok: bool  # Windows started the program (what it did then does not matter)
    refused: bool = False  # Windows would not start it, for one of the reasons in REFUSED
    code: int | None = None
    message: str = ""


def probe(exe: Path, args: list[str], timeout: float = 20.0) -> Probe:
    """Start `exe args` and see whether Windows lets it. A program that starts and fails, or runs on, is a yes."""
    kwargs: dict = {"creationflags": _CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        subprocess.run(
            [str(exe), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return Probe(True)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        return Probe(False, code in REFUSED, code, (exc.strerror or str(exc)).strip())
    return Probe(True)


# ---- the stand-ins ---------------------------------------------------------------------------------------------------------
def render_cmd(python: str, module: str) -> str:
    """The .cmd text (lines end in \\n; write it with CRLF). The last line is the trick npm's shims use: `goto` to a label that
    does not exist ends the batch file, and what follows `||` then runs as an ordinary command line, so Ctrl+C reaches the
    program and cmd.exe does not ask "Terminate batch job (Y/N)?" in the middle of a Claude session. No percent sign, caret
    or ampersand in the remarks: cmd.exe reads those even in a remark."""
    return (
        "@echo off\n"
        f"rem {MARKER}: Windows refused the launcher program, so this runs the same command through Python.\n"
        "rem claude-hr-ctl launchers remove takes it away. Arguments pass through cmd.exe, which reads percent signs in them.\n"
        "setlocal DisableDelayedExpansion\n"
        f'endlocal & goto #_undefined_# 2>NUL || "{python.replace("%", "%%")}" -I -m {module} %*\n'
    )


def render_sh(python: str, module: str) -> str:
    """The shell script for Git Bash (no cmd.exe on the way, so its arguments arrive as they were typed)."""
    posix = python.replace("\\", "/").replace("'", "'\"'\"'")
    return (
        "#!/bin/sh\n"
        f"# {MARKER}: Windows refused the launcher program, so this runs the same command through Python.\n"
        f"exec '{posix}' -I -m {module} \"$@\"\n"
    )


def _is_ours(path: Path) -> bool:
    try:
        with path.open(
            "rb"
        ) as handle:  # the marker is on the first lines; a launcher program is not read whole
            return MARKER.encode() in handle.read(600)
    except OSError:
        return False


# ---- where things are ------------------------------------------------------------------------------------------------------
def default_bin_dir() -> Path:
    """The folder `uv tool install` puts its launchers in: uv's own rules (UV_TOOL_BIN_DIR, XDG_BIN_HOME, else .local/bin in the
    home of the profile), without asking uv. A relative value is no folder to trust: the answer would depend on where we are.
    (The tests' sandbox clears both variables; and stand-ins are only written by the Python of a uv tool environment.)"""
    for var in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME"):
        value = os.environ.get(var)
        if value and Path(value).is_absolute():
            return Path(value)
    return paths.home() / ".local" / "bin"


def running_from_tool_environment() -> bool:
    """Does this Python belong to a `uv tool` environment (uv writes uv-receipt.toml at its root)? A stand-in runs whatever code
    its Python finds, so one that points into a development checkout, or a venv of a branch under review, would make every
    claude-hr command and hook run that checkout."""
    return (Path(sys.prefix) / "uv-receipt.toml").is_file()


def console_python() -> str:
    """The interpreter of this tool environment, with a console (scheduled jobs run under pythonw.exe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        exe = exe.with_name("python.exe")
    return str(exe)


def stand_in_python() -> str:
    """The Python the stand-ins run: the one the last look recorded (it ran from the tool environment), else this one."""
    recorded = state().get("python")
    if isinstance(recorded, str) and recorded and Path(recorded).is_file():
        return recorded
    return console_python()


def state_file() -> Path:
    return paths.state_dir() / "launchers.json"


def state() -> dict:
    data = read_json(state_file(), {}) or {}
    return data if isinstance(data, dict) else {}


def refused_names() -> list[str]:
    """Launchers the last look found refused by Windows (a stand-in in place or not)."""
    names = state().get("names")
    if not isinstance(names, dict):
        return []
    return [n for n, v in names.items() if isinstance(v, dict) and v.get("refused")]


def is_shimmed(name: str) -> bool:
    """Does `name` run through a stand-in on this PC (the last look found its launcher refused, and the stand-in is there)?"""
    data = state()
    names = data.get("names")
    entry = names.get(name) if isinstance(names, dict) else None
    if not isinstance(entry, dict) or not entry.get("shim") or not data.get("bin"):
        return False
    folder = Path(str(data["bin"]))
    return _is_ours(folder / f"{name}.cmd") or _is_ours(folder / name)


# ---- ensure / remove -------------------------------------------------------------------------------------------------------
@dataclass
class Outcome:
    ok: bool = True
    changed: bool = False
    lines: list[str] = field(default_factory=list)
    refused: dict[str, Probe] = field(default_factory=dict)


def _write_shims(
    bin_dir: Path, name: str, module: str, python: str, with_cmd: bool = True
) -> str | None:
    """Write the stand-ins of `name`: the shell script, and the .cmd unless `with_cmd` is False (a .cmd hands its arguments to
    cmd.exe, which acts on & | < > in them; a person who does not want that keeps only the script, and PowerShell and cmd.exe
    then have no `name`). None when done, else why not. A file that is not ours is never touched."""
    cmd, sh = bin_dir / f"{name}.cmd", bin_dir / name
    for target in (cmd, sh) if with_cmd else (sh,):
        if target.exists() and not _is_ours(target):
            return f"{target} exists and was not made by claude-hr"
    if with_cmd and not python.isascii():
        # cmd.exe reads a batch file in the console's code page, not in UTF-8: a path with other characters would be misread
        return f"the Python path {python} has characters that cmd.exe cannot read from a batch file"
    if with_cmd and "!" in python:
        # where delayed expansion is on (a registry value, a calling batch file) cmd.exe would eat the exclamation mark
        return f"the Python path {python} has an exclamation mark, which cmd.exe may read as a variable in a batch file"
    if with_cmd:
        atomic_write_text(cmd, render_cmd(python, module), newline="\r\n", follow_symlinks=False)
    elif cmd.is_file() and _is_ours(cmd):
        cmd.unlink()  # written by an earlier look, before the .cmd was switched off
    atomic_write_text(sh, render_sh(python, module), newline="\n", follow_symlinks=False)
    return None


def _drop_shims(bin_dir: Path, name: str) -> bool:
    gone = False
    for target in (bin_dir / f"{name}.cmd", bin_dir / name):
        if target.is_file() and _is_ours(target):
            target.unlink()
            gone = True
    return gone


def ensure(
    *,
    bin_dir: Path | None = None,
    python: str | None = None,
    probe_fn: Callable[[Path, list[str]], Probe] = probe,
    waits: tuple[float, ...] = WAITS,
    sleep: Callable[[float], None] = time.sleep,
    dry: bool = False,
    cmd: bool = True,
) -> Outcome:
    """Look at each launcher; give a refused one a stand-in, give a launcher that starts again its place back. `cmd`: also
    write the .cmd stand-ins (config launchers.cmd). Never raises: claude-hr must start, and set itself up, on a PC where this
    does not work."""
    out = Outcome()
    folder = Path(bin_dir) if bin_dir else default_bin_dir()
    try:
        if python is None and not running_from_tool_environment():
            out.lines.append(
                f"{sys.executable} is not the Python of a uv tool environment (no uv-receipt.toml in {sys.prefix}): "
                "stand-ins would run whatever code it finds, so nothing was changed. Run `claude-hr-ctl launchers ensure` "
                "from the installed tool."
            )
            return out
        py = python or console_python()
        if dry:
            return _ensure(out, folder, py, probe_fn, waits, sleep, True, cmd)
        with locked(paths.state_dir() / "launchers.lock", timeout=10.0, stale_after=300.0):
            return _ensure(out, folder, py, probe_fn, waits, sleep, False, cmd)
    except LockTimeout:
        out.lines.append("another claude-hr process is checking the launchers; skipped")
    except Exception as exc:  # noqa: BLE001 - fail open
        out.ok = False
        out.lines.append(f"could not check the launchers: {exc}")
    return out


def _ensure(
    out: Outcome,
    bin_dir: Path,
    python: str,
    probe_fn: Callable[[Path, list[str]], Probe],
    waits: tuple[float, ...],
    sleep: Callable[[float], None],
    dry: bool,
    with_cmd: bool,
) -> Outcome:
    found: dict[str, Path] = {}  # name -> the launcher file to look at
    moved_back: set[str] = set()
    for name in LAUNCHERS:
        exe, aside = bin_dir / f"{name}.exe", bin_dir / f"{name}.exe.refused"
        if exe.is_file():
            found[name] = exe
        elif aside.is_file():
            # Moved aside by an earlier look. Look at it under its own name again: the block may be gone, and an application
            # control rule that goes by name does not see ".exe.refused" as a program at all.
            if dry:
                found[name] = aside
                continue
            try:
                os.replace(aside, exe)
            except OSError as exc:
                out.ok = False
                out.lines.append(f"{name}: could not look at {aside.name} again: {exc}")
                continue
            found[name] = exe
            moved_back.add(name)

    last = state()
    same_folder = os.path.normcase(os.path.abspath(str(last.get("bin") or ""))) == os.path.normcase(
        os.path.abspath(str(bin_dir))
    )
    previous = (
        last.get("names") if same_folder else None
    )  # an old record of another folder moved nothing here
    moved_before = {
        n
        for n, v in (previous if isinstance(previous, dict) else {}).items()
        if isinstance(v, dict) and v.get("refused")
    }  # the launchers that an earlier look moved aside
    verdicts = {name: probe_fn(path, LAUNCHERS[name][1]) for name, path in found.items()}
    for wait in waits:
        # a launcher that was put back from `.exe.refused` has been waited for before: one more refusal is believed at once
        # (each refused start is an event for whoever watches this PC, and for those seconds the name is a program that fails)
        again = [n for n, v in verdicts.items() if v.refused and n not in moved_back]
        if not again:
            break
        sleep(wait)
        for n in again:
            verdicts[n] = probe_fn(found[n], LAUNCHERS[n][1])

    names_state: dict[str, dict] = {}
    for name, verdict in verdicts.items():
        exe, aside = bin_dir / f"{name}.exe", bin_dir / f"{name}.exe.refused"
        module = LAUNCHERS[name][0]
        if verdict.ok:
            if not dry and _drop_shims(bin_dir, name):
                out.changed = True
                out.lines.append(f"{name}: the launcher starts again; its stand-in is removed")
            if not dry and name in moved_before and aside.is_file():
                # a reinstall wrote a new launcher next to the one that was moved aside, and the new one starts: the old one is
                # nobody's now (only a file that an earlier look moved aside is ever deleted)
                try:
                    aside.unlink()
                    out.changed = True
                    out.lines.append(
                        f"{name}: the old {aside.name} is deleted, a launcher that starts took its place"
                    )
                except OSError:
                    pass
            names_state[name] = {"refused": False, "shim": False}
        elif verdict.refused:
            out.refused[name] = verdict
            why = f"error {verdict.code}: {verdict.message or REFUSED.get(verdict.code or 0, 'refused')}"
            if dry:
                out.lines.append(
                    f"{name}: Windows refuses {found[name].name} ({why}); would move it aside and write a stand-in"
                )
                names_state[name] = {"refused": True, "shim": False}
                continue
            stand_ins = (
                f"{name}.cmd and {name}" if with_cmd else f"{name} (a shell script: Git Bash only)"
            )
            problem: str | None = None
            try:
                problem = _write_shims(bin_dir, name, module, python, with_cmd)
                if problem is None and exe.is_file():
                    try:
                        os.replace(
                            exe, aside
                        )  # after the stand-in is there: there is never a moment without a way to start it
                    except OSError as exc:
                        problem = f"{exe.name} could not be moved aside: {exc}"
            except OSError as exc:
                problem = str(exc)
            standin_refused: Probe | None = None
            if problem is None and with_cmd:
                # the stand-in has to start, too: a rule on the folder (a Deny rule that new files inherit) or a script rule of
                # AppLocker refuses a .cmd as well, and then PowerShell and cmd.exe have nothing to start
                check = probe_fn(bin_dir / f"{name}.cmd", LAUNCHERS[name][1])
                if check.refused:
                    standin_refused = check
            if standin_refused is not None:
                out.ok = False
                out.lines.append(
                    f"{name}: Windows refuses {exe.name} ({why}), and refuses the stand-in {name}.cmd too "
                    f"(error {standin_refused.code}: {standin_refused.message}): PowerShell and cmd.exe cannot start {name}; "
                    "the shell script of that name still starts it in Git Bash"
                )
            elif problem is None and name in moved_back:
                # put back for this look, refused again, moved aside again: the folder is as it was
                out.lines.append(
                    f"{name}: Windows still refuses {exe.name} ({why}); {stand_ins} keep running it through Python"
                )
            elif problem is None:
                out.changed = True
                out.lines.append(
                    f"{name}: Windows refuses {exe.name} ({why}); {stand_ins} run it through Python instead"
                )
            else:
                out.ok = False
                out.lines.append(
                    f"{name}: Windows refuses {exe.name} ({why}), and no stand-in is in place: {problem}"
                )
            names_state[name] = {
                "refused": True,
                "shim": problem is None,
                "cmd": with_cmd,
                "standin_refused": standin_refused is not None,
                "code": verdict.code,
                "message": verdict.message,
            }
        else:
            # some other trouble (not found, bad format ...): no reason to touch anything but our own look
            out.ok = False
            out.lines.append(
                f"{name}: {found[name].name} did not start ({verdict.message or verdict.code}); left as it is"
            )
            if name in moved_back and not dry:
                try:
                    os.replace(exe, aside)
                except OSError:
                    pass
            names_state[name] = {"refused": False, "shim": False}

    if out.refused and with_cmd:
        # the same warning wherever the .cmd is mentioned: it is the price of a launcher that works in PowerShell
        out.lines.append(
            f"{CMD_WARNING}: never pass them text from an untrusted source; a script calls Python directly "
            f"({python} -I -m claude_hr.launcher ...). "
            "`claude-hr-ctl config set launchers.cmd false` keeps only the shell scripts (Git Bash)."
        )
    if out.refused and not dry:
        out.lines.append(
            f"To get the real launchers back, have the files in {bin_dir} ({', '.join(f'{n}.exe' for n in out.refused)}) "
            "allowed by whoever manages this PC (by file: both folders are writable by your account), then run "
            "`claude-hr-ctl launchers ensure`. The stand-ins run claude-hr through Python, which this PC allows: if the block "
            "on the launchers is a policy, ask before you rely on that."
        )
    if not out.lines:
        out.lines.append(
            ", ".join(found) + " start normally"
            if found
            else f"no launcher programs in {bin_dir} (nothing to check)"
        )
    if not dry:
        write_json(
            state_file(),
            {"ts": time.time(), "bin": str(bin_dir), "python": python, "names": names_state},
        )
    return out


def remove(*, bin_dir: Path | None = None) -> list[str]:
    """Take the stand-ins away and give each launcher that was moved aside its name back. Files that are not ours stay."""
    folder = Path(bin_dir) if bin_dir else default_bin_dir()
    try:
        with locked(paths.state_dir() / "launchers.lock", timeout=10.0, stale_after=300.0):
            return _remove(folder)
    except LockTimeout:
        return ["another claude-hr process is checking the launchers; try again in a moment"]


def _remove(folder: Path) -> list[str]:
    lines: list[str] = []
    for name in LAUNCHERS:
        try:
            if _drop_shims(folder, name):
                lines.append(f"{name}: stand-in removed")
            exe, aside = folder / f"{name}.exe", folder / f"{name}.exe.refused"
            if aside.is_file() and not exe.exists():
                os.replace(aside, exe)
                lines.append(f"{name}: {exe.name} is back")
        except OSError as exc:
            lines.append(f"{name}: {exc}")
    try:
        state_file().unlink(missing_ok=True)
    except OSError:
        pass
    return lines
