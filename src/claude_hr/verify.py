"""Repo verification: propose, trust, run and gate a repository's own checks (the fast tier at the end of a turn).

Why this exists: "done" from a model proves nothing, the repo's own tests and linters do. The commands live in
`<repo>/.claude/hr-verify.toml` as argv lists. They execute code that arrives with the repository (a clone, a PR
branch), so they only run after the user approved that exact file by hash (`claude-hr-ctl verify trust`), with no
shell, a scrubbed environment, a timeout that kills the whole process tree, and a cap on how often one failure may
block the model.

Everything a hook calls (`gate`, `fingerprint`, `test_weakening`, `is_trusted`) fails open: no exception escapes, the
worst case is that nothing is checked. Heavy imports (subprocess, tomllib, hashlib) are lazy, a hook that skips
early never pays for them.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from .jsonio import JsonFileError, atomic_write_text, read_json

_WIN = os.name == "nt"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: a pythonw-run hook must not flash a console per check
_NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP

CONFIG_NAME = "hr-verify.toml"
MAX_CHECKS = 12  # per tier
MAX_ARGV = 64
MAX_ARG_CHARS = 4096
MAX_CONFIG_BYTES = 64 * 1024
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 1, 900
TAIL_LINES = 40
TAIL_BYTES = 1500
MAX_REPOS = 50  # repos remembered in the gate state
MAX_TRUSTED_REPOS = 200
MAX_BLOCK_FPS = 20  # fingerprints with a block count, per repo
ACTIVE_ENV = "CLAUDE_HR_VERIFY_ACTIVE"  # set for every check: a nested gate (a test that drives hooks) must not recurse
_DIFF_CAP = 4 * 1024 * 1024
_ADVISORY_CAP = 5
# Time left under the hook's deadline (see stop_gate.HOOK_BUDGET_S) that the gate keeps for what happens AFTER the
# checks (second fingerprint, test-weakening scan, state write), and the least the checks themselves are worth
# starting with. Below the sum the gate does not start them (skip, note "time budget").
POST_RUN_RESERVE_S = 10.0
MIN_RUN_S = 5.0
# verify.gate_timeout_s in defaults.toml: what ALL the checks of one end-of-turn run may use together. A check's own
# timeout_s above this can never be reached in the gate (discover proposes fast checks no higher than this)
DEFAULT_GATE_TIMEOUT_S = 120
# a second Stop on the same repository waits this long for a gate run that is still going before it skips
RUN_LOCK_WAIT_S = 5.0
# audit-trail tags (GateOutcome.note) the end-of-turn hook acts on (stop_gate keeps the same literals)
NOTE_TIME_BUDGET = "time budget"
NOTE_BUSY = "another gate run in progress"
NOTE_CANNOT_START = "cannot start"
# test-weakening: one diff line is looked at only up to this many characters (a minified bundle or a hostile line
# must not cost more than a normal one), and the whole scan stops after this many seconds
_MAX_LINE = 2000
_ADVISORY_BUDGET_S = 5.0


class VerifyConfigError(ValueError):
    """`.claude/hr-verify.toml` is unreadable, malformed, or contains something unsafe."""


@dataclass(frozen=True)
class Check:
    name: str
    argv: list[str]
    timeout_s: int = 120
    cwd: str = "."


@dataclass
class Recipe:
    fast: list[Check]
    # the complete list: it repeats the fast checks, then adds the slow ones (build, ...)
    full: list[Check]
    source: str
    # rendered as comments by render_toml, never executed
    notes: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    argv: list[str]
    rc: int  # 124 timeout, 126 refused / could not start, 127 not found, else the process exit code
    seconds: float
    output: str = ""  # tail only (see TAIL_LINES / TAIL_BYTES)
    timed_out: bool = False
    clamped: bool = False  # timed out because the overall budget cut its own limit short: inconclusive, not a failure
    launch_failed: bool = False  # the process never started (tool missing, bad cwd): a setup problem, not a code failure

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class RunResult:
    results: list[CheckResult]
    seconds: float
    # names never started (stop_on_fail or overall budget)
    skipped: list[str] = field(default_factory=list)

    @property
    def failed(self) -> CheckResult | None:
        return next((r for r in self.results if r.rc != 0), None)

    @property
    def ok(self) -> bool:
        return self.failed is None and not self.skipped


@dataclass
class GateOutcome:
    action: str  # skip | pass | block | error
    reason: str = ""
    fingerprint: str = ""
    results: list[CheckResult] = field(default_factory=list)
    seconds: float = 0.0
    # short machine-readable tag for the audit trail ("time budget": the hook's deadline cut the run short or
    # left no time to start; "another gate run in progress"; "cannot start": a check could not be launched); empty for
    # every ordinary outcome
    note: str = ""
    # the check a "time budget" skip is about (the one that was cut short, or the first that never started), already
    # cleaned for display
    check: str = ""


# ---------------------------------------------------------------------------------------------------------------
# config: location, strict loading, rendering
# ---------------------------------------------------------------------------------------------------------------
def config_path(root: Path) -> Path:
    return Path(root) / ".claude" / CONFIG_NAME


_CHECK_KEYS = {"name", "argv", "timeout_s", "cwd"}
# Characters that only make sense to a shell. A real executable is never called `a && b`; seeing one means the
# author wrote a command line where an argv list belongs, which would silently not do what they meant.
_SHELL_META = frozenset(';&|<>$`"*?\n\r\0')


def _fail(where: str, msg: str):
    raise VerifyConfigError(f"{where}: {msg}")


def _resolve_cwd(root: Path, cwd: object) -> Path:
    """The directory a check runs in: relative, no `..`, and (after symlinks) still inside the repo."""
    if not isinstance(cwd, str) or not cwd.strip() or "\0" in cwd:
        raise VerifyConfigError("cwd must be a non-empty relative path")
    win, posix = PureWindowsPath(cwd), PurePosixPath(cwd)
    if win.is_absolute() or win.drive or win.root or posix.is_absolute():
        raise VerifyConfigError(f"cwd {cwd!r} must be relative to the repository root")
    if ".." in win.parts or ".." in posix.parts:
        raise VerifyConfigError(f"cwd {cwd!r} must not contain '..'")
    base = Path(os.path.realpath(root))
    target = Path(os.path.realpath(base / cwd))
    try:
        target.relative_to(base)
    except ValueError:
        raise VerifyConfigError(f"cwd {cwd!r} resolves outside the repository") from None
    return target


def _check_exe(where: str, exe: str) -> None:
    if not exe.strip():
        _fail(where, "argv[0] is blank")
    if exe != exe.strip():
        _fail(where, f"argv[0] {exe!r} has leading or trailing whitespace")
    bad = sorted(set(exe) & _SHELL_META)
    if bad:
        _fail(
            where,
            f"argv[0] {exe!r} contains shell characters {bad!r}; argv is run without a shell, "
            "so give every word its own list item",
        )
    if any(c.isspace() for c in exe) and "/" not in exe and "\\" not in exe:
        _fail(
            where,
            f"argv[0] {exe!r} looks like a whole command line; split it into list items "
            '(argv = ["npm", "run", "lint"])',
        )


def _validate_check(tier: str, index: int, raw: object, root: Path) -> Check:
    where = f"[[{tier}]] #{index + 1}"
    if not isinstance(raw, dict):
        _fail(where, "must be a table (use [[fast]] / [[full]] headers)")
    extra = sorted(set(raw) - _CHECK_KEYS)
    if extra:
        _fail(where, f"unknown key(s) {extra!r}; allowed: name, argv, timeout_s, cwd")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 60 or not name.isprintable():
        _fail(where, "name must be 1-60 printable characters")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv:
        _fail(where, 'argv must be a non-empty list of strings, e.g. argv = ["npm", "run", "lint"]')
    if len(argv) > MAX_ARGV:
        _fail(where, f"argv has more than {MAX_ARGV} items")
    for i, item in enumerate(argv):
        if not isinstance(item, str) or not item:
            _fail(where, f"argv[{i}] must be a non-empty string")
        if "\0" in item or len(item) > MAX_ARG_CHARS:
            _fail(where, f"argv[{i}] contains a NUL character or is longer than {MAX_ARG_CHARS}")
    _check_exe(where, argv[0])
    timeout = raw.get("timeout_s", 120)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not MIN_TIMEOUT_S <= timeout <= MAX_TIMEOUT_S
    ):
        _fail(
            where,
            f"timeout_s must be an integer from {MIN_TIMEOUT_S} to {MAX_TIMEOUT_S} (got {timeout!r})",
        )
    cwd = raw.get("cwd", ".")
    try:
        _resolve_cwd(root, cwd)
    except VerifyConfigError as exc:
        _fail(where, str(exc))
    return Check(name.strip(), list(argv), timeout, cwd)


def _parse(data: bytes, root: Path, source: str) -> Recipe:
    import tomllib

    if len(data) > MAX_CONFIG_BYTES:
        raise VerifyConfigError(f"{CONFIG_NAME} is larger than {MAX_CONFIG_BYTES // 1024} KB")
    try:
        doc = tomllib.loads(data.decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:  # bad UTF-8 and bad TOML are both ValueErrors
        raise VerifyConfigError(f"{CONFIG_NAME} is not valid TOML: {exc}") from exc
    unknown = sorted(set(doc) - {"fast", "full"})
    if unknown:
        raise VerifyConfigError(
            f"{CONFIG_NAME}: unknown top-level key(s) {unknown!r}; allowed: [[fast]], [[full]]"
        )
    tiers: dict[str, list[Check]] = {}
    for tier in ("fast", "full"):
        items = doc.get(tier, [])
        if not isinstance(items, list):
            raise VerifyConfigError(
                f"{CONFIG_NAME}: {tier} must be an array of tables ([[{tier}]])"
            )
        if len(items) > MAX_CHECKS:
            raise VerifyConfigError(
                f"{CONFIG_NAME}: at most {MAX_CHECKS} checks per tier ({tier} has {len(items)})"
            )
        tiers[tier] = [_validate_check(tier, i, raw, root) for i, raw in enumerate(items)]
    return Recipe(tiers["fast"], tiers["full"], source)


class _NotRegular(OSError):
    """The path exists but is not a regular file: a FIFO, a device, a directory ..."""


def _read_file(path: Path | str, limit: int) -> tuple[bytes, bool]:
    """(data, more): at most `limit` bytes of a REGULAR file, and whether the file holds more than that.

    Raises FileNotFoundError / NotADirectoryError when there is no file, _NotRegular for anything that is not a regular
    file, OSError for the rest. Every file a repository controls is read through here (the recipe, the manifests that
    discovery looks at): a committed symlink to a FIFO, /dev/ptmx or a tty would otherwise block the end-of-turn hook
    until the host kills it, and one to /dev/zero would be read until memory runs out. The kind is checked before the
    open (stat never blocks), the opened descriptor is checked again (the path may have been swapped in between, and
    O_NONBLOCK keeps that open from blocking on a FIFO), and the read never takes more than limit + 1 bytes."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise _NotRegular(f"{path} is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotRegular(f"{path} is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data[:limit], len(data) > limit


def _read_config(root: Path) -> bytes | None:
    """The raw config bytes, None when there is no file. The same bytes feed the hash and the parser (no TOCTOU).

    Only a regular file is read (see _read_file); anything else, or a file that cannot be read, is a
    VerifyConfigError. At most MAX_CONFIG_BYTES + 1 bytes: _parse rejects the file that is longer."""
    path = config_path(root)
    try:
        return _read_file(path, MAX_CONFIG_BYTES + 1)[0]
    except (FileNotFoundError, NotADirectoryError):
        return None
    except _NotRegular as exc:
        raise VerifyConfigError(f"{path} is not a regular file; it is not read") from exc
    except OSError as exc:
        raise VerifyConfigError(f"cannot read {path}: {exc}") from exc


def load(root: Path) -> Recipe | None:
    """The validated recipe, None if the repo has none. Raises VerifyConfigError for anything invalid."""
    data = _read_config(root)
    if data is None:
        return None
    return _parse(data, Path(root), str(config_path(root)))


@dataclass(frozen=True)
class Loaded:
    """One read of the config: the recipe parsed from `data`, and the digest of exactly those bytes."""

    recipe: Recipe | None
    data: bytes | None
    sha256: str | None


def load_checked(root: Path) -> Loaded:
    """load() that also hands back the bytes and their digest, so a caller can show the parsed recipe, judge its trust
    (trust_state_of) and approve it (trust(..., expect_sha256=)) all from the same read. Raises VerifyConfigError."""
    data = _read_config(root)
    if data is None:
        return Loaded(None, None, None)
    return Loaded(_parse(data, Path(root), str(config_path(root))), data, _sha256(data))


# Characters that print as nothing (or as a blank) although Python calls them printable: variation selectors, the
# combining grapheme joiner, Hangul/Braille/Khmer fillers. They are shown escaped as well.
_BLANKS = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2800\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]"
)


