"""Local token ledger: aggregate usage from Claude Code transcripts (no network, nothing leaves the machine)."""

from __future__ import annotations

import json
import ntpath
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import paths
from .jsonio import JsonFileError, read_json, write_json


# ---------------------------------------------------------------------------------------------
# which transcript files exist (shared by the ledger, `plugins audit` and the transcript secret scan)
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Transcript:
    path: Path
    project: str  # the folder name under projects/
    kind: str  # "main" (a session) or "subagent" (a subagent or workflow agent)
    size: int
    mtime: float


# Windows refuses to open a path of 260 characters or more unless long paths are switched on (they are off by default)
# or the path is written in the extended-length form. A workflow transcript below a long project folder name is such a
# path, so every open and every folder listing of a transcript goes through long_path().
_LONG_PATH = 240  # not 260: a folder is listed as <folder>\* and a file's own name is added to it
_EXTENDED = "\\\\?\\"


def long_path(path: str | os.PathLike, windows: bool | None = None) -> str:
    """`path` as open() and scandir() should get it: unchanged, except on Windows for a long absolute path, which
    becomes the extended-length form (\\\\?\\C:\\..., \\\\?\\UNC\\server\\share\\...). That form switches Windows' own
    clean-up of the path off, so it is normalised here first."""
    p = os.fspath(path)
    if not (paths.IS_WINDOWS if windows is None else windows):
        return p
    if len(p) < _LONG_PATH or p.startswith(_EXTENDED):
        return p
    full = ntpath.abspath(p)
    if full.startswith("\\\\"):
        return _EXTENDED + "UNC\\" + full[2:]
    return _EXTENDED + full


def plain_path(path: str) -> str:
    """The inverse of long_path(): what a person reads (and a message names) is the ordinary spelling."""
    if path.startswith(_EXTENDED + "UNC\\"):
        return "\\\\" + path[len(_EXTENDED) + 4 :]
    return path[len(_EXTENDED) :] if path.startswith(_EXTENDED) else path


# Claude Code keeps a session as projects/<project>/<session>.jsonl and the agents it spawned beside it, as
# projects/<project>/<session>/subagents/agent-<id>.jsonl and, for a workflow run, below
# subagents/workflows/<run>/agent-<id>.jsonl (next to a journal.jsonl that holds orchestration, no model messages).
# A project folder that keeps subagents/ directly is read the same way. Never followed: symlinked folders.
_NOT_TRANSCRIPTS = {"journal.jsonl"}


def _subdirs(path: str, problems: list[str] | None = None) -> list[os.DirEntry]:
    try:
        with os.scandir(long_path(path)) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        if problems is not None:
            problems.append(plain_path(path))
        return []


def iter_transcripts(
    root: Path, cutoff: float, subagents: bool = True, problems: list[str] | None = None
) -> Iterator[Transcript]:
    """Every transcript file below `root` modified at or after `cutoff` (epoch seconds). Files are only stat-ed here: a
    file older than the window is never opened. Order is unspecified; sort the result.

    A folder that cannot be listed, or a transcript that cannot be stat-ed, is added to `problems` (when given): a
    caller that reports what it read must also report what it could not."""
    for proj in _subdirs(str(root), problems):
        proj_path = plain_path(proj.path)
        try:
            entries = list(os.scandir(long_path(proj_path)))
        except OSError:
            if problems is not None:
                problems.append(proj_path)
            continue
        stack: list[str] = []
        for e in entries:
            try:
                if e.name.endswith(".jsonl") and e.is_file():
                    st = e.stat()
                    if st.st_mtime >= cutoff:
                        yield Transcript(
                            Path(plain_path(e.path)), proj.name, "main", st.st_size, st.st_mtime
                        )
                elif subagents and e.is_dir(follow_symlinks=False):
                    here = plain_path(e.path)
                    stack.append(here if e.name == "subagents" else os.path.join(here, "subagents"))
            except OSError:
                if problems is not None and e.name.endswith(".jsonl"):
                    problems.append(plain_path(e.path))
        while stack:  # each pending folder is a subagents/ folder or something below one
            here = stack.pop()
            try:
                with os.scandir(long_path(here)) as it:
                    below = list(it)
            except (
                FileNotFoundError,
                NotADirectoryError,
            ):  # a session without subagents: nothing is missing
                continue
            except OSError:
                if problems is not None:
                    problems.append(here)
                continue
            for e in below:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(plain_path(e.path))
                    elif (
                        e.name.endswith(".jsonl") and e.name not in _NOT_TRANSCRIPTS and e.is_file()
                    ):
                        st = e.stat()
                        if st.st_mtime >= cutoff:
                            yield Transcript(
                                Path(plain_path(e.path)),
                                proj.name,
                                "subagent",
                                st.st_size,
                                st.st_mtime,
                            )
                except OSError:
                    if problems is not None and e.name.endswith(".jsonl"):
                        problems.append(plain_path(e.path))


