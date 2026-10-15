"""Plugin and skill audit: always-on token cost, redundancy, scope suggestions, skill hygiene."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procutil
from .claude_setup import ClaudeSetup
from .config import Config


@dataclass
class PluginRow:
    id: str
    scope: str
    enabled: bool
    always_on_tokens: int = 0
    skills: int = 0
    agents: int = 0
    mcp_servers: int = 0
    hooks: int = 0
    installs: int = 1  # project-scope entries for the same plugin are folded into one row
    uses: int | None = None  # skill + subagent + MCP invocations in the usage window (None = not measured)
    skill_names: list[str] = field(default_factory=list)
    agent_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _int(text: str) -> int:
    return int(re.sub(r"[^\d]", "", text) or 0)


def _names(text: str, label: str) -> list[str]:
    m = re.search(rf"{label} \(\d+\)\s+(.+)", text)
    return [n.strip() for n in m.group(1).split(",") if n.strip()] if m else []


def plugin_cost(claude: Path, plugin_id: str) -> PluginRow:
    row = PluginRow(plugin_id, "", True)
    r = procutil.run([str(claude), "plugin", "details", plugin_id], timeout=60)
    text = r.stdout or ""
    m = re.search(r"Always-on:\s*~?([\d,]+)\s*tok", text)
    row.always_on_tokens = _int(m.group(1)) if m else 0
    for label, attr in (("Skills", "skills"), ("Agents", "agents"), ("MCP servers", "mcp_servers"), ("Hooks", "hooks")):
        mm = re.search(rf"{label} \((\d+)\)", text)
        if mm:
            setattr(row, attr, int(mm.group(1)))
    row.skill_names = _names(text, "Skills")
    row.agent_names = _names(text, "Agents")
    return row


def count_uses(row: PluginRow, counts: dict[str, int]) -> int:
    """Attribute usage counts to a plugin by skill/agent name, `plugin:` prefix, or plugin MCP server name."""
    name = row.id.split("@")[0]
    total = 0
    for key, n in counts.items():
        kind, _, ident = key.partition(":")
        if kind == "skill" and (ident.partition(":")[0] == name or ident in row.skill_names):
            total += n
        elif kind == "agent" and (ident.startswith(name + ":") or ident in row.agent_names):
            total += n
        elif kind == "mcp" and (ident.startswith(f"plugin_{name}_") or ident == name):
            total += n
    return total


def audit_plugins(cfg: Config, claude: Path | None, cwd: Path | None = None, usage_days: int = 30) -> list[PluginRow]:
    if not claude:
        return []
    from . import usage as usage_mod

    counts = usage_mod.collect_tool_use(usage_days)
    measured = bool(counts) or any((paths.claude_home() / "projects").glob("*/*.jsonl"))
    setup = ClaudeSetup(cfg, None, claude)  # type: ignore[arg-type]
    with (paths.package_data_dir() / "plugins.toml").open("rb") as fh:
        plan = tomllib.load(fh)
    redundant = {k.split("@")[0]: v for k, v in plan.get("redundant", {}).items()}
    projects = {k.split("@")[0]: v for k, v in plan.get("project_scoped", {}).items()}
    rows: list[PluginRow] = []
    sources: dict[str, set[str]] = {}  # plugin name -> distinct marketplaces it is installed from
    by_key: dict[tuple[str, str], PluginRow] = {}
    for p in setup._installed_plugins():
        if not p.get("enabled", True) and p.get("scope") == "project" and not p.get("projectEnabled"):
            continue
        name = p["id"].split("@")[0]
        sources.setdefault(name, set()).add(p["id"])
        key = (p["id"], str(p.get("scope", "")))
        if key in by_key:  # same plugin enabled in several projects: report one row
            by_key[key].installs += 1
            continue
        row = plugin_cost(claude, p["id"])
        row.scope, row.enabled = str(p.get("scope", "")), bool(p.get("enabled", True))
        by_key[key] = row
        if name in redundant:
            row.notes.append("redundant: " + redundant[name])
        if name in projects and row.scope == "user":
            row.notes.append(f"project-scoped candidate: {projects[name]['why']}")
        if row.mcp_servers:
            row.notes.append(f"{row.mcp_servers} MCP server(s): tool schemas are deferred only while tool search is on")
        if measured:
            row.uses = count_uses(row, counts)
            if row.uses == 0 and row.enabled and row.always_on_tokens >= 100 and not row.id.startswith("hr-core@"):
                row.notes.append(f"not used in the last {usage_days} days: disabling would save ~{row.always_on_tokens} tokens/session")
        rows.append(row)
    for row in rows:
        if len(sources.get(row.id.split("@")[0], ())) > 1:
            row.notes.append("same plugin name installed from several marketplaces (duplicate skills)")
        if row.installs > 1:
            row.notes.append(f"enabled in {row.installs} projects")
    return rows


def format_plugins(rows: list[PluginRow]) -> str:
    out = [f"{'plugin':<52} {'scope':<8} {'always-on':>10} {'skills':>6} {'agents':>6} {'mcp':>4} {'uses':>6}"]
    seen = set()
    total = 0
    for r in sorted(rows, key=lambda r: -r.always_on_tokens):
        key = (r.id, r.scope)
        uses = "-" if r.uses is None else str(r.uses)
        out.append(f"{r.id:<52} {r.scope:<8} {('~' + str(r.always_on_tokens)):>10} {r.skills:>6} {r.agents:>6} {r.mcp_servers:>4} {uses:>6}")
        for n in r.notes:
            out.append(f"    - {n}")
        if r.enabled and key not in seen:
            seen.add(key)
            total += r.always_on_tokens
    out.append(f"\nEstimated always-on cost of enabled plugins: ~{total:,} tokens added to every session")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------------------------
FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _frontmatter(text: str) -> dict[str, str]:
    m = FM.match(text)
    out: dict[str, str] = {}
    if not m:
        return out
    key = None
    for line in m.group(1).splitlines():
        if re.match(r"^[A-Za-z_-]+:", line):
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip().strip(">|-").strip()
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def skill_dirs(cwd: Path) -> list[tuple[str, Path]]:
    dirs = [("user", paths.claude_home() / "skills"), ("project", cwd / ".claude" / "skills")]
    cache = paths.claude_home() / "plugins" / "cache"
    if cache.is_dir():
        for sk in cache.glob("*/*/*/skills"):
            dirs.append((f"plugin:{sk.parents[1].name}", sk))
    return [(k, d) for k, d in dirs if d.is_dir()]


def audit_skills(cfg: Config, cwd: Path) -> tuple[list[str], dict]:
    max_desc = int(cfg.get("skills.max_description_chars", 400))
    msgs: list[str] = []
    total_desc = 0
    count = 0
    latest: dict[str, tuple[str, Path]] = {}
    for origin, d in skill_dirs(cwd):
        for sk in sorted(d.glob("*/SKILL.md")):
            text = sk.read_text(encoding="utf-8", errors="replace")
            fm = _frontmatter(text)
            name = fm.get("name") or sk.parent.name
            desc = (fm.get("description", "") + " " + fm.get("when_to_use", "")).strip()
            if origin.startswith("plugin:"):
                key = f"{origin}/{name}"
                latest[key] = (key, sk)  # keep newest cached version only
                continue
            count += 1
            if not desc:
                msgs.append(f"FAIL {sk}: no description (Claude cannot route to it)")
            if len(desc) > 1536:
                msgs.append(f"FAIL {sk}: description {len(desc)} chars > 1536 (truncated)")
            elif len(desc) > max_desc:
                msgs.append(f"WARN {sk}: description {len(desc)} chars (> {max_desc}); tighten it")
            if fm.get("name") and fm["name"] != sk.parent.name:
                msgs.append(f"WARN {sk}: name '{fm['name']}' != folder '{sk.parent.name}'")
            if len(text.splitlines()) > 500:
                msgs.append(f"WARN {sk}: {len(text.splitlines())} lines (keep SKILL.md under 500)")
            if desc and fm.get("disable-model-invocation", "").lower() != "true":
                total_desc += len(desc)
    plugin_skill_count = len(latest)
    for _key, sk in latest.values():
        fm = _frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
        if fm.get("disable-model-invocation", "").lower() != "true":
            total_desc += len((fm.get("description", "") + " " + fm.get("when_to_use", "")).strip())
    return msgs, {"user_project_skills": count, "plugin_skills": plugin_skill_count, "description_chars": total_desc}
