"""CLAUDE.md hygiene: discover everything Claude loads for a directory, then lint it.

Why: Anthropic documents that longer / conflicting instruction files reduce adherence, and the
files are re-sent in every session and in every subagent that doesn't set `omitClaudeMd`.
Rules are heuristics with explicit thresholds (config [claude_md]); `fail` => exit code 1.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .config import Config

EMPHASIS = {
    "CRITICAL",
    "IMPORTANT",
    "MUST",
    "NEVER",
    "ALWAYS",
    "MANDATORY",
    "FORBIDDEN",
    "ABSOLUTELY",
}
VAGUE = [
    "clean code",
    "best practices",
    "be careful",
    "write good",
    "high quality code",
    "properly",
    "as appropriate",
    "where possible",
    "if needed",
    "make sure it works",
]
STALE_MODELS = re.compile(
    r"\b(claude[- ]?3[\w.-]*|sonnet[- ]?[34](\.\d)?\b|opus[- ]?[34](\.\d)?\b|haiku[- ]?[34](\.\d)?\b)",
    re.I,
)
SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), "GitHub fine-grained PAT"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|api[_-]?key)\b\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{12,}"
        ),
        "inline credential",
    ),
]
PM_NAMES = ("npm", "pnpm", "yarn", "bun")
ALWAYS_RUN = re.compile(
    r"(?i)\b(always|must)\s+(run|execute)\s+(the\s+)?(lint|linter|tests?|format|formatter|prettier|eslint)"
)


@dataclass
class Finding:
    rule: str
    severity: str  # info | warn | fail
    file: str
    line: int
    message: str


@dataclass
class Source:
    path: Path
    kind: str  # user | project | local | rule | import | memory
    text: str
    conditional: bool = False  # path-scoped rule: loaded on demand, not at launch
    lines: int = 0

    def __post_init__(self):
        self.lines = len(self.text.splitlines())


@dataclass
class Report:
    sources: list[Source] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def always_loaded_lines(self) -> int:
        return sum(s.lines for s in self.sources if not s.conditional and s.kind != "memory")

    @property
    def est_tokens(self) -> int:
        return sum(len(s.text) for s in self.sources if not s.conditional) // 4

    def has_fail(self) -> bool:
        return any(f.severity == "fail" for f in self.findings)

    def to_json(self) -> str:
        return json.dumps(
            {
                "sources": [
                    {
                        "path": str(s.path),
                        "kind": s.kind,
                        "lines": s.lines,
                        "conditional": s.conditional,
                    }
                    for s in self.sources
                ],
                "always_loaded_lines": self.always_loaded_lines,
                "est_tokens": self.est_tokens,
                "findings": [asdict(f) for f in self.findings],
            },
            indent=2,
        )


# ---------------------------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------------------------
def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


IMPORT_RE = re.compile(r"(?m)^[^`\n]*?(?<![\w`])@((?:~|\.{1,2}|/)?[\w./\\-]+\.[\w]+)")


def _expand_imports(
    src: Source, seen: set[Path], depth: int, out: list[Source], findings: list[Finding]
) -> None:
    if depth > 4:
        findings.append(
            Finding(
                "L8",
                "warn",
                str(src.path),
                1,
                "import chain deeper than 4 hops is ignored by Claude Code",
            )
        )
        return
    for m in IMPORT_RE.finditer(src.text):
        raw = m.group(1)
        target = Path(raw).expanduser() if raw.startswith("~") else (src.path.parent / raw)
        line = src.text[: m.start()].count("\n") + 1
        try:
            target = target.resolve()
        except OSError:
            continue
        if not target.is_file():
            findings.append(
                Finding("L8", "fail", str(src.path), line, f"import @{raw} does not resolve")
            )
            continue
        if target in seen:
            continue
        seen.add(target)
        text = _read(target)
        if text is None:
            continue
        child = Source(target, "import", text)
        out.append(child)
        _expand_imports(child, seen, depth + 1, out, findings)


def project_memory_dir(cwd: Path) -> Path:
    return paths.claude_home() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def discover(cwd: Path) -> Report:
    rep = Report()
    seen: set[Path] = set()

    def add(path: Path, kind: str, conditional: bool = False) -> Source | None:
        try:
            rp = path.resolve()
        except OSError:
            return None
        if rp in seen or not rp.is_file():
            return None
        text = _read(rp)
        if text is None:
            return None
        seen.add(rp)
        s = Source(rp, kind, text, conditional)
        rep.sources.append(s)
        if not conditional:
            _expand_imports(s, seen, 1, rep.sources, rep.findings)
        return s

    add(paths.claude_user_md(), "user")
    rules_dir = paths.claude_home() / "rules"
    for f in sorted(rules_dir.rglob("*.md")) if rules_dir.is_dir() else []:
        add(f, "rule", conditional=_rule_is_conditional(f))
    here = cwd.resolve()
    chain = [here, *here.parents]
    for d in reversed(chain):
        add(d / "CLAUDE.md", "project")
        add(d / ".claude" / "CLAUDE.md", "project")
        add(d / "CLAUDE.local.md", "local")
    proj_rules = here / ".claude" / "rules"
    for f in sorted(proj_rules.rglob("*.md")) if proj_rules.is_dir() else []:
        add(f, "rule", conditional=_rule_is_conditional(f))
    mem = project_memory_dir(here) / "memory" / "MEMORY.md"
    add(mem, "memory")
    return rep


def _rule_is_conditional(path: Path) -> bool:
    text = _read(path) or ""
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    return bool(m and re.search(r"(?m)^paths\s*:", m.group(1)))


# ---------------------------------------------------------------------------------------------
# lint
# ---------------------------------------------------------------------------------------------
def lint(cwd: Path, cfg: Config) -> Report:
    rep = discover(cwd)
    c = cfg.section("claude_md")
    max_global = int(c.get("max_global_lines", 80))
    max_project = int(c.get("max_project_lines", 200))
    max_learned = int(c.get("max_learned_lines", 40))
    warn_tokens = int(c.get("warn_total_tokens", 4000))
    F = rep.findings

    for s in rep.sources:
        name = str(s.path)
        lines = s.text.splitlines()
        if s.kind == "memory":
            size_kb = len(s.text.encode("utf-8")) / 1024
            if s.lines > 200 or size_kb > 25:
                F.append(
                    Finding(
                        "L14",
                        "warn",
                        name,
                        1,
                        f"MEMORY.md is {s.lines} lines / {size_kb:.0f} KB; only the first 200 lines / 25 KB load",
                    )
                )
            continue
        limit = max_global if s.kind == "user" else max_project
        if s.lines > limit:
            F.append(
                Finding(
                    "L1",
                    "fail",
                    name,
                    1,
                    f"{s.lines} lines (limit {limit}); long files reduce adherence and cost tokens every session",
                )
            )
        elif s.lines > int(limit * 0.6):
            F.append(
                Finding(
                    "L1",
                    "warn",
                    name,
                    1,
                    f"{s.lines} lines (limit {limit}); consider trimming or moving file-specific advice to .claude/rules/",
                )
            )

        emph = 0
        fence = False
        fenced_lines = 0
        tree_glyphs = 0
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("```"):
                fence = not fence
                continue
            if fence:
                fenced_lines += 1
            words = set(re.findall(r"\b[A-Z]{4,}\b", line))
            emph += len(words & EMPHASIS)
            low = line.lower()
            for phrase in VAGUE:
                if phrase in low:
                    F.append(
                        Finding(
                            "L5",
                            "warn",
                            name,
                            i,
                            f'vague instruction ("{phrase}"): say the concrete rule or drop it',
                        )
                    )
            if re.search(r"[├└│]──", line):
                tree_glyphs += 1
            if STALE_MODELS.search(line):
                F.append(
                    Finding(
                        "L10",
                        "warn",
                        name,
                        i,
                        "refers to an older model generation; scaffolding for older models can hurt newer ones",
                    )
                )
            if re.search(r"\b(TODO|FIXME)\b", line):
                F.append(
                    Finding(
                        "L10", "warn", name, i, "TODO/FIXME in standing instructions goes stale"
                    )
                )
            if re.search(r"\b20\d\d-\d\d-\d\d\b", line):
                F.append(Finding("L10", "info", name, i, "dated statement; will go stale"))
            if ALWAYS_RUN.search(line):
                F.append(
                    Finding(
                        "L12",
                        "warn",
                        name,
                        i,
                        "deterministic step ('always run lint/tests'): enforce with a hook, not prose",
                    )
                )
            for pat, label in SECRET_PATTERNS:
                if pat.search(line):
                    F.append(
                        Finding(
                            "L13", "fail", name, i, f"possible secret in instructions ({label})"
                        )
                    )
        if emph > 2:
            F.append(
                Finding(
                    "L4",
                    "fail" if emph > 6 else "warn",
                    name,
                    1,
                    f"{emph} emphasis words (CRITICAL/MUST/NEVER...): overuse makes newer models over-trigger",
                )
            )
        if fenced_lines > 30:
            F.append(
                Finding(
                    "L7",
                    "warn",
                    name,
                    1,
                    f"{fenced_lines} lines of code blocks; point to file:line instead",
                )
            )
        if tree_glyphs > 3:
            F.append(
                Finding(
                    "L6",
                    "fail",
                    name,
                    1,
                    "directory tree in instructions: derivable from the repo, goes stale",
                )
            )
        for m in re.finditer(
            r"(?m)^#+\s*(architecture|project structure|directory structure|file structure|overview)\b.*$",
            s.text,
            re.I,
        ):
            start = s.text[: m.start()].count("\n")
            body = 0
            for nxt in lines[start + 1 :]:
                if nxt.startswith("#"):
                    break
                body += 1
            if body > 10:
                F.append(
                    Finding(
                        "L6",
                        "warn",
                        name,
                        start + 1,
                        f'"{m.group(0).strip("# ")}" section is {body} lines; Claude can read the code',
                    )
                )
        # learned-patterns block budget (Headroom `learn`)
        lm = re.search(
            r"<!-- headroom:learn:start -->(.*?)<!-- headroom:learn:end -->", s.text, re.S
        )
        if lm and len(lm.group(1).splitlines()) > max_learned:
            F.append(
                Finding(
                    "L1",
                    "warn",
                    name,
                    1,
                    f"auto-learned block is {len(lm.group(1).splitlines())} lines (budget {max_learned}); prune it",
                )
            )
        if s.kind in {"project", "local"}:
            _check_references(s, F)
        if s.kind == "rule":
            _check_rule_frontmatter(s, F)

    # always-loaded budget
    if rep.always_loaded_lines > 250:
        F.append(
            Finding(
                "L2",
                "fail",
                "(all sources)",
                0,
                f"{rep.always_loaded_lines} always-loaded lines across {len([s for s in rep.sources if not s.conditional])} files (limit 250)",
            )
        )
    if rep.est_tokens > warn_tokens:
        F.append(
            Finding(
                "L2",
                "warn",
                "(all sources)",
                0,
                f"~{rep.est_tokens} tokens of instructions load every session (budget {warn_tokens})",
            )
        )

    _check_contradictions(rep, F)
    for s in rep.sources:
        if s.kind == "local":
            gi = s.path.parent / ".gitignore"
            if gi.is_file() and "CLAUDE.local.md" not in (_read(gi) or ""):
                F.append(
                    Finding("L13", "fail", str(s.path), 1, "CLAUDE.local.md is not in .gitignore")
                )
    order = {"fail": 0, "warn": 1, "info": 2}
    rep.findings.sort(key=lambda f: (order[f.severity], f.file, f.line))
    return rep


def _check_references(s: Source, F: list[Finding]) -> None:
    base = s.path.parent
    pkg = None
    for m in re.finditer(r"`((?:\./)?[\w.-]+(?:/[\w.@-]+)+\.\w{1,6})`", s.text):
        ref = m.group(1)
        if not (base / ref).exists():
            F.append(
                Finding(
                    "L8",
                    "warn",
                    str(s.path),
                    s.text[: m.start()].count("\n") + 1,
                    f"referenced path `{ref}` does not exist",
                )
            )
    for m in re.finditer(r"`npm run ([\w:.-]+)`", s.text):
        if pkg is None:
            try:
                pkg = json.loads((base / "package.json").read_text(encoding="utf-8")).get(
                    "scripts", {}
                )
            except (OSError, ValueError):
                pkg = {}
        if pkg and m.group(1) not in pkg:
            F.append(
                Finding(
                    "L8",
                    "warn",
                    str(s.path),
                    s.text[: m.start()].count("\n") + 1,
                    f"`npm run {m.group(1)}` is not a script in package.json",
                )
            )


def _check_rule_frontmatter(s: Source, F: list[Finding]) -> None:
    m = re.match(r"---\s*\n(.*?)\n---", s.text, re.S)
    if m:
        keys = {
            ln.split(":", 1)[0].strip()
            for ln in m.group(1).splitlines()
            if ":" in ln and not ln.startswith((" ", "-"))
        }
        extra = keys - {"paths"}
        if extra:
            F.append(
                Finding(
                    "L11",
                    "warn",
                    str(s.path),
                    1,
                    f"rule frontmatter supports only `paths`; ignored: {', '.join(sorted(extra))}",
                )
            )
    if s.lines > 80:
        F.append(
            Finding(
                "L11", "warn", str(s.path), 1, f"rule file is {s.lines} lines (keep rules under 80)"
            )
        )


def _check_contradictions(rep: Report, F: list[Finding]) -> None:
    loaded = [s for s in rep.sources if not s.conditional and s.kind != "memory"]
    chosen: dict[str, set[str]] = {}
    for s in loaded:
        for m in re.finditer(
            r"(?i)\b(?:use|prefer|always use|run with)\s+`?(npm|pnpm|yarn|bun)\b", s.text
        ):
            chosen.setdefault(m.group(1).lower(), set()).add(str(s.path))
    if len(chosen) > 1:
        F.append(
            Finding(
                "L9",
                "fail",
                "(all sources)",
                0,
                "conflicting package managers asserted: " + ", ".join(sorted(chosen)),
            )
        )
    for a, b in (("tabs", "spaces"),):
        ta = [s for s in loaded if re.search(rf"(?i)\buse {a}\b", s.text)]
        tb = [s for s in loaded if re.search(rf"(?i)\buse {b}\b", s.text)]
        if ta and tb:
            F.append(
                Finding(
                    "L9",
                    "fail",
                    "(all sources)",
                    0,
                    f"conflicting indentation rules: 'use {a}' vs 'use {b}'",
                )
            )
    # near-duplicate lines across different files (waste + drift risk)
    seen: list[tuple[str, str]] = []
    for s in loaded:
        for line in s.text.splitlines():
            t = line.strip(" -*\t")
            if len(t) < 40 or t.startswith("#"):
                continue
            for other_t, other_f in seen:
                if (
                    other_f != str(s.path)
                    and difflib.SequenceMatcher(None, t, other_t).quick_ratio() > 0.9
                    and difflib.SequenceMatcher(None, t, other_t).ratio() >= 0.85
                ):
                    F.append(
                        Finding(
                            "L9",
                            "warn",
                            str(s.path),
                            0,
                            f"near-duplicate of a line in {other_f}: {t[:60]}",
                        )
                    )
                    break
            seen.append((t, str(s.path)))


def format_report(rep: Report) -> str:
    out = []
    out.append("CLAUDE.md sources loaded for this directory:")
    for s in rep.sources:
        tag = "on-demand" if s.conditional else "always"
        out.append(f"  {s.lines:4d} lines  [{s.kind:<7}] [{tag:<9}] {s.path}")
    out.append(f"  total always-loaded: {rep.always_loaded_lines} lines, ~{rep.est_tokens} tokens")
    if not rep.findings:
        out.append("\nNo findings.")
    for f in rep.findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        out.append(f"  {f.severity.upper():4} {f.rule:<3} {loc}  {f.message}")
    return "\n".join(out)
