"""The one-time offer of the end-of-turn test gate (plugin hr-gates, stop_gate.py + verify.py).

The gate runs a repository's own checks (lint, tests) after every Claude turn and sends a failure back to Claude. Those
commands come from text in the repository, so they only run after a PERSON approved that exact file by hash - never an
agent. Making that approval something a person has to find, read about and type commands for meant almost nobody
had the gate. So the claude-hr launcher offers it, once per folder, at the moment a person starts an interactive
session in a normal terminal: the question is short, the answer is one keystroke, and the model of trust is unchanged.

    maybe_offer(args, cfg)   called by the launcher for a plain interactive session; never raises, fails open
    offer_now(...)           the screens + question + action for one repository (also what `verify enable` runs)
    enable(root, proposal)   run the proposed checks once, keep the ones that pass, write the recipe, trust it
    disable(root)            untrust + remember "no" + keep the file

What may happen, and when (every one of these has a test):
  * nothing is asked in -p, agents, subcommands, a Claude Code shell, piped stdin, outside a git work tree, in a home
    directory, or for a repository that is trusted / invalid / already answered (or one that was turned on and then had
    its file removed, or has nothing to propose), or one the person said "later" about and that time has not passed
    (a bare Enter on a screen whose default was No: a week; no check passes today: 3 days; nobody answered: a day);
  * a "yes" is only ever read from a terminal (human.not_a_person) - there is no flag or variable that answers;
  * Enter means yes only when the audit preflight is clean, no script the screen shows looks like it fetches or sends
    data, and what every task-runner check (npm run, make, just) runs could be shown in full; the screen shows it the
    way make or npm reads it (the last rule that defines a target, its prerequisites, one level deep);
  * the generated recipe holds only the argv commands that were shown, written without following a symlink, hashed from
    the exact bytes and trusted with that hash;
  * the only things written into the repository are `.claude/hr-verify.toml` and one line in the git `info/exclude`.

The launcher runs on every `claude` call, so this module imports nothing heavy at the top and the common "nothing to
ask" answer (trusted, declined, no project files) is found WITHOUT importing verify or audit (see _quick_verdict).
"""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import human, paths

# state/: {repo_key: {decision, ts, recipe_sha?, reason?, marker_mtime?, at?, until_days?}}
OFFER_FILE = "verify-offer.json"
MAX_ENTRIES = 200
CHECK_CAP_S = 60  # the longest one check may take while the person watches it run once
TOTAL_CAP_S = 120  # ... and all of them together
EXCLUDE_LINE = ".claude/hr-verify.toml"
DECISIONS = ("enabled", "declined")  # what the person answered
# Not an answer: "discovery found nothing to propose when the project files looked like this" (marker_mtime). It keeps
# a folder with a pyproject.toml and no tests from costing a discovery on every launch; older code ignores it
# (decision_for only knows DECISIONS).
_NOTHING = "nothing"
# Also not an answer: "ask me again later" (a bare Enter on a screen whose default was No, no check that passes today, a
# question nobody answered). `at` is when, `until_days` how long the launcher stays quiet about this folder; older code
# ignores it like any decision it does not know. `verify enable` never looks at it.
_LATER = "later"
LATER_ENTER_DAYS = 7  # Enter on a screen whose default was No
LATER_NONE_PASS_DAYS = 3  # none of the proposed checks passed today
LATER_TIMEOUT_DAYS = 1  # nobody answered the question
_MAX_LATER_DAYS = 365
# an `at` further ahead than this was written by a wrong clock: the snooze is over
_CLOCK_SKEW_S = 86_400

# Mirrors of verify.py constants, so the "nothing to ask" answer needs no import of verify (tests/test_gate_offer.py
# asserts they have not drifted).
_CONFIG_NAME = "hr-verify.toml"
_TRUST_FILE = "verify-trust.json"
_MAX_CONFIG_BYTES = 64 * 1024
# The files verify.discover() looks at. A repository with none of them has nothing to propose, so launching in a plain
# folder never pays for importing verify/discover. Keep in step with verify._discover_* (a test checks that every
# ecosystem discover understands passes this filter).
_DISCOVER_MARKERS = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "tox.ini",
    "noxfile.py",
    "pytest.ini",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "justfile",
    "Justfile",
    ".justfile",
)
_NOT_INTERACTIVE_FLAGS = frozenset(
    {"-p", "--print", "--bg", "--background", "-h", "--help", "-v", "--version"}
)