def list_transcripts(
    root: Path, cutoff: float, subagents: bool = True, problems: list[str] | None = None
) -> list[Transcript]:
    """`iter_transcripts` as a list, newest first."""
    return sorted(iter_transcripts(root, cutoff, subagents, problems), key=lambda t: -t.mtime)


def has_transcripts(root: Path | None = None) -> bool:
    """True when any transcript exists, however old."""
    root = root or (paths.claude_home() / "projects")
    return next(iter_transcripts(root, 0.0), None) is not None


# ---------------------------------------------------------------------------------------------
# token ledger
# ---------------------------------------------------------------------------------------------
# USD per million tokens: (input, output, cache write for the 5-minute TTL, cache read). List prices of the Anthropic
# API (as of 2026-10), to ESTIMATE what the same traffic would cost there: a subscription is not billed per token.
# One flat price per model family: tiered (long-context) and batch prices are not modelled, and the report says so.
# Fast mode is: a message whose usage block says speed "fast" on a model that has it costs twice the price in every
# class (Opus 5.5 $8/$40 against $4/$20; the caching multipliers stack on the fast price). Cache hits cost 0.05x the
# input price on Opus 5.5 and Sonnet 5.5, 0.1x on the others. The first row whose marker is in the model id wins; a
# model with no row is left out of every cost, counted, and shown as n/a.
_PRICES: tuple[tuple[tuple[str, ...], tuple[float, float, float, float]], ...] = (
    (("fable-5-1", "mythos-5-1"), (10.0, 50.0, 12.5, 0.25)),
    (("fable-5", "mythos-5"), (10.0, 50.0, 12.5, 1.0)),
    (("opus-5-5",), (4.0, 20.0, 5.0, 0.20)),
    (("opus-5", "opus-4-8", "opus-4-7", "opus-4-6"), (5.0, 25.0, 6.25, 0.50)),
    (("sonnet-5-5",), (2.0, 10.0, 2.5, 0.10)),
    (("sonnet-5",), (2.0, 10.0, 2.5, 0.20)),
    (("sonnet-4-6",), (3.0, 15.0, 3.75, 0.30)),
    (("haiku-5",), (0.10, 0.50, 0.125, 0.01)),
    (("haiku-4-5",), (1.0, 5.0, 1.25, 0.10)),
)
# the models that have fast mode ("opus-5" also matches Opus 5.5)
_FAST_MODE = ("opus-5", "opus-4-8")