def _escape_char(ch: str) -> str:
    n = ord(ch)
    if n < 0x100:
        return f"\\x{n:02x}"
    return f"\\u{n:04x}" if n < 0x10000 else f"\\U{n:08x}"


def safe_text(text: str) -> str:
    """`text` with every character that could hide or alter what a terminal shows written out visibly (\\x1b, \\u202e).

    Everything from a repository's recipe that a person reads before approving it goes through here: an ESC or CR
    can erase the line that was just printed, a bidi override can reorder it, a zero-width character can make two
    different commands look equal. What stays as it is: printable text (letters, digits, punctuation, symbols, the
    plain space) in any script. A literal backslash is not doubled (Windows paths), so the text `\\x1b` typed out by
    an author looks the same as an escaped ESC; either way the person sees it and nothing is hidden.
    """
    if text.isprintable() and not _BLANKS.search(text):
        return text
    return "".join(c if c.isprintable() and not _BLANKS.match(c) else _escape_char(c) for c in text)


def _toml_str(value: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes. JSON leaves DEL, the C1 controls, bidi/format
    # characters and the line separators raw: they are written as \uXXXX too, so the proposal is as readable on a
    # terminal as it is on disk (and a lone surrogate, which TOML cannot hold, becomes U+FFFD)
    text = json.dumps(value, ensure_ascii=False)
    if text.isprintable() and not _BLANKS.search(text):
        return text
    out = []
    for c in text:
        if c.isprintable() and not _BLANKS.match(c):
            out.append(c)
        elif 0xD800 <= ord(c) <= 0xDFFF:
            out.append("\\ufffd")
        else:
            out.append(_escape_char(c).replace("\\x", "\\u00"))
    return "".join(out)


def _comment(text: str, limit: int = 200) -> str:
    """One line, nothing a terminal could act on: notes carry text from the repo (CI files) and must not break the TOML."""
    return safe_text(re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:limit])


_HEADER = """\
# claude-hr verify recipe for this repository.
#
# claude-hr runs the [[fast]] commands unattended at the end of each Claude turn (the [[full]] list is for
# `claude-hr-ctl verify run --tier full`). Each one is an argv list, never a shell line, and runs with a scrubbed
# environment and a timeout. Because they execute code that comes with the repository, claude-hr only runs them
# after you approved this exact file: review it, then run `claude-hr-ctl verify trust`, and again after every edit
# (the approval is a hash of the file, so a changed file is not trusted until you re-approve it).
#
# Per check: name (shown in failures), argv (list of strings), timeout_s (1-900), cwd (relative to the repo root).
# At most 12 checks per list. [[full]] is the complete list: repeat the fast checks you want it to include.
"""


def render_toml(r: Recipe) -> str:
    """The exact text `verify init --write` puts in `.claude/hr-verify.toml`."""
    out = [_HEADER.rstrip("\n"), "#", f"# Proposed from: {_comment(r.source) or 'nothing'}"]
    if r.notes:
        out += [
            "#",
            "# Notes (nothing below is run unless it appears in a [[fast]] / [[full]] table):",
        ]
        out += [f"# - {_comment(n)}" for n in r.notes]
    for tier, checks in (("fast", r.fast), ("full", r.full)):
        out.append("")
        if not checks:
            out.append(f"# No [[{tier}]] checks.")
        for chk in checks:
            out += [
                f"[[{tier}]]",
                f"name = {_toml_str(chk.name)}",
                f"argv = [{', '.join(_toml_str(a) for a in chk.argv)}]",
                f"timeout_s = {int(chk.timeout_s)}",
                f"cwd = {_toml_str(chk.cwd)}",
                "",
            ]
        if checks:
            out.pop()  # the blank line after the last table; one separator is added per tier
    return "\n".join(out) + "\n"


def command_line(check: Check) -> str:
    """The check as a shell-looking line, for display only (it is never executed that way).

    Passed through safe_text: an argv item with an escape sequence, a CR or a bidi override is shown as such.
    """
    import shlex

    return safe_text(shlex.join(check.argv))


class ConfigExists(VerifyConfigError):
    """`.claude/hr-verify.toml` already exists and `force` was not given."""


_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # a Windows junction (a symlink is S_ISLNK)


