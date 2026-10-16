"""`claude-hr-hook stop`: the end-of-turn gate (plugin hr-gates). ONE process per turn:

    1. format the files Claude changed, with a formatter the repo already configures (fmt.py)
    2. run the repo's trusted fast checks and, if one fails, send the failure back to Claude (verify.py)
    3. append a metadata-only record to the audit trail (auditlog.py)

Everything fails open: any error, timeout or missing tool exits 0 without output, so a gate can never break a session.
Nothing is printed unless a trusted check failed, so a normal turn adds no tokens to the context.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths, procutil

STATE_KEEP = 50  # repositories remembered in state/gates.json
# ONE time budget for the whole hook, shared by the git calls, the formatter and the checks. The plugin's hook timeout
# (plugins/hr-gates/hooks/hooks.json) is 170 s and a hook that gets killed prints nothing: the block it was about to
# deliver is silently lost, no state is saved and the checks' process tree is left running. Finishing a little early
# is always better, so every step is handed what is left of this (verify.gate_timeout_s and format.timeout_s are
# upper limits inside it, not additions to it).
HOOK_BUDGET_S = 150
_GIT_ROOT_S = 8.0  # the longest `git rev-parse` may take


def run() -> int:
    """Entry point used by hooks.main. Prints the block decision (JSON) only when a trusted check failed."""
    try:
        from .hooks import read_stdin_json

        payload = read_stdin_json()
        result = handle(payload)
        if result:
            sys.stdout.write(json.dumps(result))
    except Exception:
        _crash()
    return 0


def handle(payload: dict) -> dict | None:
    """The whole decision, separated from stdin/stdout so it can be tested. Returns the JSON to print or None."""
    deadline = time.monotonic() + HOOK_BUDGET_S
    if os.environ.get("CLAUDE_HR_GATES", "").lower() == "off":
        return None
    # Skip when a subagent is stopping (its parent session gets its own Stop) and in plan mode (nothing was changed on
    # purpose). `stop_hook_active` (Claude is continuing because a stop hook blocked) is deliberately NOT a skip: this gate
    # re-runs the real checks and counts its own blocks per working-tree state (verify.gate, default cap 2, well under
    # Claude Code's cap of 8 consecutive blocks), so a fix attempt is verified instead of waved through.
    if payload.get("agent_id") or payload.get("permission_mode") == "plan":
        return None
    from . import config

    cfg = config.load()
    if not cfg.flag("gates.enabled", True):
        return None
    cwd = str(payload.get("cwd") or os.getcwd())
    root = _git_root(cwd, timeout=min(_GIT_ROOT_S, deadline - time.monotonic()))
    if root is None:
        return None
    if _state_inside(root, cwd):
        _state_inside_note(root)
        return None

    from . import verify

    state = _State.load()
    key = _repo_key(root)
    sig_before = _signature(verify, root, deadline)
    # an empty signature is "cannot tell" (git failed, the status listing was cut off, no time): never an idle tree
    if sig_before and state.seen(key) == sig_before:
        return None  # nothing changed since the last time this gate looked at the tree (and the recipe's trust did not)
    started = time.monotonic()

    formatted: list[str] = []
    skipped = errors = 0
    formatters: list[str] = []
    # the formatter may use at most half of what is left, so the checks always keep the rest
    fmt_budget = min(float(cfg.get("format.timeout_s", 30)), (deadline - time.monotonic()) / 2)
    if cfg.flag("format.enabled", True) and fmt_budget >= 1.0:
        from . import fmt

        rep = fmt.format_changed(
            root,
            timeout_s=fmt_budget,
            max_files=int(cfg.get("format.max_files", 40)),
            max_kb=int(cfg.get("format.max_file_kb", 512)),
            skip_globs=tuple(cfg.get("format.skip_globs", []) or ()),
            allow=list(cfg.get("format.allow", []) or []) or None,
            clean_gate=cfg.flag("format.clean_gate", True),
            trusted=repo_tools_allowed(cfg, root),
        )
        formatted, skipped, errors = list(rep.formatted), len(rep.skipped), len(rep.errors)
        formatters = list(rep.formatters)

    outcome = None
    if cfg.flag("verify.gate", True):
        outcome = verify.gate(
            root,
            paths.state_dir(),
            max_blocks=int(cfg.get("verify.gate_max_blocks", 2)),
            timeout_s=int(cfg.get("verify.gate_timeout_s", 120)),
            tier=str(cfg.get("verify.tier", "fast")),
            test_guard=cfg.flag("verify.test_guard", True),
            deadline=deadline,
            env_allow=cfg.get("verify.env_allow", []) or [],
        )

    action = getattr(outcome, "action", "")
    note = str(getattr(outcome, "note", "") or "")
    blocked = action == "block"
    # A note with a skip and no check run ("time budget": not even started; "another gate run in progress"): the tree was
    # NOT looked at. Retry next time like an error (the formatter or git ate the budget this turn, the other run may
    # have found something); a run that started and was cut short is remembered, or an over-long suite would be started
    # and cut short again on every Stop.
    not_looked_at = bool(note) and action == "skip" and not getattr(outcome, "results", None)
    # "cannot start": a check's tool is missing. The checks that could run passed, so the tree is settled: remembering it
    # keeps an idle Stop from running the first checks again every time (the reason is in the trail). Any other error is
    # not remembered (a broken state file may be fixed, a bug may not recur).
    cannot_start = action == "error" and note == "cannot start"
    if not blocked and (action != "error" or cannot_start) and not not_looked_at:
        # remember the tree as it is NOW (after formatting) so the next idle Stop is a no-op. A blocked tree is NOT
        # remembered (if Claude stops again without changing anything, the failing checks must be looked at again).
        # The state AFTER the checks: they may leave files behind (coverage output, build artifacts that are not
        # gitignored), and the next idle Stop must not mistake that for a change by Claude. (Accepted trade-off: an
        # edit someone else makes during the run is absorbed the same way, and is only checked on the next change.)
        # An empty signature ("cannot tell") is never remembered: the gate and the formatter run again next time.
        sig_after = _signature(verify, root, deadline)
        if sig_after:
            state.remember(key, sig_after)
            state.save()

    if cfg.flag("gates.trail", True):
        _trail(
            cfg,
            root,
            formatted,
            skipped,
            errors,
            formatters,
            outcome,
            time.monotonic() - started,
            note=note,
        )

    if blocked:
        return {"decision": "block", "reason": outcome.reason}
    if action == "skip" and "block cap reached" in str(getattr(outcome, "reason", "")):
        # the cap ended the blocking: tell the USER (systemMessage is shown to them, it is not a block and adds nothing
        # to the model's context) that the failure is still there, instead of stopping silently with failing tests
        return {"systemMessage": f"claude-hr: {_first_line(outcome.reason, 300)}"}
    if action == "skip" and note == "time budget" and getattr(outcome, "results", None):
        # a check was cut short by the time budget (not by its own timeout_s): that is not a failure and never blocks,
        # but the USER should know the gate did not get to a verdict. Said once: the tree is remembered above, so the
        # next idle Stop is a no-op, and a changed tree gets a fresh run and a fresh message.
        check = str(getattr(outcome, "check", "") or "") or str(
            getattr(outcome.results[-1], "name", "") or "a check"
        )
        return {
            "systemMessage": f"claude-hr: {_plain(check, 60)} did not finish inside the time budget; not blocking"
        }
    return None


def _plain(text: object, limit: int) -> str:
    """`text` as one short printable line (a check name comes from the repository's recipe)."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))[:limit].strip()


def _first_line(text: object, limit: int) -> str:
    """One short line of `text` (a skip/error reason names a check and an exit code, never the check's output)."""
    line = str(text or "").strip().splitlines()[0:1]
    return (line[0] if line else "")[:limit]


def _trail(
    cfg, root: Path, formatted, skipped, errors, formatters, outcome, seconds: float, note: str = ""
) -> None:
    from . import auditlog

    checks = [r.name for r in getattr(outcome, "results", []) or []] if outcome else []
    # e.g. note="time budget": the hook's deadline cut the checks short (or left no time to start them)
    extra = {"note": note} if note else {}
    if outcome is not None and getattr(outcome, "action", "") in ("skip", "error"):
        extra["reason"] = _first_line(
            getattr(outcome, "reason", ""), 110
        )  # why nothing ran / what went wrong
    auditlog.record(
        "gate",
        repo=auditlog.repo_id(root),
        formatted=len(formatted),
        format_skipped=skipped,
        format_errors=errors,
        formatters=formatters,
        verify=outcome.action if outcome else "off",
        checks=checks,
        seconds=round(seconds, 2),
        **extra,
    )


def repo_tools_allowed(cfg, root: Path) -> bool:
    """May the formatter step use tools and configuration that live in the repository (a .venv or node_modules binary,
    prettier config that runs code)? Only for a repository the user trusted, or when format.repo_tools = always."""
    if str(cfg.get("format.repo_tools", "trusted")).strip().lower() == "always":
        return True
    try:
        from . import verify

        return verify.trust_state(root, paths.state_dir()) == "trusted"
    except Exception:  # noqa: BLE001
        return False


def _signature(verify, root: Path, deadline: float | None = None) -> str:
    """The working tree plus the recipe's trust state: trusting a recipe must re-run the gate even if no file changed.
    `deadline` (a time.monotonic() value) bounds the git calls of the fingerprint. "" when the tree cannot be fingerprinted
    (git missing or failing, a status listing cut off at git's cap, out of time): "cannot tell", which the caller must
    never take for "nothing changed" nor remember."""
    fp = verify.fingerprint(root, deadline=deadline)
    if not fp:
        return ""
    return f"{fp}|{verify.trust_state(root, paths.state_dir())}"


def _state_inside(root: Path, cwd: str) -> bool:
    """Is the state directory (it holds the trust record) inside the repository being gated or the folder Claude works in?

    CLAUDE_HR_HOME decides where it is, and the hook inherits the environment Claude Code built from the project's own
    .claude/settings.json: a repository could point it at a folder it ships, with a trust record already in it, and have
    its own recipe run unprompted. The hook trusts only a record the repository cannot have written, so it does nothing
    here. The CLI keeps honouring CLAUDE_HR_HOME. Not being able to tell counts as inside."""
    try:
        state = _fold(paths.state_dir())
        trees = (_fold(root), _fold(cwd))
    except (OSError, ValueError):
        return True
    for tree in trees:
        try:
            if os.path.commonpath([state, tree]) == tree:
                return True
        except ValueError:  # another drive: not inside
            continue
    return False


def _fold(path: Path | str) -> str:
    """The real location of `path`, compared without regard to case on Windows."""
    return os.path.normcase(os.path.realpath(path))


def _state_inside_note(root: Path) -> None:
    """One audit-trail line saying why nothing ran (auditlog needs nothing but paths: no heavy import for a skip)."""
    try:
        from . import auditlog

        auditlog.record(
            "gate",
            repo=auditlog.repo_id(root),
            verify="skip",
            note="state directory inside the repository",
            reason="the state directory is inside this repository, so its trust record is not honoured",
        )
    except Exception:  # noqa: BLE001 - a note is never worth a failed hook
        pass


def _git_root(cwd: str, timeout: float = _GIT_ROOT_S) -> Path | None:
    if timeout <= 0.25:  # the hook's budget is already spent
        return None
    r = procutil.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=timeout)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    return Path((r.stdout or "").strip().splitlines()[0])