# ---------------------------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------------------------
def stderr_say(msg: str = "") -> None:
    """One line on stderr (the launcher keeps stdout clean for Claude). Never raises."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        try:
            print(msg.encode("ascii", "replace").decode(), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------------------------------------------
# the state file: what the person answered, per repository
# ---------------------------------------------------------------------------------------------------------------
def _repo_key(root: str | Path) -> str:
    """The same key as verify._repo_key (the trust record uses it too)."""
    key = os.path.realpath(root)
    return key.casefold() if os.name == "nt" else key


def offer_path() -> Path:
    return paths.state_dir() / OFFER_FILE


def _json_dict(path: Path) -> dict:
    """A small state file as a dict; missing, unreadable or corrupt = {} (it only holds regenerable bookkeeping)."""
    import json

    try:
        with open(path, "rb") as fh:
            raw = fh.read(2_000_000)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_for(root: str | Path) -> dict | None:
    """Whatever is recorded for this repository, a "nothing to propose" memo included."""
    entry = _json_dict(offer_path()).get(_repo_key(root))
    return entry if isinstance(entry, dict) else None


def decision_for(root: str | Path) -> dict | None:
    """The recorded answer for this repository ({decision, ts, recipe_sha?, reason?}) or None."""
    entry = _entry_for(root)
    if entry is not None and entry.get("decision") in DECISIONS:
        return entry
    return None


def record(
    root: str | Path,
    decision: str,
    *,
    recipe_sha: str | None = None,
    reason: str | None = None,
    marker_mtime: int | None = None,
    days: float | None = None,
) -> bool:
    """Remember the answer (replacing an earlier one). At most MAX_ENTRIES repositories, oldest dropped. True if written.
    A "later" is not an answer: the launcher stays quiet about the folder for `days` days from now (see _snoozed)."""
    if decision not in (*DECISIONS, _NOTHING, _LATER):
        raise ValueError(f"unknown decision {decision!r}")
    if decision == _LATER and not (
        isinstance(days, (int, float))
        and not isinstance(days, bool)
        and 0 < days <= _MAX_LATER_DAYS
    ):
        raise ValueError(f"a 'later' needs a number of days between 0 and {_MAX_LATER_DAYS}")
    try:
        import json

        from .jsonio import atomic_write_text

        path = offer_path()
        state = _json_dict(path)
        key = _repo_key(root)
        state.pop(key, None)  # re-insert last: dict order doubles as recency
        now = int(time.time())
        entry: dict = {"decision": decision, "ts": now}
        if decision == _LATER:
            entry["at"] = now
            entry["until_days"] = days
        if recipe_sha:
            entry["recipe_sha"] = recipe_sha
        if reason:
            entry["reason"] = reason[:200]
        if isinstance(marker_mtime, int):
            entry["marker_mtime"] = marker_mtime
        state[key] = entry
        while len(state) > MAX_ENTRIES:
            # an answer outlives a "nothing to propose" memo (which only saves a discovery): memos go first
            memo = next(
                (
                    k
                    for k, v in state.items()
                    if isinstance(v, dict) and v.get("decision") == _NOTHING
                ),
                None,
            )
            state.pop(memo if memo is not None else next(iter(state)))
        atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)
        paths.tighten_existing()  # a state dir this write just created is owner-only like the others
        return True
    except Exception:  # noqa: BLE001 - bookkeeping; the worst case is one more question next time
        return False


def forget(root: str | Path) -> bool:
    """Drop the recorded answer for this repository. True if there was one and it was removed."""
    try:
        import json

        from .jsonio import atomic_write_text

        path = offer_path()
        state = _json_dict(path)
        if state.pop(_repo_key(root), None) is None:
            return False
        atomic_write_text(path, json.dumps(state, indent=1) + "\n", follow_symlinks=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def _when(entry: dict, key: str = "ts", plus_days: float = 0) -> str:
    try:
        return time.strftime(
            "%Y-%m-%d", time.localtime(int(entry.get(key, 0)) + int(plus_days * 86_400))
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return "earlier"


def _snooze_days(entry: dict | None) -> float | None:
    """How many days a "later" entry asks for, None when it is not a well-formed one (then it is not a snooze)."""
    if not isinstance(entry, dict) or entry.get("decision") != _LATER:
        return None
    at, days = entry.get("at"), entry.get("until_days")
    for number in (at, days):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
    return min(days, _MAX_LATER_DAYS) if days > 0 else None


def _snoozed(entry: dict | None, now: float | None = None) -> bool:
    """Is this a "later" that is still running: now < at + until_days. An `at` more than a day ahead of the clock
    (a clock that was wrong when it was written) counts as over, so a bad date can never silence the offer for long."""
    try:
        days = _snooze_days(entry)
        if days is None:
            return False
        now = time.time() if now is None else now
        return entry["at"] - _CLOCK_SKEW_S <= now < entry["at"] + days * 86_400
    except Exception:  # noqa: BLE001 - cannot tell: ask
        return False


# ---------------------------------------------------------------------------------------------------------------
# cheap conditions (the launcher pays for these on every interactive start)
# ---------------------------------------------------------------------------------------------------------------
def _wanted(cfg) -> bool:
    """verify.offer = ask, the gate and its plugin are on. Anything but "ask" (off, false, a typo) means no question."""
    if str(cfg.get("verify.offer", "ask")).strip().lower() != "ask":
        return False
    if not cfg.flag("gates.enabled", True) or not cfg.flag("verify.gate", True):
        return False
    if os.environ.get("CLAUDE_HR_GATES", "").lower() == "off":
        return False
    # profile "minimal" does not install hr-gates (the end-of-turn hook): a gate nothing would run is not offered
    if str(cfg.get("plugins.profile", "standard")).strip().lower() == "minimal":
        return False
    return True


def _can_see_the_question() -> bool:
    """The offer is written to stderr (stdout belongs to Claude): with stderr redirected to a file or a pipe the person
    would never see the question while the launch waited for a key. No terminal on stderr, no question. (A real console:
    on Windows the NUL device also reports isatty.)"""
    return human.is_terminal(sys.stderr)


def _plain_session(args) -> bool:
    """Is this launch an interactive Claude session? The launcher only calls us for its kind == 'session'; this is the
    second look (a flag or subcommand that means "no person is typing" is never answered with a question)."""
    args = [str(a) for a in args or []]
    if any(a in _NOT_INTERACTIVE_FLAGS for a in args):
        return False
    try:
        from . import launcher

        return launcher.classify(args) == "session"
    except Exception:  # noqa: BLE001 - cannot tell: do not ask
        return False


def work_tree(cwd: str | Path) -> Path | None:
    """The top of the git work tree that contains `cwd`, found without spawning git; None when there is none, and for the
    places an offer makes no sense: inside a .git directory, a bare repository (no .git to find) and a home directory
    (a dotfiles repository), or the filesystem root."""
    base = Path(os.path.realpath(cwd))
    top = next((c for c in (base, *base.parents) if os.path.exists(os.path.join(c, ".git"))), None)
    if top is None:
        return None
    try:
        if ".git" in base.relative_to(top).parts:
            return None
    except ValueError:
        return None
    if top == top.parent:
        return None
    try:
        home = os.path.realpath(paths.home())
    except OSError:
        home = ""
    if home and os.path.normcase(str(top)) == os.path.normcase(home):
        return None
    return top


def _sha256_hex(data: bytes) -> str:
    """hashlib.sha256(data).hexdigest() without importing hashlib: loading its OpenSSL backend costs ~8 ms on Windows, and
    this runs on every interactive start. The interpreter's built-in SHA-2 module gives the same digest (CPython 3.12
    calls it _sha2, 3.11 _sha256); any interpreter without it falls back to hashlib."""
    try:
        from _sha2 import sha256
    except ImportError:
        try:
            from _sha256 import sha256
        except ImportError:
            from hashlib import sha256
    return sha256(data).hexdigest()


# What else decides whether discovery proposes anything once a marker file exists (a tests/ folder, a tool's own config
# file): part of the "nothing to propose" memo's signature, not of the marker filter.
_MEMO_EXTRAS = (
    "tests",
    "test",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
)


def _might_have_checks(root: Path) -> bool:
    return any(os.path.exists(os.path.join(root, m)) for m in _DISCOVER_MARKERS)


def _marker_signature(root: Path) -> int | None:
    """The newest modification time (ns) among the project files discovery looks at, None when there are none: only
    stat calls, so a folder with nothing to propose costs a few of them per launch. Any edit to one of those files (or
    a file added to the tests folder) moves it."""
    newest: int | None = None
    has_marker = False
    for name in (*_DISCOVER_MARKERS, *_MEMO_EXTRAS):
        try:
            mtime = os.stat(os.path.join(root, name)).st_mtime_ns
        except OSError:
            continue
        has_marker = has_marker or name in _DISCOVER_MARKERS
        if newest is None or mtime > newest:
            newest = mtime
    # the extras alone propose nothing: discovery needs a marker file
    return newest if has_marker else None


def _recipe_bytes(root: Path) -> tuple[str, bytes | None]:
    """('ok', bytes) | ('missing', None) | ('odd', None): a regular file read in full (to one byte past the limit),
    no file, or something that is not a readable regular file (a directory, a FIFO, an unreadable file)."""
    path = os.path.join(root, ".claude", _CONFIG_NAME)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "odd", None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "odd", None
            chunks: list[bytes] = []
            total = 0
            while total <= _MAX_CONFIG_BYTES:
                chunk = os.read(fd, _MAX_CONFIG_BYTES + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError):
        return "missing", None
    except OSError:
        return "odd", None
    return "ok", b"".join(chunks)


def _quick_verdict(root: Path) -> str:
    """'quiet' when there is certainly nothing to ask, else 'look'. Imports neither verify nor audit.

    Quiet: the recipe is the one that was trusted (the trust record holds its sha256); the person said no to this
    folder (and the recipe, if any, is the one they said no to), or "later" and that time has not passed; there is no
    recipe because the person removed the
    file they had turned on; there is no recipe and nothing in the folder that verify.discover could propose from (or
    it found nothing last time and the project files have not changed since); the recipe is not a readable regular
    file or is over the size limit (verify calls those invalid and nothing in them is ever run). Everything else -
    including every doubt - is "look", and offer_now then asks verify itself."""
    state, data = _recipe_bytes(root)
    if state == "odd":
        return "quiet"
    if state == "missing":
        entry = _entry_for(root)
        decision = entry.get("decision") if entry is not None else None
        if decision in DECISIONS:  # said no, or turned it on and then deleted the file
            return "quiet"
        if _snoozed(entry) and not entry.get(
            "recipe_sha"
        ):  # "later", asked while there was no recipe
            return "quiet"
        signature = _marker_signature(root)
        if signature is None:
            return "quiet"
        if decision == _NOTHING and entry.get("marker_mtime") == signature:
            return "quiet"
        return "look"
    if len(data) > _MAX_CONFIG_BYTES:
        return "quiet"
    sha = _sha256_hex(data)
    trusted = _json_dict(paths.state_dir() / _TRUST_FILE).get(_repo_key(root))
    if isinstance(trusted, str) and trusted == sha:
        return "quiet"
    entry = _entry_for(root)
    if entry is not None and entry.get("recipe_sha") == sha:  # an answer about exactly this file
        if entry.get("decision") == "declined" or _snoozed(entry):
            return "quiet"
    return "look"


_MAX_GLOBAL_JSON_BYTES = (
    32 * 1024 * 1024
)  # ~/.claude.json is a few hundred KB; nothing legitimate is near this


def _folder_key(p: str | Path) -> str:
    """How Claude Code keys a folder in ~/.claude.json: forward slashes, no trailing slash (case-folded on Windows)."""
    s = str(p).replace("\\", "/").rstrip("/")
    return s.casefold() if os.name == "nt" else s


def claude_trusts(root: Path) -> bool:
    """Has the person already answered Claude Code's own "do you trust this folder?" question with yes, for the
    repository's top folder? Claude Code records that as projects[<folder>].hasTrustDialogAccepted in its global state
    file (paths.claude_global_json: ~/.claude.json, or inside CLAUDE_CONFIG_DIR).

    Only the top folder counts: the checks run there, so it is the folder the person has to have vouched for. Trust given
    to a subfolder one happened to launch in vouches for nothing above it, and a trusted parent of the repository
    vouches for nothing inside it.

    Only a regular file up to a sane size is read, once, and only on the rare path where a question would otherwise be
    asked; anything unexpected (no file, bad JSON, another layout in a future Claude Code) answers False, which means
    "do not ask now" - never "ask anyway"."""
    try:
        import json

        path = paths.claude_global_json()
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_GLOBAL_JSON_BYTES:
            return False
        with open(path, "rb") as fh:
            data = json.loads(fh.read())
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            return False
        wanted = {_folder_key(x) for x in (root, os.path.realpath(root))}
        return any(
            isinstance(value, dict)
            and value.get("hasTrustDialogAccepted") is True
            and _folder_key(key) in wanted
            for key, value in projects.items()
        )
    except Exception:  # noqa: BLE001 - cannot tell: do not ask
        return False


# ---------------------------------------------------------------------------------------------------------------
# the launcher entry
# ---------------------------------------------------------------------------------------------------------------
def maybe_offer(args, cfg) -> None:
    """Offer the test gate for the current folder if (and only if) everything fits. NEVER raises: any error means
    "launch Claude as usual". Ctrl-C and EOF at the question are "not now" (nothing is recorded)."""
    try:
        _maybe_offer(args, cfg)
    except KeyboardInterrupt:
        stderr_say()
    except Exception:  # noqa: BLE001 - the launcher must always go on to start Claude
        pass


def _maybe_offer(args, cfg) -> None:
    if not _wanted(cfg):
        return
    if not _plain_session(args):
        return
    root = work_tree(os.getcwd())
    if root is None:
        return
    if _quick_verdict(root) == "quiet":
        return
    # the terminal checks come after the file checks: on Windows telling a console from NUL loads ctypes (~10 ms), which
    # a launch that has nothing to ask must not pay
    if human.not_a_person() is not None or not _can_see_the_question():
        return
    if not claude_trusts(root):
        # Claude Code has not been told to trust this folder yet (it asks on the first launch). A freshly cloned repository
        # must not be offered "run its checks after every turn" before its owner has vouched for it even once: the offer
        # comes on a later launch, after that answer. `claude-hr-ctl verify enable` works at any time.
        return
    offer_now(root, cfg, stderr_say, explicit=False)


# ---------------------------------------------------------------------------------------------------------------
# what a person is shown
# ---------------------------------------------------------------------------------------------------------------
def gate_limits(cfg) -> tuple[float, str]:
    """(seconds all checks of one end-of-turn run may use together, which tier the gate runs): what really applies."""
    from . import stop_gate, verify

    try:
        budget = float(cfg.get("verify.gate_timeout_s", verify.DEFAULT_GATE_TIMEOUT_S))
    except (TypeError, ValueError):
        budget = float(verify.DEFAULT_GATE_TIMEOUT_S)
    budget = min(budget, stop_gate.HOOK_BUDGET_S - verify.POST_RUN_RESERVE_S)
    return budget, str(cfg.get("verify.tier", "fast"))


# ---- what a task-runner check really runs -------------------------------------------------------------------------
# `npm run lint` or `make test` is only a name: the code is in package.json or the Makefile, which the repository
# controls and the audit preflight does not read. The screens show it (a few lines, cut short), and the default answer
# turns to No when it looks like it fetches or sends data, and when what runs could not be shown honestly (_View.notes).
_BODY_LINES_SHOWN = (
    3  # script lines printed under one check (plus the first line of each prerequisite)
)
_BODY_LINES_SCANNED = 30  # script lines of one recipe looked at for the default answer
_BODY_CHARS_SHOWN = 100
_WHERE_CHARS_SHOWN = 200  # the label in front of a script line ("Makefile test", "after build")
_NAME_CHARS_SHOWN = 40  # one target name inside a label or a note
_NOTE_CHARS_SHOWN = 300
_SCAN_CHARS = 2_000  # a script line longer than this is not scanned: it is called what it is (very long) instead
_SCAN_TEXTS = 1_000  # command and script lines looked at in one go
_RUNNER_FILE_BYTES = 1_000_000
_PREREQS_SCANNED = 6  # prerequisites of one make/just target whose recipes are read
_RULE_LINES_COUNTED = (
    50_000  # recipe lines of one rule that are counted (the count is only for "+N more lines")
)
_PARSE_BUDGET_S = 0.5  # one task file is read through in at most this long
_NODE_TOOLS = frozenset({"npm", "pnpm", "yarn", "bun"})
# GNU make reads the first of these that exists, so that is the one shown
_TASK_FILES = {
    "make": ("GNUmakefile", "makefile", "Makefile"),
    "just": ("justfile", "Justfile", ".justfile"),
}
RUNNER_NOTE = (
    "This also runs the project's own scripts and test code (package.json scripts, Makefile targets, conftest.py) "
    "and lets the end-of-turn formatter use tools and config from this folder (.venv, node_modules, prettier "
    "configuration)."
)
_AFTER = "after "  # the label of a prerequisite's lines ("after build"); a script file's own label never starts so
_UNREADABLE = "could not read what this runs"


def _tool_name(arg0: str) -> str:
    base = str(arg0).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


class _View(list):
    """What a task-runner check runs, as (where, text) pairs (a plain list of them to everything that only reads it),
    and what could not be shown honestly: `notes` are plain sentences (any note turns the default answer to No) and
    `unseen` counts the script lines that were counted but not kept."""

    def __init__(self, lines=(), notes=None, unseen: int = 0):
        super().__init__(lines)
        self.notes: list[str] = list(notes or [])
        self.unseen = unseen


# What npm, pnpm, yarn and bun accept before the script name that is not a script: `npm install` runs no script of the
# project's that could be shown here.
_NODE_NOT_SCRIPTS = frozenset(
    {
        "install", "i", "ci", "add", "exec", "x", "dlx", "create", "init", "audit", "outdated", "update", "up",
        "upgrade", "uninstall", "remove", "rm", "publish", "pack", "link", "unlink", "view", "info", "why", "ls",
        "list", "config", "cache", "prune", "dedupe", "rebuild", "store", "help", "version",
    }
)  # fmt: skip
# Flags that make a node tool run the script of another folder or of several packages: what runs is not in this folder's
# package.json, so it cannot be shown.
_NODE_REDIRECT = frozenset(
    {
        "--prefix", "--dir", "-C", "--cwd", "--workspace", "-w", "--workspaces", "-ws", "--filter", "-F",
        "--recursive", "-r", "--include-workspace-root",
    }
)  # fmt: skip
# The only flags of make and just that leave the file and the target alone (`-C dir`, `-f file`, --eval ... do not).
_MAKE_FLAGS = frozenset(
    {
        "-k", "-s", "-B", "-n", "-w", "--silent", "--quiet", "--keep-going", "--no-print-directory",
        "--print-directory", "--always-make", "--dry-run", "--just-print",
    }
)  # fmt: skip
_JUST_FLAGS = frozenset({"-q", "--quiet", "--no-deps", "--unstable", "--no-highlight"})


def _node_target(tool: str, args: list[str]) -> tuple[str, str | None] | None:
    words: list[str] = []
    for a in args:
        if a == "--":
            break
        if a.startswith("-"):
            if a.split("=", 1)[0] in _NODE_REDIRECT:
                return tool, None
            continue
        words.append(a)
    if not words:
        return None
    first = words[0]
    if first in (
        "workspace",
        "workspaces",
    ):  # yarn workspace foo test: the script of another package
        return tool, None
    if first in ("run", "run-script", "rum", "urn"):
        return (tool, words[1]) if len(words) > 1 else None
    if first in ("test", "t", "tst"):
        return tool, "test"
    if first in _NODE_NOT_SCRIPTS:
        return None
    if tool == "npm":
        # `npm start` runs the script of that name; anything else here is something this does not know (a flag's value
        # taken for the command, a subcommand): it is said so rather than guessed at
        return (tool, first) if first in ("start", "stop", "restart") else (tool, None)
    return tool, first  # `yarn lint`: shorthand for the script


def _task_target(tool: str, args: list[str]) -> tuple[str, str | None]:
    allowed = _MAKE_FLAGS if tool == "make" else _JUST_FLAGS
    words: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("-"):
            if tool == "make" and a in ("-j", "--jobs") and i < len(args) and args[i].isdigit():
                i += 1
                continue
            if a in allowed or (tool == "make" and a.startswith("-j") and a[2:].isdigit()):
                continue
            return tool, None  # -C, -f, --eval ...: another folder, another file, other rules
        words.append(a)
    # `make` alone runs whatever target comes first, and `make a b` or `make X=y` run more than the one named here
    return (tool, words[0]) if len(words) == 1 and "=" not in words[0] else (tool, None)


def _runner_target(argv) -> tuple[str, str | None] | None:
    """('npm', 'lint') for `npm run lint`, ('npm', 'test') for `npm test`, ('make', 'test') for `make test`; None for
    anything else (a tool called directly runs no script of the project's that could be shown). A task-runner call whose
    script cannot be told from the command line (`npm --prefix x run lint`, `make -C sub test`, `yarn workspace a test`,
    `make` alone) is (tool, None)."""
    if not argv:
        return None
    tool = _tool_name(argv[0])
    args = [str(a) for a in argv[1:]]
    if tool in _NODE_TOOLS:
        return _node_target(tool, args)
    if tool in _TASK_FILES:
        return _task_target(tool, args)
    return None


def _read_small_more(path: Path) -> tuple[str, bool] | None:
    """(text, more): a regular file up to _RUNNER_FILE_BYTES as text (a prefix is enough for a Makefile; a cut-off
    package.json does not parse and shows nothing), and whether the file goes on past that. A BOM is not part of the text.
    None for anything else; never blocks on a FIFO."""
    from . import verify

    try:
        data, more = verify._read_file(path, _RUNNER_FILE_BYTES)
    except OSError:
        return None
    return data.decode("utf-8-sig", errors="replace"), more


def _read_small(path: Path) -> str | None:
    got = _read_small_more(path)
    return None if got is None else got[0]


def _clean(text: str) -> str:
    """One line with whitespace runs collapsed to one space (padding cannot push the rest of a line out of view)."""
    return " ".join(text.split())[
        : _SCAN_CHARS + 1
    ]  # one more than is scanned, so "too long" can be told


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _names(names: list[str]) -> str:
    shown = ", ".join(_short(n, _NAME_CHARS_SHOWN) for n in names[:3])
    return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def _node_lines(base: Path, name: str) -> _View | None:
    import json

    text = _read_small(base / "package.json")
    pkg = json.loads(text) if text else None
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    if not isinstance(scripts, dict):
        return None
    out = _View()
    # npm runs the pre and post scripts of a script along with it
    for script in (f"pre{name}", name, f"post{name}"):
        body = scripts.get(script)
        if isinstance(body, str) and body.strip():
            out.append((f'package.json "{script}"', _clean(body)))
    return out or None


class _Rule:
    """One rule head of a Makefile or justfile and the lines of its recipe (the first _BODY_LINES_SCANNED, and a count)."""

    __slots__ = ("targets", "needs", "double", "lines", "total")

    def __init__(self, targets: list[str], needs: list[str], double: bool):
        self.targets = targets
        self.needs = needs
        self.double = double
        self.lines: list[str] = []
        self.total = 0

    def add(self, line: str) -> None:
        if self.total < _RULE_LINES_COUNTED:
            self.total += 1
            if len(self.lines) < _BODY_LINES_SCANNED:
                self.lines.append(line)


def _logical_lines(text: str):
    """The lines of a task file the way make and just read them: split at newline only (a form feed or U+2028 does not end a
    line for them, and must not end one here), and a line that ends in an odd number of backslashes goes on in the next."""
    pending: list[str] | None = None
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if (len(line) - len(line.rstrip("\\"))) % 2 == 1:
            if pending is None:
                pending = [line[:-1]]
            else:
                pending.append(line[:-1].lstrip())
            continue
        if pending is not None:
            pending.append(line.lstrip())
            yield " ".join(pending)
            pending = None
        else:
            yield line
    if pending is not None:
        yield " ".join(pending)


def _rule_head(line: str, tool: str) -> tuple[list[str], str, bool] | None:
    """(targets, what follows the colon, double colon) for a line that starts a rule, None for anything else (an
    assignment, a directive, a line with no colon)."""
    before, colon, after = line.partition(":")
    if not colon:
        return None
    double = after.startswith(":")
    if double:
        after = after[1:]
    if after.startswith("="):  # `a := b`, `a ::= b`
        return None
    names = before.split()
    if tool == "just":
        names = names[:1]  # the rest are parameters: `test *args:`
    elif any("=" in n for n in names):  # `A = b:c`
        return None
    if not names or "=" in names[0]:
        return None
    return names, after, double


def _parse_rules(text: str, tool: str) -> tuple[list[_Rule], list[str], bool]:
    """(rules, hidden, cut) of a Makefile or justfile: every rule head in the file; what the file does that this reading
    cannot follow, in plain words (it includes other files, changes the recipe prefix, makes rules with $(eval ...)); and
    whether reading it was cut short (it took too long). Linear in the size of the file; no regular expression is run over
    a whole line."""
    import re

    make = tool == "make"
    directive = re.compile(
        r"(?:-?include|sinclude)(?:[ \t]|$)" if make else r"(?:import|mod)\??[ \t]+\S"
    )
    conditional = re.compile(r"(?:ifeq|ifneq|ifdef|ifndef|else|endif)(?:[ \t(]|$)")
    rules: list[_Rule] = []
    hidden: list[str] = []
    cur: _Rule | None = None
    cut = False
    if make and ("$(eval" in text or "${eval" in text):
        hidden.append("makes rules with $(eval ...)")
    deadline = time.monotonic() + _PARSE_BUDGET_S
    for n, line in enumerate(_logical_lines(text)):
        if n % 1024 == 1023 and time.monotonic() > deadline:
            cut = True
            break
        if not line.strip():
            continue
        if make:
            if line[0] == "\t":  # a recipe line
                if cur is not None and not line.lstrip().startswith("#"):
                    cur.add(line)
                continue
            # spaces in front of a rule or a directive do not matter to make
            line = line.lstrip(" ")
        elif line[0] in " \t":  # the body of a justfile recipe
            if cur is not None and not line.lstrip().startswith("#"):
                cur.add(line)
            continue
        if line[0] == "#":
            continue
        if directive.match(line):
            phrase = "includes other files" if make else "imports other files"
            if phrase not in hidden:
                hidden.append(phrase)
        if make and line.startswith(".RECIPEPREFIX") and "changes .RECIPEPREFIX" not in hidden:
            hidden.append("changes .RECIPEPREFIX")  # recipe lines no longer start with a tab
        if make and conditional.match(line):
            continue  # a conditional can sit inside a recipe: the recipe goes on after it
        head = _rule_head(line, tool)
        if head is None:
            continue  # an assignment or a directive does not end a recipe for this purpose either
        targets, rest, double = head
        inline = ""
        if make:
            rest, _, inline = rest.partition(";")  # `test: deps ; recipe` on one line
        # a word with = is a target-specific variable (`test: FOO=1`), and | and && only separate what comes before
        needs = [w for w in rest.split("#", 1)[0].split() if w not in ("|", "&&") and "=" not in w]
        cur = _Rule(targets, needs, double)
        if inline.strip():
            cur.add(inline)
        rules.append(cur)
    return rules, hidden, cut


def _defs(rules: list[_Rule], name: str) -> list[_Rule]:
    return [r for r in rules if name in r.targets]


def _recipes_of(defs: list[_Rule]) -> list[_Rule]:
    """The rules whose recipe runs for a target: all of them for a `::` target, else the last one that has a recipe (GNU
    make warns 'overriding recipe' and uses it), else the last rule."""
    if any(r.double for r in defs):
        return list(defs)
    with_recipe = [r for r in defs if r.total]
    return [with_recipe[-1]] if with_recipe else [defs[-1]]


def _defined_note(fname: str, name: str, tool: str, defs: list[_Rule]) -> str:
    if any(r.double for r in defs):
        return f"{fname} defines {_short(name, _NAME_CHARS_SHOWN)} {len(defs)} times with '::'; {tool} runs all of them"
    return (
        f"{fname} defines {_short(name, _NAME_CHARS_SHOWN)} {len(defs)} times; {tool} runs the last recipe, "
        "which is the one shown"
    )


def _read_task_file(base: Path, tool: str) -> tuple[str, str, bool] | None:
    """(file name as it is on disk, text, more) of the file make or just reads in `base`: the first of _TASK_FILES that
    exists, the way they pick one."""
    import itertools

    try:
        wanted = next(n for n in _TASK_FILES[tool] if (base / n).is_file())
        with os.scandir(
            base
        ) as it:  # the spelling on disk (a case-insensitive file system answers to any)
            on_disk = [
                e.name for e in itertools.islice(it, 20_000) if e.name.lower() == wanted.lower()
            ]
    except (StopIteration, OSError):
        return None
    fname = wanted if wanted in on_disk or not on_disk else on_disk[0]
    got = _read_small_more(base / fname)
    return None if got is None else (fname, got[0], got[1])


def _is_file(base: Path, name: str) -> bool:
    """Is there a file or folder of this name, so the name is something to build from and not a rule to run?"""
    if any(c in name for c in "$%*?["):
        return False
    try:
        return os.path.lexists(os.path.join(base, name))
    except (OSError, ValueError):
        return False


def _task_lines(base: Path, tool: str, target: str) -> _View | None:
    """What `make <target>` / `just <target>` runs: the recipe of the LAST rule that defines the target (also one of
    several targets on a line), then one level of prerequisites (labelled "after <name>"). Whatever this cannot show
    honestly - the target defined more than once, an include, a recipe that was cut short, a prerequisite it cannot read -
    is said in the view's notes. None when the file or the target is not there."""
    got = _read_task_file(base, tool)
    if got is None:
        return None
    fname, text, more = got
    rules, hidden, cut = _parse_rules(text, tool)
    defs = _defs(rules, target)
    if not defs:
        return None
    view = _View()
    notes = view.notes
    label = f"{fname} {_short(target, _NAME_CHARS_SHOWN)}"

    def add(chosen: list[_Rule], where: str, what: str) -> None:
        total = sum(r.total for r in chosen)
        kept = 0
        for r in chosen:
            view.extend((where, _clean(ln)) for ln in r.lines)
            kept += len(r.lines)
        view.unseen += total - kept
        if total > kept:
            notes.append(
                f"only the first {_BODY_LINES_SCANNED} of {total} lines of {what} were checked"
            )

    if len(defs) > 1:
        notes.append(_defined_note(fname, target, tool, defs))
    notes.extend(f"{fname} {phrase}, so what runs may not all be shown" for phrase in hidden)
    if more:
        notes.append(
            f"{fname} is longer than {_RUNNER_FILE_BYTES} bytes and only the start of it was read"
        )
    if cut:
        notes.append(f"{fname} took too long to read through")
    add(_recipes_of(defs), label, _short(target, _NAME_CHARS_SHOWN))
    needs = list(dict.fromkeys(n for r in defs for n in r.needs if n != target))
    if len(needs) > _PREREQS_SCANNED:
        notes.append(
            f"{_short(target, _NAME_CHARS_SHOWN)} has {len(needs)} prerequisites; only the first "
            f"{_PREREQS_SCANNED} were read"
        )
    seen = {target, *needs[:_PREREQS_SCANNED]}
    unread: list[str] = []
    for need in needs[:_PREREQS_SCANNED]:
        pdefs = _defs(rules, need)
        if not pdefs:
            if not _is_file(base, need):
                unread.append(
                    need
                )  # no rule here: an include, a pattern rule or a variable supplies it, or nothing
            continue
        if len(pdefs) > 1:
            notes.append(_defined_note(fname, need, tool, pdefs))
        chosen = _recipes_of(pdefs)
        add(chosen, f"{_AFTER}{_short(need, _NAME_CHARS_SHOWN)}", _short(need, _NAME_CHARS_SHOWN))
        deeper = [
            n
            for r in chosen
            for n in r.needs
            if n not in seen and any(d.total or d.needs for d in _defs(rules, n))
        ]
        if deeper:
            seen.update(deeper)
            notes.append(
                f"{_short(need, _NAME_CHARS_SHOWN)} also depends on {_names(deeper)}, which are not shown"
            )
    if unread:
        notes.append(
            f"{_names(unread)} (before {_short(target, _NAME_CHARS_SHOWN)}) could not be read: {fname} has no rule "
            f"for {'it' if len(unread) == 1 else 'them'}"
        )
    return view


def _runner_lines(root: Path, chk) -> _View:
    """What a task-runner check will run, as a _View of (where, text) pairs (text whitespace-collapsed and bounded; at
    most _BODY_LINES_SCANNED per recipe). An empty view for a check that is no such call. For a task-runner check whose
    text cannot be found or told (an unreadable or invalid manifest, `--prefix`, `-C`, a workspace, a missing target) the
    view carries the note "could not read what this runs". Reads one small regular file. Never raises."""
    view = _View()
    try:
        target = _runner_target(chk.argv)
    except Exception:  # noqa: BLE001
        return view
    if target is None:
        return view
    try:
        tool, name = target
        if name is not None:
            from . import verify

            base = verify._resolve_cwd(Path(root), chk.cwd)
            got = _node_lines(base, name) if tool in _NODE_TOOLS else _task_lines(base, tool, name)
            if got is not None:
                return got
    except Exception:  # noqa: BLE001 - what cannot be shown is said, not shown
        pass
    view.notes.append(_UNREADABLE)
    return view


def _body_lines(view, indent: str) -> list[str]:
    """The first lines of a check's script as screen lines (through safe_text, each cut to 100 characters): the first
    three of its own, the first of each prerequisite, how many more there are, and the view's notes in parentheses."""
    from . import verify

    own = [e for e in view if not e[0].startswith(_AFTER)]
    shown = own[:_BODY_LINES_SHOWN]
    firsts: dict[str, tuple[str, str]] = {}
    for entry in view:
        if entry[0].startswith(_AFTER):
            firsts.setdefault(entry[0], entry)
    shown += list(firsts.values())
    lines = []
    for where, text in shown:
        text = verify.safe_text(text)
        text = _short(text, _BODY_CHARS_SHOWN)
        lines.append(f"{indent}{_short(verify.safe_text(where), _WHERE_CHARS_SHOWN)}: {text}")
    more = len(view) + getattr(view, "unseen", 0) - len(shown)
    if lines and more > 0:
        lines[-1] += f"   (+{more} more lines in the file)"
    for note in getattr(view, "notes", None) or []:
        lines.append(f"{indent}({_short(verify.safe_text(str(note)), _NOTE_CHARS_SHOWN)})")
    return lines


def _unshown(views) -> bool:
    """Is there a check whose script could not be shown in full (a note under it)?"""
    return any(getattr(view, "notes", None) for view in views)


# ---- the suspicious-script scan ----------------------------------------------------------------------------------
# Every check below is linear in the length of the text, whatever the text is: a regular expression has no nested
# quantifier over a repeating token, and what needs more than a pattern (rm, .env) walks the words of the line once.
# Each line is looked at up to _SCAN_CHARS characters, and one call has a time budget on top of that.
_SUSPICION_BUDGET_S = 0.25
_ENV_TEMPLATES = frozenset(
    {"example", "sample", "template", "dist", "test", "tpl"}
)  # ".env.example" holds no secret
# commands that read a file's content (and so read the secrets in a .env file); cp, mv and the like do not
_READERS = frozenset(
    {
        "cat", "type", "more", "less", "head", "tail", "tac", "nl", "source", ".", "get-content", "gc", "strings",
        "xxd", "od", "hexdump", "grep", "egrep", "fgrep", "rg", "awk", "sed", "cut", "sort", "base64", "tar", "zip",
        "scp", "rsync", "curl", "wget",
    }
)  # fmt: skip
_SUSPICIOUS: list = []  # (what it means, find(text) -> index of the match or None), built on first use


def _rx_find(rx):
    def find(text: str) -> int | None:
        m = rx.search(text)
        return m.start() if m else None

    return find


def _env_file(word: str) -> bool:
    """Is this word (a path, `@path` of curl -d, `key=@path`) a .env file with secrets in it, not a template of one?"""
    word = word.strip("\"'").rpartition("@")[2].strip("\"'")
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name == ".env" or (name.startswith(".env.") and name[5:] not in _ENV_TEMPLATES)


def _find_env_read(text: str) -> int | None:
    """Index of a .env file that a command reads (cat, type, source, Get-Content, curl -d @ ...), or None. `cp .env.example
    .env`, `dotenv -e .env.test -- jest` and a mention in a comment read nothing."""
    import re

    for seg in re.finditer(r"[^;&|`()]+", text):
        reading = False
        for word in re.finditer(r"\S+", seg.group()):
            w = word.group()
            if w.startswith("<"):  # input redirection: `jest < .env`
                reading = True
                w = w.lstrip("<")
                if not w:
                    continue
            if reading and _env_file(w):
                return seg.start() + word.start()
            if _tool_name(w.strip("\"'")) in _READERS:
                reading = True
    return None


def _root_operand(word: str) -> bool:
    """Is this word / or ~ or $HOME (a trailing / and * allowed), the places `rm -r` must never be pointed at?"""
    w = word.strip("\"'")
    if w.endswith("*"):
        w = w[:-1]
    if w.endswith("/") and len(w) > 1:
        w = w[:-1]
    return w.lower() in ("/", "~", "$home", "${home}")


def _find_rm_everything(text: str) -> int | None:
    """Index of an `rm` that is recursive and has / ~ or $HOME among its operands (`rm -rf /`, `rm -r -f ~/`, `rm
    --recursive /*`, `sudo rm -rf $HOME`), or None. One pass over the words: the flags may come before or after the operand."""
    import re

    for seg in re.finditer(r"[^;&|`()]+", text):
        start = recursive = root = None
        done_with_flags = False
        for word in re.finditer(r"\S+", seg.group()):
            w = word.group().strip("\"'")
            if start is None:
                if _tool_name(w) == "rm":
                    start = seg.start() + word.start()
                continue
            if w == "--":
                done_with_flags = True
            elif w.startswith("-") and not done_with_flags:
                low = w.lower()
                if low == "--recursive" or (
                    not low.startswith("--")
                    and low[1:].isascii()
                    and low[1:].isalpha()
                    and "r" in low[1:]
                ):
                    recursive = True
            elif _root_operand(w):
                root = True
        if start is not None and recursive and root:
            return start
    return None


def _suspicious_checks() -> list:
    if not _SUSPICIOUS:
        import re

        word = r"(?<![\w./\\$=-])(?:{})(?:\.exe)?(?![\w=./\\-])"
        local = r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|host\.docker\.internal)"
        for what, find in (
            (
                "fetches or sends data",
                _rx_find(
                    re.compile(
                        r"\b(?:curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm|certutil|bitsadmin)\b",
                        re.IGNORECASE,
                    )
                ),
            ),
            (
                "fetches or sends data",
                # nc, ncat, netcat as a program: not NC=1, not a file called nc.md, not part of a longer word
                _rx_find(re.compile(word.format("nc|ncat|netcat"))),
            ),
            (
                "fetches or sends data",
                _rx_find(
                    re.compile(
                        r"https?://(?!" + local + r"""(?::\d+)?(?:[/?#\s'"]|$))[^\s'"]+""",
                        re.IGNORECASE,
                    )
                ),
            ),
            (
                "downloads or decodes code and runs it",
                _rx_find(
                    re.compile(
                        r"\|\s*(?:sh|bash|zsh|powershell|pwsh|iex)\b|\b(?:iex|invoke-expression|eval)\b"
                        r"|\s-(?:enc|encodedcommand)\b",
                        re.IGNORECASE,
                    )
                ),
            ),
            (
                "downloads or decodes code and runs it",
                # base64 as a program (or a module): not a file called base64.test.js
                _rx_find(re.compile(word.format("base64"), re.IGNORECASE)),
            ),
            (
                "reaches for credentials or keys",
                _rx_find(
                    re.compile(
                        r"\.(?:ssh|aws|gnupg|netrc|npmrc)\b|\bcredentials\b|\bid_(?:rsa|ed25519)\b",
                        re.IGNORECASE,
                    )
                ),
            ),
            ("reaches for credentials or keys", _find_env_read),
            ("deletes everything under / or your home folder", _find_rm_everything),
        ):
            _SUSPICIOUS.append((what, find))
    return _SUSPICIOUS