def _int(v: object) -> int:
    try:
        return int(v or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def estimate_cost(model: str, u: dict) -> float | None:
    """API list-price estimate in USD of one message's usage block; None when the model has no price."""
    price = next((p for markers, p in _PRICES if any(m in model for m in markers)), None)
    if price is None:
        return None
    p_in, p_out, p_write, p_read = price
    write = _int(u.get("cache_creation_input_tokens"))
    cc = u.get("cache_creation")
    hour = min(write, _int(cc.get("ephemeral_1h_input_tokens"))) if isinstance(cc, dict) else 0
    cost = (
        _int(u.get("input_tokens")) * p_in
        + _int(u.get("output_tokens")) * p_out
        + (write - hour) * p_write
        + hour * p_in * 2  # a 1-hour cache write costs twice the input price
        + _int(u.get("cache_read_input_tokens")) * p_read
    ) / 1_000_000
    if u.get("speed") == "fast" and any(m in model for m in _FAST_MODE):
        cost *= 2  # fast mode: every price class doubles
    return cost


@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    messages: int = 0
    cost: float = 0.0  # estimated, see estimate_cost; a model without a price adds nothing to it
    unpriced: int = 0  # messages of a model without a price

    def add(self, u: dict, cost: float | None = 0.0) -> None:
        """`cost` None = the model has no price: the message counts, its cost is unknown (not zero)."""
        self.input += _int(u.get("input_tokens"))
        self.output += _int(u.get("output_tokens"))
        self.cache_write += _int(u.get("cache_creation_input_tokens"))
        self.cache_read += _int(u.get("cache_read_input_tokens"))
        self.messages += 1
        if cost is None:
            self.unpriced += 1
        else:
            self.cost += cost

    @property
    def total_in(self) -> int:
        return self.input + self.cache_write + self.cache_read

    @property
    def cache_hit(self) -> float:
        return (self.cache_read / self.total_in) if self.total_in else 0.0


@dataclass
class Ledger:
    days: int
    by_day: dict[str, Bucket] = field(default_factory=lambda: defaultdict(Bucket))
    by_model: dict[str, Bucket] = field(default_factory=lambda: defaultdict(Bucket))
    by_project: dict[str, Bucket] = field(default_factory=lambda: defaultdict(Bucket))
    total: Bucket = field(default_factory=Bucket)
    files: int = 0
    # the same messages again, split by where they ran
    main: Bucket = field(default_factory=Bucket)  # the sessions you talk to
    sub: Bucket = field(default_factory=Bucket)  # subagents and workflow agents
    files_main: int = 0
    files_sub: int = 0
    subagents: bool = True  # False: subagent and workflow transcripts were not read
    unpriced: int = 0  # messages of a model without a price (in no cost figure)
    partial: bool = False  # the time budget ran out before every file was read
    unreadable: int = 0  # transcript files (or folders) that could not be read: in no figure
    seconds: float = 0.0
    # the scan window began at `since` and the scan started at `until` (epoch seconds): a UTC day that lies
    # between them was seen whole (see write_snapshot)
    since: float = 0.0
    until: float = 0.0

    def subagent_share(self) -> float | None:
        """Share of the estimated cost spent in subagents and workflows (of output tokens when nothing is priced)."""
        if self.total.cost > 0:
            return self.sub.cost / self.total.cost
        if self.total.output > 0:
            return self.sub.output / self.total.output
        return None


_REQUEST_ID = b'"requestId":"'


def _request_id(line: bytes) -> bytes | None:
    i = line.find(_REQUEST_ID)
    if i < 0:
        return None
    j = line.find(b'"', i + len(_REQUEST_ID))
    return line[i + len(_REQUEST_ID) : j] if j > 0 else None


def _read_messages(
    path: Path, cutoff: float
) -> tuple[dict[tuple, tuple[datetime, str, dict]], bool]:
    """The model messages of one transcript inside the window: ({key: (time, model, usage)}, read completely).

    A streamed message is logged once per content block, all with the same message id and request id; the first
    lines carry a partial output count and the LAST line the final one, which is the one kept (input and cache
    counts are the same on every line). The lines of a message sit together, so only the last line of each run
    with one request id is parsed: json is most of the cost of a scan. A file that cannot be opened or read to its
    end gives what was read and False: the caller counts it instead of skipping it silently."""
    got: dict[tuple, tuple[datetime, str, dict]] = {}

    def take(line: bytes, n: int) -> None:
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):  # includes a line that is not UTF-8
            return
        msg = rec.get("message") if isinstance(rec, dict) else None
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict):
            return
        model = str(msg.get("model") or "unknown")
        if model == "<synthetic>":
            return
        ts = rec.get("timestamp", "")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
        except (AttributeError, ValueError):
            return
        if when.timestamp() < cutoff:
            return
        if msg.get("id"):
            key: tuple = (str(msg["id"]), str(rec.get("requestId") or ""))
        elif rec.get("uuid"):
            key = (str(rec["uuid"]),)
        else:
            key = ("", n)  # nothing to tell two of them apart by: each line is a message
        got[key] = (when, model, u)

    ok = True
    try:
        # bytes, not text: most lines are skipped on a substring test and decoding them first costs a third of the scan
        with open(long_path(path), "rb", buffering=1 << 20) as fh:
            held: tuple[bytes, int] | None = (
                None  # the last line of the current run, and its line number
            )
            run: bytes | None = None
            for n, line in enumerate(fh):
                if b'"usage"' not in line:
                    continue
                rid = _request_id(line)
                if held is not None and (rid is None or rid != run):
                    take(*held)
                    held = None
                if rid is None:
                    take(line, n)
                else:
                    held, run = (line, n), rid
            if held is not None:
                take(*held)
    except OSError:
        ok = False
    return got, ok


