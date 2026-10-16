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
import time
from collections import Counter
from collections.abc import Iterator
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
_STAT_CAP = 200
_ADVISORY_CAP = 5


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


def _read_config(root: Path) -> bytes | None:
    """The raw config bytes, None when there is no file. The same bytes feed the hash and the parser (no TOCTOU)."""
    path = config_path(root)
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_CONFIG_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise VerifyConfigError(f"cannot read {path}: {exc}") from exc


def load(root: Path) -> Recipe | None:
    """The validated recipe, None if the repo has none. Raises VerifyConfigError for anything invalid."""
    data = _read_config(root)
    if data is None:
        return None
    return _parse(data, Path(root), str(config_path(root)))


def _toml_str(value: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes, except that TOML also forbids a raw DEL
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _comment(text: str, limit: int = 200) -> str:
    """One line, no control characters: notes carry text from the repo (CI files) and must not break the TOML."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()[:limit]


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
    """The check as a shell-looking line, for display only (it is never executed that way)."""
    import shlex

    return shlex.join(check.argv)


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


def trust_state(root: Path, state_dir: Path) -> str:
    """no-config | invalid | untrusted | modified (trusted once, edited since) | trusted. Never raises."""
    try:
        data = _read_config(root)
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


def is_trusted(root: Path, state_dir: Path) -> bool:
    return trust_state(root, state_dir) == "trusted"


def trust(root: Path, state_dir: Path) -> str:
    """Record the hash of the config as it is now. Refuses a missing or invalid file. Returns the hash."""
    data = _read_config(root)
    if data is None:
        raise VerifyConfigError(
            f"no {config_path(root)} to trust (create it with: claude-hr-ctl verify init --write)"
        )
    _parse(data, Path(root), "")
    digest = _sha256(data)
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
    """Forget the approval for this repo. True if there was one."""
    path = _trust_file(state_dir)
    state = _read_dict(path)
    if state.pop(_repo_key(root), None) is None:
        return False
    atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)
    return True


# ---------------------------------------------------------------------------------------------------------------
# running checks
# ---------------------------------------------------------------------------------------------------------------
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|KEY|CREDENTIAL", re.IGNORECASE)
_KEEP_ENV = frozenset(
    {"PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "LANG", "LANGUAGE"}
)


def scrubbed_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without anything that looks like a credential, plus CI=1.

    A deny-list, not an allow-list: toolchains need many harmless variables (SYSTEMROOT, APPDATA, CARGO_HOME,
    VIRTUAL_ENV, ...) and a check that only works in a hand-picked environment would fail in the gate and pass in
    the terminal. The names in _KEEP_ENV are never dropped.
    """
    src = dict(os.environ if base is None else base)
    env = {}
    for name, value in src.items():
        upper = name.upper()
        secret = (
            _SECRET_NAME.search(name) or upper.startswith("ANTHROPIC_") or upper == "SSH_AUTH_SOCK"
        )
        if not secret or upper in _KEEP_ENV:
            env[name] = value
    env["CI"] = "1"
    env[ACTIVE_ENV] = "1"
    return env


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


def _kill_tree(proc) -> None:
    """Kill the check and everything it started. A test runner's workers must not outlive a timeout."""
    import subprocess

    try:
        if _WIN:
            taskkill = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
            )
            subprocess.run(
                [
                    taskkill if os.path.isfile(taskkill) else "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=_NO_WINDOW,
                check=False,
            )
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


def _resolve_exe(name: str, cwd: Path, path_env: str) -> str | None:
    """Find the executable like a shell would, but never in the repo unless asked with an explicit path.

    shutil.which on Windows looks in the current directory first, so a repo could plant `pytest.bat` next to its
    config; a bare name is therefore only searched on PATH. A relative path with a separator is relative to the
    check's cwd (POSIX does that natively, CreateProcess does not).
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
        if directory:
            found = next((c for c in variants(os.path.join(directory, name)) if usable(c)), None)
            if found:
                return found
    return None


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
    tail = _Tail(proc.stdout)
    timed_out = False
    try:
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
    except BaseException:
        _kill_tree(proc)
        raise
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


def run(
    root: Path,
    checks: list[Check],
    *,
    total_timeout_s: float = 300,
    stop_on_fail: bool = False,
) -> RunResult:
    """Run checks in order. No shell, scrubbed env, per-check timeout, the process tree dies on timeout.

    Never raises: a missing tool is a result with rc 127. `total_timeout_s` bounds the whole run (each check gets
    the smaller of its own limit and what is left). The gate passes stop_on_fail=True, `verify run` wants them all.
    """
    root = Path(root)
    env = scrubbed_env()
    start = time.monotonic()
    results: list[CheckResult] = []
    skipped: list[str] = []
    for i, chk in enumerate(checks):
        remaining = total_timeout_s - (time.monotonic() - start)
        if remaining <= 0:
            skipped += [c.name for c in checks[i:]]
            break
        res = _run_one(root, chk, env, min(chk.timeout_s, remaining), remaining < chk.timeout_s)
        results.append(res)
        if res.rc != 0 and stop_on_fail:
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


def _git(
    top: Path, *args: str, cap: int = 16_000_000, timeout: float = 20.0
) -> tuple[bytes, bool] | None:
    """(stdout, truncated) of a git command, None if it failed or timed out. Output beyond `cap` is dropped."""
    import subprocess
    import threading

    kwargs: dict = {"creationflags": _NO_WINDOW} if _WIN else {}
    try:
        proc = subprocess.Popen(
            ["git", "--no-optional-locks", *args],
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
        out.append((code, tok[3:].decode("utf-8", "replace")))
    return out


def fingerprint(root: Path) -> str:
    """Hash of the working-tree change: HEAD, `status`, the `diff HEAD` content, untracked files' (size, mtime).

    Equal fingerprints mean "nothing Claude could have changed since last time", so the gate skips. Content is
    hashed for tracked files (cheap, exact); untracked files only by (size, mtime_ns) - a new file's bytes are
    not in `diff HEAD`. A diff over 4 MB is hashed up to the cap and the tracked files' (size, mtime_ns) are added
    so a change past the cap still moves the value. Returns "" when git is unavailable or this is not a work tree
    (never raises): the caller treats that as "cannot tell".
    """
    try:
        return _fingerprint(Path(root))
    except Exception:
        return ""


def _fingerprint(root: Path) -> str:
    import hashlib

    top = _toplevel(root)
    status, head, diff = _git_parallel(
        top,
        [
            (("status", "--porcelain=v1", "-z", "-uall"), {}),
            (("rev-parse", "--verify", "-q", "HEAD"), {}),
            (("diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"), {"cap": _DIFF_CAP}),
        ],
    )
    if status is None:
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
    for rel in watched[:_STAT_CAP]:
        try:
            st = os.lstat(top / rel)
        except OSError:
            h.update(f"{rel}\0gone\0".encode("utf-8", "replace"))
            continue
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\0".encode("utf-8", "replace"))
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
_TOL_KW_RE = re.compile(
    rf"\b(abs|rel|atol|rtol|delta|tol|tolerance|epsilon|eps|places|decimal|digits|precision|\w*timeout\w*)"
    rf"\s*[=:(]\s*(?P<n>{_NUM})(?![\w.])",
    re.IGNORECASE,
)
_TOL_POS_RE = re.compile(rf"\b(approx|toBeCloseTo)\s*\(\s*[^,()]+,\s*(?P<n>{_NUM})(?![\w.])")
_NUM_RE = re.compile(rf"(?<![\w.])(?:{_NUM})(?![\w.])")
# for these a smaller number is a looser check (fewer digits compared)
_INVERTED = frozenset({"places", "decimal", "digits", "precision", "tobecloseto"})


def _is_test_path(path: str) -> bool:
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
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _code_lines(lines: list[str]) -> list[str]:
    return [s for s in (ln.strip() for ln in lines) if s and not _is_comment(s)]


def _skip_hits(line: str) -> str | None:
    for rx in _SKIP_RES:
        m = rx.search(line)
        if m:
            return m.group(0).strip()[:30]
    return None


def _tolerances(line: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for rx in (_TOL_KW_RE, _TOL_POS_RE):
        for m in rx.finditer(line):
            try:
                found.setdefault(m.group(1).lower(), float(m.group("n")))
            except ValueError:
                continue
    return found


def _skeleton(line: str) -> str:
    return _NUM_RE.sub("#", " ".join(line.split()))


def _widened(removed: list[str], added: list[str]) -> list[str]:
    """Tolerance/timeout keywords whose number grew (or whose digit count shrank) between a removed and an added line."""
    by_skeleton: dict[str, list[str]] = {}
    for line in added:
        by_skeleton.setdefault(_skeleton(line), []).append(line)
    hits: list[str] = []
    for line in removed:
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
    return re.sub(r"[\x00-\x1f\x7f]+", " ", path)[:100]


def _advisories(diff_text: str) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for idx, fd in enumerate(_parse_diff(diff_text)):
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
        keys = sorted(set(_widened(removed, added)))
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


def test_weakening(root: Path) -> list[str]:
    """ADVISORY regex scan of `git diff -U0 HEAD` for ways a change could make the tests pass by checking less.

    At most 5 short '<file>: <what>' strings; [] when nothing stands out or git cannot answer. It only flags,
    it never blocks by itself, and it never raises. Only tracked changes are in `diff HEAD`; a brand-new
    untracked test file is not scanned.
    """
    try:
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
        )
        if res is None:
            return []
        return _advisories(res[0].decode("utf-8", errors="replace"))
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


def _mark_green(fp: str):
    def update(entry: dict) -> None:
        entry["last_green"] = fp
        entry["blocks"] = {}
        entry.pop("last_block", None)

    return update


def _mark_block(fp: str, summary: str):
    def update(entry: dict) -> None:
        raw = entry.get("blocks")
        blocks = dict(raw) if isinstance(raw, dict) else {}
        blocks[fp] = _count(blocks.pop(fp, 0)) + 1
        while len(blocks) > MAX_BLOCK_FPS:
            blocks.pop(next(iter(blocks)))
        entry["blocks"] = blocks
        entry["last_block"] = {"fp": fp, "summary": summary}

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
) -> GateOutcome:
    """Run the trusted recipe's tier for the work tree as it is now. Never raises, never runs untrusted config.

    skip  - nothing to do: no/untrusted/empty config, nothing changed since the last green run, or this change
            already blocked `max_blocks` times (the reason still names the failure), or the run was inconclusive
    pass  - every check passed; the fingerprint is remembered as green
    block - a check failed; `reason` is what to hand the model (summary line, output tail, test-weakening hints)
    error - something outside the code under test went wrong (missing tool, broken state file, bug): fail open
    """
    t0 = time.monotonic()
    try:
        out = _gate(Path(root), Path(state_dir), max_blocks, timeout_s, tier, test_guard)
    except Exception as exc:
        out = GateOutcome("error", f"verify gate failed: {type(exc).__name__}: {exc}")
    out.seconds = time.monotonic() - t0
    return out


def _gate(
    root: Path, state_dir: Path, max_blocks: int, timeout_s: float, tier: str, test_guard: bool
) -> GateOutcome:
    def skip(reason: str, fp: str = "") -> GateOutcome:
        return GateOutcome("skip", reason, fp)

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
    fp = fingerprint(root)
    if not fp:
        return skip("cannot fingerprint the work tree (git missing or not a repository)")
    entry = _gate_entry(state_dir, key)
    if entry.get("last_green") == fp:
        return skip("unchanged since the last passing run", fp)
    blocks = entry.get("blocks")
    seen = _count(blocks.get(fp)) if isinstance(blocks, dict) else 0
    if seen >= max_blocks:
        last = entry.get("last_block")
        summary = last.get("summary") if isinstance(last, dict) and last.get("fp") == fp else None
        tail = f"; last failure: {summary}" if isinstance(summary, str) else ""
        return skip(f"block cap reached ({seen}/{max_blocks}) for this change{tail}", fp)

    rr = run(root, checks, total_timeout_s=timeout_s, stop_on_fail=True)
    failed = rr.failed
    if failed is None and not rr.skipped:
        try:
            _record(state_dir, key, _mark_green(fp))
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
            f"time budget ({timeout_s:g}s) used up after {done} checks; not blocking",
            fp,
            rr.results,
        )
    if failed.launch_failed:
        return GateOutcome(
            "error",
            f"claude-hr verify: cannot run {_clean(failed.name)}: {failed.output}",
            fp,
            rr.results,
        )
    if failed.clamped:
        return GateOutcome(
            "skip",
            f"time budget ({timeout_s:g}s) ran out inside {_clean(failed.name)}; inconclusive, not blocking",
            fp,
            rr.results,
        )
    summary = _failure_summary(failed)
    lines = [summary]
    if failed.output:
        lines.append(failed.output)
    if test_guard:
        lines += [f"Possible test weakening: {adv}" for adv in test_weakening(root)]
    try:
        _record(state_dir, key, _mark_block(fp, summary))
    except OSError as exc:
        # without a block counter the cap cannot work: fail open instead of nagging forever
        return GateOutcome(
            "error",
            f"claude-hr verify: cannot record gate state ({exc}); {summary}",
            fp,
            rr.results,
        )
    return GateOutcome("block", "\n".join(lines), fp, rr.results)


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
        chk = Check(name, list(argv), timeout or _TIMEOUTS[category])
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
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return None


def _load_json(path: Path) -> object:
    text = _read_text(path)
    try:
        return json.loads(text) if text is not None else None
    except ValueError:
        return None


def _load_toml(path: Path) -> dict:
    import tomllib

    text = _read_text(path)
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
    pkg = _load_json(root / "package.json")
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
    pyproject = _load_toml(root / "pyproject.toml")
    tool = pyproject.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    setup_cfg = _read_text(root / "setup.cfg") or ""

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
