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
    directory, or for a repository that is trusted / invalid / already answered;
  * a "yes" is only ever read from a terminal (human.not_a_person) - there is no flag or variable that answers;
  * the generated recipe holds only the argv commands that were shown, written without following a symlink, hashed from
    the exact bytes and trusted with that hash;
  * the only things written into the repository are `.claude/hr-verify.toml` and one line in `.git/info/exclude`.

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

OFFER_FILE = "verify-offer.json"  # state/: {repo_key: {decision, ts, recipe_sha?, reason?}}
MAX_ENTRIES = 200
CHECK_CAP_S = 60  # the longest one check may take while the person watches it run once
TOTAL_CAP_S = 120  # ... and all of them together
EXCLUDE_LINE = ".claude/hr-verify.toml"
DECISIONS = ("enabled", "declined")

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


def decision_for(root: str | Path) -> dict | None:
    """The recorded answer for this repository ({decision, ts, recipe_sha?, reason?}) or None."""
    entry = _json_dict(offer_path()).get(_repo_key(root))
    if isinstance(entry, dict) and entry.get("decision") in DECISIONS:
        return entry
    return None


def record(
    root: str | Path, decision: str, *, recipe_sha: str | None = None, reason: str | None = None
) -> bool:
    """Remember the answer (replacing an earlier one). At most MAX_ENTRIES repositories, oldest dropped. True if written."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    try:
        import json

        from .jsonio import atomic_write_text

        path = offer_path()
        state = _json_dict(path)
        key = _repo_key(root)
        state.pop(key, None)  # re-insert last: dict order doubles as recency
        entry: dict = {"decision": decision, "ts": int(time.time())}
        if recipe_sha:
            entry["recipe_sha"] = recipe_sha
        if reason:
            entry["reason"] = reason[:200]
        state[key] = entry
        while len(state) > MAX_ENTRIES:
            state.pop(next(iter(state)))
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


def _when(entry: dict) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(entry.get("ts", 0))))
    except (TypeError, ValueError, OverflowError, OSError):
        return "earlier"


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
    would never see the question while the launch waited for a key. No terminal on stderr, no question."""
    try:
        return bool(sys.stderr and sys.stderr.isatty())
    except (OSError, ValueError, AttributeError):
        return False


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