def _is_link(path: Path) -> bool:
    """A symbolic link (dangling or not) or a Windows junction. False when nothing is there."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    )


def write_config(root: Path, text: str, *, force: bool = False) -> Path:
    """Write `text` as `<root>/.claude/hr-verify.toml` (what `verify init --write` does). Returns the path.

    The repository is not trusted, and neither is anything it commits: `.claude/hr-verify.toml` or `.claude` itself may be
    a symbolic link to a file or directory elsewhere (a dangling link to a path that does not exist yet as well).
    Writing through it would overwrite, or create, a file of the user's outside the repository, so a link at either
    place is refused (also with `force`), and so is a target that is not a regular file. Nothing is ever written
    through a link: a new file is created with O_EXCL (which does not follow a link), an existing one is replaced
    atomically by a new file next to it (jsonio.atomic_write_text with follow_symlinks=False).

    Raises ConfigExists when the file exists and `force` is not set, VerifyConfigError for a link or a target of the
    wrong kind, OSError when the write fails."""
    cp = config_path(root)
    parent = cp.parent
    for link in (parent, cp):
        if _is_link(link):
            raise VerifyConfigError(
                f"{link} is a symbolic link or junction; nothing is written through it "
                "(remove the link, or write the recipe yourself)"
            )
    try:
        parent_st = os.lstat(parent)
    except FileNotFoundError:
        os.makedirs(parent, exist_ok=True)
    else:
        if not stat.S_ISDIR(parent_st.st_mode):
            raise VerifyConfigError(f"{parent} is not a directory")
    try:
        old = os.lstat(cp)
    except FileNotFoundError:
        old = None
    if old is not None:
        if not stat.S_ISREG(old.st_mode):
            raise VerifyConfigError(f"{cp} is not a regular file; nothing is written over it")
        if not force:
            raise ConfigExists(str(cp))
        atomic_write_text(cp, text, follow_symlinks=False)
        if os.name != "nt":
            # the file the replacement came from was created 0600: give back the old permissions
            try:
                os.chmod(cp, stat.S_IMODE(old.st_mode))
            except OSError:
                pass
        return cp
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(cp, flags, 0o666)
    except FileExistsError as exc:  # made (or linked) by someone else since the look above
        raise ConfigExists(str(cp)) from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
    except BaseException:
        try:
            os.unlink(cp)
        except OSError:
            pass
        raise
    return cp


# ---------------------------------------------------------------------------------------------------------------
# trust: the config only runs once the user approved its exact bytes
# ---------------------------------------------------------------------------------------------------------------
def _repo_key(root: Path) -> str:
    key = os.path.realpath(root)
    return key.casefold() if _WIN else key


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _read_dict(path: Path) -> dict:
    """A state file we own: a corrupt one is treated as empty (it only holds regenerable bookkeeping)."""
    try:
        data = read_json(path, {})
    except JsonFileError:
        return {}
    return data if isinstance(data, dict) else {}


def _trust_file(state_dir: Path) -> Path:
    return Path(state_dir) / "verify-trust.json"


def _trusted_hash(root: Path, state_dir: Path) -> str | None:
    value = _read_dict(_trust_file(state_dir)).get(_repo_key(root))
    return value if isinstance(value, str) else None


def trust_state_of(root: Path, state_dir: Path, data: bytes | None) -> str:
    """trust_state for config bytes the caller already read (so the verdict and the parsed recipe share one read)."""
    try:
        if data is None:
            return "no-config"
        try:
            _parse(data, Path(root), "")
        except VerifyConfigError:
            return "invalid"
        recorded = _trusted_hash(root, state_dir)
        if recorded is None:
            return "untrusted"
        return "trusted" if recorded == _sha256(data) else "modified"
    except (OSError, ValueError):
        return "untrusted"


def trust_state(root: Path, state_dir: Path) -> str:
    """no-config | invalid | untrusted | modified (trusted once, edited since) | trusted. Never raises."""
    try:
        data = _read_config(root)
    except VerifyConfigError:
        return "invalid"  # not a regular file, unreadable: nothing in it is ever run
    except (OSError, ValueError):
        return "untrusted"
    return trust_state_of(root, state_dir, data)


def is_trusted(root: Path, state_dir: Path) -> bool:
    return trust_state(root, state_dir) == "trusted"


def trust(root: Path, state_dir: Path, expect_sha256: str | None = None) -> str:
    """Record the hash of the config as it is now. Refuses a missing or invalid file. Returns the hash.

    `expect_sha256` is the digest of the bytes a person was shown (Loaded.sha256): when the file on disk no longer
    hashes to it, nothing is recorded (VerifyConfigError). Without it the approval would cover whatever is on disk
    at the moment of the write, which is not necessarily what was on the screen.
    """
    data = _read_config(root)
    if data is None:
        raise VerifyConfigError(
            f"no {config_path(root)} to trust (create it with: claude-hr-ctl verify init --write)"
        )
    digest = _sha256(data)
    if expect_sha256 is not None and digest != expect_sha256:
        raise VerifyConfigError(
            f"{config_path(root)} changed after it was shown to you; nothing was trusted. "
            "Read it again, then run `claude-hr-ctl verify trust` again"
        )
    _parse(data, Path(root), "")
    path = _trust_file(state_dir)
    state = _read_dict(path)
    key = _repo_key(root)
    state.pop(key, None)  # re-insert: the dict order doubles as recency
    state[key] = digest
    while len(state) > MAX_TRUSTED_REPOS:
        state.pop(next(iter(state)))
    atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)
    return digest


def untrust(root: Path, state_dir: Path) -> bool:
    """Forget the approval for this repo. True if there was one. Needs only the repo's key: it works while the recipe is
    missing, unreadable or invalid (revoking must never depend on the file being well-formed)."""
    path = _trust_file(state_dir)
    state = _read_dict(path)
    if state.pop(_repo_key(root), None) is None:
        return False
    atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)
    return True


# ---------------------------------------------------------------------------------------------------------------
# running checks
# ---------------------------------------------------------------------------------------------------------------
# THE environment deny-list. Every child process claude-hr starts from a repository's text (the verify checks here,
# the formatter step in fmt.py) gets the environment minus these. A variable is dropped when its name, upper-cased,
#   - contains one of SECRET_ENV_WORDS, or starts with one of SECRET_ENV_PREFIXES, or is one of SECRET_ENV_NAMES,
#   - unless it is one of KEEP_ENV / starts with a KEEP_ENV_PREFIXES entry (what a toolchain cannot run without and
#     that holds no secret: GIT_AUTHOR_NAME would otherwise match "AUTH", the CA-bundle paths match "CERT").
# Values are also dropped when they embed credentials in a URL (https://user:pass@host). A deny-list, not an
# allow-list, so a harmless variable some tool needs keeps working; it is a speed bump for a naive `print(os.environ)`,
# not a boundary (the check runs as you and can read ~/.aws or its parent's environment).
# A person can exempt names with the config key verify.env_allow (scrubbed_env(allow=...)): the escape hatch for the
# DATABASE_URL / AWS_PROFILE / DJANGO_SECRET_KEY-style variable a repository's own suite reads from the shell.
# fmt.py (the formatter step) does not import this module and keeps a list of its own, stricter in some places
# (JWT, WEBHOOK, PAT) and not yet in step with the exemptions below (PWD, GOPRIVATE, AWS_REGION, npm settings) nor
# aware of verify.env_allow.
SECRET_ENV_WORDS = (
    "TOKEN",
    "SECRET",
    "PASS",  # PASSWORD, PASSPHRASE, DB_PASS, PGPASSFILE, GIT_ASKPASS, ...
    "PWD",  # MYSQL_PWD (and OLDPWD: stale shell state; the shell's own PWD is kept and set per check, see _run_one)
    "KEY",
    "CRED",
    "AUTH",  # also SSH_AUTH_SOCK
    "COOKIE",
    "DSN",
    "CONNECTION",
    "PRIVATE",
    "CERT",
)
SECRET_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GCP_",
    "GITHUB_",
    "GH_",
)
# NPM_ is not a prefix of its own: npm_config_registry / _cache / _prefix are settings. The credential-bearing ones
# (npm_config__auth, npm_config_//host/:_authToken, NPM_TOKEN, ..._password) hold a word above, and a registry URL with
# credentials in it is caught by the value rule.
SECRET_ENV_NAMES = frozenset({"DATABASE_URL", "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"})
KEEP_ENV = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "LANG",
        "LANGUAGE",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "VIRTUAL_ENV",
        "GOPATH",
        "JAVA_HOME",
        # paths to public CA bundles (they contain "CERT" but no secret): behind a TLS-inspecting proxy a check that
        # downloads anything cannot work without them
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "NIX_SSL_CERT_FILE",
        "PIP_CERT",
        "DENO_CERT",
        "DOCKER_CERT_PATH",
        # names a word above would catch although they hold no secret
        "PWD",  # the shell's current directory; each check gets its own (see _run_one)
        "GOPRIVATE",  # module paths that skip the public proxy (go test fails on a private module without it)
        "NODE_TLS_REJECT_UNAUTHORIZED",  # a switch (contains AUTH)
        "XAUTHORITY",  # a path
        "AWS_REGION",  # a region name; the credentials around it stay dropped
        "AWS_DEFAULT_REGION",
    }
)
KEEP_ENV_PREFIXES = ("GIT_AUTHOR_", "GIT_COMMITTER_")
_URL_CREDENTIALS = re.compile(r"://[^/@\s:]+:[^/@\s]+@")


def is_secret_env(name: str, value: str = "") -> bool:
    """Would the deny-list drop this variable? (see SECRET_ENV_WORDS; verify.env_allow is applied by scrubbed_env)"""
    upper = name.upper()
    if upper in KEEP_ENV or upper.startswith(KEEP_ENV_PREFIXES):
        return False
    if (
        upper in SECRET_ENV_NAMES
        or upper.startswith(SECRET_ENV_PREFIXES)
        or any(word in upper for word in SECRET_ENV_WORDS)
    ):
        return True
    return "://" in value and bool(_URL_CREDENTIALS.search(value[:4096]))


def env_allow_names(raw: object) -> frozenset[str]:
    """The names in verify.env_allow, upper-cased: a list of strings, or one string with names separated by commas or
    spaces (what an environment-variable override of the key gives). Anything else is ignored."""
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(item.strip().upper() for item in raw if isinstance(item, str) and item.strip())


def scrubbed_env(
    base: dict[str, str] | None = None, *, allow: Collection[str] | str = ()
) -> dict[str, str]:
    """A copy of the environment without the names and values the deny-list above drops, plus CI=1.

    A deny-list, not an allow-list: toolchains need many harmless variables (SYSTEMROOT, APPDATA, CARGO_HOME,
    VIRTUAL_ENV, ...) and a check that only works in a hand-picked environment would fail in the gate and pass in
    the terminal. `allow` (the config key verify.env_allow) names variables that are kept although the deny-list
    would drop them; the person who set it chose them, a repository cannot.
    """
    src = dict(os.environ if base is None else base)
    keep = env_allow_names(allow)
    env = {
        name: value
        for name, value in src.items()
        if name.upper() in keep or not is_secret_env(name, value)
    }
    env["CI"] = "1"
    env[ACTIVE_ENV] = "1"
    return env


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
# (the C1 controls too: U+009B is a one-character CSI on terminals that honour 8-bit controls in UTF-8)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _tail_text(raw: bytes) -> str:
    """The last TAIL_LINES lines / TAIL_BYTES bytes of captured output, ANSI-free, as plain text."""
    text = _ANSI_RE.sub("", raw.decode("utf-8", errors="replace"))
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if "\r" in line:
            # a progress bar redraws one line: keep what the terminal would finally show
            parts = [p for p in line.split("\r") if p]
            line = parts[-1] if parts else ""
        lines.append(_CTRL_RE.sub("", line).rstrip())
    while lines and not lines[-1]:
        lines.pop()
    out = "\n".join(lines[-TAIL_LINES:])
    enc = out.encode("utf-8")
    if len(enc) > TAIL_BYTES:
        cut = enc[-TAIL_BYTES:].decode("utf-8", errors="ignore")
        if enc[-TAIL_BYTES - 1 : -TAIL_BYTES] != b"\n":
            # the first line is probably partial: drop it
            _, nl, rest = cut.partition("\n")
            if nl and rest:
                cut = rest
        out = cut
    return out


class _Tail:
    """Drains a pipe on a thread, keeping only the last 64 KB (a runaway check must not fill memory or disk)."""

    _KEEP = 64 * 1024

    def __init__(self, stream) -> None:
        import threading

        self._buf = bytearray()
        self.thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self.thread.start()

    def _pump(self, stream) -> None:
        # this thread owns the pipe and closes it at EOF: closing it from the caller while a lingering grandchild
        # still holds the other end would race with the read above
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                self._buf += chunk
                if len(self._buf) > 2 * self._KEEP:
                    del self._buf[: -self._KEEP]
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def data(self) -> bytes:
        return bytes(self._buf[-self._KEEP :])


def _kill_quietly(proc) -> None:
    try:
        proc.kill()
    except OSError:
        pass


_JOB_API: dict = {}  # ctypes handles for the job object calls, built on first use


def _job_api() -> dict:
    """kernel32 job-object entry points with their ctypes signatures (Windows only; raises elsewhere)."""
    if not _JOB_API:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [
                (n, ctypes.c_uint64) for n in "r_ops w_ops o_ops r_bytes w_bytes o_bytes".split()
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("Io", Counters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _JOB_API.update(ctypes=ctypes, k32=k32, Extended=Extended)
    return _JOB_API


class _Job:
    """A Windows job object that holds a check and every process it starts, then ends them all.

    taskkill /T walks the parent links of a LIVE process, so it finds nothing once the check has exited and its
    children were re-parented; a job keeps the membership. KILL_ON_JOB_CLOSE also ends the group if this process is
    killed itself (the hook's own timeout). Best effort: if the job cannot be made or joined (an enclosing job that
    forbids nesting) the object is inert and the callers fall back to taskkill. Inert on every other OS.
    """

    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMITS = 9

    def __init__(self, proc) -> None:
        self._handle = None
        if not _WIN:
            return
        api = None
        try:
            api = _job_api()
            k32, ctypes = api["k32"], api["ctypes"]
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = api["Extended"]()
            info.Basic.LimitFlags = self._KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, self._EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)
            ) or not k32.AssignProcessToJobObject(job, int(proc._handle)):
                k32.CloseHandle(job)
                return
            self._handle = job
        except Exception:  # noqa: BLE001 - a missing job is a degraded kill, never a failed check
            self._handle = None

    def terminate(self) -> bool:
        """End every process in the job. False when there is no job (the caller then uses the other mechanism)."""
        if self._handle is None:
            return False
        try:
            return bool(_job_api()["k32"].TerminateJobObject(self._handle, 1))
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                _job_api()["k32"].CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


def _taskkill_tree(proc) -> None:
    import subprocess

    from . import procutil

    taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    if not os.path.isfile(taskkill):
        taskkill = procutil.resolve_exe("taskkill")  # never the current directory
    if taskkill is None:
        return
    subprocess.run(
        [taskkill, "/PID", str(proc.pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        creationflags=_NO_WINDOW,
        check=False,
    )


def _kill_tree(proc, job: _Job | None = None) -> None:
    """Kill the check and everything it started. A test runner's workers must not outlive a timeout."""
    import subprocess

    try:
        if _WIN:
            if job is None or not job.terminate():
                _taskkill_tree(proc)
        else:
            import signal

            # the check leads its own session (start_new_session), so its pid is the group id
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    _kill_quietly(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _reap_group(proc, job: _Job | None) -> None:
    """The check exited on its own: end whatever it left running (a dev server, a database, a watcher).

    Otherwise one process tree leaks per turn, holding ports and (on Windows) file locks, and may keep rewriting
    files the next fingerprint then sees. POSIX: the check led its own session, so its pid is the group id and the
    group outlives the leader as long as it has members (the kernel does not reuse a pid that is a live group id).
    Windows: the job object.
    """
    try:
        if job is not None:
            job.terminate()
        elif not _WIN:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # no member left (ProcessLookupError), or not ours to signal
        pass


def _exit_when_terminated() -> Callable[[], None]:
    """POSIX: while a check runs, SIGTERM and SIGHUP end this process through SystemExit, so the `except BaseException`
    in _run_one kills the check's whole process group first. Returns the function that puts the old handlers back.

    Without it a hook that the host ends (Esc during the Stop hook, the session closing) dies at once and the check,
    which leads its own session, keeps running: a test runner, a dev server or a database that holds ports and files
    and collides with the next run. Windows has the job object (KILL_ON_JOB_CLOSE) instead. What stays uncovered: a
    SIGKILL of the hook (the host's own timeout is 170 s, the checks are held to HOOK_BUDGET_S = 150 s, so only a
    manual kill -9 gets there) and the few microseconds between starting the check and installing the handler. There is
    no reliable stdlib way to ask the kernel to kill a child when its parent dies (prctl PR_SET_PDEATHSIG needs libc
    from a preexec_fn, which is unsafe next to the reader threads, and does not exist on macOS), so that is not tried.
    A handler that someone else installed (the host program, nohup's SIGHUP ignore) is left alone. Fails open: off the
    main thread, where signal.signal raises, nothing is installed."""
    if _WIN:
        return lambda: None
    import signal

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)

    previous: dict = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        try:
            if sig is not None and signal.getsignal(sig) == signal.SIG_DFL:
                previous[sig] = signal.signal(sig, on_signal)
        except (ValueError, OSError):
            continue

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass

    return restore