def _suspicion(texts) -> str | None:
    """A plain clause ("A script below fetches or sends data (curl ...)") when one of the texts (script lines, command
    lines) looks like it fetches or sends data, runs what it decodes or downloads, reaches for credentials or deletes
    broadly, or is too long to look through; else None. Conservative on purpose: a match only turns the default answer
    to No. Bounded in every direction: each text is looked at up to _SCAN_CHARS characters (a longer one is itself the
    finding, so padding cannot hide a command), at most _SCAN_TEXTS texts are looked at, and the whole call has
    _SUSPICION_BUDGET_S seconds; running out of either is "too long to read" (the default answer is No). Never raises."""
    try:
        from . import verify

        checks = _suspicious_checks()  # built (and its patterns compiled) before the budget starts
        started = time.monotonic()
        every = list(texts)
        bounded: dict[str, None] = {}
        too_long = False
        for t in every[:_SCAN_TEXTS]:
            s = str(t)
            too_long = too_long or len(s) > _SCAN_CHARS
            bounded.setdefault(s[:_SCAN_CHARS])
        if len(every) > _SCAN_TEXTS:
            return f"A script below is too long to read here (more than {_SCAN_TEXTS} lines)"
        for what, find in checks:
            for text in bounded:
                if time.monotonic() - started > _SUSPICION_BUDGET_S:
                    return (
                        f"A script below is too long to read here (looking through it took more than "
                        f"{_SUSPICION_BUDGET_S:g} seconds)"
                    )
                at = find(text)
                if at is not None:
                    snippet = text[at : at + 60]
                    more = "..." if len(text) > at + 60 else ""
                    return f"A script below {what} ({verify.safe_text(snippet)}{more})"
        if too_long:
            return f"A script below is very long (over {_SCAN_CHARS} characters, more than can be read here)"
    except Exception:  # noqa: BLE001 - cannot tell is handled by the caller's other checks
        return None
    return None


