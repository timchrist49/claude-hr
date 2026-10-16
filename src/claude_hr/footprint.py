"""Plugin footprint: what each installed plugin costs in always-on context, including the hidden costs.

`claude plugin details` counts only skill/agent/command name + description text. It prints hooks as "harness-only" and MCP
servers as "resolved at runtime", so a SessionStart hook that injects a whole skill (Superpowers does) or a hook that spawns
a process on every tool call is invisible there. Everything here is a static estimate (1 token ~ 4 characters) read from
the plugin files; `probe` is the only function that spends tokens and it runs only when someone asks for it.

Nothing here executes plugin code: hook scripts are only read, and only when they sit inside the plugin directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

CHARS_PER_TOKEN = 4
# Hook events whose output Claude Code adds to the conversation before the model answers.
CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse")
PROBE_PROMPT = "Reply with OK"
# description + when_to_use are truncated to this many characters in the skill listing
_LISTING_CAP = 1536
# never slurp more than this from one file (hook scripts are scanned with regexes: keep those inputs small)
_MAX_READ = 4 * 1024 * 1024
_MAX_SCRIPT = 256 * 1024
_MAX_LINE = 2000
_MAX_LOG_READ = 4 * 1024 * 1024
_BOM = chr(0xFEFF)


@dataclass
class PluginFootprint:
    name: str
    version: str = ""
    skills: int = 0  # all skills, manual-only ones included
    agents: int = 0
    commands: int = 0
    manual_only_skills: int = 0  # disable-model-invocation: true, so no description in context
    description_tokens_est: int = 0
    hook_events: list[str] = field(default_factory=list)
    # True: a hook injects context. None: a context-capable hook exists but cannot be inspected.
    injects_context: bool | None = False
    mcp_servers: int = 0
    notes: list[str] = field(default_factory=list)
    # size of the files a SessionStart hook injects (once per session start); not part of description_tokens_est
    injected_tokens_est: int = 0

    @property
    def always_on_tokens_est(self) -> int:
        return self.description_tokens_est + self.injected_tokens_est


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
def estimate_tokens(chars: int) -> int:
    return -(-max(chars, 0) // CHARS_PER_TOKEN)


def always_on_tokens(fps: list[PluginFootprint]) -> int:
    return sum(fp.always_on_tokens_est for fp in fps)


def _read_text(path: Path, limit: int = _MAX_READ) -> str | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(limit)
    except OSError:
        return None
    return data.decode("utf-8-sig", errors="replace")


def _read_json(path: Path) -> Any:
    text = _read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _inside(base: Path, path: Path) -> Path | None:
    """`path` resolved, or None when it escapes the plugin directory (a symlink or `..` must not widen what we read)."""
    try:
        resolved = path.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


_VERSIONISH = re.compile(r"\d+\.\d+[\w.+-]*|[0-9a-f]{12,40}", re.I)


def _name_version_from_path(root: Path) -> tuple[str, str]:
    """Cache layouts are `<marketplace>/<plugin>/<version>`: use the plugin name when the folder is a version."""
    if _VERSIONISH.fullmatch(root.name) and root.parent.name:
        return root.parent.name, root.name
    return root.name, ""


# ---------------------------------------------------------------------------------------------
# frontmatter: just enough YAML for skills, agents and commands (plain, quoted and >/| block scalars)
# ---------------------------------------------------------------------------------------------
_FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FM_KEY = re.compile(r"^([A-Za-z_][\w.-]*)[ \t]*:(?:[ \t]+(.*))?$")
_BLOCK = re.compile(r"[|>][+-]?\d*")


def _scalar(head: str, cont: list[str]) -> str:
    if _BLOCK.fullmatch(head):
        sep = "\n" if head[0] == "|" else " "
        return sep.join(c for c in cont if c or sep == "\n").strip()
    text = " ".join([head] + [c for c in cont if c]).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        if text[0] == "'":
            return text[1:-1].replace("''", "'")
        try:
            # YAML double-quoted escapes match JSON for everything used in practice
            return str(json.loads(text))
        except ValueError:
            return text[1:-1]
    return text


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(fields, body); keys are lower-cased. No frontmatter -> ({}, text)."""
    text = text.removeprefix(_BOM)
    m = _FM.match(text)
    if not m:
        return {}, text
    entries: list[tuple[str, str, list[str]]] = []
    for line in m.group(1).splitlines():
        km = _FM_KEY.match(line)
        if km:
            entries.append((km.group(1).lower(), (km.group(2) or "").strip(), []))
        elif entries and (line[:1] in (" ", "\t") or not line.strip()):
            entries[-1][2].append(line.strip())
    return {k: _scalar(head, cont) for k, head, cont in entries}, text[m.end() :]