def _resolve_exe(name: str, cwd: Path, path_env: str) -> str | None:
    """Find the executable like a shell would, but never in the repo unless asked with an explicit path.

    shutil.which on Windows looks in the current directory first, so a repo could plant `pytest.bat` next to its
    config; a bare name is therefore only searched on PATH, and only through ABSOLUTE PATH entries: a relative one
    (`node_modules/.bin`, `.`) would be resolved against the hook's working directory, i.e. the repository (same rule
    as procutil.resolve_exe). A relative path with a separator is relative to the check's cwd (POSIX does that
    natively, CreateProcess does not).
    """
    pathext = (
        [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        if _WIN
        else []
    )

    def variants(base: str) -> Iterator[str]:
        if not _WIN:
            yield base
            return
        if os.path.splitext(base)[1].lower() in pathext:
            yield base
        for ext in pathext:
            yield base + ext

    def usable(path: str) -> bool:
        return os.path.isfile(path) and (_WIN or os.access(path, os.X_OK))

    if os.path.isabs(name) or "/" in name or (_WIN and "\\" in name):
        base = name if os.path.isabs(name) else str(cwd / name)
        return next((c for c in variants(base) if usable(c)), None)
    for directory in path_env.split(os.pathsep):
        directory = directory.strip().strip('"')
        if directory and _is_absolute_dir(directory):
            found = next((c for c in variants(os.path.join(directory, name)) if usable(c)), None)
            if found:
                return found
    return None


def _is_absolute_dir(directory: str) -> bool:
    """Anchored somewhere that is not the working directory (on Windows a drive or UNC share, not `\\tools`)."""
    return os.path.isabs(directory) and (not _WIN or bool(os.path.splitdrive(directory)[0]))


_CMD_UNSAFE = frozenset('&|<>^%"\r\n')


def _run_one(
    root: Path, chk: Check, env: dict[str, str], limit: float, clamped: bool
) -> CheckResult:
    import subprocess

    t0 = time.monotonic()

    def refused(rc: int, msg: str) -> CheckResult:
        return CheckResult(
            chk.name, list(chk.argv), rc, time.monotonic() - t0, msg, launch_failed=True
        )

    try:
        cwd = _resolve_cwd(root, chk.cwd)
    except VerifyConfigError as exc:
        return refused(126, f"refused: {exc}")
    if not cwd.is_dir():
        return refused(126, f"cwd {chk.cwd!r} does not exist")
    argv = list(chk.argv)
    if not argv or not all(isinstance(a, str) and a and "\0" not in a for a in argv):
        return refused(126, "refused: argv must be a non-empty list of non-empty strings")
    exe = _resolve_exe(argv[0], cwd, env.get("PATH", ""))
    if exe is None:
        return refused(
            127, f"not found: {argv[0]} (searched PATH; check the name in .claude/{CONFIG_NAME})"
        )
    if _WIN and exe.lower().endswith((".bat", ".cmd")):
        # cmd.exe re-parses the command line: an argument must not be able to smuggle in a second command
        bad = next((a for a in argv[1:] if _CMD_UNSAFE & set(a)), None)
        if bad is not None:
            return refused(126, f"refused: argument {bad!r} has characters cmd.exe would interpret")
    argv[0] = exe
    if not _WIN:
        # what a shell does before it runs a command: the check's directory, not the hook's (GNU make reads $(PWD) and
        # docker compose ${PWD} only from the environment). Windows shells do not set PWD; whatever is there stays.
        env = {**env, "PWD": str(cwd)}
    kwargs: dict = (
        {"creationflags": _NO_WINDOW | _NEW_GROUP} if _WIN else {"start_new_session": True}
    )
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except FileNotFoundError:
        return refused(127, f"not found: {chk.argv[0]}")
    except (OSError, ValueError) as exc:
        return refused(126, f"could not start {chk.argv[0]}: {exc}")
    job = _Job(proc) if _WIN else None
    tail = _Tail(proc.stdout)
    timed_out = False
    restore_signals = _exit_when_terminated()
    try:
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc, job)
        else:
            _reap_group(proc, job)
    except BaseException:
        # includes the SystemExit of _exit_when_terminated: the check's group dies with the hook
        _kill_tree(proc, job)
        raise
    finally:
        restore_signals()
        if job is not None:
            job.close()
    # a grandchild that outlives the check may keep the pipe open: do not wait for EOF forever
    tail.thread.join(2.0 if timed_out else 1.0)
    rc = 124 if timed_out else proc.returncode
    return CheckResult(
        chk.name,
        list(chk.argv),
        rc,
        time.monotonic() - t0,
        _tail_text(tail.data()),
        timed_out=timed_out,
        clamped=timed_out and clamped,
    )


def _budget_cut(own_s: float, limit: float) -> bool:
    """Did the overall budget shorten a check's own timeout by a meaningful amount?

    Only then is a timeout "inconclusive" (the suite may simply have needed more time). A check whose own timeout_s
    equals the budget gets a hair less than that (the clock has already ticked), which is not a cut: it ran for as
    long as it was allowed to and a hang is a failure. A meaningful amount is more than a second or more than a tenth
    of its own limit, whichever is smaller."""
    return own_s - limit > min(1.0, 0.1 * own_s)


def run(
    root: Path,
    checks: list[Check],
    *,
    total_timeout_s: float = 300,
    stop_on_fail: bool = False,
    env_allow: Collection[str] | str = (),
) -> RunResult:
    """Run checks in order. No shell, scrubbed env, per-check timeout, the process tree dies on timeout, and what a
    check leaves running when it exits on its own (a dev server, a watcher) is ended with it.

    Never raises: a missing tool is a result with rc 127 (launch_failed). `total_timeout_s` bounds the whole run (each
    check gets the smaller of its own limit and what is left; see _budget_cut for when a timeout counts as cut short).
    The gate passes stop_on_fail=True, `verify run` wants them all. stop_on_fail stops at a check that RAN and failed:
    one that could not be launched (a tool that is not installed) does not hide the checks after it, and the first
    check's missing tool must not switch the whole gate off. `env_allow` is verify.env_allow (see scrubbed_env).
    """
    root = Path(root)
    env = scrubbed_env(allow=env_allow)
    start = time.monotonic()
    results: list[CheckResult] = []
    skipped: list[str] = []
    for i, chk in enumerate(checks):
        remaining = total_timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            skipped += [c.name for c in checks[i:]]
            break
        limit = min(chk.timeout_s, remaining)
        res = _run_one(root, chk, env, limit, _budget_cut(chk.timeout_s, limit))
        results.append(res)
        if res.rc != 0 and stop_on_fail and not res.launch_failed:
            skipped += [c.name for c in checks[i + 1 :]]
            break
    return RunResult(results, time.monotonic() - start, skipped)


# ---------------------------------------------------------------------------------------------------------------
# git helpers, fingerprint
# ---------------------------------------------------------------------------------------------------------------
def _toplevel(root: Path) -> Path:
    """The work-tree root, found without spawning git (the fingerprint runs at the end of every turn)."""
    base = Path(os.path.realpath(root))
    return next((c for c in (base, *base.parents) if (c / ".git").exists()), base)


_GIT_EXE: dict[str, str] = {}  # PATH value -> the git it resolved to (a successful lookup only)


def _git_exe() -> str | None:
    """Absolute path of git, found only through absolute PATH entries (procutil.resolve_exe): Popen(["git"]) on
    Windows would first try the process's own current directory, which is the repository."""
    path = os.environ.get("PATH", "")
    exe = _GIT_EXE.get(path)
    if exe is None:
        from . import procutil

        exe = procutil.resolve_exe("git")
        if exe is not None:
            if len(_GIT_EXE) > 4:
                _GIT_EXE.clear()
            _GIT_EXE[path] = exe
    return exe


def _git_timeout(deadline: float | None, cap: float = 20.0) -> float | None:
    """Seconds one git call may take: `cap`, less what the overall deadline (a time.monotonic() value) leaves.
    None when (almost) nothing is left: the caller then gives up instead of starting a call it cannot finish."""
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    return min(cap, left) if left > 0.25 else None