def collect(
    days: int = 7,
    projects_dir: Path | None = None,
    subagents: bool = True,
    max_seconds: float = 120.0,
) -> Ledger:
    """Aggregate the last `days` days of transcripts, subagent and workflow ones included unless `subagents` is False.

    A message is counted once however many files repeat it (a forked subagent starts with a copy of its parent's
    history), and the main sessions are read first so a copied message stays with the session it came from. Reading
    stops after `max_seconds` (Ledger.partial); files are read newest first."""
    root = projects_dir or (paths.claude_home() / "projects")
    started = time.monotonic()
    now = time.time()
    cutoff = now - days * 86400
    led = Ledger(days, subagents=subagents, since=cutoff, until=now)
    if not root.is_dir():
        return led
    problems: list[str] = []
    files = list_transcripts(root, cutoff, subagents, problems)
    led.unreadable = len(problems)
    files.sort(key=lambda t: t.kind != "main")  # stable: newest first inside each kind
    seen: set[tuple] = set()
    for t in files:
        if time.monotonic() - started > max_seconds:
            led.partial = True
            break
        messages, ok = _read_messages(t.path, cutoff)
        if ok:
            led.files += 1
            if t.kind == "main":
                led.files_main += 1
            else:
                led.files_sub += 1
        else:
            led.unreadable += 1
        is_main = t.kind == "main"
        for key, (when, model, u) in messages.items():
            if key[0]:  # a keyless message (key[0] == "") has nothing to be a duplicate of
                if key in seen:
                    continue
                seen.add(key)
            cost = estimate_cost(model, u)
            if cost is None:
                led.unpriced += 1
            led.by_day[when.strftime("%Y-%m-%d")].add(u, cost)
            led.by_model[model].add(u, cost)
            led.by_project[t.project].add(u, cost)
            led.total.add(u, cost)
            (led.main if is_main else led.sub).add(u, cost)
    led.seconds = time.monotonic() - started
    return led


def _n(v: int) -> str:
    return (
        f"{v / 1_000_000:.2f}M" if v >= 1_000_000 else (f"{v / 1000:.0f}k" if v >= 1000 else str(v))
    )


def _usd(v: float) -> str:
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"


def _cost(b: Bucket) -> str:
    """The est.cost cell: n/a when nothing in the bucket has a price (never $0.00), a trailing * when some of it has none."""
    if b.messages and b.unpriced == b.messages:
        return "n/a"
    return _usd(b.cost) + ("*" if b.unpriced else "")


def _unpriced_models(led: Ledger, n: int = 3) -> str:
    names = [
        m for m, b in sorted(led.by_model.items(), key=lambda kv: -kv[1].unpriced) if b.unpriced
    ]
    shown = ", ".join(_label(m) for m in names[:n])
    return shown + (f" and {len(names) - n} more" if len(names) > n else "")


def _label(text: str) -> str:
    """A model id comes from a transcript, so it is data: printable text only."""
    return "".join(c if c.isprintable() else "?" for c in text)[:60]