def _repo_key(root: Path) -> str:
    key = str(root.resolve())
    return key.casefold() if paths.IS_WINDOWS else key


class _State:
    """state/gates.json: the working-tree fingerprint each repository had when this gate last finished."""

    def __init__(self, data: dict):
        self.data = data
        # the keys this process set: save() writes only those over whatever is on disk by then
        self._remembered: list[str] = []

    @classmethod
    def load(cls) -> _State:
        from .jsonio import read_json

        try:
            data = read_json(paths.state_dir() / "gates.json", {}) or {}
        except Exception:
            data = {}
        return cls(data if isinstance(data, dict) else {})

    def seen(self, key: str) -> str | None:
        entry = self.data.get(key)
        return entry.get("fp") if isinstance(entry, dict) else None

    def remember(self, key: str, fp: str) -> None:
        self.data.pop(key, None)  # re-insert last: dict order doubles as recency
        self.data[key] = {"fp": fp, "ts": int(time.time())}
        while len(self.data) > STATE_KEEP:
            self.data.pop(next(iter(self.data)))
        if key in self._remembered:
            self._remembered.remove(key)
        self._remembered.append(key)

    def save(self) -> None:
        """Write this process's entries into gates.json, merged into the file as it is NOW and under a lock: two
        sessions that finish together must not overwrite each other's repositories with a stale copy."""
        from .jsonio import atomic_write_text, read_json

        if not self._remembered:
            return
        try:
            state_dir = paths.state_dir()
            state_dir.mkdir(parents=True, exist_ok=True)
            with _best_effort_lock(state_dir / "gates.lock"):
                try:
                    current = read_json(state_dir / "gates.json", {}) or {}
                except Exception:
                    current = {}
                if not isinstance(current, dict):
                    current = {}
                for key in self._remembered:
                    if key in self.data:
                        current.pop(key, None)  # re-insert last: dict order doubles as recency
                        current[key] = self.data[key]
                while len(current) > STATE_KEEP:
                    current.pop(next(iter(current)))
                self.data = current
                atomic_write_text(
                    state_dir / "gates.json", json.dumps(current) + "\n", follow_symlinks=False
                )
        except OSError:
            pass


@contextmanager
def _best_effort_lock(path: Path):
    """An advisory lock that never blocks the hook for long: after 2 s (or if no lock file can be made) go on
    without it, the worst case is one lost bookkeeping entry (the next Stop re-runs the gate once)."""
    lock = None
    try:
        from .filelock import locked

        lock = locked(path, timeout=2.0, stale_after=60.0)
        lock.__enter__()
    except OSError:  # includes the lock timeout
        lock = None
    try:
        yield
    finally:
        if lock is not None:
            lock.__exit__(None, None, None)


def _crash() -> None:
    """Hooks run windowless: leave a trace for `doctor` / the user, never raise."""
    import traceback

    try:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        with open(paths.logs_dir() / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} claude-hr-hook stop\n{traceback.format_exc()}\n"
            )
    except OSError:
        pass
