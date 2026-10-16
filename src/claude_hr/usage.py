"""Local token ledger: aggregate usage from Claude Code transcripts (no network, nothing leaves the machine)."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import paths


@dataclass
class Bucket:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    messages: int = 0

    def add(self, u: dict) -> None:
        self.input += int(u.get("input_tokens") or 0)
        self.output += int(u.get("output_tokens") or 0)
        self.cache_write += int(u.get("cache_creation_input_tokens") or 0)
        self.cache_read += int(u.get("cache_read_input_tokens") or 0)
        self.messages += 1

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


def collect(days: int = 7, projects_dir: Path | None = None) -> Ledger:
    root = projects_dir or (paths.claude_home() / "projects")
    cutoff = time.time() - days * 86400
    led = Ledger(days)
    if not root.is_dir():
        return led
    seen_ids: set[str] = set()
    for f in root.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        led.files += 1
        project = f.parent.name
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    msg = rec.get("message") if isinstance(rec, dict) else None
                    if not isinstance(msg, dict) or "usage" not in msg:
                        continue
                    ts = rec.get("timestamp", "")
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)
                    except ValueError:
                        continue
                    if when.timestamp() < cutoff:
                        continue
                    key = msg.get("id") or rec.get("uuid")
                    if key:  # a streamed message is logged several times; count each once
                        if (key, project) in seen_ids:
                            continue
                        seen_ids.add((key, project))  # type: ignore[arg-type]
                    model = str(msg.get("model") or "unknown")
                    if model == "<synthetic>":
                        continue
                    u = msg["usage"]
                    led.by_day[when.strftime("%Y-%m-%d")].add(u)
                    led.by_model[model].add(u)
                    led.by_project[project].add(u)
                    led.total.add(u)
        except OSError:
            continue
    return led


def _n(v: int) -> str:
    return (
        f"{v / 1_000_000:.2f}M" if v >= 1_000_000 else (f"{v / 1000:.0f}k" if v >= 1000 else str(v))
    )


def format_ledger(led: Ledger, by: str = "day", top: int = 12) -> str:
    groups = {"day": led.by_day, "model": led.by_model, "project": led.by_project}[by]
    rows = (
        sorted(
            groups.items(), key=(lambda kv: kv[0]) if by == "day" else (lambda kv: -kv[1].total_in)
        )[-top:]
        if by == "day"
        else sorted(groups.items(), key=lambda kv: -kv[1].total_in)[:top]
    )
    out = [f"Token usage, last {led.days} days ({led.files} transcript files, local data only)", ""]
    out.append(
        f"{by:<44} {'msgs':>6} {'input':>8} {'cache-wr':>9} {'cache-rd':>9} {'output':>8} {'hit%':>5}"
    )
    for key, b in rows:
        label = key if len(key) <= 44 else "…" + key[-43:]
        out.append(
            f"{label:<44} {b.messages:>6} {_n(b.input):>8} {_n(b.cache_write):>9} {_n(b.cache_read):>9} {_n(b.output):>8} {b.cache_hit * 100:>4.0f}%"
        )
    t = led.total
    out.append("")
    out.append(
        f"{'TOTAL':<44} {t.messages:>6} {_n(t.input):>8} {_n(t.cache_write):>9} {_n(t.cache_read):>9} {_n(t.output):>8} {t.cache_hit * 100:>4.0f}%"
    )
    out.append("")
    out.append(
        "input = uncached prompt tokens; cache-rd = tokens served from the prompt cache (cheap); hit% = cache-rd / all prompt tokens."
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# which skills / subagents / MCP servers are actually used (feeds `plugins audit`)
# ---------------------------------------------------------------------------------------------
def collect_tool_use(days: int = 30, projects_dir: Path | None = None) -> dict[str, int]:
    """Counts like {"skill:superpowers:brainstorming": 4, "agent:explorer": 9, "mcp:plugin_vercel_vercel": 31}."""
    root = projects_dir or (paths.claude_home() / "projects")
    cutoff = time.time() - days * 86400
    counts: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    if not root.is_dir():
        return counts
    for f in root.glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"tool_use"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    content = (
                        (rec.get("message") or {}).get("content") if isinstance(rec, dict) else None
                    )
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
    return "\n".join(out).rstrip()