def format_ledger(led: Ledger, by: str = "day", top: int = 12) -> str:
    groups = {"day": led.by_day, "model": led.by_model, "project": led.by_project}[by]
    rows = (
        sorted(
            groups.items(), key=(lambda kv: kv[0]) if by == "day" else (lambda kv: -kv[1].total_in)
        )[-top:]
        if by == "day"
        else sorted(groups.items(), key=lambda kv: -kv[1].total_in)[:top]
    )
    if led.subagents:
        what = f"{led.files_main} main + {led.files_sub} subagent/workflow transcript files"
    else:
        what = f"{led.files_main} main transcript files, subagents and workflows left out"
    out = [f"Token usage, last {led.days} days ({what}, local data only)", ""]

    def head(label: str) -> str:
        return f"{label:<44} {'msgs':>6} {'input':>8} {'cache-wr':>9} {'cache-rd':>9} {'output':>8} {'hit%':>5} {'est.cost':>9}"

    def line(label: str, b: Bucket) -> str:
        return (
            f"{label:<44} {b.messages:>6} {_n(b.input):>8} {_n(b.cache_write):>9} {_n(b.cache_read):>9} "
            f"{_n(b.output):>8} {b.cache_hit * 100:>4.0f}% {_cost(b):>9}"
        )

    out.append(head(by))
    for key, b in rows:
        out.append(line(key if len(key) <= 44 else "…" + key[-43:], b))
    out.append("")
    out.append(line("TOTAL", led.total))
    if led.subagents:
        out.append("")
        out.append(head("where it ran"))
        out.append(line("main sessions", led.main))
        out.append(line("subagents and workflows", led.sub))
        share = led.subagent_share()
        if share is not None and share > 0.5:
            out.append(
                f"note: most of your token use is subagent/workflow work: look at workflow design, not plugins "
                f"({share * 100:.0f}% of the estimated cost)."
            )
    out.append("")
    out.append(
        "input = uncached prompt tokens; cache-rd = tokens served from the prompt cache (cheap); hit% = cache-rd / all prompt tokens."
    )
    out.append(
        "est.cost = an estimate of what these tokens would cost at the Anthropic API list prices, not a bill: "
        "fast mode is counted (2x on Opus); tiered, long-context and batch pricing is ignored, and a subscription plan is not "
        "billed per token (extra usage once a window is full is: see `claude-hr-ctl status`)."
    )
    if led.unpriced:
        out.append(
            f"n/a = no price known for the model; * = the figure leaves out {led.unpriced} message(s) of a model "
            f"without a price ({_unpriced_models(led)})."
        )
    if led.partial:
        out.append(
            f"PARTIAL: reading stopped after {led.seconds:.0f} s; the oldest transcript files were not read."
        )
    if led.unreadable:
        out.append(
            f"{led.unreadable} transcript file(s) could not be read; the figures leave them out."
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# the daily snapshot (state/usage/<month>.json): a record that outlives Claude Code's own transcript clean-up
# ---------------------------------------------------------------------------------------------
SCOPE_ALL = "main+subagents"  # main sessions plus subagent and workflow transcripts
SCOPE_MAIN = "main"  # main sessions only: what a snapshot from before the scope field existed holds


def snapshot_scope(entry: object) -> str:
    """What one stored day counts. A day without the field was written when only main sessions were read."""
    scope = entry.get("scope") if isinstance(entry, dict) else None
    return scope if isinstance(scope, str) and scope else SCOPE_MAIN


@dataclass
class SnapshotResult:
    """What write_snapshot did, day by day (the days are YYYY-MM-DD, UTC)."""

    file: Path | None = None
    scope: str = ""
    # stored, or replaced by a figure that is not smaller
    written: list[str] = field(default_factory=list)
    # left as stored: it counts something else
    kept_other_scope: list[str] = field(default_factory=list)
    # left as stored: it is larger (Claude Code pruned transcripts since)
    kept_larger: list[str] = field(default_factory=list)
    # not stored: the scan saw only part of that day
    not_whole: list[str] = field(default_factory=list)
    skipped: str = ""  # why nothing was stored at all

    def lines(self) -> list[str]:
        if self.skipped:
            return [f"usage snapshot not written: {self.skipped}"]
        out = [
            f"snapshot written: {self.file} ({len(self.written)} day(s), scope {self.scope})"
            if self.written
            else f"snapshot unchanged: {self.file} (no day to store, scope {self.scope})"
        ]
        for days, why in (
            (self.kept_other_scope, "kept as stored (counted with another scope)"),
            (self.kept_larger, "kept as stored (larger than this scan: transcripts were pruned)"),
            (self.not_whole, "not stored (the scan saw only part of the day)"),
        ):
            if days:
                out.append(
                    f"  {len(days)} day(s) {why}: {', '.join(days[:4])}{' ...' if len(days) > 4 else ''}"
                )
        return out


def _whole_day(day: str, since: float, until: float) -> bool:
    """True when the scan window covered all of that UTC day: it began before the day did and ended after it."""
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return False
    return since > 0 and start >= since and start + 86400 <= until


def write_snapshot(led: Ledger, directory: Path | None = None) -> SnapshotResult:
    """Store the per-day totals of `led` in state/usage/<month>.json, each day with its scope.

    Only a complete scan is stored (a cut-short one would write lower numbers over good ones), and only whole days: the
    first day of the window and the day the scan ran on are partial. A day that is already stored is replaced only by
    a figure of the same scope that is not smaller: Claude Code deletes old transcripts, so the stored day may be all
    that is left of it, and a day stored with another scope (all of the days from before the field existed are
    main-only) is never overwritten by a number that counts something else."""
    res = SnapshotResult(scope=SCOPE_ALL if led.subagents else SCOPE_MAIN)
    if led.partial:
        res.skipped = "the scan was cut short (a partial scan would overwrite complete days)"
        return res
    directory = directory or (paths.state_dir() / "usage")
    res.file = directory / f"{time.strftime('%Y-%m')}.json"
    try:
        cur = read_json(res.file, {}) or {}
    except JsonFileError:
        cur = None
    if not isinstance(cur, dict):
        res.skipped = f"{res.file.name} is not valid JSON; not overwriting it"
        return res
    for day, b in sorted(led.by_day.items()):
        if not _whole_day(day, led.since, led.until):
            res.not_whole.append(day)
            continue
        old = cur.get(day)
        if isinstance(old, dict):
            if snapshot_scope(old) != res.scope:
                res.kept_other_scope.append(day)
                continue
            if _int(old.get("messages")) > b.messages:
                res.kept_larger.append(day)
                continue
        cur[day] = {
            "input": b.input,
            "output": b.output,
            "cache_write": b.cache_write,
            "cache_read": b.cache_read,
            "messages": b.messages,
            "scope": res.scope,
        }
        res.written.append(day)
    if res.written:
        directory.mkdir(parents=True, exist_ok=True)
        write_json(res.file, cur)
    return res


# ---------------------------------------------------------------------------------------------
# which skills / subagents / MCP servers are actually used (feeds `plugins audit`)
# ---------------------------------------------------------------------------------------------
# Only a line that names one of the tools counted below is parsed: most tool calls are Bash/Read/Edit.
_COUNTED_TOOL = re.compile(rb'"name"\s*:\s*"(?:Skill|Task|Agent|mcp__)')


class ToolCounts(defaultdict):
    """{"skill:x": n, ...}; `partial` is True when the time budget ran out and the counts are lower bounds, and
    `unreadable` counts the transcript files (or folders) that could not be read and are in no count."""

    partial = False
    unreadable = 0


def collect_tool_use(
    days: int = 30,
    projects_dir: Path | None = None,
    subagents: bool = True,
    max_seconds: float = 120.0,
) -> ToolCounts:
    """Counts like {"skill:superpowers:brainstorming": 4, "agent:explorer": 9, "mcp:plugin_vercel_vercel": 31}.

    Subagent and workflow transcripts count too: a skill or MCP tool a subagent used is a use."""
    root = projects_dir or (paths.claude_home() / "projects")
    started = time.monotonic()
    cutoff = time.time() - days * 86400
    counts = ToolCounts(int)
    seen: set[str] = set()
    if not root.is_dir():
        return counts
    problems: list[str] = []
    files = list_transcripts(root, cutoff, subagents, problems)
    counts.unreadable = len(problems)
    for t in files:
        if time.monotonic() - started > max_seconds:
            counts.partial = True
            break
        try:
            with open(long_path(t.path), "rb", buffering=1 << 20) as fh:
                for line in fh:
                    if b'"tool_use"' not in line or not _COUNTED_TOOL.search(line):
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    msg = rec.get("message") if isinstance(rec, dict) else None
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        bid = str(block.get("id") or "")
                        if bid and bid in seen:
                            continue
                        seen.add(bid)
                        name = str(block.get("name") or "")
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        if name == "Skill" and inp.get("skill"):
                            counts[f"skill:{inp['skill']}"] += 1
                        elif name in {"Task", "Agent"}:
                            counts[f"agent:{inp.get('subagent_type') or 'general-purpose'}"] += 1
                        elif name.startswith("mcp__") and name.count("__") >= 2:
                            counts[f"mcp:{name.split('__')[1]}"] += 1
        except OSError:
            counts.unreadable += 1
    return counts


def format_tool_use(counts: dict[str, int], days: int, top: int = 25) -> str:
    out = [f"Skills, subagents and MCP servers actually used, last {days} days", ""]
    for kind, title in (("skill", "skills"), ("agent", "subagents"), ("mcp", "MCP servers")):
        rows = sorted(
            ((k.split(":", 1)[1], n) for k, n in counts.items() if k.startswith(kind + ":")),
            key=lambda r: -r[1],
        )[:top]
        out.append(f"{title}:")
        out += [f"  {n:5d}  {name}" for name, n in rows] or ["  (none)"]
        out.append("")
    if getattr(counts, "partial", False):
        out.append("PARTIAL: reading stopped early; the counts are lower bounds.")
    if getattr(counts, "unreadable", 0):
        out.append(
            f"{counts.unreadable} transcript file(s) could not be read; the counts leave them out."
        )
    return "\n".join(out).rstrip()