def _git(
    top: Path, *args: str, cap: int = 16_000_000, timeout: float = 20.0
) -> tuple[bytes, bool] | None:
    """(stdout, truncated) of a git command, None if it failed or timed out. Output beyond `cap` is dropped."""
    import subprocess
    import threading

    exe = _git_exe()
    if exe is None:
        return None
    kwargs: dict = {"creationflags": _NO_WINDOW} if _WIN else {}
    try:
        proc = subprocess.Popen(
            [exe, "--no-optional-locks", *args],
            cwd=str(top),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except (OSError, ValueError):
        return None
    timer = threading.Timer(timeout, _kill_quietly, (proc,))
    timer.daemon = True
    timer.start()
    chunks: list[bytes] = []
    total = 0
    truncated = False
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            if total + len(chunk) > cap:
                chunks.append(chunk[: cap - total])
                truncated = True
                _kill_quietly(proc)
                break
            chunks.append(chunk)
            total += len(chunk)
    except (OSError, ValueError):
        _kill_quietly(proc)
        return None
    finally:
        timer.cancel()
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_quietly(proc)
    if truncated:
        return b"".join(chunks), True
    return (b"".join(chunks), False) if proc.returncode == 0 else None


def _git_parallel(top: Path, jobs: list[tuple[tuple[str, ...], dict]]) -> list:
    """Run independent git commands at once: on Windows each spawn costs ~50 ms and the gate runs every turn."""
    import threading

    results: list = [None] * len(jobs)

    def work(i: int) -> None:
        try:
            results[i] = _git(top, *jobs[i][0], **jobs[i][1])
        except Exception:
            results[i] = None

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(1, len(jobs))]
    for t in threads:
        t.start()
    work(0)
    for t in threads:
        t.join()
    return results


def _status_entries(raw: bytes) -> list[tuple[str, str]]:
    """(XY, path) from `git status --porcelain=v1 -z`; a rename/copy entry is followed by its source path."""
    tokens = raw.split(b"\0")
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4:
            continue
        code = tok[:2].decode("ascii", "replace")
        if "R" in code or "C" in code:
            i += 1
        # POSIX names are bytes: surrogateescape keeps a non-UTF-8 name equal to what os.scandir reports
        path = tok[3:].decode("utf-8", "replace") if _WIN else os.fsdecode(tok[3:])
        out.append((code, path))
    return out


def _hash_stats(h, top: Path, rels: list[str]) -> None:
    """Fold (size, mtime_ns) of every path in `rels` (git's `/`-separated, relative to `top`) into `h`.

    One directory listing per directory instead of one lstat per file: on Windows a scandir entry carries its stat
    for free, which is ~10x cheaper (5000 files: 0.03 s instead of 0.3 s) and the fingerprint is taken up to three
    times per turn. A path that has vanished is hashed as "gone". Deterministic: sorted, no timing dependence.
    """
    by_dir: dict[str, list[str]] = {}
    for rel in rels:
        head, _, name = rel.rstrip("/").rpartition("/")  # a nested repository is listed as `dir/`
        by_dir.setdefault(head, []).append(name)
    base = str(top)
    for head in sorted(by_dir):
        wanted = set(by_dir[head])
        found: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(f"{base}/{head}" if head else base) as listing:
                for entry in listing:
                    if entry.name in wanted:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        found[entry.name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
        for name in sorted(wanted):
            stat = found.get(name)
            where = f"{head}/{name}" if head else name
            tail = f"{stat[0]}\0{stat[1]}" if stat else "gone"
            h.update(f"{where}\0{tail}\0".encode("utf-8", "replace"))


def fingerprint(root: Path, *, deadline: float | None = None) -> str:
    """Hash of the working-tree change: HEAD, `status`, the `diff HEAD` content, untracked files' (size, mtime).

    Equal fingerprints mean "nothing Claude could have changed since last time", so the gate skips. Content is
    hashed for tracked files (cheap, exact); untracked files only by (size, mtime_ns) - a new file's bytes are
    not in `diff HEAD` - and ALL of them: there is no cap, so an in-place edit of any untracked file moves the value
    (a freshly scaffolded repo, an unignored node_modules). The only limit is git's own: a `status` listing past
    16 MB (roughly 250,000 untracked paths) is "cannot tell". A diff over 4 MB is hashed up to the cap and the
    tracked files' (size, mtime_ns) are added so a change past the cap still moves the value.

    Returns "" when git is unavailable, this is not a work tree, the listing is truncated or `deadline` (a
    time.monotonic() value) has passed (never raises): the caller treats that as "cannot tell".
    """
    try:
        return _fingerprint(Path(root), deadline)
    except Exception:
        return ""


def _fingerprint(root: Path, deadline: float | None = None) -> str:
    import hashlib

    timeout = _git_timeout(deadline)
    if timeout is None:
        return ""
    top = _toplevel(root)
    status, head, diff = _git_parallel(
        top,
        [
            (("status", "--porcelain=v1", "-z", "-uall"), {"timeout": timeout}),
            (("rev-parse", "--verify", "-q", "HEAD"), {"timeout": timeout}),
            (
                ("diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"),
                {"cap": _DIFF_CAP, "timeout": timeout},
            ),
        ],
    )
    if status is None or status[1]:
        return ""
    h = hashlib.sha1(usedforsecurity=False)
    h.update(b"hr-verify-fp1\0")
    h.update((head[0].strip() if head else b"unborn") + b"\0")
    h.update(status[0] + b"\0")
    oversize = False
    if diff is None:  # no commits yet: `diff HEAD` has nothing to compare with
        h.update(b"no-diff\0")
    else:
        h.update(hashlib.sha1(diff[0], usedforsecurity=False).digest())
        oversize = diff[1]
        if oversize:
            h.update(b"oversize\0")
    entries = _status_entries(status[0])
    watched = [p for code, p in entries if code == "??"]
    if oversize:
        watched += [p for code, p in entries if code != "??"]
    _hash_stats(h, top, watched)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# test-weakening advisory
# ---------------------------------------------------------------------------------------------------------------
_CODE_EXTS = frozenset(
    "py js jsx ts tsx mjs cjs java kt kts go rs rb php cs scala swift c cc cpp h hpp ex exs clj cljs sh lua dart".split()
)
_TEST_DIR_RE = re.compile(r"(?:^|/)(?i:tests?|specs?|__tests__|__test__)/")
_TEST_NAME_RE = re.compile(
    r"(?:^|/)(?:test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(?:test|spec)\.[^/]+|[^/]*_spec\.[^/]+"
    r"|[^/]*Tests?\.(?:java|kt|cs|scala|swift)|conftest\.py)$"
)
_ASSERT_RE = re.compile(
    r"\bassert\w*\s*[(!]|\bassert\s+\S|\bexpect\s*\(|\bexpect_\w+|\.should\b"
    r"|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\bpytest\.raises\b|\bverify\s*\(|\brequire\.\w+\s*\("
)
_SKIP_RES = [
    re.compile(
        r"@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail)\s*\("
        r"|@unittest\.(?:skip\w*|expectedFailure)\b|@(?:skip|skipIf|skipUnless|expectedFailure)\b"
    ),
    re.compile(
        r"\b(?:it|test|describe|context|suite|specify|spec)\.(?:skip|todo|failing)\b"
        r"|\b(?:xit|xtest|xdescribe|xcontext|xspecify)(?=\s*\(|\s+[\"'])"
    ),
    re.compile(r"\b\w+\.Skip(?:f|Now)?\s*\("),
    re.compile(r"@(?:Ignore|Disabled|Pending)\b|\[(?:Ignore|Skip)\]|\[Fact\(Skip"),
    re.compile(r"#\[ignore\b"),
    re.compile(r"\b(?:it|test|describe)\.only\s*\("),
    re.compile(r"^\s*(?:skip|pending)\b(?:\s+[\"']|\s*\()"),
]
_SUPPRESS_RE = re.compile(
    r"#\s*noqa\b|#\s*type:\s*ignore\b|#\s*pyright:\s*ignore\b|#\s*pylint:\s*disable\b|#\s*nosec\b"
    r"|@ts-(?:ignore|expect-error|nocheck)\b|eslint-disable|//\s*nolint\b|#\[allow\("
)
_NUM = r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?"
# These two run over text that comes from the repository, so they must stay LINEAR in the length of a line (no
# `\w*timeout\w*`-style nested/overlapping repeats: `timeout` x n made that one quadratic and stalled the hook past
# its deadline). The keyword is any identifier of up to 64 characters followed by `=`/`:`/`(` and a number; whether
# it is a tolerance or timeout word is decided afterwards (_is_tolerance_word). `\s*` and the following class never
# overlap, so every position has one way to match.
_TOL_WORDS = frozenset(
    "abs rel atol rtol delta tol tolerance epsilon eps places decimal digits precision".split()
)
_TOL_KW_RE = re.compile(rf"\b(\w{{1,64}})\s*[=:(]\s*(?P<n>{_NUM})(?![\w.])")
_TOL_POS_RE = re.compile(
    rf"\b(approx|toBeCloseTo)\s*\(\s*[^,()\s][^,()]*,\s*(?P<n>{_NUM})(?![\w.])"
)
_NUM_RE = re.compile(rf"(?<![\w.])(?:{_NUM})(?![\w.])")
# for these a smaller number is a looser check (fewer digits compared)
_INVERTED = frozenset({"places", "decimal", "digits", "precision", "tobecloseto"})


def _is_test_path(path: str) -> bool:
    if len(path) > 1024:  # no real checkout has one; the pattern matching below stays cheap
        return False
    p = path.replace("\\", "/")
    ext = p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
    return ext in _CODE_EXTS and bool(_TEST_DIR_RE.search(p) or _TEST_NAME_RE.search(p))


def _is_comment(stripped: str) -> bool:
    return stripped.startswith(("//", "/*", "*", "--")) or (
        stripped.startswith("#") and not stripped.startswith("#[")
    )


@dataclass
class _FileDiff:
    path: str = ""
    deleted: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _diff_path(raw: str) -> str:
    raw = raw.rstrip("\t").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def _parse_diff(text: str) -> list[_FileDiff]:
    """Per-file added/removed lines from `git diff -U0`. Header lines are only trusted before the first hunk."""
    files: list[_FileDiff] = []
    cur: _FileDiff | None = None
    in_header = False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = _FileDiff()
            files.append(cur)
            in_header = True
            m = re.match(r"diff --git a/(.+) b/\1$", line)
            if m:
                cur.path = m.group(1)
        elif cur is None:
            continue
        elif in_header:
            if line.startswith("deleted file mode"):
                cur.deleted = True
            elif line.startswith("rename to "):
                cur.path = line[len("rename to ") :].strip()
            elif line.startswith("--- "):
                if line[4:].strip() != "/dev/null" and not cur.path:
                    cur.path = _diff_path(line[4:])
            elif line.startswith("+++ "):
                if line[4:].strip() != "/dev/null":
                    cur.path = _diff_path(line[4:])
            elif line.startswith("@@"):
                in_header = False
        elif line.startswith("+"):
            cur.added.append(line[1 : _MAX_LINE + 1])
        elif line.startswith("-"):
            cur.removed.append(line[1 : _MAX_LINE + 1])
    return files


def _code_lines(lines: list[str]) -> list[str]:
    return [s for s in (ln.strip() for ln in lines) if s and not _is_comment(s)]


def _skip_hits(line: str) -> str | None:
    for rx in _SKIP_RES:
        m = rx.search(line)
        if m:
            return m.group(0).strip()[:30]
    return None


def _is_tolerance_word(word: str) -> bool:
    return word in _TOL_WORDS or "timeout" in word


def _tolerances(line: str) -> dict[str, float]:
    found: dict[str, float] = {}
    line = line[:_MAX_LINE]
    for rx in (_TOL_KW_RE, _TOL_POS_RE):
        for m in rx.finditer(line):
            word = m.group(1).lower()
            if rx is _TOL_KW_RE and not _is_tolerance_word(word):
                continue
            try:
                found.setdefault(word, float(m.group("n")))
            except ValueError:
                continue
    return found


def _skeleton(line: str) -> str:
    return _NUM_RE.sub("#", " ".join(line.split()))


def _widened(removed: list[str], added: list[str], deadline: float | None = None) -> list[str]:
    """Tolerance/timeout keywords whose number grew (or whose digit count shrank) between a removed and an added line."""
    by_skeleton: dict[str, list[str]] = {}
    for line in added:
        by_skeleton.setdefault(_skeleton(line), []).append(line)
    hits: list[str] = []
    for line in removed:
        if deadline is not None and time.monotonic() > deadline:
            break  # advisory only: out of time means fewer hints, never a stalled hook
        old = _tolerances(line)
        if not old:
            continue
        for cand in by_skeleton.get(_skeleton(line), [])[:1]:
            new = _tolerances(cand)
            for key, before in old.items():
                after = new.get(key)
                if after is None or after == before:
                    continue
                if (after < before) if key in _INVERTED else (after > before):
                    hits.append(key[:30])
    return hits


def _clean(path: str) -> str:
    return safe_text(re.sub(r"[\x00-\x1f\x7f]+", " ", path)[:100])


def _advisories(diff_text: str, deadline: float | None = None) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for idx, fd in enumerate(_parse_diff(diff_text)):
        if deadline is not None and time.monotonic() > deadline:
            break
        if not fd.path or not _is_test_path(fd.path):
            continue
        name = _clean(fd.path)

        def add(rank: int, what: str, idx: int = idx, name: str = name) -> None:
            ranked.append((rank, idx, f"{name}: {what}"))

        if fd.deleted:
            add(0, "test file deleted")
            continue
        removed, added = _code_lines(fd.removed), _code_lines(fd.added)
        gone = Counter(removed)
        new_skips = []
        for line in added:
            label = _skip_hits(line)
            if label is None:
                continue
            if gone[line] > 0:  # the same marker was already there (moved or reformatted)
                gone[line] -= 1
            else:
                new_skips.append(label)
        if new_skips:
            more = f" +{len(new_skips) - 1} more" if len(new_skips) > 1 else ""
            add(1, f"skip/xfail added ({new_skips[0]}{more})")
        lost = sum(bool(_ASSERT_RE.search(s)) for s in removed) - sum(
            bool(_ASSERT_RE.search(s)) for s in added
        )
        if lost > 0:
            add(2, f"{lost} assertion line(s) removed without a replacement")
        keys = sorted(set(_widened(removed, added, deadline)))
        if keys:
            add(3, f"tolerance/timeout widened ({', '.join(keys[:3])})")
        net = sum(bool(_SUPPRESS_RE.search(s)) for s in fd.added) - sum(
            bool(_SUPPRESS_RE.search(s)) for s in fd.removed
        )
        if net > 0:
            marker = next(
                (m.group(0) for s in fd.added if (m := _SUPPRESS_RE.search(s))), "suppression"
            )
            add(4, f"lint/type suppression added ({marker[:30]})")
    ranked.sort()
    return [text for _, _, text in ranked[:_ADVISORY_CAP]]


def test_weakening(root: Path, *, deadline: float | None = None) -> list[str]:
    """ADVISORY regex scan of `git diff -U0 HEAD` for ways a change could make the tests pass by checking less.

    At most 5 short '<file>: <what>' strings; [] when nothing stands out or git cannot answer. It only flags,
    it never blocks by itself, and it never raises. Only tracked changes are in `diff HEAD`; a brand-new
    untracked test file is not scanned. Every line is looked at only up to _MAX_LINE characters, the patterns are
    linear, and the scan stops after _ADVISORY_BUDGET_S seconds (or at `deadline`, a time.monotonic() value): the
    text is the repository's own and must not be able to stall the end-of-turn hook.
    """
    try:
        timeout = _git_timeout(deadline)
        if timeout is None:
            return []
        res = _git(
            _toplevel(Path(root)),
            "-c",
            "core.quotepath=off",
            "diff",
            "-U0",
            "-M",
            "HEAD",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            cap=2 * 1024 * 1024,
            timeout=timeout,
        )
        if res is None:
            return []
        stop = time.monotonic() + _ADVISORY_BUDGET_S
        if deadline is not None:
            stop = min(stop, deadline)
        return _advisories(res[0].decode("utf-8", errors="replace"), stop)
    except Exception:
        return []


# pytest would otherwise try to collect this from the namespace of a test module that imports it
test_weakening.__test__ = False


# ---------------------------------------------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------------------------------------------
@contextmanager
def _state_lock(state_dir: Path) -> Iterator[None]:
    """Serialise read-modify-write of the gate state between sessions; best effort, never blocks the hook for long."""
    lock = None
    try:
        from .filelock import locked

        lock = locked(Path(state_dir) / "verify-gate.lock", timeout=2.0, stale_after=60.0)
        lock.__enter__()
    except (OSError, TimeoutError):
        lock = None  # an unlocked update can at worst lose one bookkeeping entry
    try:
        yield
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _record(state_dir: Path, key: str, update) -> None:
    path = Path(state_dir) / "verify-gate.json"
    with _state_lock(state_dir):
        state = _read_dict(path)
        entry = state.pop(key, None)  # re-insert below: the dict order doubles as recency
        entry = entry if isinstance(entry, dict) else {}
        update(entry)
        state[key] = entry
        while len(state) > MAX_REPOS:
            state.pop(next(iter(state)))
        atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)


