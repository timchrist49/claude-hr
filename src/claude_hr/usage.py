"""Local token ledger: aggregate usage from Claude Code transcripts (no network, nothing leaves the machine)."""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import paths


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


# Claude Code keeps a session as projects/<project>/<session>.jsonl and the agents it spawned beside it, as
# projects/<project>/<session>/subagents/agent-<id>.jsonl and, for a workflow run, below
# subagents/workflows/<run>/agent-<id>.jsonl (next to a journal.jsonl that holds orchestration, no model messages).
# A project folder that keeps subagents/ directly is read the same way. Never followed: symlinked folders.
_NOT_TRANSCRIPTS = {"journal.jsonl"}


def _subdirs(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def iter_transcripts(root: Path, cutoff: float, subagents: bool = True) -> Iterator[Transcript]:
    """Every transcript file below `root` modified at or after `cutoff` (epoch seconds). Files are only stat-ed here: a
    file older than the window is never opened. Order is unspecified; sort the result."""
    for proj in _subdirs(str(root)):
        try:
            entries = list(os.scandir(proj.path))
        except OSError:
            continue
        stack: list[str] = []
        for e in entries:
            try:
                if e.name.endswith(".jsonl") and e.is_file():
                    st = e.stat()
                    if st.st_mtime >= cutoff:
                        yield Transcript(Path(e.path), proj.name, "main", st.st_size, st.st_mtime)
                elif subagents and e.is_dir(follow_symlinks=False):
                    stack.append(
                        e.path if e.name == "subagents" else os.path.join(e.path, "subagents")
                    )
            except OSError:
                continue
        while stack:  # each pending folder is a subagents/ folder or something below one
            here = stack.pop()
            try:
                with os.scandir(here) as it:
                    below = list(it)
            except OSError:
                continue
            for e in below:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif (
                        e.name.endswith(".jsonl") and e.name not in _NOT_TRANSCRIPTS and e.is_file()
                    ):
                        st = e.stat()
                        if st.st_mtime >= cutoff:
                            yield Transcript(
                                Path(e.path), proj.name, "subagent", st.st_size, st.st_mtime
                            )
                except OSError:
                    continue


def list_transcripts(root: Path, cutoff: float, subagents: bool = True) -> list[Transcript]:
    """`iter_transcripts` as a list, newest first."""
    return sorted(iter_transcripts(root, cutoff, subagents), key=lambda t: -t.mtime)


def has_transcripts(root: Path | None = None) -> bool:
    """True when any transcript exists, however old."""
    root = root or (paths.claude_home() / "projects")
    return next(iter_transcripts(root, 0.0), None) is not None


# ---------------------------------------------------------------------------------------------
# token ledger
# ---------------------------------------------------------------------------------------------
# USD per million tokens: (input, output, cache write for the 5-minute TTL, cache read). List prices of the Anthropic
# API (as of 2026-10), to ESTIMATE what the same traffic would cost there: a subscription is not billed per token.
# The first row whose marker is in the model id wins; a model with no row is left out of every cost and counted.
_PRICES: tuple[tuple[tuple[str, ...], tuple[float, float, float, float]], ...] = (
    (("fable-5-1", "mythos-5-1"), (10.0, 50.0, 12.5, 0.25)),
    (("fable-5", "mythos-5"), (10.0, 50.0, 12.5, 1.0)),
    (("opus-5-5",), (4.0, 20.0, 5.0, 0.20)),
    (("opus-5", "opus-4-8", "opus-4-7", "opus-4-6"), (5.0, 25.0, 6.25, 0.50)),
    (("sonnet-5",), (2.0, 10.0, 2.5, 0.20)),
    (("sonnet-4-6",), (3.0, 15.0, 3.75, 0.30)),
    (("haiku-5",), (0.10, 0.50, 0.125, 0.01)),
    (("haiku-4-5",), (1.0, 5.0, 1.25, 0.10)),
)


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
    return (
        _int(u.get("input_tokens")) * p_in
        + _int(u.get("output_tokens")) * p_out
        + (write - hour) * p_write
        + hour * p_in * 2  # a 1-hour cache write costs twice the input price
        + _int(u.get("cache_read_input_tokens")) * p_read
    ) / 1_000_000


@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    messages: int = 0
    cost: float = 0.0  # estimated, see estimate_cost

    def add(self, u: dict, cost: float = 0.0) -> None:
        self.input += _int(u.get("input_tokens"))
        self.output += _int(u.get("output_tokens"))
        self.cache_write += _int(u.get("cache_creation_input_tokens"))
        self.cache_read += _int(u.get("cache_read_input_tokens"))
        self.messages += 1
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
    seconds: float = 0.0

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


def _read_messages(path: Path, cutoff: float) -> dict[tuple, tuple[datetime, str, dict]]:
    """The model messages of one transcript inside the window: {key: (time, model, usage)}.

    A streamed message is logged once per content block, all with the same message id and request id; the first
    lines carry a partial output count and the LAST line the final one, which is the one kept (input and cache
    counts are the same on every line). The lines of a message sit together, so only the last line of each run
    with one request id is parsed: json is most of the cost of a scan."""
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

    try:
        # bytes, not text: most lines are skipped on a substring test and decoding them first costs a third of the scan
        with open(path, "rb", buffering=1 << 20) as fh:
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
        pass
    return got


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
    cutoff = time.time() - days * 86400
    led = Ledger(days, subagents=subagents)
    if not root.is_dir():
        return led
    files = list_transcripts(root, cutoff, subagents)
    files.sort(key=lambda t: t.kind != "main")  # stable: newest first inside each kind
    seen: set[tuple] = set()
    for t in files:
        if time.monotonic() - started > max_seconds:
            led.partial = True
            break
        led.files += 1
        is_main = t.kind == "main"
        if is_main:
            led.files_main += 1
        else:
            led.files_sub += 1
        for key, (when, model, u) in _read_messages(t.path, cutoff).items():
            if key[0]:  # a keyless message (key[0] == "") has nothing to be a duplicate of
                if key in seen:
                    continue
                seen.add(key)
            cost = estimate_cost(model, u)
            if cost is None:
                led.unpriced += 1
            c = cost or 0.0
            led.by_day[when.strftime("%Y-%m-%d")].add(u, c)
            led.by_model[model].add(u, c)
            led.by_project[t.project].add(u, c)
            led.total.add(u, c)
            (led.main if is_main else led.sub).add(u, c)
    led.seconds = time.monotonic() - started
    return led


def _n(v: int) -> str:
    return (
        f"{v / 1_000_000:.2f}M" if v >= 1_000_000 else (f"{v / 1000:.0f}k" if v >= 1000 else str(v))
    )


def _usd(v: float) -> str:
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}"


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
            f"{_n(b.output):>8} {b.cache_hit * 100:>4.0f}% {_usd(b.cost):>9}"
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
        "est.cost = what these tokens would cost at the Anthropic API list prices (a subscription is not billed per token)"
        + (
            f"; leaves out {led.unpriced} message(s) of a model without a price"
            if led.unpriced
            else ""
        )
        + "."
    )
    if led.partial:
        out.append(
            f"PARTIAL: reading stopped after {led.seconds:.0f} s; the oldest transcript files were not read."
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# which skills / subagents / MCP servers are actually used (feeds `plugins audit`)
# ---------------------------------------------------------------------------------------------
# Only a line that names one of the tools counted below is parsed: most tool calls are Bash/Read/Edit.
_COUNTED_TOOL = re.compile(rb'"name"\s*:\s*"(?:Skill|Task|Agent|mcp__)')


class ToolCounts(defaultdict):
    """{"skill:x": n, ...}; `partial` is True when the time budget ran out and the counts are lower bounds."""

    partial = False


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
    for t in list_transcripts(root, cutoff, subagents):
        if time.monotonic() - started > max_seconds:
            counts.partial = True
            break
        try:
            with open(t.path, "rb", buffering=1 << 20) as fh:
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
            continue
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
    return "\n".join(out).rstrip()