def _first_paragraph(body: str) -> str:
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        lines.append(line.lstrip("#").strip())
    return " ".join(lines)


class _Item(NamedTuple):
    chars: int
    manual_only: bool


def _describe(path: Path, default_name: str, *, listing: bool) -> _Item | None:
    """Characters this item puts in the always-on listing. `listing` = skills/commands (capped, body fallback)."""
    text = _read_text(path)
    if text is None:
        return None
    fm, body = _frontmatter(text)
    name = fm.get("name") or default_name
    desc = fm.get("description", "")
    if not desc and listing:
        desc = _first_paragraph(body)  # Claude Code falls back to the first paragraph of the body
    when = fm.get("when_to_use") or fm.get("when-to-use", "")
    text_len = len(desc) + len(when)
    if listing:
        text_len = min(text_len, _LISTING_CAP)
    manual = fm.get("disable-model-invocation", "").strip().lower() == "true"
    return _Item(len(name) + text_len, manual)


def _component_files(
    base: Path, default: str, extra: Any, *, pattern: str, readme_ok: bool = False
) -> list[Path]:
    """Default directory plus any extra paths named in plugin.json, kept inside the plugin and de-duplicated."""
    found: dict[Path, None] = {}
    candidates = [base / default] + [base / e for e in _as_list(extra) if isinstance(e, str)]
    for cand in candidates:
        hit = _inside(base, cand)
        if hit is None:
            continue
        try:
            if hit.is_file():
                files = [hit]
            elif hit.is_dir():
                files = sorted(hit.glob(pattern))
            else:
                files = []
        except OSError:
            continue
        for f in files:
            if f.suffix.lower() == ".md" and (readme_ok or not f.name.lower().startswith("readme")):
                inside = _inside(base, f)
                if inside is not None and _is_file(inside):
                    found[inside] = None
    return list(found)


def _skill_files(base: Path, extra: Any) -> list[Path]:
    found: dict[Path, None] = {}
    roots = [base / "skills"] + [base / e for e in _as_list(extra) if isinstance(e, str)]
    for root in roots:
        hit = _inside(base, root)
        if hit is None:
            continue
        try:
            if (hit / "SKILL.md").is_file():
                files = [hit / "SKILL.md"]
            elif hit.is_dir():
                files = sorted(hit.glob("*/SKILL.md"))
            else:
                files = []
        except OSError:
            continue
        for f in files:
            inside = _inside(base, f)
            if inside is not None and _is_file(inside):
                found[inside] = None
    return list(found)


def _scan_components(base: Path, manifest: dict, fp: PluginFootprint) -> None:
    chars = 0
    for f in _skill_files(base, manifest.get("skills")):
        item = _describe(f, f.parent.name, listing=True)
        if item is None:
            continue
        fp.skills += 1
        if item.manual_only:
            fp.manual_only_skills += 1
        else:
            chars += item.chars
    for f in _component_files(base, "agents", manifest.get("agents"), pattern="*.md"):
        item = _describe(f, f.stem, listing=False)
        if item is not None:
            fp.agents += 1
            chars += item.chars
    manual_commands = 0
    for f in _component_files(base, "commands", manifest.get("commands"), pattern="**/*.md"):
        item = _describe(f, f.stem, listing=True)
        if item is None:
            continue
        fp.commands += 1
        if item.manual_only:
            manual_commands += 1
        else:
            chars += item.chars
    fp.description_tokens_est = estimate_tokens(chars)
    if manual_commands:
        fp.notes.append(f"{manual_commands} command(s) are manual-only (no description in context)")