def _gate_entry(state_dir: Path, key: str) -> dict:
    entry = _read_dict(Path(state_dir) / "verify-gate.json").get(key)
    return entry if isinstance(entry, dict) else {}


def _mark_green(fp: str, fp_after: str = ""):
    """Remember `fp` (the tree the checks ran on) as green, and `fp_after` (the tree they left: a check may write
    coverage files, a test database, a log) so the next look at that tree is not mistaken for a change."""

    def update(entry: dict) -> None:
        entry["last_green"] = fp
        if fp_after and fp_after != fp:
            entry["last_green_after"] = fp_after
        else:
            entry.pop("last_green_after", None)
        entry["blocks"] = {}
        entry.pop("last_block", None)

    return update


def _mark_block(fp: str, summary: str, fp_after: str = ""):
    """Count one block for the change `fp`, and under `fp_after` too: the next Stop fingerprints the tree the failing
    check left behind, and that must count as the same change or a check that writes a file never reaches the cap."""

    def update(entry: dict) -> None:
        raw = entry.get("blocks")
        blocks = dict(raw) if isinstance(raw, dict) else {}
        count = _count(blocks.pop(fp, 0)) + 1
        blocks[fp] = count
        last = {"fp": fp, "summary": summary}
        if fp_after and fp_after != fp:
            blocks[fp_after] = max(count, _count(blocks.pop(fp_after, 0)))
            last["fp_after"] = fp_after
        while len(blocks) > MAX_BLOCK_FPS:
            blocks.pop(next(iter(blocks)))
        entry["blocks"] = blocks
        entry["last_block"] = last

    return update


def _failure_summary(res: CheckResult) -> str:
    detail = f"exit {res.rc}"
    if res.timed_out:
        detail += f", timed out after {res.seconds:.0f}s"
    return f"claude-hr verify: {_clean(res.name)} failed ({detail})"


def gate(
    root: Path,
    state_dir: Path,
    *,
    max_blocks: int = 2,
    timeout_s: float = 120,
    tier: str = "fast",
    test_guard: bool = True,
    deadline: float | None = None,
    env_allow: Collection[str] | str = (),
) -> GateOutcome:
    """Run the trusted recipe's tier for the work tree as it is now. Never raises, never runs untrusted config.

    skip  - nothing to do: no/untrusted/empty config, nothing changed since the last green run, or this change
            already blocked `max_blocks` times (the reason still names the failure), or the run was inconclusive,
            or another gate run on this repository is still going (note "another gate run in progress")
    pass  - every check passed; the fingerprint is remembered as green
    block - a check failed; `reason` is what to hand the model (summary line, output tail, test-weakening hints)
    error - something outside the code under test went wrong (broken state file, bug), or a check could not be
            launched (a tool that is not installed; note "cannot start": the checks that could run all passed): fail open

    `deadline` (a time.monotonic() value) is the caller's overall budget, the end-of-turn hook's, which a killed
    hook would silently turn into a lost block. The checks get min(timeout_s, what is left minus
    POST_RUN_RESERVE_S), every git call is bounded by it, and with less than POST_RUN_RESERVE_S + MIN_RUN_S left
    nothing is started (skip, note "time budget"). A check that hits ITS OWN timeout_s is a failed check (block); one
    that was cut short by this overall budget (it could have run for meaningfully longer on its own limit) is
    inconclusive: skip, note "time budget", `check` names it. `env_allow` is verify.env_allow (see scrubbed_env).
    """
    t0 = time.monotonic()
    try:
        out = _gate(
            Path(root),
            Path(state_dir),
            max_blocks,
            timeout_s,
            tier,
            test_guard,
            deadline,
            env_allow,
        )
    except Exception as exc:
        out = GateOutcome("error", f"verify gate failed: {type(exc).__name__}: {exc}")
    out.seconds = time.monotonic() - t0
    return out


def _fingerprint_by(root: Path, deadline: float | None) -> str:
    return fingerprint(root) if deadline is None else fingerprint(root, deadline=deadline)


def _run_lock_path(state_dir: Path, key: str) -> Path:
    return Path(state_dir) / f"verify-run-{_sha256(key.encode('utf-8', 'replace'))[:16]}.lock"