def _scan_texts(checks, views) -> list[str]:
    """Everything a person is shown for these checks, as text to look at: the command lines and the script lines."""
    # the program by its bare name: the folder it was found in says nothing about what the check does
    texts = [" ".join([_tool_name(c.argv[0]), *map(str, c.argv[1:])]) for c in checks if c.argv]
    for view in views:
        texts += [text for _where, text in view]
    return texts


def recipe_views(root: Path, recipe) -> dict:
    """{(tier, n): script lines} for every check of both tiers (see _runner_lines)."""
    return {
        (tier, n): _runner_lines(root, c)
        for tier in ("fast", "full")
        for n, c in enumerate(getattr(recipe, tier), 1)
    }


def recipe_lines(
    recipe,
    gate_timeout_s: float | None = None,
    gate_tier: str = "fast",
    root: Path | None = None,
    views: dict | None = None,
) -> list[str]:
    """Every check of both tiers, in full, exactly as the gate would run it.

    This is what a person reads before approving the file, so nothing is left out and nothing is printed raw: name,
    argv items and cwd go through verify.safe_text (an escape sequence or bidi override in the repository's text is
    shown as \\x1b / \\u202e, it cannot erase or reorder the line). A [[full]] check is only labelled a repeat when
    it is IDENTICAL to a [[fast]] check (name, argv, timeout and cwd); one that merely shares a name with a fast
    check is shown like any other, so a difference cannot hide behind a familiar name.

    `gate_timeout_s` (verify.gate_timeout_s) is what ALL the checks of one end-of-turn run get together. A check of the
    tier the gate runs whose own timeout_s is above it is shown with the limit that really applies there, and what
    happens at that limit: the gate stops it, tells the user and does not block (`verify run` honours timeout_s).

    With `root`, a check that goes through a task runner (npm run, make, just) is followed by the first lines of what
    it runs (`views`, from recipe_views, when the caller already has them).
    """
    from . import verify

    lines: list[str] = []
    fast = list(recipe.fast)
    gate_tier = "full" if gate_tier == "full" and recipe.full else "fast"
    if root is not None and views is None:
        views = recipe_views(root, recipe)
    for tier in ("fast", "full"):
        for n, c in enumerate(getattr(recipe, tier), 1):
            same = (
                next((i for i, f in enumerate(fast, 1) if f == c), None) if tier == "full" else None
            )
            repeat = f"   (identical to [fast {same}])" if same else ""
            limit = f"timeout {c.timeout_s}s"
            if gate_timeout_s is not None and tier == gate_tier and c.timeout_s > gate_timeout_s:
                limit = (
                    f"timeout {c.timeout_s}s, but the end-of-turn gate stops it after {gate_timeout_s:g}s "
                    "(its time budget, verify.gate_timeout_s, shared by all checks) and then does not block"
                )
            lines.append(
                f"  [{tier} {n}] {verify.safe_text(c.name)}: {verify.command_line(c)}"
                f"   ({limit}, cwd {verify.safe_text(c.cwd)}){repeat}"
            )
            if views and not same:
                lines += _body_lines(views.get((tier, n), []), "      ")
    for note in getattr(recipe, "notes", None) or []:
        lines.append(f"  note: {verify.safe_text(str(note)[:300])}")
    return lines