# ---------------------------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------------------------
def _server_names(data: Any, *, wrapped: bool) -> set[str]:
    if not isinstance(data, dict):
        return set()
    if isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    elif wrapped:
        return set()
    return {str(k) for k, v in data.items() if isinstance(v, dict)}


def _scan_mcp(base: Path, manifest: dict, fp: PluginFootprint) -> None:
    names = _server_names(_read_json(base / ".mcp.json"), wrapped=False)
    inline = manifest.get("mcpServers")
    if isinstance(inline, dict):
        names |= _server_names({"mcpServers": inline}, wrapped=True)
    else:
        for ref in _as_list(inline):
            hit = _inside(base, base / ref) if isinstance(ref, str) else None
            if hit is not None:
                names |= _server_names(_read_json(hit), wrapped=False)
    _set_mcp(fp, len(names))


def _set_mcp(fp: PluginFootprint, count: int) -> None:
    fp.notes[:] = [n for n in fp.notes if "MCP server(s):" not in n]
    fp.mcp_servers = count
    if count:
        fp.notes.append(
            f"{count} MCP server(s): tool names appear at start, schemas are deferred only while tool search is on"
        )


# ---------------------------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------------------------
class _Hook(NamedTuple):
    event: str
    type: str
    command: str


def _events_from(data: Any) -> dict[str, Any]:
    """The `hooks` wrapper of hooks.json (or plugin.json); a flat {Event: [...]} map is tolerated."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("hooks")
    if isinstance(inner, dict):
        return inner
    return {k: v for k, v in data.items() if isinstance(v, list) and k[:1].isupper()}


def _load_hooks(base: Path, manifest: dict) -> list[_Hook]:
    sources: list[Any] = []
    default = _inside(base, base / "hooks" / "hooks.json")
    if default is not None and _is_file(default):
        sources.append(_read_json(default))
    extra = manifest.get("hooks")
    if isinstance(extra, dict):
        sources.append(extra)
    else:
        for ref in _as_list(extra):
            hit = _inside(base, base / ref) if isinstance(ref, str) else None
            if hit is not None and hit != default and _is_file(hit):
                sources.append(_read_json(hit))
    hooks: list[_Hook] = []
    for src in sources:
        for event, groups in _events_from(src).items():
            for group in _as_list(groups):
                if not isinstance(group, dict):
                    continue
                entries = group.get("hooks")
                if not isinstance(entries, list):
                    entries = [group] if ("command" in group or "type" in group) else []
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    cmd = e.get("command") if isinstance(e.get("command"), str) else ""
                    kind = str(e.get("type") or ("command" if cmd else "")).lower()
                    if kind == "command" and not cmd:  # malformed: nothing would run
                        continue
                    if kind:
                        hooks.append(_Hook(str(event), kind, cmd))
    return hooks


# A hook can feed the model by printing hookSpecificOutput/additionalContext JSON or plain stdout; "cat <markdown>" is the
# plain-stdout form Superpowers-style plugins use.
_MARKERS = re.compile(r"additionalContext|hookSpecificOutput")
_CATS_MD = re.compile(
    r"(?<![\w\"'.-])(?:cat|type|Get-Content|gc)[ \t]+[^\n|;&<>]{0,300}?\.md\b", re.I
)
_ROOT_VAR = re.compile(
    r"\$\{CLAUDE_PLUGIN_ROOT\}|\$CLAUDE_PLUGIN_ROOT\b|%CLAUDE_PLUGIN_ROOT%", re.I
)
_VAR_PREFIX = re.compile(r"^(?:\$\{[^}]*\}|\$[A-Za-z_]\w*|%[^%]*%)[/\\]?")
_MD_REF = re.compile(r"[^\s\"'`()<>|;&=,]+\.md\b", re.I)
_READ_VERB = re.compile(
    r"(?<![\w-])(?:cat|type|Get-Content|gc|read_text|readFileSync|readFile|read|open)(?![\w-])",
    re.I,
)
_COMMENT = ("#", "//", "REM ", "rem ", "::", "--", "*")
# claude-hr's own hook binary is not a plugin file, but its contract is known: silent unless it blocks (stderr reason).
_OWN_HOOK = re.compile(r"(?<![\w-])claude-hr-hook(?![\w-])")


def _split_command(cmd: str) -> list[str]:
    """Whitespace split honouring quotes. Deliberately no backslash escapes: Windows paths are full of them."""
    toks: list[str] = []
    cur: list[str] = []
    quote = ""
    started = False
    for ch in cmd:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
        elif ch in "\"'":
            quote, started = ch, True
        elif ch.isspace():
            if started or cur:
                toks.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
    if started or cur:
        toks.append("".join(cur))
    return toks


def _expand_root(text: str, base: Path) -> str:
    return _ROOT_VAR.sub(lambda _m: str(base), text)


def _pick_file(base: Path, token: str, dirs: list[Path]) -> Path | None:
    """The plugin file a command token names: absolute, relative to the plugin, or next to an earlier script
    (Superpowers runs `run-hook.cmd session-start`, where `session-start` is a sibling file)."""
    if not token or token.startswith("-") or any(c in token for c in "*?<>|"):
        return None
    tok = _expand_root(token, base)
    if "$" in tok or "%" in tok:
        return None
    try:
        p = Path(tok)
    except ValueError:
        return None
    cands = [p] if p.is_absolute() else [base / p, *(d / p for d in dirs)]
    for cand in cands:
        hit = _inside(base, cand)
        if hit is not None and _is_file(hit):
            return hit
    return None


@dataclass
class _Inspected:
    verdict: bool | None  # True injects, False inspected and clean, None could not be inspected
    command: str
    files: list[Path]
    texts: list[str]


def _emits_context(text: str) -> bool:
    if _MARKERS.search(text):
        return True
    # only lines that mention markdown can match, and very long (minified) lines are cut: keeps the regex cheap
    return any(
        _CATS_MD.search(line[:_MAX_LINE]) for line in text.splitlines() if ".md" in line.lower()
    )


def _inspect_command(base: Path, command: str) -> _Inspected:
    files: list[Path] = []
    dirs: list[Path] = []
    for tok in _split_command(command)[:16]:
        f = _pick_file(base, tok, dirs)
        if f is not None and f not in files:
            files.append(f)
            if f.parent not in dirs:
                dirs.append(f.parent)
    texts = [t for t in (_read_text(f, _MAX_SCRIPT) for f in files) if t is not None]
    # the command line itself counts too: `cat "${CLAUDE_PLUGIN_ROOT}/NOTES.md"` needs no script file
    if any(_emits_context(t) for t in [command, *texts]):
        verdict: bool | None = True
    elif texts or _OWN_HOOK.search(command):
        verdict = False
    else:
        verdict = None
    return _Inspected(verdict, command, files, texts)


def _resolve_md(base: Path, raw: str, dirs: list[Path]) -> Path | None:
    rel = _VAR_PREFIX.sub("", raw, count=1).replace("\\", "/")
    try:
        p = Path(rel)
    except ValueError:
        return None
    cands = [p] if p.is_absolute() else [base / p, *(d / p for d in dirs)]
    for cand in cands:
        hit = _inside(base, cand)
        if hit is not None and _is_file(hit):
            return hit
    return None


def _injected_markdown(base: Path, ins: _Inspected) -> list[Path]:
    """Markdown files a context-injecting hook reads (the lines that read something, not comments or docstrings)."""
    dirs = list(dict.fromkeys(f.parent for f in ins.files))
    found: dict[Path, None] = {}
    for chunk in [ins.command, *ins.texts]:
        for line in chunk.splitlines():
            stripped = line.strip()
            if ".md" not in stripped.lower() or stripped.startswith(_COMMENT):
                continue
            if not _READ_VERB.search(stripped):
                continue
            for m in _MD_REF.finditer(line[:_MAX_LINE]):
                hit = _resolve_md(base, m.group(0), dirs)
                if hit is not None:
                    found[hit] = None
    return list(found)


def _scan_hooks(base: Path, manifest: dict, fp: PluginFootprint) -> None:
    hooks = _load_hooks(base, manifest)
    if not hooks:
        return
    fp.hook_events = list(dict.fromkeys(h.event for h in hooks))
    cache: dict[str, _Inspected] = {}
    verdicts: list[bool | None] = []
    uninspectable: dict[str, None] = {}
    # context-capable event -> markdown files it injects
    inject_files: dict[str, dict[Path, None]] = {}
    other_context: dict[str, None] = {}
    procs: dict[str, int] = {}
    model_hooks = 0
    for h in hooks:
        in_context_event = h.event in CONTEXT_EVENTS
        if h.type in ("prompt", "agent"):
            model_hooks += 1
            continue
        if h.type not in ("command", ""):  # http and anything newer: we cannot see what it returns
            if in_context_event:
                verdicts.append(None)
                uninspectable[h.event] = None
            continue
        if not h.command:
            continue
        procs[h.event] = procs.get(h.event, 0) + 1
        ins = cache.get(h.command)
        if ins is None:
            ins = cache[h.command] = _inspect_command(base, h.command)
        if in_context_event:
            verdicts.append(ins.verdict)
            if ins.verdict is None:
                uninspectable[h.event] = None
            elif ins.verdict:
                files = inject_files.setdefault(h.event, {})
                for f in _injected_markdown(base, ins):
                    files[f] = None
        elif ins.verdict:
            other_context[h.event] = None
    if any(v is True for v in verdicts):
        fp.injects_context = True
    elif any(v is None for v in verdicts):
        fp.injects_context = None
    else:
        fp.injects_context = False
    sizes: dict[Path, int] = {}
    for event, files in inject_files.items():
        once = event == "SessionStart"
        suffix = "" if once else " each time it fires"
        if not files:
            fp.notes.append(f"{event} hook can inject context{suffix} (size not measurable)")
        for f in files:
            if f not in sizes:
                text = _read_text(f)
                sizes[f] = estimate_tokens(len(text)) if text is not None else 0
            rel = f.relative_to(base).as_posix()
            fp.notes.append(f"{event} hook injects {rel} (about {sizes[f]} tokens){suffix}")
            if once:
                fp.injected_tokens_est += sizes[f]
    if uninspectable:
        fp.notes.append(
            f"{', '.join(uninspectable)} hook cannot be inspected (inline command, missing script or http): may inject context"
        )
    if other_context:
        fp.notes.append(
            f"{', '.join(other_context)} hook output can add context when it fires (size not measurable)"
        )
    if procs:
        counts = ", ".join(f"{e} x{n}" for e, n in procs.items())
        fp.notes.append(f"hook processes: {counts} (each spawns only when its matcher matches)")
    if model_hooks:
        fp.notes.append(f"{model_hooks} prompt/agent hook(s): a model call each time they fire")


# ---------------------------------------------------------------------------------------------
# scanning entry points
# ---------------------------------------------------------------------------------------------
def scan_plugin_dir(path: Path) -> PluginFootprint:
    """Static footprint of one plugin directory. Never raises."""
    root = Path(path)
    name, version = _name_version_from_path(root)
    fp = PluginFootprint(name=name, version=version)
    try:
        if not root.is_dir():
            fp.notes.append("install path not found")
            return fp
        base = root.resolve()
        manifest = _read_json(base / ".claude-plugin" / "plugin.json")
        if not isinstance(manifest, dict):
            manifest = {}
        if isinstance(manifest.get("name"), str) and manifest["name"].strip():
            fp.name = manifest["name"].strip()
        if isinstance(manifest.get("version"), (str, int, float)) and str(manifest["version"]):
            fp.version = str(manifest["version"])
        _scan_components(base, manifest, fp)
        _scan_hooks(base, manifest, fp)
        _scan_mcp(base, manifest, fp)
        if not (fp.skills or fp.agents or fp.commands or fp.hook_events or fp.mcp_servers):
            fp.notes.append("nothing found (empty plugin or an unsupported layout)")
    except (OSError, ValueError, RuntimeError) as exc:
        fp.notes.append(f"scan incomplete: {type(exc).__name__}: {exc}")
    return fp


def scan_installed(plugin_list: list[dict]) -> list[PluginFootprint]:
    """Footprints for the enabled entries of `claude plugin list --json`; a missing install path is a note, not an error."""
    entries = plugin_list.get("plugins") if isinstance(plugin_list, dict) else plugin_list
    if not isinstance(entries, list):
        return []
    # the same plugin enabled at several scopes (or in several projects) loads once: group by id
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        groups.setdefault(_entry_id(entry), []).append(entry)
    out: list[PluginFootprint] = []
    for pid, group in groups.items():
        entry = next((e for e in group if e.get("scope") == "user"), group[0])
        ip = entry.get("installPath")
        install = ip if isinstance(ip, str) else ""
        if install:
            fp = scan_plugin_dir(Path(install))
        else:
            fp = PluginFootprint(name=pid, notes=["install path not found"])
        fp.name = pid
        if entry.get("version"):
            fp.version = str(entry["version"])
        # `plugin list` also reports the servers it resolved, which covers a missing install path
        resolved = entry.get("mcpServers")
        if isinstance(resolved, dict) and len(resolved) > fp.mcp_servers:
            _set_mcp(fp, len(resolved))
        if len(group) > 1:
            fp.notes.append(f"listed {len(group)} times (several scopes or projects); counted once")
        out.append(fp)
    return out


def _entry_id(entry: dict) -> str:
    ip = entry.get("installPath")
    fallback = Path(ip).name if isinstance(ip, str) and ip else ""
    return str(entry.get("id") or entry.get("name") or fallback or "?")


# ---------------------------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------------------------
_HIDDEN_COSTS = (
    "Hidden costs not counted by `claude plugin details`:",
    "  - Hooks: shown as 'harness-only'. SessionStart/UserPromptSubmit/PreToolUse output becomes context, and hooks",
    "    on PostToolUse/Stop spawn a process per matching event (time, not tokens).",
    "  - MCP servers: shown as 'resolved at runtime'. Tool names are listed at start; full schemas load only without tool search.",
    "  - Skills with disable-model-invocation: true cost nothing until you call them (not counted above).",
    "  - Numbers are estimates (1 token ~ 4 characters). `footprint --probe` measures a real one-turn session.",
)


def _hook_events_cell(events: list[str]) -> str:
    if not events:
        return "-"
    shown = ", ".join(events[:3])
    return shown + (f" +{len(events) - 3}" if len(events) > 3 else "")


def _injects_cell(fp: PluginFootprint) -> str:
    if fp.injects_context is None:
        return "unknown"
    if not fp.injects_context:
        return "no"
    return f"yes ~{fp.injected_tokens_est}" if fp.injected_tokens_est else "yes"


def _table(rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r in rows:
        cells = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(r)]
        lines.append("  ".join(cells).rstrip())
    return lines


def render(fps: list[PluginFootprint], budget: int | None = None) -> str:
    """Compact table, a totals line (with OK/OVER when a budget is given) and the hidden-cost reminders."""
    out: list[str] = []
    if fps:
        rows = [["plugin", "skills/agents/cmds", "desc tokens", "hook events", "injects", "mcp"]]
        ordered = sorted(fps, key=lambda f: (-f.always_on_tokens_est, f.name))
        for fp in ordered:
            manual = f"({fp.manual_only_skills}m)" if fp.manual_only_skills else ""
            rows.append(
                [
                    fp.name,
                    f"{fp.skills}{manual}/{fp.agents}/{fp.commands}",
                    f"~{fp.description_tokens_est}",
                    _hook_events_cell(fp.hook_events),
                    _injects_cell(fp),
                    str(fp.mcp_servers),
                ]
            )
        table = _table(rows, right={2, 5})
        out.append(table[0])
        for line, fp in zip(table[1:], ordered, strict=True):
            out.append(line)
            out.extend(f"    - {n}" for n in fp.notes)
        if any(f.manual_only_skills for f in fps):
            out.append("  (skills column: (Nm) = N manual-only skills, which are not in context)")
    else:
        out.append("No enabled plugins found.")
    desc = sum(f.description_tokens_est for f in fps)
    injected = sum(f.injected_tokens_est for f in fps)
    total = desc + injected
    line = f"Estimated always-on: ~{total:,} tokens (descriptions {desc:,} + injected {injected:,}) from {len(fps)} plugin(s)"
    if budget is not None:
        if total <= budget:
            line += f"; budget {budget:,}: OK ({budget - total:,} under)"
        else:
            line += f"; budget {budget:,}: OVER by {total - budget:,}"
    unknown = sum(1 for f in fps if f.injects_context is None)
    out.append("")
    out.append(line)
    if unknown:
        out.append(
            f"  {unknown} plugin(s) have hooks that could not be inspected: their injected size is not included."
        )
    out.append("")
    out.extend(_HIDDEN_COSTS)
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# probe: ONE real one-turn call. Costs tokens, so it only ever runs on explicit request.
# ---------------------------------------------------------------------------------------------
def _default_runner(argv: list[str]):
    import tempfile

    from . import procutil

    # an empty working directory keeps the number comparable across repos (no project CLAUDE.md, .mcp.json or hooks)
    with tempfile.TemporaryDirectory(prefix="claude-hr-probe-", ignore_cleanup_errors=True) as cwd:
        return procutil.run(argv, timeout=120, cwd=cwd)


def _result_object(text: str) -> dict | None:
    """The `result` object from `claude -p --output-format json` (a dict; verbose output is a list of events)."""
    candidates = [text.strip()]
    candidates += [ln for ln in reversed(text.strip().splitlines()) if ln.strip()][:3]
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, list):
            data = next(
                (d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"),
                None,
            )
        if isinstance(data, dict):
            return data
    return None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def probe(claude: str, runner=None) -> dict:
    """One tiny `claude -p` call; returns the token counts and cost. `runner(argv) -> CompletedProcess` is injectable.

    Never raises: any failure comes back as ok=False with an `error` string.
    """
    argv = [
        claude,
        "-p",
        PROBE_PROMPT,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--no-session-persistence",
    ]
    result: dict[str, Any] = {
        "input_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "total_cost_usd": 0.0,
        "ok": False,
        "error": "",
    }
    try:
        cp = (runner or _default_runner)(argv)
    except Exception as exc:  # an injected runner or a missing binary must not take the caller down
        result["error"] = f"could not run claude: {type(exc).__name__}: {exc}"
        return result
    stdout = getattr(cp, "stdout", "") or ""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    stderr = getattr(cp, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    rc = getattr(cp, "returncode", 1)
    payload = _result_object(stdout)
    if payload is None:
        tail = (stderr.strip() or stdout.strip()).splitlines()[-1:] or [""]
        why = f"claude exited with code {rc}" if rc else "could not parse claude's JSON output"
        result["error"] = f"{why}: {tail[0][:200]}".rstrip(": ")
        return result
    usage = payload.get("usage")
    if isinstance(usage, dict):
        result["input_tokens"] = _count(usage.get("input_tokens"))
        result["cache_creation_tokens"] = _count(usage.get("cache_creation_input_tokens"))
        result["cache_read_tokens"] = _count(usage.get("cache_read_input_tokens"))
        result["output_tokens"] = _count(usage.get("output_tokens"))
    cost = payload.get("total_cost_usd", payload.get("cost_usd"))
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        result["total_cost_usd"] = float(cost)
    if rc != 0:
        result["error"] = f"claude exited with code {rc}: {stderr.strip()[:200]}".rstrip(": ")
    elif payload.get("is_error"):
        result["error"] = f"claude reported an error: {str(payload.get('result', ''))[:200]}"
    elif not isinstance(usage, dict):
        result["error"] = "claude's JSON output has no usage block"
    else:
        result["ok"] = True
    return result


def session_input_tokens(result: dict) -> int:
    """Everything the model read in the probe turn: fresh input + cache writes + cache reads."""
    return sum(
        int(result.get(k) or 0)
        for k in ("input_tokens", "cache_creation_tokens", "cache_read_tokens")
    )


def render_probe(result: dict) -> str:
    if not result.get("ok"):
        return f"Probe failed: {result.get('error') or 'unknown error'}"
    return "\n".join(
        [
            f"Measured one-turn session (run in an empty temp directory): {session_input_tokens(result):,} input tokens "
            f"(fresh {result['input_tokens']:,} + cache write {result['cache_creation_tokens']:,} "
            f"+ cache read {result['cache_read_tokens']:,}), {result['output_tokens']:,} output, "
            f"cost ${result['total_cost_usd']:.4f}.",
            "This includes Claude Code's own system prompt and tools; compare it before and after changing plugins.",
        ]
    )


# ---------------------------------------------------------------------------------------------
# helpers for `doctor`
# ---------------------------------------------------------------------------------------------
_SG_SKIPPED = re.compile(r"LLM review disabled|no API credentials", re.I)
_SG_COMPLETED = re.compile(
    r"no security issues found|LLM reviews? took|no (?:medium\+ |high/critical )?(?:vulnerabilities|concerns) found"
    r"|found \d+ high/critical",
    re.I,
)
_SG_COST = re.compile(r"\b(?:est_)?cost_usd\s*=\s*(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


def security_log_path() -> Path:
    """Where the security-guidance plugin writes its log (same precedence as the plugin itself)."""
    import os

    from . import paths

    explicit = os.environ.get("SECURITY_GUIDANCE_DEBUG_LOG")
    if explicit:
        return Path(explicit)
    state = os.environ.get("SECURITY_WARNINGS_STATE_DIR")
    return (Path(state) if state else paths.claude_home() / "security") / "log.txt"


def security_guidance_health(log_path: Path) -> dict | None:
    """Summarise the security-guidance log: skipped reviews, completed reviews and any reported cost.

    A "skipped" line means the LLM review did not run (typically no API credentials), so the plugin silently did less than
    people assume. Counts are log lines: one review can log several completion lines. None if the log is missing or
    unreadable. Never raises.
    """
    import math

    try:
        with Path(log_path).open("rb") as fh:
            fh.seek(0, 2)
            start = max(fh.tell() - _MAX_LOG_READ, 0)
            fh.seek(max(start - 1, 0))
            data = fh.read(_MAX_LOG_READ + 1)
    except (OSError, ValueError):
        return None
    if start > 0:  # only the tail was read: drop the line it starts in the middle of
        data = data[1:] if data[:1] == b"\n" else data.partition(b"\n")[2]
    lines = data.decode("utf-8", errors="replace").splitlines()
    skipped = completed = 0
    cost = 0.0
    for line in lines:
        if _SG_SKIPPED.search(line):
            skipped += 1
        elif _SG_COMPLETED.search(line):
            completed += 1
        m = _SG_COST.search(line)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if math.isfinite(value):
                cost += value
    return {
        "skipped_reviews": skipped,
        "completed_reviews": completed,
        "est_cost_usd": round(cost, 6),
        "lines": len(lines),
    }


def hook_binary_on_path(name: str = "claude-hr-hook") -> str | None:
    """Absolute path of `name` if it is on PATH. The plugin hooks call it by name, so a miss means they silently do nothing."""
    import shutil

    try:
        return shutil.which(name)
    except (OSError, ValueError):
        return None