def _might_have_checks(root: Path) -> bool:
    return any(os.path.exists(os.path.join(root, m)) for m in _DISCOVER_MARKERS)


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
    folder (and the recipe, if any, is the one they said no to); there is no recipe and nothing in the folder that
    verify.discover could propose from; the recipe is not a readable regular file or is over the size limit (verify
    calls those invalid and nothing in them is ever run). Everything else - including every doubt - is "look", and
    offer_now then asks verify itself."""
    state, data = _recipe_bytes(root)
    if state == "odd":
        return "quiet"
    if state == "missing":
        entry = decision_for(root)
        if (entry is not None and entry.get("decision") == "declined") or not _might_have_checks(
            root
        ):
            return "quiet"
        return "look"
    if len(data) > _MAX_CONFIG_BYTES:
        return "quiet"
    sha = _sha256_hex(data)
    trusted = _json_dict(paths.state_dir() / _TRUST_FILE).get(_repo_key(root))
    if isinstance(trusted, str) and trusted == sha:
        return "quiet"
    entry = decision_for(root)
    if entry is not None and entry.get("decision") == "declined" and entry.get("recipe_sha") == sha:
        return "quiet"
    return "look"


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
    if human.not_a_person() is not None or not _can_see_the_question():
        return
    if not _plain_session(args):
        return
    root = work_tree(os.getcwd())
    if root is None:
        return
    if _quick_verdict(root) == "quiet":
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


def recipe_lines(recipe, gate_timeout_s: float | None = None, gate_tier: str = "fast") -> list[str]:
    """Every check of both tiers, in full, exactly as the gate would run it.

    This is what a person reads before approving the file, so nothing is left out and nothing is printed raw: name,
    argv items and cwd go through verify.safe_text (an escape sequence or bidi override in the repository's text is
    shown as \\x1b / \\u202e, it cannot erase or reorder the line). A [[full]] check is only labelled a repeat when
    it is IDENTICAL to a [[fast]] check (name, argv, timeout and cwd); one that merely shares a name with a fast
    check is shown like any other, so a difference cannot hide behind a familiar name.

    `gate_timeout_s` (verify.gate_timeout_s) is what ALL the checks of one end-of-turn run get together. A check of the
    tier the gate runs whose own timeout_s is above it is shown with the limit that really applies there, and what
    happens at that limit: the gate stops it, tells the user and does not block (`verify run` honours timeout_s).
    """
    from . import verify

    lines: list[str] = []
    fast = list(recipe.fast)
    gate_tier = "full" if gate_tier == "full" and recipe.full else "fast"
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
    for note in getattr(recipe, "notes", None) or []:
        lines.append(f"  note: {verify.safe_text(str(note)[:300])}")
    return lines


def offer_lines(recipe) -> list[str]:
    """The plain-language offer for a folder that has no recipe yet (the fast checks discovery found)."""
    from . import verify

    lines = [
        "",
        "claude-hr can double-check Claude's work in this folder (optional; asked once per folder).",
        "After each turn it would run this project's own checks and send any failure back to Claude to fix:",
    ]
    lines += [f"    {n}. {verify.command_line(c)}" for n, c in enumerate(recipe.fast, 1)]
    lines += [
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


def _try_checks(root: Path, candidates: list, cfg, say) -> tuple[list, list]:
    """Run each proposed check once, with the person watching, and split them into (kept, left_out).

    Same runner as the gate (verify.run: no shell, scrubbed environment, the process tree is killed on a timeout, stdin
    closed). A check gets min(its own timeout_s, CHECK_CAP_S), the whole step TOTAL_CAP_S. Only a check that exits 0 today
    is kept: a gate that blocks Claude on failures that were there before it touched anything just wastes tokens."""
    from . import verify

    allowed_env = cfg.get("verify.env_allow", []) or []
    kept: list = []
    left_out: list = []
    started = time.monotonic()
    say("")
    say("Running each check once, here, to see which pass today (this can take a minute) ...")
    for chk in candidates:
        label = verify.safe_text(chk.name)
        remaining = TOTAL_CAP_S - (time.monotonic() - started)
        if remaining < 1:
            left_out.append((chk, "not run: the time limit for this step was used up"))
            say(f"  [skip] {label}: not run (time limit for this step)")
            continue
        probe = replace(chk, timeout_s=max(1, min(int(chk.timeout_s), CHECK_CAP_S)))
        say(f"  running {label} ...")
        ran = verify.run(root, [probe], total_timeout_s=remaining, env_allow=allowed_env)
        if not ran.results:
            left_out.append((chk, "not run: the time limit for this step was used up"))
            say(f"  [skip] {label}: not run (time limit for this step)")
            continue
        res = ran.results[0]
        if res.ok:
            kept.append(chk)
            say(f"  [ok]   {label} ({res.seconds:.1f}s)")
            continue
        if res.launch_failed:
            why = "could not start (is the tool installed and on PATH?)"
        elif res.timed_out:
            why = f"did not finish within {min(probe.timeout_s, remaining):.0f}s"
        else:
            why = f"failing today (exit {res.rc})"
        left_out.append((chk, why))
        say(f"  [FAIL] {label}: {why}")
        tail = _last_line(res.output)
        if tail:
            say(f"           {tail}")
    return kept, left_out


def exclude_locally(root: Path) -> str:
    """Add `.claude/hr-verify.toml` to `.git/info/exclude` so the generated file never shows up in `git status` or gets
    committed by accident. Local to this clone; idempotent. 'added' | 'already' | 'skipped' (when .git is a file - a
    worktree or submodule - or anything about .git/info/exclude is not a plain directory/file, or on any error).
    Never raises, and never writes through a link."""
    from . import verify

    git = Path(root) / ".git"
    try:
        if verify._is_link(git) or not stat.S_ISDIR(os.lstat(git).st_mode):
            return "skipped"
        info = git / "info"
        try:
            ist = os.lstat(info)
        except FileNotFoundError:
            os.mkdir(info)
            ist = os.lstat(info)
        if verify._is_link(info) or not stat.S_ISDIR(ist.st_mode):
            return "skipped"
        exclude = info / "exclude"
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
    except OSError:
        return "skipped"


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
        say(
            "Failing today, not turned on (fix them, or add them to .claude/hr-verify.toml yourself):"
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

    enabled | declined | not-now (EOF / Ctrl-C) | already-on | nothing-to-offer | declined-before (silent) |
    none-pass | unavailable (invalid recipe) | failed | refused.

    `explicit` is `claude-hr-ctl verify enable`: the person asked, so an earlier "no" does not silence it and every
    outcome is explained; the launcher passes False and stays quiet unless it really asks."""
    from . import verify

    sdir = paths.state_dir()
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
    entry = decision_for(root)
    declined = entry is not None and entry.get("decision") == "declined"
    if state == "no-config":
        if declined and not explicit:
            return "declined-before"
        return _offer_new(root, cfg, say, explicit)
    if declined and not explicit and entry.get("recipe_sha") == loaded.sha256:
        return "declined-before"
    return _offer_existing(root, cfg, say, loaded, state)