def _start_differs(start, root) -> bool:
    try:
        return os.path.normcase(os.path.realpath(start)) != os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError, TypeError):
        return False


def offer_lines(
    recipe, root: Path | None = None, start=None, views: list | None = None
) -> list[str]:
    """The plain-language offer for a folder that has no recipe yet (the fast checks discovery found).

    With `root` (the repository's top folder), a check that goes through a task runner is followed by what it will run
    (`views`: one _runner_lines result per check, when the caller already has them), and `start` (where Claude was
    started) is named when the checks will run somewhere else."""
    from . import verify

    lines = [
        "",
        "claude-hr can double-check Claude's work in this folder (optional; asked once per folder).",
    ]
    if root is not None and start is not None and _start_differs(start, root):
        lines.append(
            f"Started in {verify.safe_text(str(start))}; the checks run in the repository's top folder "
            f"{verify.safe_text(str(root))}."
        )
    lines.append(
        "After each turn it would run this project's own checks and send any failure back to Claude to fix:"
    )
    if root is not None and views is None:
        views = [_runner_lines(root, c) for c in recipe.fast]
    for n, c in enumerate(recipe.fast, 1):
        lines.append(f"    {n}. {verify.command_line(c)}")
        if views and n - 1 < len(views):
            lines += _body_lines(views[n - 1], "         ")
    lines += [
        RUNNER_NOTE,
        "Nothing runs unless you say yes now. If you do, they run once, here, to see which pass today.",
        "These checks run code from this folder, so say no if you do not trust it.",
    ]
    return lines