@contextmanager
def _run_lock(state_dir: Path, key: str, wait_s: float, stale_s: float) -> Iterator[bool]:
    """Held across one gate run of one repository, so that two sessions that stop within one suite's run time do not
    run the suite at once (a port, a sqlite file, a build directory: the second would fail on what the first holds, and
    its model would be told to fix a failure its code did not cause).

    Yields True with the lock held; False when another run still holds it after `wait_s` (the caller skips); True
    without a lock when none can be made (a read-only state directory): this never blocks the hook for long and never
    fails it. A lock whose owner process is gone is taken over at once, and one older than `stale_s` (longer than any
    run can last, see _gate) whoever owns it."""
    from .filelock import LockTimeout, locked

    lock = None
    busy = False
    try:
        lock = locked(_run_lock_path(state_dir, key), timeout=max(0.0, wait_s), stale_after=stale_s)
        lock.__enter__()
    except LockTimeout:
        lock, busy = None, True
    except OSError:
        lock = None
    try:
        yield not busy
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _gate(
    root: Path,
    state_dir: Path,
    max_blocks: int,
    timeout_s: float,
    tier: str,
    test_guard: bool,
    deadline: float | None = None,
    env_allow: Collection[str] | str = (),
) -> GateOutcome:
    def skip(reason: str, fp: str = "") -> GateOutcome:
        return GateOutcome("skip", reason, fp)

    def room() -> float:
        """Seconds the checks themselves may take under the caller's deadline (inf without one)."""
        if deadline is None:
            return float("inf")
        return deadline - time.monotonic() - POST_RUN_RESERVE_S

    def out_of_time(fp: str = "") -> GateOutcome:
        left = max(0.0, room() + POST_RUN_RESERVE_S)
        return GateOutcome(
            "skip",
            f"time budget: {left:.0f}s of the hook's deadline left, too little to run the checks; not blocking",
            fp,
            note=NOTE_TIME_BUDGET,
        )

    if os.environ.get(ACTIVE_ENV):
        return skip("running inside a verify check; not nesting")
    if tier not in ("fast", "full"):
        return GateOutcome("error", f"unknown verify tier {tier!r} (use fast or full)")
    try:
        data = _read_config(root)
    except VerifyConfigError as exc:
        return GateOutcome("error", f"claude-hr verify: {exc}")
    if data is None:
        return skip(f"no .claude/{CONFIG_NAME}")
    key = _repo_key(root)
    recorded = _trusted_hash(root, state_dir)
    if recorded != _sha256(data):
        why = "changed since it was trusted" if recorded else "not trusted"
        return skip(
            f".claude/{CONFIG_NAME} {why} (review it, then run: claude-hr-ctl verify trust)"
        )
    try:
        recipe = _parse(data, root, str(config_path(root)))
    except VerifyConfigError as exc:
        return GateOutcome("error", f"claude-hr verify: {exc}")
    checks = (recipe.full or recipe.fast) if tier == "full" else recipe.fast
    if not checks:
        return skip(f"no {tier} checks in .claude/{CONFIG_NAME}")
    if max_blocks < 1:
        return skip("blocking is disabled (gate_max_blocks < 1)")
    if room() < MIN_RUN_S:
        return out_of_time()

    def look_and_run() -> GateOutcome:
        fp = _fingerprint_by(root, deadline)
        if not fp:
            return skip(
                "cannot fingerprint the work tree (git missing, not a repository, too many untracked files "
                "or out of time)"
            )
        entry = _gate_entry(state_dir, key)
        if fp in (entry.get("last_green"), entry.get("last_green_after")):
            return skip("unchanged since the last passing run", fp)
        blocks = entry.get("blocks")
        seen = _count(blocks.get(fp)) if isinstance(blocks, dict) else 0
        if seen >= max_blocks:
            last = entry.get("last_block")
            same = isinstance(last, dict) and fp in (last.get("fp"), last.get("fp_after"))
            summary = last.get("summary") if same else None
            tail = f"; last failure: {summary}" if isinstance(summary, str) else ""
            return skip(f"block cap reached ({seen}/{max_blocks}) for this change{tail}", fp)
        if room() < MIN_RUN_S:  # the fingerprint and the state lookup may have used what was left
            return out_of_time(fp)
        budget = min(timeout_s, room())

        rr = run(root, checks, total_timeout_s=budget, stop_on_fail=True, env_allow=env_allow)
        # a check that could not be launched is a setup problem, not a verdict on the code: it never hides the
        # checks after it (run() goes on) and it is not the failure the model is told about
        failed = next((r for r in rr.results if r.rc != 0 and not r.launch_failed), None)
        unlaunched = [r for r in rr.results if r.launch_failed]
        if failed is None and not rr.skipped:
            if unlaunched:
                first = unlaunched[0]
                more = f" (and {len(unlaunched) - 1} more)" if len(unlaunched) > 1 else ""
                # nothing is recorded as green (not every check ran). The checks that could run passed: the hook
                # remembers this tree (note "cannot start") so an idle Stop does not run them again, and the reason
                # is in the audit trail.
                return GateOutcome(
                    "error",
                    f"claude-hr verify: cannot run {_clean(first.name)}: {_comment(first.output, 300)}{more}",
                    fp,
                    rr.results,
                    rr.seconds,
                    note=NOTE_CANNOT_START,
                )
            # the tree the checks leave behind (coverage files, a test database) is what the next Stop will look at
            after = _fingerprint_by(root, deadline)
            try:
                _record(state_dir, key, _mark_green(fp, after))
            except OSError:
                pass  # not remembering a green run only costs one re-run
            return GateOutcome(
                "pass",
                f"{len(rr.results)} check(s) passed in {rr.seconds:.1f}s",
                fp,
                rr.results,
                rr.seconds,
            )
        if failed is None:
            done = f"{len(rr.results)} of {len(checks)}"
            return GateOutcome(
                "skip",
                f"time budget ({budget:g}s) used up after {done} checks; not blocking",
                fp,
                rr.results,
                note=NOTE_TIME_BUDGET,
                check=_clean(rr.skipped[0]),
            )
        if failed.clamped:
            return GateOutcome(
                "skip",
                f"time budget ({budget:g}s) ran out inside {_clean(failed.name)}; inconclusive, not blocking",
                fp,
                rr.results,
                note=NOTE_TIME_BUDGET,
                check=_clean(failed.name),
            )
        summary = _failure_summary(failed)
        lines = [summary]
        if failed.output:
            lines.append(failed.output)
        # the failing check may have written files; the next Stop fingerprints that tree and must find the count there
        after = _fingerprint_by(root, deadline)
        if test_guard:
            advice = (
                test_weakening(root)
                if deadline is None
                else test_weakening(root, deadline=deadline)
            )
            lines += [f"Possible test weakening: {adv}" for adv in advice]
        try:
            _record(state_dir, key, _mark_block(fp, summary, after))
        except OSError as exc:
            # without a block counter the cap cannot work: fail open instead of nagging forever
            return GateOutcome(
                "error",
                f"claude-hr verify: cannot record gate state ({exc}); {summary}",
                fp,
                rr.results,
            )
        return GateOutcome("block", "\n".join(lines), fp, rr.results)

    # One run per repository at a time, from the fingerprint to the state write: a second session that Stops while the
    # first still runs the suite waits a moment (and then finds the first one's verdict), or skips and is retried on its
    # next Stop. The lock outlives any run: the checks get at most `timeout_s`, the hook at most its deadline.
    stale_s = max(180.0, float(timeout_s) + 60.0)
    wait_s = max(0.0, min(RUN_LOCK_WAIT_S, room() - MIN_RUN_S))
    with _run_lock(state_dir, key, wait_s, stale_s) as held:
        if not held:
            return GateOutcome(
                "skip",
                "another gate run is in progress for this repository; not running the checks twice at once",
                note=NOTE_BUSY,
            )
        return look_and_run()


# ---------------------------------------------------------------------------------------------------------------
# discovery: propose a recipe from what the repo already declares (reads files, executes nothing)
# ---------------------------------------------------------------------------------------------------------------
_RANK = {"lint": 0, "typecheck": 1, "check": 2, "test": 3, "build": 4}
_TIMEOUTS = {"lint": 90, "typecheck": 120, "check": 120, "test": 180, "build": 300}


class _Builder:
    def __init__(self) -> None:
        self.fast: list[tuple[int, Check]] = []
        self.full_only: list[tuple[int, Check]] = []
        self.categories: set[str] = set()
        self.notes: list[str] = []
        self.sources: list[str] = []

    def add(
        self,
        category: str,
        name: str,
        argv: list[str],
        *,
        tier: str = "fast",
        timeout: int | None = None,
    ) -> None:
        limit = timeout or _TIMEOUTS[category]
        if tier == "fast":
            # the end-of-turn gate gives ALL the checks together verify.gate_timeout_s: a longer own limit could never
            # be reached there (the run is cut short and reported as inconclusive), so it is not proposed
            limit = min(limit, DEFAULT_GATE_TIMEOUT_S)
        chk = Check(name, list(argv), limit)
        if tier == "fast":
            self.fast.append((_RANK[category], chk))
            self.categories.add(category)
        else:
            self.full_only.append((_RANK[category], chk))

    def recipe(self) -> Recipe:
        # cheapest first: with stop-at-first-failure a lint error should not wait for the test suite
        fast = [c for _, c in sorted(self.fast, key=lambda t: t[0])]
        full = [c for _, c in sorted(self.fast + self.full_only, key=lambda t: t[0])]
        notes = list(self.notes)
        if len(fast) > MAX_CHECKS or len(full) > MAX_CHECKS:
            notes.append(
                f"more than {MAX_CHECKS} checks were found; kept the first {MAX_CHECKS} per list"
            )
        source = (
            ("discovered from " + ", ".join(self.sources))
            if self.sources
            else "discovered: nothing recognised"
        )
        return Recipe(fast[:MAX_CHECKS], full[:MAX_CHECKS], source, notes)


def _read_text(path: Path, limit: int = 256_000) -> str | None:
    """The first `limit` bytes of a regular file as text (docs, CI files and Makefiles are only searched, a prefix is
    enough). None when there is no such file, it is not a regular file, or it cannot be read."""
    try:
        return _read_file(path, limit)[0].decode("utf-8", errors="replace")
    except OSError:
        return None


# no real package.json / pyproject.toml / setup.cfg is anywhere near this (fmt.py and initrepo.py use the same bound)
_MAX_MANIFEST_BYTES = 1_000_000


def _read_manifest(path: Path, notes: list[str]) -> str | None:
    """A manifest read WHOLE: a cut-off package.json or pyproject.toml would not parse and discovery would then report
    "nothing recognised" about a repository it simply did not read. None (with a note saying why) for a file over
    _MAX_MANIFEST_BYTES or one that is not a regular file; None without a note when there is no file."""
    try:
        data, more = _read_file(path, _MAX_MANIFEST_BYTES)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except _NotRegular:
        notes.append(f"{path.name} is not a regular file: not inspected")
        return None
    except OSError:
        return None
    if more:
        notes.append(
            f"{path.name} is too large to inspect (over {_MAX_MANIFEST_BYTES // 1000} KB): "
            "nothing was proposed from it"
        )
        return None
    return data.decode("utf-8", errors="replace")