def _offer_new(root: Path, cfg, say, explicit: bool) -> str:
    from . import verify

    proposal = verify.discover(root)
    if not proposal.fast:
        if explicit:
            say(
                "claude-hr found no lint or test commands it recognises in this folder, so there is nothing to "
                "turn on. You can write .claude/hr-verify.toml yourself (claude-hr-ctl verify init shows the format)."
            )
        return "nothing-to-offer"
    extra, default_yes = _preflight(root)
    for ln in offer_lines(proposal):
        say(ln)
    for ln in extra:
        say(ln)
    answer = human.ask_yes_no("Turn it on?", default_yes, tell=say)
    if answer is None:
        say("")
        return "not-now"
    if not answer:
        record(root, "declined")
        say("Okay, not turning it on. To change your mind later: claude-hr-ctl verify enable")
        return "declined"
    result = enable(root, proposal, cfg, say)
    if result.status == "enabled":
        return "enabled"
    if result.status == "none-pass":
        say("")
        say(
            "None of these checks pass today, so none were turned on (a gate that fails on problems that were "
            "already there only wastes Claude's time). Fix them, then run: claude-hr-ctl verify enable"
        )
        record(root, "declined", reason="none of the checks pass today")
        return "none-pass"
    if result.status == "failed":
        if verify.trust_state(root, paths.state_dir()) == "trusted":
            return "already-on"  # a second terminal got there first; its answer stands
        say(f"Could not turn it on: {verify.safe_text(result.detail)}")
        record(root, "declined", reason=f"could not turn on: {result.detail}")
        return "failed"
    say(f"Not turned on: {verify.safe_text(result.detail)}")
    return "refused"


def _offer_existing(root: Path, cfg, say, loaded, state: str) -> str:
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
    for ln in recipe_lines(loaded.recipe, budget, tier):
        say(ln)
    extra, _default = _preflight(root)
    for ln in extra:
        say(ln)
    say(f"file sha256: {loaded.sha256}")
    answer = human.ask_yes_no("Run these checks after every turn?", False, tell=say)
    if answer is None:
        say("")
        return "not-now"
    if not answer:
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
    entry = decision_for(root)
    if entry is not None and entry.get("decision") == "declined":
        reason = f" ({entry['reason']})" if entry.get("reason") else ""
        return f"declined on {_when(entry)}{reason}: run claude-hr-ctl verify enable to turn the checks on"
    if state == "no-config":
        if cfg is not None and str(cfg.get("verify.offer", "ask")).strip().lower() != "ask":
            return "never offered here (verify.offer is off): run claude-hr-ctl verify enable to turn the checks on"
        return (
            "never offered here: claude-hr offers the checks once, when you start Claude in this folder in a "
            "terminal; or run claude-hr-ctl verify enable now"
        )
    return None