def _preflight(root: Path) -> tuple[list[str], bool]:
    """(lines to show, whether Enter may mean yes). The audit preflight reads files only and never runs repo code; it
    has its own short time budget. Any high or medium finding, or a scan that cannot run, makes the default No."""
    try:
        from . import audit

        findings = audit.preflight(root)
    except Exception:  # noqa: BLE001 - cannot tell is not "clean"
        return [
            "claude-hr could not scan this folder's agent settings first, so the default answer is No."
        ], False
    if not findings:
        return [], True
    return [
        "",
        "Before you decide: this folder has settings that can steer Claude or run commands on their own.",
        "Read them first; the default answer is No.",
        audit.render(findings, [], root=root),
    ], False


# ---------------------------------------------------------------------------------------------------------------
# turning it on
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class EnableResult:
    status: str  # enabled | none-pass | refused | failed
    kept: list = field(default_factory=list)  # the Checks written to the recipe
    left_out: list = field(
        default_factory=list
    )  # (Check, why) for every proposed check that was not kept
    sha256: str | None = None  # of exactly the bytes written and trusted
    detail: str = ""  # why it failed / was refused


def _generated_header() -> str:
    return (
        "# claude-hr test gate for this folder.\n"
        f"# Generated by claude-hr on {time.strftime('%Y-%m-%d')}, after you said yes (nothing else wrote or approved this file).\n"
        "#\n"
        "# After each Claude turn, claude-hr runs the [[fast]] commands below and sends any failure back to Claude to\n"
        "# fix. Each is an argv list, never a shell line, and runs with a scrubbed environment and a timeout.\n"
        "#\n"
        "# Edit it freely: one [[fast]] table per check with name, argv (list of strings), timeout_s (1-900) and cwd\n"
        "# (relative to the repo root). After an edit claude-hr asks you again, the next time you start Claude here,\n"
        "# before it runs the new version (your approval is a hash of this exact file).\n"
        "# Turn it off:  claude-hr-ctl verify disable      See it:  claude-hr-ctl verify status\n"
        "# Turn it on again:  claude-hr-ctl verify enable"
    )


def _target_problem(root: Path) -> str | None:
    """Why `.claude/hr-verify.toml` cannot be written here (checked BEFORE any check runs), or None."""
    from . import verify

    cp = verify.config_path(root)
    for link in (cp.parent, cp):
        if verify._is_link(link):
            return f"{link} is a symbolic link or junction; nothing is written through it"
    try:
        if not stat.S_ISDIR(os.lstat(cp.parent).st_mode):
            return f"{cp.parent} is not a directory"
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"cannot look at {cp.parent}: {exc}"
    if os.path.lexists(cp):
        return f"{cp} already exists; it is never overwritten here"
    return None


def _last_line(text: str) -> str:
    from . import verify

    for ln in reversed((text or "").splitlines()):
        if ln.strip():
            return verify.safe_text(ln.strip()[:120])
    return ""


# Why a proposed check was left out. enable() and the messages tell them apart by these prefixes: a tool that is not
# installed or a check that ran out of time says something different about the person's setup than a check that fails.
WHY_NOT_RUN = "not run: the time limit for this step was used up"
WHY_NOT_START = "could not start (is the tool installed and on PATH?)"
WHY_TIMEOUT = "did not finish within"
WHY_FAILING = "failing today"
WHY_EDITS = "it edits tracked files (a check that rewrites files cannot run after every turn)"
_SIGNATURE_PATHS = 200
_SIGNATURE_BYTES = 1024 * 1024
_SIGNATURE_TIMEOUT_S = 5.0


def _why_kind(why: str) -> str:
    """'failing' | 'start' | 'timeout' | 'edits' | 'skipped' for one left-out reason."""
    for kind, prefix in (
        ("failing", WHY_FAILING),
        ("start", "could not start"),
        ("timeout", WHY_TIMEOUT),
        ("edits", "it edits tracked files"),
    ):
        if why.startswith(prefix):
            return kind
    return "skipped"


def _file_picture(path: str):
    """What one listed path holds: (size, sha256 of the first _SIGNATURE_BYTES bytes) for a regular file, the target for
    a symbolic link, the kind for anything else; None for a path that is gone. Content, not modification time: a check
    that only touches a file, or rewrites it with the same bytes, leaves it as it was."""
    from . import verify

    try:
        st = os.lstat(path)
    except OSError:
        return None  # a deleted file is part of the picture
    if stat.S_ISLNK(st.st_mode):
        try:
            return ("link", os.readlink(path))
        except OSError:
            return ("link", None)
    if not stat.S_ISREG(st.st_mode):
        return ("kind", stat.S_IFMT(st.st_mode))
    try:
        data, _more = verify._read_file(path, _SIGNATURE_BYTES)
    except OSError:
        return (st.st_size, None)
    return (st.st_size, _sha256_hex(data))


def _tracked_signature(root: Path):
    """What the tracked part of the work tree looks like right now: `git status` without untracked files, plus what the
    first _SIGNATURE_PATHS paths it lists hold (_file_picture: size and a hash of the start, not the modification time).
    A rewritten tracked file moves it, whether it was clean or already modified; a file that was only touched, a new
    untracked or ignored file (a cache, __pycache__) does not. None when it cannot be told (no git, git failed or timed
    out, too much output): then nothing is detected. Never raises."""
    try:
        from . import verify

        got = verify._git(
            Path(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
            cap=1_000_000,
            timeout=_SIGNATURE_TIMEOUT_S,
        )
        if got is None or got[1]:
            return None
        pictures = [
            (rel, _file_picture(os.path.join(root, rel)))
            for _code, rel in verify._status_entries(got[0])[:_SIGNATURE_PATHS]
        ]
        return got[0], tuple(pictures)
    except Exception:  # noqa: BLE001
        return None


def _try_checks(root: Path, candidates: list, cfg, say) -> tuple[list, list]:
    """Run each proposed check once, with the person watching, and split them into (kept, left_out).

    Same runner as the gate (verify.run: no shell, scrubbed environment, the process tree is killed on a timeout, stdin
    closed). A check gets min(its own timeout_s, CHECK_CAP_S), the whole step TOTAL_CAP_S. Only a check that exits 0 today
    is kept: a gate that blocks Claude on failures that were there before it touched anything just wastes tokens. A
    check that rewrites tracked files (`eslint --fix`, `prettier --write`) is left out as well: it would do that to the
    person's work after every turn, and it has just done it once to their uncommitted work."""
    from . import verify

    allowed_env = cfg.get("verify.env_allow", []) or []
    kept: list = []
    left_out: list = []
    started = time.monotonic()
    say("")
    say("Running each check once, here, to see which pass today (this can take a minute) ...")
    signature = _tracked_signature(root)
    for chk in candidates:
        label = verify.safe_text(chk.name)
        remaining = TOTAL_CAP_S - (time.monotonic() - started)
        if remaining < 1:
            left_out.append((chk, WHY_NOT_RUN))
            say(f"  [skip] {label}: not run (time limit for this step)")
            continue
        probe = replace(chk, timeout_s=max(1, min(int(chk.timeout_s), CHECK_CAP_S)))
        say(f"  running {label} ...")
        ran = verify.run(root, [probe], total_timeout_s=remaining, env_allow=allowed_env)
        if not ran.results:
            left_out.append((chk, WHY_NOT_RUN))
            say(f"  [skip] {label}: not run (time limit for this step)")
            continue
        res = ran.results[0]
        after = _tracked_signature(root)  # also the starting point of the next check
        edited = signature is not None and after is not None and after != signature
        signature = after
        if edited:
            left_out.append((chk, WHY_EDITS))
            say(f"  [FAIL] {label}: {WHY_EDITS}")
            continue
        if res.ok:
            kept.append(chk)
            say(f"  [ok]   {label} ({res.seconds:.1f}s)")
            continue
        if res.launch_failed:
            why = WHY_NOT_START
        elif res.timed_out:
            why = f"{WHY_TIMEOUT} {min(probe.timeout_s, remaining):.0f}s"
        else:
            why = f"{WHY_FAILING} (exit {res.rc})"
        left_out.append((chk, why))
        say(f"  [FAIL] {label}: {why}")
        tail = _last_line(res.output)
        if tail:
            say(f"           {tail}")
    return kept, left_out


def _plain_dir(path: Path) -> bool:
    """A directory that is not a link (a junction counts as one)."""
    from . import verify

    try:
        return not verify._is_link(path) and stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _exclude_file(root: Path) -> Path | None:
    """The `info/exclude` file that applies to this work tree, or None when there is any doubt.

    A normal clone has a .git directory: `.git/info/exclude`, whose `info` folder is created when it is missing. In a
    linked worktree or a submodule .git is a FILE and the exclude file lives elsewhere (the main repository's common
    directory, or .git/modules/...): git says where, and that path gets the same rules, except that nothing is created
    outside the git directory of this tree: `info` and the directory around it must already be real directories."""
    from . import procutil, verify

    git = Path(root) / ".git"
    gst = os.lstat(git)
    if verify._is_link(git):
        return None
    if stat.S_ISDIR(gst.st_mode):
        info = git / "info"
        try:
            os.lstat(info)
        except FileNotFoundError:
            os.mkdir(info)
        return info / "exclude" if _plain_dir(info) else None
    if not stat.S_ISREG(gst.st_mode):
        return None
    done = procutil.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"], timeout=5
    )
    lines = (done.stdout or "")[:4096].splitlines() if done.returncode == 0 else []
    if len(lines) != 1 or not lines[0].strip() or "\0" in lines[0]:
        return None
    exclude = Path(lines[0].strip())
    if not exclude.is_absolute():
        exclude = Path(root) / exclude
    info = exclude.parent
    if exclude.name != "exclude" or info.name != "info":
        return None
    return exclude if _plain_dir(info) and _plain_dir(info.parent) else None