def _load_json(path: Path, notes: list[str]) -> object:
    text = _read_manifest(path, notes)
    try:
        return json.loads(text) if text is not None else None
    except (ValueError, RecursionError):
        return None


def _load_toml(path: Path, notes: list[str]) -> dict:
    import tomllib

    text = _read_manifest(path, notes)
    try:
        doc = tomllib.loads(text) if text is not None else {}
    except (ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _node_pm(root: Path, pkg: dict) -> str:
    for lock, pm in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
        ("package-lock.json", "npm"),
        ("npm-shrinkwrap.json", "npm"),
    ):
        if (root / lock).exists():
            return pm
    declared = pkg.get("packageManager")
    name = declared.split("@", 1)[0] if isinstance(declared, str) else ""
    return name if name in {"npm", "pnpm", "yarn", "bun"} else "npm"


_WATCH_RE = re.compile(r"--watch(?:All)?\b(?!\s*=\s*false)")


def _discover_node(root: Path, b: _Builder) -> None:
    pkg = _load_json(root / "package.json", b.notes)
    if not isinstance(pkg, dict):
        return
    b.sources.append("package.json")
    scripts = pkg.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    pm = _node_pm(root, pkg)
    seen: set[str] = set()
    for script, category, tier in (
        ("lint", "lint", "fast"),
        ("typecheck", "typecheck", "fast"),
        ("type-check", "typecheck", "fast"),
        ("check", "check", "fast"),
        ("test:unit", "test", "fast"),
        ("test", "test", "fast"),
        ("build", "build", "full"),
    ):
        body = scripts.get(script)
        if not isinstance(body, str) or not body.strip():
            continue
        if "no test specified" in body or (category == "test" and _WATCH_RE.search(body)):
            b.notes.append(
                f"package.json script '{script}' is a placeholder or a watcher; not proposed"
            )
            continue
        if category in seen and tier == "fast":
            if category == "test":
                tier = "full"  # test:unit is the fast one, the plain `test` may include slow suites
            else:
                continue
        seen.add(category)
        b.add(category, f"{pm} {script}", [pm, "run", script], tier=tier)
    if b.fast and not (root / "node_modules").exists():
        b.notes.append(
            "node_modules is absent here: install dependencies before trusting the node checks"
        )


_DOC_FILES = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    "CLAUDE.md",
    "AGENTS.md",
    "Makefile",
    "makefile",
    "justfile",
    "Justfile",
    "tox.ini",
    "noxfile.py",
)
_PY_DOC_RE = re.compile(r"\b(python3?)\s+-m\s+(?:pytest|mypy|ruff)\b")


def _documented_python(root: Path) -> str | None:
    """'python' or 'python3' if the repo's own docs/CI invoke tools that way; only a fixed word is ever taken from them."""
    candidates = [root / name for name in _DOC_FILES]
    try:
        candidates += sorted((root / ".github" / "workflows").glob("*.y*ml"))[:10]
    except OSError:
        pass
    for path in candidates:
        text = _read_text(path, 100_000)
        m = _PY_DOC_RE.search(text) if text else None
        if m:
            return m.group(1)
    return None


def _has_py_files(directory: Path) -> bool:
    try:
        with os.scandir(directory) as it:
            entries = list(it)[:200]
        if any(e.is_file() and e.name.endswith(".py") for e in entries):
            return True
        for e in entries:
            if e.is_dir() and not e.name.startswith((".", "__")):
                with os.scandir(e.path) as sub:
                    if any(s.is_file() and s.name.endswith(".py") for s in list(sub)[:200]):
                        return True
    except OSError:
        pass
    return False


def _discover_python(root: Path, b: _Builder) -> None:
    markers = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "tox.ini",
        "noxfile.py",
        "pytest.ini",
    )
    present = [m for m in markers if (root / m).exists()]
    if not present:
        return
    pyproject = _load_toml(root / "pyproject.toml", b.notes)
    tool = pyproject.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    setup_cfg = _read_manifest(root / "setup.cfg", b.notes) or ""

    runner: list[str] = []
    for lock, argv in (
        ("uv.lock", ["uv", "run"]),
        ("poetry.lock", ["poetry", "run"]),
        ("pdm.lock", ["pdm", "run"]),
    ):
        if (root / lock).exists():
            runner = argv
            break
    documented = None if runner else _documented_python(root)
    py = documented or "python"
    used_bare_python = False

    def module(name: str, *args: str) -> list[str]:
        nonlocal used_bare_python
        if runner:
            return [*runner, name, *args]
        used_bare_python = True
        return [py, "-m", name, *args]

    def binary(name: str, *args: str) -> list[str]:
        return [*runner, name, *args]

    if "ruff" in tool or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        b.add("lint", "ruff", binary("ruff", "check", "."))
    if (
        "mypy" in tool
        or (root / "mypy.ini").exists()
        or (root / ".mypy.ini").exists()
        or "[mypy" in setup_cfg
    ):
        cfg = tool.get("mypy")
        has_files = isinstance(cfg, dict) and "files" in cfg
        b.add("typecheck", "mypy", module("mypy", *([] if has_files else ["."])))
    if "pyright" in tool or (root / "pyrightconfig.json").exists():
        b.add("typecheck", "pyright", binary("pyright"))
    tests_dir = next((root / d for d in ("tests", "test") if _has_py_files(root / d)), None)
    if (
        "pytest" in tool
        or (root / "pytest.ini").exists()
        or "[tool:pytest]" in setup_cfg
        or tests_dir
    ):
        b.add("test", "pytest", module("pytest", "-q"))
    if (root / "tox.ini").exists() or "tox" in tool or (root / "noxfile.py").exists():
        b.notes.append(
            "tox/nox is configured; no command is proposed because its environments differ per repo (run it yourself)"
        )
    if b.fast or b.full_only:
        b.sources.append(", ".join(present[:2]))
    if used_bare_python and documented is None:
        b.notes.append(
            "Python commands use 'python -m ...': confirm the executable on your machines "
            "('python3', 'uv run', 'poetry run' or a venv path)"
        )


def _discover_rust(root: Path, b: _Builder) -> None:
    if not (root / "Cargo.toml").exists():
        return
    b.sources.append("Cargo.toml")
    b.add("lint", "cargo clippy", ["cargo", "clippy", "-q"], timeout=180)
    b.add("test", "cargo test", ["cargo", "test", "-q"], timeout=300)
    b.add("build", "cargo build", ["cargo", "build", "-q"], tier="full")
    b.notes.append("cargo clippy needs the clippy component (rustup component add clippy)")


def _discover_go(root: Path, b: _Builder) -> None:
    if not (root / "go.mod").exists():
        return
    b.sources.append("go.mod")
    b.add("lint", "go vet", ["go", "vet", "./..."], timeout=120)
    b.add("test", "go test", ["go", "test", "./..."])
    b.add("build", "go build", ["go", "build", "./..."], tier="full")


_TARGET_RE = re.compile(r"(?m)^(test|lint|check)[ \t]*:(?![=:])")


def _discover_tasks(root: Path, b: _Builder) -> None:
    """Makefile / justfile targets. A category that a language tool already covers becomes a note, not a duplicate run."""
    for tool, names in (
        ("make", ("Makefile", "makefile", "GNUmakefile")),
        ("just", ("justfile", "Justfile", ".justfile")),
    ):
        path = next((root / n for n in names if (root / n).is_file()), None)
        text = _read_text(path) if path else None
        if not text:
            continue
        for target in dict.fromkeys(m.group(1) for m in _TARGET_RE.finditer(text)):
            if target in b.categories:
                b.notes.append(
                    f"'{tool} {target}' exists ({path.name}) but is not added: another command already covers {target}"
                )
                continue
            if path.name not in b.sources:
                b.sources.append(path.name)
            b.add(target, f"{tool} {target}", [tool, target])


_CI_RUN_RE = re.compile(r"^(\s*(?:-\s+)?)run:\s*(.*)$")
_CI_LOOKS_RE = re.compile(
    r"\b(?:pytest|py\.test|tox|nox|ruff|mypy|pyright|flake8|pylint|black\s+--check|eslint|tsc|jest|vitest|"
    r"cargo\s+(?:test|clippy|fmt)|go\s+(?:test|vet)|dotnet\s+test|mvn|gradle|rspec|phpunit|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|lint|typecheck|check|build)|(?:make|just)\s+(?:test|lint|check))\b"
)


def _ci_commands(root: Path) -> list[str]:
    try:
        files = sorted((root / ".github" / "workflows").glob("*.y*ml"))[:10]
    except OSError:
        return []
    found: list[str] = []
    for path in files:
        lines = (_read_text(path, 200_000) or "").splitlines()
        i = 0
        while i < len(lines):
            m = _CI_RUN_RE.match(lines[i])
            i += 1
            if not m:
                continue
            col = len(m.group(1))
            rest = m.group(2).strip()
            if rest[:1] in ("|", ">"):
                block = []
                while i < len(lines) and (
                    not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > col
                ):
                    block.append(lines[i].strip())
                    i += 1
                cmds = block
            else:
                cmds = [rest.strip("'\"")]
            for cmd in cmds:
                if (
                    cmd
                    and not cmd.startswith("#")
                    and _CI_LOOKS_RE.search(cmd)
                    and cmd not in found
                ):
                    found.append(cmd)
    return found


def _discover_notes(root: Path, b: _Builder) -> None:
    if (root / ".pre-commit-config.yaml").exists():
        b.notes.append(
            ".pre-commit-config.yaml found: not run by the gate (hooks may rewrite files); "
            "run `pre-commit run --all-files` yourself if you want it"
        )
    for cmd in _ci_commands(root)[:8]:
        b.notes.append(f"CI runs (not included automatically): {cmd[:120]}")


def discover(root: Path) -> Recipe:
    """Propose a recipe from what the repo already declares. Reads files only; nothing is ever executed.

    fast = lint, typecheck, unit tests (should take about a minute); full = those plus the build and the slow
    suites. CI `run:` lines and tox/nox/pre-commit are only reported as notes. Never raises.
    """
    root = Path(root)
    b = _Builder()
    for step in (
        _discover_node,
        _discover_python,
        _discover_rust,
        _discover_go,
        _discover_tasks,
        _discover_notes,
    ):
        try:
            step(root, b)
        except Exception:
            continue  # one unreadable or odd file must not hide what the other ecosystems found
    return b.recipe()