def exclude_locally(root: Path) -> str:
    """Add `.claude/hr-verify.toml` to the git `info/exclude` file so the generated file never shows up in `git status` or
    gets committed by accident. Local to this clone (in a linked worktree that file is shared with the main checkout and
    its other worktrees); idempotent. 'added' | 'already' | 'skipped' (when anything about that file or the folders
    around it is not a plain directory/regular file, git cannot say where it is, or on any error). Never raises, and
    never writes through a link."""
    from . import verify

    try:
        exclude = _exclude_file(root)
        if exclude is None:
            return "skipped"
        existing = b""
        try:
            est = os.lstat(exclude)
        except FileNotFoundError:
            pass
        else:
            if verify._is_link(exclude) or not stat.S_ISREG(est.st_mode) or est.st_size > 1_000_000:
                return "skipped"
            with open(exclude, "rb") as fh:
                existing = fh.read(1_000_001)
        have = {ln.strip() for ln in existing.decode("utf-8", errors="replace").splitlines()}
        if EXCLUDE_LINE in have or "/" + EXCLUDE_LINE in have:
            return "already"
        sep = b"" if (not existing or existing.endswith(b"\n")) else b"\n"
        with open(exclude, "ab") as fh:
            fh.write(sep + EXCLUDE_LINE.encode("ascii") + b"\n")
        return "added"
    except Exception:  # noqa: BLE001 - bookkeeping that must never stop the enable
        return "skipped"


HR_GATES_NOTE = (
    "Note: the hr-gates plugin is not enabled, so nothing runs yet. "
    "Enable it: claude-hr-ctl setup --only plugins"
)
_MAX_SETTINGS_BYTES = 2_000_000


def _hr_gates_note() -> str | None:
    """HR_GATES_NOTE when the user's Claude settings.json is readable and none of its enabledPlugins entries named
    hr-gates@... is true (the end-of-turn hook lives in that plugin, so without it a turned-on gate never runs); None
    when it is on, and None when the file cannot be read (then nothing is known). Never raises."""
    try:
        import json

        path = paths.claude_settings()
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_SETTINGS_BYTES:
            return None
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8-sig"))
        if not isinstance(data, dict):
            return None
        plugins = data.get("enabledPlugins")
        if isinstance(plugins, dict) and any(
            isinstance(key, str) and key.startswith("hr-gates@") and value is True
            for key, value in plugins.items()
        ):
            return None
        return HR_GATES_NOTE
    except Exception:  # noqa: BLE001 - cannot tell: say nothing
        return None


def enable(root: Path, proposal, cfg, say) -> EnableResult:
    """Turn the gate on for `root` from the discovered `proposal` (the person has just said yes to its commands).

    (a) runs each proposed fast check once, keeps only the ones that pass today; (b) writes them as
    `.claude/hr-verify.toml` without following a link and never over an existing file; (c) hides that file from
    git locally; (d) trusts the sha256 of exactly the bytes written; (e) remembers the answer. Shared by the
    launcher's offer and `claude-hr-ctl verify enable`. It refuses (status "refused") unless a person is at a terminal.
    """
    from . import verify

    root = Path(root)
    why = human.not_a_person()
    if why:
        return EnableResult("refused", detail=why)
    candidates = list(proposal.fast)[: verify.MAX_CHECKS]
    if not candidates:
        return EnableResult("failed", detail="there are no checks to turn on")
    problem = _target_problem(root)
    if problem:
        return EnableResult("failed", detail=problem)

    kept, left_out = _try_checks(root, candidates, cfg, say)
    if left_out:
        say("")
        if all(_why_kind(reason) == "failing" for _chk, reason in left_out):
            say(
                "Failing today, not turned on (fix them, or add them to .claude/hr-verify.toml yourself):"
            )
        else:
            say(
                "Not turned on (fix or install them, or add them to .claude/hr-verify.toml yourself):"
            )
        for chk, reason in left_out:
            say(f"    {verify.command_line(chk)}   [{reason}]")
    if not kept:
        return EnableResult("none-pass", left_out=left_out)

    notes = [f"not turned on, {reason}: {verify.command_line(chk)}" for chk, reason in left_out]
    recipe = verify.Recipe(list(kept), list(kept), proposal.source, notes)
    text = verify.render_toml(recipe, header=_generated_header())
    data = text.encode("utf-8")
    try:
        # the file we are about to write must read back as exactly the commands that were shown and passed
        parsed = verify._parse(data, root, "")
        if parsed.fast != kept:
            return EnableResult(
                "failed", detail="the recipe did not read back as the commands shown"
            )
    except verify.VerifyConfigError as exc:
        return EnableResult(
            "failed", detail=f"the recipe is not valid: {verify.safe_text(str(exc))}"
        )
    digest = _sha256_hex(data)
    try:
        verify.write_config(root, text, force=False)
    except (ValueError, OSError) as exc:
        return EnableResult(
            "failed", detail=f"cannot write the recipe: {verify.safe_text(str(exc))}"
        )
    exclude = exclude_locally(root)
    try:
        verify.trust(
            root, paths.state_dir(), expect_sha256=digest
        )  # refuses if the file is not the bytes we wrote
    except (ValueError, OSError) as exc:
        return EnableResult("failed", detail=f"not trusted: {verify.safe_text(str(exc))}")
    record(root, "enabled", recipe_sha=digest)

    say("")
    say(
        f"Turned on: claude-hr will run {'this check' if len(kept) == 1 else 'these checks'} after each turn in {verify.safe_text(str(root))}:"
    )
    for chk in kept:
        say(f"    {verify.command_line(chk)}")
    say(
        f"The list is {verify.safe_text(str(verify.config_path(root)))}"
        + (
            " (kept out of `git status` through .git/info/exclude)."
            if exclude in ("added", "already")
            else "."
        )
    )
    note = _hr_gates_note()
    if note:
        say(note)
    say("See it: claude-hr-ctl verify status      Turn it off: claude-hr-ctl verify disable")
    return EnableResult("enabled", kept=kept, left_out=left_out, sha256=digest)


def disable(root: Path) -> bool:
    """Take the approval back (the recipe file is kept) and remember that the person does not want to be asked. True if
    the folder was trusted. Needs only the repository's key: it works while the recipe is missing or invalid."""
    from . import verify

    removed = verify.untrust(root, paths.state_dir())
    sha = None
    try:
        data = verify._read_config(root)
        if data is not None:
            sha = verify._sha256(data)
    except (OSError, ValueError):
        sha = None
    record(root, "declined", recipe_sha=sha, reason="turned off with verify disable")
    return removed


# ---------------------------------------------------------------------------------------------------------------
# the question
# ---------------------------------------------------------------------------------------------------------------
def offer_now(root: Path, cfg, say, *, explicit: bool = False) -> str:
    """Look at this repository's state, show the right screen, ask, act. Returns what happened:

    enabled | declined | later (Enter on a screen whose default was No: asked again in a week) |
    not-now (EOF / Ctrl-C / nobody answered) | already-on | nothing-to-offer | declined-before (silent) |
    snoozed (silent: a "later" that is still running) | removed-before (silent: it was turned on and the file was
    deleted) | none-pass | unavailable (invalid recipe) | failed | refused.

    `explicit` is `claude-hr-ctl verify enable`: the person asked, so an earlier "no" or "later" does not silence it
    and every outcome is explained; the launcher passes False and stays quiet unless it really asks."""
    from . import verify

    sdir = paths.state_dir()
    if paths.state_inside(root):
        # the end-of-turn hook refuses a state folder inside the repository (see paths.state_inside), so an approval recorded
        # there would be believed and never honoured: say nothing from the launcher, explain under `verify enable`
        if explicit:
            say(
                f"Not turned on: the state folder {verify.safe_text(str(sdir))} is inside this repository, so the "
                "end-of-turn hook would ignore the approval. Set CLAUDE_HR_HOME to a folder outside the repository, or unset it."
            )
        return "unavailable"
    try:
        loaded = verify.load_checked(root)
    except verify.VerifyConfigError as exc:
        if explicit:
            say(f"{verify.config_path(root)}: {verify.safe_text(str(exc))}")
        return "unavailable"
    state = verify.trust_state_of(root, sdir, loaded.data)
    if state == "trusted":
        if explicit:
            say(
                "The end-of-turn checks are already on for this folder (claude-hr-ctl verify status shows them)."
            )
            record(root, "enabled", recipe_sha=loaded.sha256)
        return "already-on"
    if state == "invalid":
        if explicit:
            say(
                f"{verify.config_path(root)} is not valid; fix it, then run claude-hr-ctl verify enable"
            )
        return "unavailable"
    entry = _entry_for(root)
    decision = entry.get("decision") if entry is not None else None
    declined = decision == "declined"
    removed = decision == "enabled"
    if state == "no-config":
        if (declined or removed) and not explicit:
            # a person who turned it on and then deleted the file (the README's way to remove it) is not asked again
            return "declined-before" if declined else "removed-before"
        if not explicit and _snoozed(entry) and not entry.get("recipe_sha"):
            return "snoozed"
        return _offer_new(root, cfg, say, explicit)
    if not explicit and entry is not None and entry.get("recipe_sha") == loaded.sha256:
        if declined:
            return "declined-before"
        if _snoozed(entry):
            return "snoozed"
    return _offer_existing(root, cfg, say, loaded, state, explicit)


def _start_folder() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


_KIND_TEXT = (
    ("failing", "{n} failing today"),
    ("start", "{n} could not start (is the tool installed and on PATH?)"),
    ("timeout", "{n} did not finish in time"),
    ("edits", "{n} would rewrite tracked files"),
    ("skipped", "{n} not run (time limit for this step)"),
)


def _none_pass_text(left_out: list) -> tuple[str, str]:
    """(what to tell the person, the reason to record) when no proposed check could be turned on. Checks that simply fail
    are 'failing today'; a tool that is not installed, a check that ran out of time or one that rewrites files is
    said as what it is."""
    kinds = [_why_kind(why) for _chk, why in left_out]
    if all(k == "failing" for k in kinds):
        return (
            "None of these checks pass today, so none were turned on (a gate that fails on problems that were "
            "already there only wastes Claude's time). Run claude-hr-ctl verify enable after fixing them.",
            "none of the checks pass today",
        )
    parts = [text.format(n=kinds.count(kind)) for kind, text in _KIND_TEXT if kind in kinds]
    return (
        f"None of these checks could be turned on: {', '.join(parts)}. "
        "Run claude-hr-ctl verify enable after fixing them.",
        "none of the checks could be turned on today",
    )


def _timeout_note(explicit: bool) -> str:
    """What is printed when nobody answers: the launcher's question is on the way to starting Claude, the command is not."""
    return human.TIMEOUT_NOTE if explicit else human.LAUNCHER_TIMEOUT_NOTE


def _later(root: Path, say, recipe_sha: str | None = None) -> str:
    """A bare Enter on a screen whose default was No is "not now", not a no: ask again in LATER_ENTER_DAYS days (an
    explicit n or no is the permanent decline)."""
    reason = "Enter, and No was the default"
    if recipe_sha:
        record(root, _LATER, recipe_sha=recipe_sha, days=LATER_ENTER_DAYS, reason=reason)
    else:
        record(root, _LATER, days=LATER_ENTER_DAYS, reason=reason)
    again = _when({"at": int(time.time())}, "at", LATER_ENTER_DAYS)
    say(
        f"Okay, not now. claude-hr asks again after {again}; "
        "to turn it on sooner: claude-hr-ctl verify enable"
    )
    return "later"


def _offer_new(root: Path, cfg, say, explicit: bool) -> str:
    from . import verify

    proposal = verify.discover(root)
    if not proposal.fast:
        if explicit:
            say(
                "claude-hr found no lint or test commands it recognises in this folder, so there is nothing to "
                "turn on. You can write .claude/hr-verify.toml yourself (claude-hr-ctl verify init shows the format)."
            )
        else:
            # remember it against the project files as they are now: the next launch costs a few stat calls, and an
            # edit to one of them lets this look again
            signature = _marker_signature(root)
            if signature is not None:
                record(root, _NOTHING, marker_mtime=signature)
        return "nothing-to-offer"
    extra, default_yes = _preflight(root)
    views = [_runner_lines(root, c) for c in proposal.fast]
    alarm = _suspicion(_scan_texts(proposal.fast, views))
    for ln in offer_lines(proposal, root, _start_folder(), views):
        say(ln)
    for ln in extra:
        say(ln)
    if alarm:
        default_yes = False
        say("")
        say(f"{alarm}, so the default answer is No.")
    if _unshown(views):
        default_yes = False
        say("")
        say(
            "What at least one of these checks runs could not be shown in full (see the lines in parentheses), "
            "so the default answer is No."
        )
    answer, how = human.ask(
        "Turn it on?", default_yes, tell=say, timeout_note=_timeout_note(explicit)
    )
    if answer is None:
        if how == "timeout" and not explicit:
            record(root, _LATER, days=LATER_TIMEOUT_DAYS, reason="nobody answered the question")
        say("")
        return "not-now"
    if not answer:
        if how == "enter":
            return _later(root, say)
        record(root, "declined")
        say("Okay, not turning it on. To change your mind later: claude-hr-ctl verify enable")
        return "declined"
    result = enable(root, proposal, cfg, say)
    if result.status == "enabled":
        return "enabled"
    if result.status == "none-pass":
        text, reason = _none_pass_text(result.left_out)
        say("")
        say(text)
        record(root, _LATER, days=LATER_NONE_PASS_DAYS, reason=reason)
        return "none-pass"
    if result.status == "failed":
        if verify.trust_state(root, paths.state_dir()) == "trusted":
            return "already-on"  # a second terminal got there first; its answer stands
        say(f"Could not turn it on: {verify.safe_text(result.detail)}")
        record(root, "declined", reason=f"could not turn on: {result.detail}")
        return "failed"
    say(f"Not turned on: {verify.safe_text(result.detail)}")
    return "refused"


def _offer_existing(root: Path, cfg, say, loaded, state: str, explicit: bool = False) -> str:
    """A recipe exists in the folder but nobody approved these exact bytes: a clone that ships one, or an edit."""
    from . import verify

    budget, tier = gate_limits(cfg)
    say("")
    if state == "modified":
        say(
            "The check list in this folder (.claude/hr-verify.toml) has changed since you approved it."
        )
    else:
        say("This folder has a check list (.claude/hr-verify.toml) that you have not approved.")
    say(
        f"If you approve it, claude-hr runs these commands after every Claude turn, without asking, in "
        f"{verify.safe_text(str(root))}:"
    )
    views = recipe_views(root, loaded.recipe)
    for ln in recipe_lines(loaded.recipe, budget, tier, root, views):
        say(ln)
    say(RUNNER_NOTE)
    extra, _default = _preflight(root)
    for ln in extra:
        say(ln)
    alarm = _suspicion(
        _scan_texts([*loaded.recipe.fast, *loaded.recipe.full], list(views.values()))
    )
    if alarm:
        say("")
        say(f"{alarm}.")
    say(f"file sha256: {loaded.sha256}")
    # nothing is cut from the list above (a hidden command is worse than a long screen): with many checks the question
    # says how many there are, so the length of what was printed is not a surprise. A [[full]] check that repeats a
    # [[fast]] one is one command, shown once
    distinct: list = []
    for chk in (*loaded.recipe.fast, *loaded.recipe.full):
        if chk not in distinct:
            distinct.append(chk)
    count = len(distinct)
    question = (
        f"Run these {count} checks after every turn?"
        if count > 12
        else "Run these checks after every turn?"
    )
    answer, how = human.ask(question, False, tell=say, timeout_note=_timeout_note(explicit))
    if answer is None:
        if how == "timeout" and not explicit:
            record(
                root,
                _LATER,
                recipe_sha=loaded.sha256,
                days=LATER_TIMEOUT_DAYS,
                reason="nobody answered the question",
            )
        say("")
        return "not-now"
    if not answer:
        if how == "enter":
            return _later(root, say, loaded.sha256)
        record(root, "declined", recipe_sha=loaded.sha256)
        say("Okay, they will not run. To approve them later: claude-hr-ctl verify enable")
        return "declined"
    why = human.not_a_person()
    if why:
        say(f"Not approved: {why}")
        return "refused"
    try:
        digest = verify.trust(root, paths.state_dir(), expect_sha256=loaded.sha256)
    except (ValueError, OSError) as exc:
        say(verify.safe_text(str(exc)))
        return "failed"
    record(root, "enabled", recipe_sha=digest)
    say(f"Approved ({digest[:12]}...): claude-hr will run these checks after each turn.")
    note = _hr_gates_note()
    if note:
        say(note)
    say("See it: claude-hr-ctl verify status      Turn it off: claude-hr-ctl verify disable")
    return "enabled"


# ---------------------------------------------------------------------------------------------------------------
# `claude-hr-ctl verify status`
# ---------------------------------------------------------------------------------------------------------------
def status_hint(root: Path, state: str, cfg=None) -> str | None:
    """One extra line for `verify status`: has the person been asked, and what did they answer? Nothing for a folder that is
    on (an earlier no, since changed by `verify trust` or a second terminal, is history)."""
    if state == "trusted":
        return None
    entry = _entry_for(root)
    if entry is not None and entry.get("decision") == "declined":
        reason = f" ({entry['reason']})" if entry.get("reason") else ""
        return f"declined on {_when(entry)}{reason}: run claude-hr-ctl verify enable to turn the checks on"
    days = _snooze_days(entry)
    if days is not None:
        reason = f" ({entry['reason']})" if entry.get("reason") else ""
        if _snoozed(entry):
            return (
                f"asked on {_when(entry, 'at')}{reason}, will ask again after {_when(entry, 'at', days)}: "
                "run claude-hr-ctl verify enable to turn the checks on now"
            )
        return (
            f"asked on {_when(entry, 'at')}{reason}, will ask again the next time you start Claude here in a "
            "terminal: or run claude-hr-ctl verify enable now"
        )
    if state == "no-config":
        if entry is not None and entry.get("decision") == "enabled":
            return "turned on earlier, the file was removed: claude-hr-ctl verify enable turns it on again"
        if cfg is not None and str(cfg.get("verify.offer", "ask")).strip().lower() != "ask":
            return "never offered here (verify.offer is off): run claude-hr-ctl verify enable to turn the checks on"
        return (
            "never offered here: claude-hr offers the checks once, when you start Claude in this folder in a "
            "terminal; or run claude-hr-ctl verify enable now"
        )
    return None
