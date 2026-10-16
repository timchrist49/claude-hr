"""Read-only, zero-token audit of a repository: is it safe to point an agent at?

Nothing from the audited repo is ever executed, imported or fetched: files are only read (at most 512 KB each, 4 MB
for settings and .mcp.json, never binaries, never through a symlink that leaves the root). Findings never echo repo
text: they carry a fixed label, a sanitised identifier, line numbers and (for secrets) a masked prefix, and even file
names are reduced to a short ASCII-safe form without spaces, so the report itself cannot carry an injection into a
session that reads it. External scanners (gitleaks, pip-audit, npm audit, osv-scanner) run only when they are already
on PATH, with a hard timeout, a scrubbed environment and capped output; nothing is ever installed.

Public entry points never raise: problems become findings or a `ToolRun` with status "error". A hostile file never
discards the rest of the report: every per-file check is isolated, and a file that cannot be parsed or scanned is a
medium finding (so the trust preflight shows it and `--strict` fails).

Limits (stated, not hidden): these are pattern checks, not proof. Surface names (`.claude/settings.json`, `CLAUDE.md`,
...) match case-insensitively on every OS. Agent-surface files are never counted against the file cap; ordinary files
beyond it, and directories beyond the directory/time budgets, are reported as a medium `scan-truncated` finding.
Dependency directories (node_modules, venvs, caches) are only searched, to a bounded depth and size, for agent-facing
names (CLAUDE.md, AGENTS.md, SKILL.md, anything under .claude/); their other files are not scanned.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import math
import os
import posixpath
import re
import stat
import time
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from . import procutil

MAX_FILE_BYTES = 512 * 1024
MAX_CONFIG_BYTES = (
    4 * 1024 * 1024
)  # settings*.json and .mcp.json: Claude Code parses any size, so must the audit
MAX_WALK_ENTRIES = 50_000  # ordinary files; agent-surface files never count against it
MAX_WALK_DIRS = 200_000
MAX_SURFACE_FILES = 5_000
MAX_ALIAS_DIRS = (
    500  # re-expansions of an already visited directory (symlink aliases under .claude/)
)
MAX_PRUNED_DIRS = (
    20_000  # directories searched inside node_modules, venvs, caches (agent-facing names only)
)
MAX_PRUNED_DEPTH = 3
MAX_PRUNED_ENTRIES = 2_000  # entries read per directory inside a pruned tree
MAX_SCAN_SECONDS = 120.0  # wall clock for the walk and the secret scan
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SECRET_FINDINGS = 200
MAX_TOOL_OUTPUT = 2 * 1024 * 1024
MAX_HIDDEN_MATCHES = 20_000  # invisible characters examined per file (the counts saturate)
VCS_DIRS = frozenset({".git", ".hg", ".svn"})  # never entered
PRUNE_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache"}
)  # ordinary files inside are skipped; only agent-facing names are looked for (see _Repo.walk)
BINARY_EXTS = frozenset(
    ".png .jpg .jpeg .gif .ico .bmp .webp .pdf .zip .gz .tgz .bz2 .xz .7z .rar .jar .war .class "
    ".exe .dll .so .dylib .bin .o .a .lib .pyc .whl .woff .woff2 .ttf .otf .eot .mp3 .mp4 .mov "
    ".avi .webm .wasm .sqlite .db .pdb .iso .psd .heic".split()
)
SEVERITIES = ("high", "medium", "low", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# kind -> (group, one-line fix). Groups: agent-config, hidden-unicode, agent-text (the trust preflight),
# workflow, secrets, hygiene, meta.
KINDS: dict[str, tuple[str, str]] = {
    "cmd-pipe-to-shell": (
        "agent-config",
        "Remove it: repo config must not download and run code. Vendor a reviewed script instead.",
    ),
    "cmd-network-fetch": (
        "agent-config",
        "Remove the download or replace it with a reviewed script inside the repo.",
    ),
    "cmd-base64-decode": (
        "agent-config",
        "Replace the encoded payload with a readable script; legitimate tooling does not hide its commands.",
    ),
    "cmd-writes-outside": (
        "agent-config",
        "Keep writes inside the project; never touch home, shell profiles or system paths from repo config.",
    ),
    "cmd-external-exe": (
        "agent-config",
        "Point it at a script inside the repo (via $CLAUDE_PROJECT_DIR) so it can be reviewed.",
    ),
    "hook-http": (
        "agent-config",
        "Delete the http hook: it posts session data (tool inputs, prompts) to an outside server.",
    ),
    "hook-defined": (
        "agent-config",
        "Read every hook command before you trust this folder; hooks run with your privileges.",
    ),
    "env-endpoint-override": (
        "agent-config",
        "Delete it from the repo; set endpoints and proxies only in your user or managed settings.",
    ),
    "env-credential-override": (
        "agent-config",
        "Delete it; credentials never belong in repo settings (use your login or a secret manager).",
    ),
    "env-code-injection": (
        "agent-config",
        "Delete it; these variables load code into, or choose which programs run for, every process Claude starts.",
    ),
    "env-guard-override": (
        "agent-config",
        "Delete it; a repo must not set claude-hr's switches (guards, gates, formatter and config overrides).",
    ),
    "plugin-auto-install": (
        "agent-config",
        "Delete it; install plugins and marketplaces yourself, from sources you chose (they bring hooks and MCP servers).",
    ),
    "settings-literal-secret": (
        "agent-config",
        "Remove the literal, reference an environment variable instead, and rotate the value.",
    ),
    "mcp-auto-approve": (
        "agent-config",
        "Remove it so Claude asks before starting each project MCP server.",
    ),
    "credential-helper": (
        "agent-config",
        "Delete it from project settings; helpers run as commands and receive your credentials.",
    ),
    "statusline-command": (
        "agent-config",
        "Review the command; repo-supplied status line and file-suggestion commands run repeatedly with your privileges.",
    ),
    "permissions-broad-allow": (
        "agent-config",
        "Remove the allow rule; keep approvals narrow: specific commands and in-project paths, never wildcards, shells or home paths.",
    ),
    "permissions-bypass": (
        "agent-config",
        "Remove it; a repo must not choose bypassPermissions for you.",
    ),
    "hooks-disabled": (
        "agent-config",
        "Remove disableAllHooks; it switches off the claude-hr safety hooks.",
    ),
    "config-unparseable": (
        "agent-config",
        "Fix the JSON (or remove the extreme nesting) so Claude Code and this audit read the same thing; review it by hand.",
    ),
    "mcp-unpinned-package": (
        "agent-config",
        "Pin an exact version (npx pkg@1.2.3, uvx pkg==1.2.3) or a git URL ending in @<40-hex sha>.",
    ),
    "mcp-insecure-url": ("agent-config", "Use https:// for remote MCP servers."),
    "mcp-literal-secret": (
        "agent-config",
        "Use ${VAR} expansion or OAuth instead of a literal value, and rotate the exposed secret.",
    ),
    "skill-broad-tools": (
        "agent-config",
        "Narrow allowed-tools to specific commands such as Bash(git diff *), never Bash(*) or shells.",
    ),
    "skill-inline-command": (
        "agent-config",
        "Review the command: it runs when the skill loads. Do not interpolate arguments into it.",
    ),
    "skill-ignore-rules": (
        "agent-config",
        "Remove text that tells the agent to ignore rules, skip approvals or hide actions from you.",
    ),
    "symlink-outside-root": (
        "agent-config",
        "Replace the symlink with a real in-repo file; it points outside the repository (not read).",
    ),
    "file-not-scanned": (
        "agent-config",
        "Review the file by hand: it is binary, over the size limit, unreadable or crashed a check, so it was not scanned.",
    ),
    "hidden-unicode": (
        "hidden-unicode",
        "Strip the invisible characters (find them with grep -nP on U+200B-200F, U+202A-202E, "
        "U+2066-2069, U+FE00-FE0F, U+E0000-E007F, NUL) and review who added them.",
    ),
    "agent-directed-instruction": (
        "agent-text",
        "Read the nearby instruction yourself before an agent follows it; common and often legitimate "
        "(llms.txt style), a risk when it asks for installs, sign-ins or downloads.",
    ),
    "prompt-injection-phrase": (
        "agent-text",
        "Treat the text as data, not instructions; remove or rewrite it unless it is an intentional example.",
    ),
    "agent-directed-address": (
        "agent-text",
        "Informational: the file addresses AI agents directly; check what it asks them to do.",
    ),
    "workflow-unpinned-action": (
        "workflow",
        "Pin third-party actions to a full 40-character commit SHA (keep the version in a comment); "
        "Dependabot can update them.",
    ),
    "workflow-privileged-trigger": (
        "workflow",
        "Never check out or run PR code in pull_request_target/issue_comment/workflow_run workflows or "
        "expose secrets to them; keep permissions minimal.",
    ),
    "workflow-missing-permissions": (
        "workflow",
        "Add a top-level `permissions: contents: read` and grant more per job only where needed.",
    ),
    "workflow-write-all": (
        "workflow",
        "Replace `permissions: write-all` with the specific scopes each job needs.",
    ),
    "workflow-non-write-users": (
        "workflow",
        "Remove allowed_non_write_users; letting non-collaborators trigger an agent lets them drive it.",
    ),
    "workflow-script-injection": (
        "workflow",
        "Pass untrusted context values through `env:` and read them as shell variables instead of "
        "expanding ${{ ... }} inside `run:`.",
    ),
    "workflow-secret-inline": (
        "workflow",
        "Pass the secret through `env:` instead of expanding it inside the run script.",
    ),
    "workflow-pipe-to-shell": (
        "workflow",
        "Download, verify a checksum or signature, then run; do not pipe into a shell.",
    ),
    "secret-token": (
        "secrets",
        "Rotate the credential now, remove it from the repo and its history, and load it from the environment.",
    ),
    "secret-env-value": (
        "secrets",
        "Rotate it, `git rm --cached` the file, purge it from history and add it to .gitignore.",
    ),
    "files-skipped": (
        "secrets",
        "Run gitleaks (or another scanner) for the files that were too large to read here.",
    ),
    "scan-truncated": (
        "secrets",
        "The scan is incomplete (file, directory, size or time budget); scan the rest with gitleaks and review the agent files by hand.",
    ),
    "env-file-tracked": (
        "hygiene",
        "`git rm --cached` the file, add it to .gitignore, rotate real values and commit a .env.example.",
    ),
    "local-file-tracked": (
        "hygiene",
        "`git rm --cached` the file and ignore it; it is personal to one machine.",
    ),
    "gitignore-missing-env": (
        "hygiene",
        "Add `.env` and `.env.*` (with `!.env.example`) to .gitignore.",
    ),
    "gitignore-missing-claude-local": ("hygiene", "Add `CLAUDE.local.md` to .gitignore."),
    "git-unavailable": (
        "hygiene",
        "Install git (or run inside a working git checkout) so tracked-file checks can run.",
    ),
    "root-missing": ("meta", "Pass an existing directory."),
    "audit-error": (
        "meta",
        "The audit could not run (or a whole check crashed): fix the cause and rerun; the exit code is 2.",
    ),
}
PREFLIGHT_GROUPS = frozenset({"agent-config", "hidden-unicode", "agent-text"})
ALL_GROUPS = frozenset(g for g, _ in KINDS.values())


@dataclass(frozen=True)
class Finding:
    severity: str  # high | medium | low | info
    kind: str  # stable slug, a key of KINDS
    path: str  # repo-relative, forward slashes
    detail: (
        str  # fixed labels, sanitised identifiers, line numbers, masked prefixes: never repo text
    )
    line: int = 0  # first relevant line, 0 when the finding is about the whole file


@dataclass
class ToolRun:
    name: str
    status: str  # ok | findings | not installed | skipped | error | timeout
    summary: str = ""
    count: int = 0
    hint: str = ""


def hint(kind: str) -> str:
    return KINDS.get(kind, ("meta", "Review it manually."))[1]


def counts(findings: list[Finding]) -> dict[str, int]:
    out = dict.fromkeys(SEVERITIES, 0)
    for f in findings:
        if f.severity in out:
            out[f.severity] += 1
    return out


def blocking(findings: list[Finding]) -> list[Finding]:
    """What `audit --strict` should fail on (exit 1): every high and medium finding.

    That includes the medium `scan-truncated`, `config-unparseable` and `file-not-scanned` findings, so an
    incomplete or blind scan never passes as clean.
    """
    return [f for f in findings if f.severity in ("high", "medium")]


def could_not_run(findings: list[Finding]) -> bool:
    """True when the audit itself failed (no such directory, unreadable root, a whole check crashed).

    `report()` then exits 2, with or without --strict: "no findings" must never mean "nothing was looked at".
    """
    return any(f.kind in ("root-missing", "audit-error") for f in findings)


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_@./:+\-\[\]()*=#~]")


def _safe(text: object, limit: int = 60) -> str:
    """Printable ASCII, no spaces, short: identifiers from the repo (names, hosts, paths) are untrusted data,
    and without spaces they cannot read as a sentence addressed to whoever reads the report."""
    s = _SAFE_CHARS.sub("?", re.sub(r"\s+", "_", str(text)[: limit * 8].strip()))
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _safe_path(path: str) -> str:
    """A repo-relative path as the report shows it: ASCII-safe, no spaces, each name and the whole path short.

    File names are repo text like any other (a name can be a whole sentence addressed to an agent), so they get
    the same treatment as identifiers: whitespace becomes `_`, anything outside the safe set becomes `?`, and long
    names keep their start and their end (the extension).
    """
    parts = []
    for part in str(path)[:4000].split("/"):
        s = _SAFE_CHARS.sub("?", re.sub(r"\s+", "_", part))
        parts.append(s if len(s) <= 40 else s[:24] + "..." + s[-9:])
    out = "/".join(parts)
    return out if len(out) <= 120 else out[:50] + "..." + out[-67:]


def _mask(secret: str) -> str:
    """First characters only, and never more than a quarter of the value."""
    return secret[: min(4, len(secret) // 4)] + "\u2026"


def _first_lines(lines: list[int], n: int = 8) -> str:
    shown = ", ".join(str(x) for x in lines[:n])
    return shown + (f" (+{len(lines) - n} more)" if len(lines) > n else "")


def _finding(sev: str, kind: str, path: str, detail: str, line: int = 0) -> Finding:
    # repo file names are untrusted text: shortened and sanitised like every other identifier
    return Finding(sev, kind, _safe_path(path), detail, line)


def _root_finding(sev: str, kind: str, root: object, detail: str) -> Finding:
    # the audited directory is what the person typed, not repo text: shown as given (printable ASCII only)
    return Finding(sev, kind, re.sub(r"[^\x20-\x7e]", "?", str(root))[:200], detail, 0)


def _crash_finding(rel: str, exc: BaseException, *, surface: bool) -> Finding:
    """One hostile file (deep JSON, huge expression, ...) must not hide the rest: it becomes its own finding."""
    return _finding(
        "medium" if surface else "low",
        "file-not-scanned",
        rel,
        f"not scanned: a check crashed on this file ({type(exc).__name__}); the other files were still checked",
    )


def _sorted(findings: list[Finding]) -> list[Finding]:
    uniq = dict.fromkeys(findings)
    return sorted(uniq, key=lambda f: (_RANK.get(f.severity, 9), f.path, f.line, f.kind, f.detail))


def _which(name: str, root_real: str | None = None) -> str | None:
    """PATH lookup that never considers the current directory or anything inside the audited repo."""
    for cand in procutil.iter_which(name):
        if root_real and _within(os.path.realpath(cand), root_real):
            continue
        return str(cand)
    return None


def _within(real: str, root_real: str) -> bool:
    try:
        return os.path.commonpath([root_real, real]) == root_real
    except ValueError:
        return False


class _Proc(NamedTuple):
    rc: int  # 124 = timed out, 126 = could not start
    out: str
    err: str
    truncated: bool


def _kill_tree(proc) -> None:
    import subprocess

    try:
        if os.name == "nt":
            taskkill = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
            )
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=0x08000000,
            )
        else:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _run_capped(
    argv: list[str],
    *,
    cwd: Path | str,
    timeout_s: float,
    env: dict[str, str] | None = None,
    max_bytes: int | None = None,
) -> _Proc:
    """Run argv (no shell) with a hard timeout that kills the whole process tree.

    Output goes to temp files, not pipes: a grandchild that outlives a killed parent cannot block us on an open
    pipe, and the amount we read back is capped.
    """
    import subprocess
    import tempfile

    cap = MAX_TOOL_OUTPUT if max_bytes is None else max_bytes
    extra: dict = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    with tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=fout,
                stderr=ferr,
                **extra,
            )
        except (OSError, ValueError) as exc:
            return _Proc(126, "", type(exc).__name__, False)
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            rc = 124
        texts = []
        truncated = False
        for fh in (fout, ferr):
            fh.seek(0)
            data = fh.read(cap + 1)
            truncated = truncated or len(data) > cap
            texts.append(data[:cap].decode("utf-8", errors="replace"))
    return _Proc(rc, texts[0], texts[1], truncated)


# ---------------------------------------------------------------------------------------------
# secret patterns (creds.py is the source of truth; a few extra shapes are added here)
# ---------------------------------------------------------------------------------------------
_EXTRA_PATTERNS = [
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), "GitLab token"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"), "OpenAI-style API key"),
    (re.compile(r"https://hooks\.slack\.com/services/T\w+/B\w+/\w+"), "Slack webhook"),
    (
        re.compile(r"(?i)aws_secret_access_key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}\b"),
        "AWS secret access key",
    ),
]
_QUICK = (
    "ghp_ gho_ ghu_ ghs_ ghr_ github_pat_ sk-ant- sk- AKIA -----BEGIN xox AIza sk_live_ npm_ eyJ "
    "glpat- hooks.slack.com aws_secret"
).split()
_PATTERNS_CACHE: list[tuple[re.Pattern[str], str]] | None = None
_SECRETISH_KEY: re.Pattern[str] | None = None


def _token_patterns() -> list[tuple[re.Pattern[str], str]]:
    global _PATTERNS_CACHE
    if _PATTERNS_CACHE is None:
        from .creds import TOKEN_PATTERNS

        _PATTERNS_CACHE = [*TOKEN_PATTERNS, *_EXTRA_PATTERNS]
    return _PATTERNS_CACHE


def _secretish_key(key: str) -> bool:
    global _SECRETISH_KEY
    if _SECRETISH_KEY is None:
        from .creds import SECRETISH_ENV

        _SECRETISH_KEY = SECRETISH_ENV
    return bool(_SECRETISH_KEY.search(key))


_WINDOW = 2048
_OVERLAP = 256  # longer than the shortest text any token pattern needs to match


def _windows(text: str):
    """(line number, offset, chunk) for every line; a line longer than _WINDOW is cut into overlapping windows.

    Some token patterns (the JWT one in creds.py) backtrack quadratically on a long run of look-alike text.
    Bounding the chunk bounds the cost, and the overlap keeps every match that is shorter than _OVERLAP whole
    in at least one window.
    """
    off = 0
    step = _WINDOW - _OVERLAP
    for n, line in enumerate(text.split("\n"), 1):
        size = len(line)
        if size <= _WINDOW:
            yield n, off, line
        else:
            for s in range(0, size, step):
                yield n, off + s, line[s : s + _WINDOW]
                if s + _WINDOW >= size:
                    break
        off += size + 1


def _token_shaped(value: str) -> bool:
    pats = _token_patterns()
    return any(pat.search(chunk) for _, _, chunk in _windows(value) for pat, _ in pats)


def _redact(text: str) -> str:
    text = text[:4000]
    for pat, _ in _token_patterns():
        text = pat.sub("[redacted]", text)
    return text


_PLACEHOLDER_WORDS = (
    "your",
    "example",
    "changeme",
    "change_me",
    "placeholder",
    "xxxx",
    "redacted",
    "todo",
    "dummy",
    "fake",
    "sample",
    "replace",
    "insert",
)


def _placeholder(value: str) -> bool:
    v = value.strip().strip("\"'")
    if not v or v.startswith("$") or "${" in v or "{{" in v or "%" in v[:1]:
        return True
    if (v.startswith("<") and v.endswith(">")) or len(set(v)) <= 2:
        return True
    low = v.lower()
    return any(w in low for w in _PLACEHOLDER_WORDS)


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _secret_like_value(value: str) -> bool:
    v = value.strip().strip("\"'")
    return len(v) >= 12 and not re.search(r"\s", v) and not _placeholder(v) and _entropy(v) >= 3.0


# ---------------------------------------------------------------------------------------------
# the repo view: one bounded walk, safe reads, optional `git ls-files`
# ---------------------------------------------------------------------------------------------
_PRIORITY_DIRS = frozenset({".claude", ".github"})
_TRUNCATION = {
    "files": "only the first {n} ordinary files were listed (agent-surface files were still scanned)",
    "dirs": "directory limit reached; directories after it were not visited",
    "surface": "too many agent-surface files; the rest were not scanned",
    "time": "time budget used up while listing files",
    "aliases": "symlink alias budget used up; some linked directories were not listed",
}


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.real = os.path.realpath(root)
        self.files: list[str] = []
        self.fileset: set[str] = set()
        # casefolded path -> listed path (Windows and macOS open `X.SH` for `x.sh`)
        self._folded: dict[str, str] = {}
        self.escaped: list[str] = []  # symlinks that leave the root: never read
        self.trunc: set[str] = set()  # keys of _TRUNCATION: why the listing is incomplete
        # text files whose NUL bytes were dropped before scanning: path -> (count, first line)
        self.nul: dict[str, tuple[int, int]] = {}
        self.deadline = time.monotonic() + MAX_SCAN_SECONDS
        self._cache: dict[tuple[str, bool], tuple[str | None, str]] = {}
        self._git: tuple[set[str], set[str]] | None | bool = False  # False = not asked yet

    @property
    def truncated(self) -> bool:
        return bool(self.trunc)

    def expired(self) -> bool:
        return time.monotonic() > self.deadline

    def inside(self, real: str) -> bool:
        return _within(real, self.real)

    def _add(self, rel: str) -> None:
        self.files.append(rel)
        self.fileset.add(rel)
        self._folded.setdefault(rel.casefold(), rel)

    def lookup(self, rel: str) -> str | None:
        """The listed spelling of `rel` (case-insensitive: Windows and macOS open `X.SH` for `x.sh`), or `rel`
        itself when it is a file on disk the listing skipped (past the cap, or inside a dependency directory)."""
        hit = self._folded.get(rel.casefold())
        if hit is not None:
            return hit
        try:
            ok = os.path.isfile(os.path.join(str(self.root), *rel.split("/")))
        except (OSError, ValueError):
            return None
        return rel if ok else None

    def walk(self) -> None:
        """List the files, agent-surface ones first-class.

        Agent-surface files (`_walk_surface`) are always recorded: the cap counts ordinary files only, and after it
        is reached the walk keeps visiting directories to find them. Dependency directories are searched, to a
        bounded depth and size, for agent-facing names only (`_pruned_surface`). Every real directory is fully
        expanded once, so symlink forests cannot make the walk exponential. A directory reached again through a
        symlink alias is expanded once more per alias, surface names only and within a global budget, because the
        audit must see the files at the path Claude Code reads them from (`.claude/skills` -> `shared/skills`);
        such surface-only subtrees never claim a directory, so they cannot hide ordinary files of the real one.
        """
        root_n = os.path.normcase(self.real)
        visited = {root_n}
        alias_budget = MAX_ALIAS_DIRS
        dirs_seen = pruned_seen = surface_n = ordinary_n = 0
        # (path, prefix, ancestors, depth in a pruned tree, True inside a symlink alias)
        stack: list[tuple[str, str, frozenset[str], int, bool]] = [
            (str(self.root), "", frozenset({root_n}), 0, False)
        ]
        while stack:
            if time.monotonic() > self.deadline:
                self.trunc.add("time")
                return
            base, prefix, ancestors, depth, alias = stack.pop()
            dirs_seen += 1
            if dirs_seen > MAX_WALK_DIRS:
                self.trunc.add("dirs")
                return
            limited = bool(depth or alias)
            try:
                with os.scandir(base) as it:
                    chosen = itertools.islice(it, MAX_PRUNED_ENTRIES) if limited else it
                    entries = sorted(chosen, key=lambda e: e.name)
            except OSError:
                continue
            subdirs = []
            for e in entries:
                rel = prefix + e.name
                try:
                    is_dir = e.is_dir()
                    is_file = e.is_file()
                except OSError:
                    continue
                if is_dir:
                    if e.name in VCS_DIRS:
                        continue
                    child_depth, child_alias = depth, alias
                    if not alias and (depth or e.name in PRUNE_DIRS):
                        child_depth = depth if _CLAUDE_DIR_RE.search(rel) else depth + 1
                        if child_depth > MAX_PRUNED_DEPTH or pruned_seen >= MAX_PRUNED_DIRS:
                            continue
                        pruned_seen += 1
                    real = os.path.realpath(e.path)
                    if not self.inside(real):
                        if _is_agent_surface(rel):
                            self.escaped.append(rel)
                        continue
                    n = os.path.normcase(real)
                    if n in ancestors:
                        continue
                    if alias or (n in visited and not child_depth):
                        if alias_budget <= 0:
                            self.trunc.add("aliases")
                            continue
                        alias_budget -= 1
                        child_alias = True
                    elif not child_depth:
                        visited.add(n)
                    entry = (e.path, rel + "/", ancestors | {n}, child_depth, child_alias)
                    # `.claude` and `.github` are walked before their siblings, so that a directory or time
                    # budget spent on thousands of decoy directories can never be what hides them
                    if e.name.casefold() in _PRIORITY_DIRS:
                        subdirs.insert(0, entry)
                    else:
                        subdirs.append(entry)
                elif is_file and e.name != ".git":
                    if depth and not _pruned_surface(rel):
                        continue
                    if alias and not (_walk_surface(rel) or _in_text_scope(rel)):
                        continue
                    if e.is_symlink() and not self.inside(os.path.realpath(e.path)):
                        if _is_agent_surface(rel):
                            self.escaped.append(rel)
                        continue
                    if limited or _walk_surface(rel):
                        surface_n += 1
                        if surface_n > MAX_SURFACE_FILES:
                            self.trunc.add("surface")
                            continue
                    else:
                        ordinary_n += 1
                        if ordinary_n > MAX_WALK_ENTRIES:
                            self.trunc.add("files")
                            continue
                    self._add(rel)
            stack.extend(reversed(subdirs))

    def read(self, rel: str, *, cache: bool = True, text: bool = False) -> tuple[str | None, str]:
        """(text, reason): reason is ok | nul | binary | too-large | unreadable | outside.

        Files an agent reads as text (`_text_like`, or `text=True`) are never "binary": NUL bytes are dropped
        before scanning (so `cu\\0rl` still reads as curl) and reported as reason "nul" (see `self.nul`).
        """
        key = (rel, text)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        result = self._read(rel, text)
        if cache:
            if len(self._cache) >= 400:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def _read(self, rel: str, text_like: bool) -> tuple[str | None, str]:
        path = os.path.join(str(self.root), *rel.split("/"))
        limit = (
            MAX_CONFIG_BYTES if (SETTINGS_RE.search(rel) or MCP_RE.search(rel)) else MAX_FILE_BYTES
        )
        try:
            real = os.path.realpath(path)
            if not self.inside(real):
                return None, "outside"
            if not stat.S_ISREG(os.stat(real).st_mode):
                return None, "unreadable"
            with open(real, "rb") as fh:
                data = fh.read(limit + 1)
        except (OSError, ValueError):
            return None, "unreadable"
        if len(data) > limit:
            return None, "too-large"
        reason = "ok"
        if b"\0" in data:
            if text_like or _text_like(rel):
                first = data.index(b"\0")
                self.nul[rel] = (data.count(b"\0"), data.count(b"\n", 0, first) + 1)
                data = data.replace(b"\0", b"")
                reason = "nul"
            elif b"\0" in data[:8192]:
                return None, "binary"
        return data.decode("utf-8", errors="replace"), reason

    def git_files(self) -> tuple[set[str], set[str]] | None:
        """(tracked, untracked-but-not-ignored) via `git ls-files`, or None when git cannot answer."""
        if self._git is not False:
            return self._git  # type: ignore[return-value]
        self._git = None
        exe = _which("git", self.real)
        if not exe:
            return None
        base = [exe, "-c", "core.fsmonitor=false", "ls-files", "-z"]
        cap = 32 * 1024 * 1024
        a = _run_capped(base, cwd=self.root, timeout_s=30, max_bytes=cap)
        b = _run_capped(
            [*base, "--others", "--exclude-standard"], cwd=self.root, timeout_s=30, max_bytes=cap
        )
        if a.rc == 0 and b.rc == 0 and not a.truncated and not b.truncated:
            self._git = (set(a.out.split("\0")) - {""}, set(b.out.split("\0")) - {""})
        return self._git


# ---------------------------------------------------------------------------------------------
# which files are agent-facing
# ---------------------------------------------------------------------------------------------
# Every surface pattern is case-insensitive on every OS: Windows and macOS open `.CLAUDE/Settings.JSON` for
# `.claude/settings.json`, and on a case-sensitive file system matching more only means scanning more, never missing.
SETTINGS_RE = re.compile(r"(?:^|/)\.claude/settings(?:\.local)?\.json$", re.I)
MCP_RE = re.compile(r"(?:^|/)\.mcp\.json$", re.I)
SKILL_RE = re.compile(r"(?:^|/)\.claude/skills/[^/]+/SKILL\.md$", re.I)
AGENT_FILE_RE = re.compile(
    r"(?:^|/)\.claude/(?:agents|commands|rules|output-styles|workflows|routines)/.+\.md$", re.I
)
CLAUDE_MD_RE = re.compile(
    r"(?:^|/)(?:CLAUDE(?:\.local)?|AGENTS|GEMINI)\.md$|(?:^|/)\.claude/.+\.md$", re.I
)
README_RE = re.compile(r"(?:^|/)README(?:\.[A-Za-z]+)?$", re.I)
LLMS_RE = re.compile(r"(?:^|/)llms[\w.-]*\.txt$", re.I)
COPILOT_RE = re.compile(r"(?:^|/)(?:\.github/copilot-instructions\.md|\.cursorrules)$", re.I)
WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$", re.I)


_SURFACE_DIR_RE = re.compile(r"(?:^|/)\.claude(?:/(?:skills|agents|commands|rules|hooks))?$", re.I)
_CLAUDE_DIR_RE = re.compile(r"(?:^|/)\.claude(?:/|$)", re.I)


def _is_agent_surface(rel: str) -> bool:
    return bool(
        SETTINGS_RE.search(rel)
        or MCP_RE.search(rel)
        or SKILL_RE.search(rel)
        or AGENT_FILE_RE.search(rel)
        or CLAUDE_MD_RE.search(rel)
        or _SURFACE_DIR_RE.search(rel)
    )


def _walk_surface(rel: str) -> bool:
    """Files the walk always records (never counted against the file cap): what an agent loads or what the
    trust preflight reads. README and llms.txt count only at the root; elsewhere they are ordinary files."""
    return bool(
        SETTINGS_RE.search(rel)
        or MCP_RE.search(rel)
        or SKILL_RE.search(rel)
        or AGENT_FILE_RE.search(rel)
        or CLAUDE_MD_RE.search(rel)
        or COPILOT_RE.search(rel)
        or WORKFLOW_RE.search(rel)
        or ("/" not in rel and (README_RE.search(rel) or LLMS_RE.search(rel)))
    )


def _pruned_surface(rel: str) -> bool:
    """The only files looked for inside node_modules, venvs and caches: agent-facing names."""
    return bool(
        SETTINGS_RE.search(rel)
        or SKILL_RE.search(rel)
        or AGENT_FILE_RE.search(rel)
        or CLAUDE_MD_RE.search(rel)
    )


def _text_like(rel: str) -> bool:
    """Files an agent or a workflow runner reads as text: NUL bytes are dropped (and reported), never 'binary'."""
    return bool(
        _walk_surface(rel)
        or README_RE.search(rel)
        or LLMS_RE.search(rel)
        or (rel.lower().startswith("docs/") and rel.lower().endswith(".md"))
    )


_WHY = {
    "binary": "binary or non-UTF-8 content",
    "too-large": "over the size limit",
    "unreadable": "unreadable",
    "outside": "resolves outside the repository",
}


def _load(repo: _Repo, rel: str, out: list[Finding], severity: str = "medium") -> str | None:
    """Read an agent-facing file; a file we could not read is reported, not skipped silently.

    An agent-surface file the audit cannot see into is a blind spot, so it is a medium finding (preflight and
    --strict show it); README-style files that are merely read for text checks stay informational.
    """
    text, reason = repo.read(rel)
    if text is None:
        why = _WHY.get(reason, reason)
        out.append(_finding(severity, "file-not-scanned", rel, f"not scanned: {why}"))
    return text


# ---------------------------------------------------------------------------------------------
# (1) command analysis shared by hooks, status line, helpers, MCP servers, referenced scripts, skills
# ---------------------------------------------------------------------------------------------
# Every pattern below runs on text the repo controls, so each is linear: no unbounded gap that is retried from
# every start position (the old `curl[^|\n]*\|` and `cp(?:\s+\S+)+?` shapes were quadratic or worse).
_FETCH = r"(?:curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm)"
_PIPE_FIXED = re.compile(
    r"\|\s*(?:iex|invoke-expression)\b"
    r"|\b(?:iex|invoke-expression)\b"
    r"|\b(?:(?:ba|z|da|k)?sh|eval|source)\s+(?:-c\s+)?[\"']?(?:\$\(|`)\s*" + _FETCH + r"\b"
    r"|\b(?:(?:ba|z)?sh|source)\s+<\(\s*" + _FETCH + r"\b",
    re.I,
)
_FETCH_VERB = re.compile(r"\b" + _FETCH + r"(?:\.exe)?\b", re.I)
_SHELL_AFTER_PIPE = re.compile(
    r"\|\s*(?:sudo\s+(?:-\w+\s+)*)?"
    r"(?:(?:ba|z|da|k|c)?sh|python[\d.]*|perl|ruby|node|php|iex|invoke-expression|pwsh|powershell)\b",
    re.I,
)


def _pipe_to_shell(text: str) -> bool:
    """A download piped into a shell or interpreter, iex, `bash <(curl ...)`, `sh -c "$(curl ...)"`.

    Linear: a fetch verb matters only if the FIRST pipe after it (on its line) feeds an interpreter, so the
    pipe positions are found once per line and each verb is paired with its pipe by bisection.
    """
    if _PIPE_FIXED.search(text):
        return True
    for line in text.split("\n"):
        if "|" not in line:
            continue
        pipes = [i for i, ch in enumerate(line) if ch == "|"]
        last_k, last_ok = -1, False
        for m in _FETCH_VERB.finditer(line):
            k = bisect_left(pipes, m.end())
            if k == len(pipes):
                break  # no pipe after this verb, so none after the later ones either
            if k != last_k:
                last_k, last_ok = k, bool(_SHELL_AFTER_PIPE.match(line, pipes[k]))
            if last_ok:
                return True
    return False


_NETWORK = re.compile(
    r"\b"
    + _FETCH
    + r"(?:\.exe)?\b|\bnew-object\s+net\.webclient\b|\bdownload(?:string|file|data)\b"
    r"|\bstart-bitstransfer\b|\bbitsadmin\b|\bcertutil\b[^\n]{0,400}-urlcache|\b(?:ncat|netcat|socat)\b"
    r"|/dev/tcp/",
    re.I,
)
_BASE64 = re.compile(
    r"\bbase64\s+(?:-\w+\s+)*(?:-d|-D|--decode)\b|\bfrombase64string\b|\bcertutil\b[^\n]{0,400}-decode"
    r"|\bb64decode\b|\batob\s*\(|\bopenssl\s+(?:enc|base64)\b[^\n]{0,400}\s-d\b"
    r"|\s-(?:enc|encodedcommand|ec)\s+[A-Za-z0-9+/=]{16,}",
    re.I,
)
_OUTSIDE = (
    r"(?:~(?=[/\\\s\"']|$)|\$\{?HOME\}?|\$env:(?:USERPROFILE|HOME|APPDATA|LOCALAPPDATA)"
    r"|%(?:USERPROFILE|HOME|APPDATA|LOCALAPPDATA)%|/(?:etc|usr|var|root|opt|Library|boot|bin|sbin|home)\b"
    r"|\.\./|[A-Za-z]:[\\/](?:Windows|Users|ProgramData|Program Files))"
)
_OUTSIDE_RE = re.compile(_OUTSIDE, re.I)
_QUOTE_OUTSIDE = re.compile(r"[\"']?" + _OUTSIDE, re.I)
_REDIRECT_OUTSIDE = re.compile(
    r">>?\s*[\"']?" + _OUTSIDE + r"|\btee\b(?:\s+-\w+)*\s+[\"']?" + _OUTSIDE, re.I
)
_COPY_VERB = re.compile(r"\b(?:cp|mv|ln|install|rsync|copy|move|xcopy)\b", re.I)
_CMDLET_VERB = re.compile(
    r"\b(?:set-content|add-content|out-file|new-item|copy-item|move-item)\b", re.I
)
_STATEMENT = re.compile(r"[^;&|\n]+")


def _write_outside(cmd: str) -> tuple[int, int] | None:
    """(start, end) of the first write to home, a system path or a parent directory, else None.

    Redirections and `tee` are one regex. For cp/mv/ln-style verbs only the LAST argument is the destination;
    for PowerShell cmdlets any outside path in the statement counts. Both are decided per statement from the
    first verb in it (a later verb sees a subset of the same arguments), so the work stays linear.
    """
    best: tuple[int, int] | None = None
    m = _REDIRECT_OUTSIDE.search(cmd)
    if m:
        best = (m.start(), m.end())
    for seg in _STATEMENT.finditer(cmd):
        s, base = seg.group(), seg.start()
        found: tuple[int, int] | None = None
        for v in itertools.islice(_COPY_VERB.finditer(s), 8):
            if not s[v.end() : v.end() + 1].isspace():
                continue  # `cp.exe`, `my-cp-tool`: not the verb followed by arguments
            toks = s[v.end() :].split()
            if len(toks) >= 2 and _QUOTE_OUTSIDE.match(toks[-1]):
                found = (base + v.start(), base + len(s.rstrip()))
            break  # the first real verb has the longest argument list
        if found is None:
            c = _CMDLET_VERB.search(s)
            o = _OUTSIDE_RE.search(s, c.end()) if c else None
            if c and o:
                found = (base + c.start(), base + o.end())
        if found and (best is None or found[0] < best[0]):
            best = found
    return best


_PERSIST = re.compile(
    r"\.(?:bashrc|zshrc|profile|bash_profile|zprofile|ssh)|authorized_keys|crontab|/etc/"
    r"|launchagents|[\\/]startup\b|\.config/systemd|\.claude",
    re.I,
)
_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_INTERPRETERS = frozenset("sh bash zsh dash node deno bun ruby perl php pwsh powershell py".split())
_SYSTEM_EXE_PREFIXES = (
    "/bin/",
    "/usr/bin/",
    "/usr/local/bin/",
    "/sbin/",
    "/usr/sbin/",
    "/opt/homebrew/bin/",
    "c:\\windows\\",
    "c:/windows/",
    "c:\\program files",
    "c:/program files",
)
_SCRIPT_RUN = re.compile(r"[\w$%{}./\\~:-]+")
_SCRIPT_END = re.compile(
    r"(?=([\w}%]\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|ps1|cmd|bat|rb|pl)\b))", re.I
)
_PROJECT_VARS = (
    "${CLAUDE_PROJECT_DIR}",
    "$CLAUDE_PROJECT_DIR",
    "%CLAUDE_PROJECT_DIR%",
    "$env:CLAUDE_PROJECT_DIR",
)
_QUOTED_PROJECT_VAR = re.compile(
    r"([\"'])(\$\{?CLAUDE_PROJECT_DIR\}?|%CLAUDE_PROJECT_DIR%|\$env:CLAUDE_PROJECT_DIR)\1(?=[\\/])",
    re.I,
)


def _script_refs(cmd: str) -> list[str]:
    """Script-looking tokens (.sh .py .js ...) of a command, as every prefix of a token that ends in one.

    `"$CLAUDE_PROJECT_DIR"/x.sh` (the quoted form Claude Code's docs use) is normalised to
    `$CLAUDE_PROJECT_DIR/x.sh` first. Linear: the command is split into tokens once, each token is tested
    position by position, tokens over 1 KB are not paths, and at most 64 references are returned.
    """
    cmd = _QUOTED_PROJECT_VAR.sub(r"\2", cmd)
    out: dict[str, None] = {}
    for run in _SCRIPT_RUN.finditer(cmd):
        tok = run.group()
        if len(tok) > 1024 or "." not in tok:
            continue
        for m in _SCRIPT_END.finditer(tok):
            out[tok[: m.start() + len(m.group(1))]] = None
            if len(out) >= 64:
                return list(out)
    return list(out)


def _exe_base(token: str) -> str:
    base = posixpath.basename(token.replace("\\", "/")).lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _is_external_path(tok: str) -> bool:
    if any(v in tok for v in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "$PWD", "${PWD}")):
        return False
    return bool(
        tok.startswith(("~", "/", "$HOME", "${HOME}", "%USERPROFILE%", "$env:USERPROFILE", "../"))
        or re.match(r"[A-Za-z]:[\\/]", tok)
        or "/../" in tok
    )


def _external_exe(cmd: str) -> bool:
    toks = [t.strip("\"'") for t in _TOKEN_RE.findall(cmd)]
    if not toks:
        return False
    first = toks[0]
    base = _exe_base(first)
    if _is_external_path(first) and not first.lower().startswith(_SYSTEM_EXE_PREFIXES):
        return True
    if base in _INTERPRETERS or re.fullmatch(r"python[\d.]*", base):
        script = next((t for t in toks[1:] if not t.startswith("-")), "")
        return bool(script) and _is_external_path(script)
    return False


def _label(m: re.Match[str]) -> str:
    tok = re.sub(r"[^a-z.\-]", "", m.group(0).lower())[:20]
    return tok or "network tool"


# --- a small shell tokenizer: simple commands as lists of unquoted words ---------------------------------------
def _shell_commands(cmd: str) -> list[list[str]]:
    """Split a command line into simple commands (lists of words, quotes removed), roughly as a shell would.

    `;`, newline, `|`, `&`/`&&`, backticks and parentheses end a command; quotes group words; a backslash is
    a path separator (Windows commands are common), not an escape. One pass, linear.
    """
    cmds: list[list[str]] = []
    words: list[str] = []
    buf: list[str] = []
    started = False
    quote = ""
    n = len(cmd)
    i = 0
    while i < n:
        c = cmd[i]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < n and cmd[i + 1] in '"\\$`':
                buf.append(cmd[i + 1])
                i += 1
            else:
                buf.append(c)
        elif c in "\"'":
            quote, started = c, True
        elif c in " \t\r\f\v":
            if started:
                words.append("".join(buf))
                buf.clear()
                started = False
        elif c in ";\n`()|" or (
            c == "&" and not ((i > 0 and cmd[i - 1] in "<>") or (i + 1 < n and cmd[i + 1] == ">"))
        ):
            if started:
                words.append("".join(buf))
                buf.clear()
                started = False
            if words:
                cmds.append(words)
                words = []
        else:
            buf.append(c)
            started = True
        i += 1
    if started:
        words.append("".join(buf))
    if words:
        cmds.append(words)
    return cmds


_REDIRECT_WORD = re.compile(r"^(?:\d*|&)([<>]{1,2})(&?\d*-?)(.*)$", re.S)


def _strip_redirects(words: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for w in words:
        if skip:
            skip = False
            continue
        m = _REDIRECT_WORD.match(w)
        if m:
            skip = not m.group(2) and not m.group(3)  # `>` alone: the target is the next word
            continue
        out.append(w)
    return out


_PREFIXES = frozenset(
    "env nohup exec command builtin time nice ionice stdbuf timeout then do else elif if while until ! { }".split()
)
_ELEVATORS = frozenset({"sudo", "doas"})
# options of the wrapper programs that take a value (`sudo -u root cmd`, `nice -n 5 cmd`); `sudo -n` does not
_OPTION_VALUE = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"}),
    "doas": frozenset({"-u", "-C"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "timeout": frozenset({"-s", "-k"}),
    "env": frozenset({"-u", "-C", "-S"}),
}


def _command_head(words: list[str], *, through_sudo: bool = False) -> int:
    """Index of the program a simple command runs, skipping `VAR=x`, `env`/`nohup`/`time` prefixes and options."""
    i = 0
    while i < len(words):
        w = words[i]
        base = _exe_base(w)
        if re.match(r"[A-Za-z_]\w*=", w):
            i += 1
        elif base in _PREFIXES or (through_sudo and base in _ELEVATORS):
            i += 1
            while i < len(words) and (words[i].startswith("-") or words[i].isdigit()):
                value_next = words[i] in _OPTION_VALUE.get(base, ())
                i += 2 if value_next else 1
        else:
            break
    return i


# Network code in an interpreter's inline program or in a script an interpreter runs. Every alternative is a literal
# or a bounded shape, so the scan is linear.
_NET_API = re.compile(
    r"\burllib\b|\burlopen\b|\brequests\.(?:get|post|put|patch|request|Session)\b|\bhttpx\b|\bhttp\.client\b"
    r"|\baiohttp\b|\bsocket\.(?:socket|create_connection)\b|\bftplib\b|\bsmtplib\b|\bparamiko\b|\bpycurl\b"
    r"|require\(\s*['\"](?:node:)?(?:https?|net|tls|dgram|http2)['\"]\s*\)"
    r"|\bfrom\s+['\"](?:node:)?(?:https?|net|tls|dgram)['\"]|\bimport\s*\(\s*['\"](?:node:)?https?"
    r"|\bfetch\s*\(|\bnew\s+WebSocket\b|\bXMLHttpRequest\b|\bnet\.(?:connect|createConnection|Socket)\b"
    r"|\bLWP::|\bIO::Socket\b|\bNet::(?:HTTP|FTP|SMTP|Telnet)\b|\bfsockopen\b|\bcurl_init\b"
    r"|\bfile_get_contents\(\s*['\"]https?|\bTCPSocket\b|\bopen-uri\b|\bUri\.open\b"
    r"|\bInvoke-WebRequest\b|\bSystem\.Net\.|\bWebClient\b",
    re.I,
)
_NET_HINT = re.compile(
    r"nc|ssh|scp|ftp|telnet|rsh|rcp|rsync|openssl|git|py|node|deno|bun|ruby|perl|php|powershell|pwsh",
    re.I,
)
_NET_EXES = frozenset(
    "nc ncat netcat socat telnet ftp tftp sftp scp ssh sshpass ssh-copy-id rsh rcp".split()
)
_GIT_NETWORK = frozenset({"clone", "fetch", "pull", "push", "ls-remote"})
_SHELLS = frozenset("sh bash zsh dash ksh ash fish cmd powershell pwsh".split())
_INLINE_FLAGS = {
    "python": ("-c",),
    "py": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "bun": ("-e", "--eval", "-p"),
    "deno": ("eval",),
    "ruby": ("-e",),
    "perl": ("-e", "-E"),
    "php": ("-r",),
    "powershell": ("-c", "-command"),
    "pwsh": ("-c", "-command"),
}
_REMOTE_SPEC = re.compile(r"^(?:rsync://|(?:[\w.-]+@)?[\w.-]{2,}:)")


def _interp_family(exe: str) -> str:
    return "python" if re.fullmatch(r"python[\d.]*", exe) else exe


def _inline_code(exe: str, args: list[str]) -> str | None:
    flags = _INLINE_FLAGS.get(_interp_family(exe))
    if not flags:
        return None
    for j, a in enumerate(args):
        if a.lower() in flags:
            return " ".join(args[j + 1 :])
    return None


def _shell_c_script(exe: str, args: list[str]) -> str | None:
    for j, a in enumerate(args):
        low = a.lower()
        if exe == "cmd":
            if low in ("/c", "/k"):
                return " ".join(args[j + 1 :])
        elif exe in ("powershell", "pwsh"):
            if low in ("-c", "-command"):
                return " ".join(args[j + 1 :])
        elif re.fullmatch(r"-[a-z]*c[a-z]*", a):
            return args[j + 1] if j + 1 < len(args) else None
    return None


def _git_subcommand(args: list[str]) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a.lower()
    return ""


def _network_tool(cmd: str, depth: int = 0) -> tuple[str, str] | None:
    """(severity, label) when a command talks to the network with a tool `_NETWORK` does not name.

    nc/ssh/scp/telnet/ftp and `openssl s_client` are high (they carry data out), git clone/fetch/pull/push and
    rsync to a host are medium, and an interpreter's inline program that uses network APIs is high. The tools are
    recognised at command position only, so `echo ssh` or `~/.ssh` is not a hit. Labels are fixed text.
    """
    if depth > 3 or not _NET_HINT.search(cmd):
        return None
    for words in _shell_commands(cmd):
        words = _strip_redirects(words)
        i = _command_head(words, through_sudo=True)
        if i >= len(words):
            continue
        exe, args = _exe_base(words[i]), words[i + 1 :]
        if exe in _NET_EXES:
            return "high", exe
        if exe == "rsync" and any(_REMOTE_SPEC.match(a) for a in args):
            return "medium", "rsync"
        if exe == "openssl" and any(a in ("s_client", "s_server", "s_time") for a in args[:4]):
            return "high", "openssl s_client"
        if exe == "git":
            sub = _git_subcommand(args)
            if sub in _GIT_NETWORK:
                return "medium", f"git {sub}"
        if exe in _SHELLS:
            inner = _shell_c_script(exe, args)
            if inner is not None:
                hit = _network_tool(inner, depth + 1)
                if hit:
                    return hit
        code = _inline_code(exe, args)
        if code is not None and _NET_API.search(code):
            return "high", f"{_interp_family(exe)} network code"
    return None


# --- "clearly local and benign": the only hooks that stay purely informational -----------------------------------
_LOCAL_TOOLS = frozenset(
    "echo printf true false exit : test [ [[ cd pwd ls cat head tail grep egrep fgrep rg sort uniq wc date "
    "mkdir touch rm cp mv sed awk tr cut jq basename dirname realpath stat diff cmp tee find make "
    "ruff black isort flake8 pylint mypy pyright pytest prettier eslint biome tsc rustfmt gofmt shfmt "
    "shellcheck markdownlint yamllint taplo stylua clang-format hadolint "
    "claude-hr claude-hr-hook claude-hr-ctl".split()
)
_LOCAL_GIT = frozenset(
    "status diff log show add commit rev-parse branch ls-files stash apply restore checkout switch "
    "describe blame check-ignore shortlog tag merge-base symbolic-ref".split()
)
_LOCAL_RUNNERS = {
    "uv": frozenset({"run"}),
    "npm": frozenset({"run", "run-script", "test", "t"}),
    "pnpm": frozenset({"run", "test", "lint", "format"}),
    "yarn": frozenset({"run", "test", "lint", "format"}),
    "bun": frozenset({"run", "test"}),
    "cargo": frozenset({"fmt", "clippy", "check", "test", "build"}),
    "go": frozenset({"fmt", "vet", "test", "build"}),
    "dotnet": frozenset({"format", "test", "build"}),
}
_LOCAL_PY_MODULES = frozenset(
    "pytest unittest ruff black isort mypy flake8 pylint pyright compileall json.tool claude_hr".split()
)
_SCRIPT_EXT = re.compile(r"\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|ps1|cmd|bat|rb|pl)$", re.I)
_INTERP_LOCAL = frozenset("sh bash zsh dash ksh node deno ruby perl php pwsh powershell py".split())


def _local_script_word(word: str) -> bool:
    """A word naming a script inside the repository (relative, or under a project/plugin variable)."""
    return bool(_SCRIPT_EXT.search(word)) and not _is_external_path(word) and "://" not in word


def _simple_is_local(words: list[str], depth: int) -> bool:
    i = _command_head(words)
    if i >= len(words):
        return True  # only assignments, redirects or keywords
    exe, args = _exe_base(words[i]), words[i + 1 :]
    if exe in _LOCAL_TOOLS:
        return True
    if exe == "git":
        return _git_subcommand(args) in _LOCAL_GIT
    if exe in _LOCAL_RUNNERS:
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in _LOCAL_RUNNERS[exe]
    family = _interp_family(exe)
    if family == "python" or exe in _INTERP_LOCAL:
        inner = _shell_c_script(exe, args) if exe in _SHELLS else None
        if inner is not None:
            return depth < 2 and _clearly_local_text(inner, depth + 1)
        if _inline_code(exe, args) is not None:
            return False  # an inline program is not something to wave through
        j = 0
        while j < len(args):
            a = args[j]
            if family == "python" and a == "-m":
                return j + 1 < len(args) and args[j + 1] in _LOCAL_PY_MODULES
            if a.startswith("-"):
                j += 1
                continue
            return _local_script_word(a)
        return False
    return _local_script_word(words[i])


def _clearly_local_text(cmd: str, depth: int = 0) -> bool:
    return all(_simple_is_local(_strip_redirects(w), depth) for w in _shell_commands(cmd))


def _clearly_local(cmd: str) -> bool:
    """True for a hook command that only does something local and ordinary: echo/ls-style builtins, formatters,
    linters and test runners, read-only git, a package script, or an interpreter running a script that lives in
    the repository. Anything else (an unknown program, an inline program, npx, a path outside the repo) is not
    clearly benign, so the hook is a finding instead of just a note."""
    try:
        return _clearly_local_text(cmd)
    except RecursionError:
        return False


def _analyze_command(cmd: str) -> list[tuple[str, str, str]]:
    """(kind, severity, label) for one command line. Labels are fixed text, never the command itself."""
    out: list[tuple[str, str, str]] = []
    if _pipe_to_shell(cmd):
        out.append(
            (
                "cmd-pipe-to-shell",
                "high",
                "downloads and runs code (pipe to a shell, iex, bash <(curl))",
            )
        )
    else:
        m = _NETWORK.search(cmd)
        if m:
            out.append(("cmd-network-fetch", "high", f"fetches from the network ({_label(m)})"))
        else:
            tool = _network_tool(cmd)
            if tool:
                out.append(("cmd-network-fetch", tool[0], f"talks to the network ({tool[1]})"))
    if _BASE64.search(cmd):
        out.append(
            (
                "cmd-base64-decode",
                "high",
                "decodes base64 data or runs an encoded PowerShell command",
            )
        )
    w = _write_outside(cmd)
    if w:
        sev = "high" if _PERSIST.search(cmd[w[0] : w[1] + 60]) else "medium"
        out.append(
            (
                "cmd-writes-outside",
                sev,
                "writes outside the project (home, system path or parent dir)",
            )
        )
    if _external_exe(cmd):
        out.append(
            ("cmd-external-exe", "medium", "runs an executable or script outside the repository")
        )
    return out


def _command_findings(
    repo: _Repo, cmd: str, rel: str, source: str, *, scripts: bool = True
) -> list[Finding]:
    out = [
        _finding(sev, kind, rel, f"{source}: {label}") for kind, sev, label in _analyze_command(cmd)
    ]
    if scripts:
        found = 0
        for tok in _script_refs(cmd):
            script = _resolve_script(repo, tok)
            if script and found < 3:
                found += 1
                out.extend(_script_findings(repo, script, source))
    return out


def _resolve_script(repo: _Repo, token: str) -> str | None:
    t = token.strip("\"'")
    had_var = False
    low = t.lower()
    for var in _PROJECT_VARS:
        if low.startswith(var.lower()):
            t, had_var = t[len(var) :], True
            break
    t = t.replace("\\", "/")
    if (
        t.startswith(("~", "$", "%"))
        or re.match(r"[A-Za-z]:", t)
        or (t.startswith("/") and not had_var)
    ):
        return None
    norm = posixpath.normpath(t.lstrip("/") if had_var else t)
    if norm.startswith("..") or norm == ".":
        return None
    return repo.lookup(norm)


def _script_findings(repo: _Repo, rel: str, source: str) -> list[Finding]:
    """A hook usually just names a script: read that script (never run it) and apply the same checks.

    The script is read as text whatever its bytes (an interpreter runs it, NUL or not). One that cannot be read is
    a medium finding rather than silence. Besides the command patterns, network APIs of the script's own language
    (urllib, require('https'), fetch(), sockets ...) count as a medium network finding.
    """
    text, reason = repo.read(rel, text=True)
    if text is None:
        why = _WHY.get(reason, reason)
        return [_finding("medium", "file-not-scanned", rel, f"hook script not scanned: {why}")]
    first: dict[str, tuple[int, str, str]] = {}
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        hits = _analyze_command(line)
        if _NET_API.search(line):
            hits.append(
                ("cmd-network-fetch", "medium", "uses network APIs (sockets, http clients)")
            )
        for kind, sev, label in hits:
            if kind == "cmd-external-exe":
                continue  # the first token of a script line is not an interpreter choice
            if kind == "cmd-network-fetch":
                sev = "medium"  # scripts legitimately fetch things; the hook line itself is judged separately
            if kind not in first:
                first[kind] = (n, sev, label)
    return [
        _finding(sev, kind, rel, f"script run by {source}: {label}", n)
        for kind, (n, sev, label) in first.items()
    ]


# ---------------------------------------------------------------------------------------------
# (1) .claude/settings*.json
# ---------------------------------------------------------------------------------------------
_ENDPOINT_ENV = re.compile(
    r"(?i)^(?:ANTHROPIC_(?:[A-Z0-9]+_)*BASE_URL|(?:HTTPS?|ALL)_PROXY"
    r"|OTEL_EXPORTER_OTLP_(?:[A-Z]+_)?ENDPOINT|AWS_ENDPOINT_URL(?:_[A-Z0-9_]+)?)$"
)
_CREDENTIAL_ENV = re.compile(r"(?i)^(?:ANTHROPIC_(?:API_KEY|AUTH_TOKEN)|CLAUDE_CODE_OAUTH_TOKEN)$")
# Project settings `env` is applied to Claude and every process it starts once the folder is trusted. These names
# load code, choose which program runs, or redirect traffic. Names are compared upper-cased (Windows variables are
# case-insensitive). The list is a pattern check, not a complete inventory of dangerous variables.
_INJECTION_ENV = frozenset(
    (
        "LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH DYLD_FRAMEWORK_PATH "
        "NODE_OPTIONS NODE_PATH NODE_EXTRA_CA_CERTS NODE_TLS_REJECT_UNAUTHORIZED "
        "BASH_ENV ENV PROMPT_COMMAND SHELL EDITOR VISUAL PAGER MANPAGER BROWSER LESSOPEN "
        "PYTHONSTARTUP PYTHONPATH PYTHONHOME PYTHONBREAKPOINT PYTHONUSERBASE PYTHONEXECUTABLE "
        "RUBYOPT RUBYLIB PERL5OPT PERL5LIB JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS CLASSPATH "
        "GIT_SSH GIT_SSH_COMMAND GIT_PROXY_COMMAND GIT_EXTERNAL_DIFF GIT_ASKPASS GIT_PAGER GIT_EDITOR "
        "GIT_SEQUENCE_EDITOR GIT_EXEC_PATH GIT_TEMPLATE_DIR SSH_ASKPASS "
        "SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE PIP_CONFIG_FILE"
    ).split()
)
_INJECTION_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG", "NPM_CONFIG_")
_INJECTION_PATTERN = re.compile(r"^(?:PIP|UV|POETRY|YARN)_[A-Z_]*(?:INDEX|REGISTRY|URL)[A-Z_]*$")
_CREDENTIAL_HELPER_KEYS = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "gcpAuthRefresh",
    "otelHeadersHelper",
    "proxyAuthHelper",
    "processWrapper",
    "policyHelpers",
)
_COMMAND_UI_KEYS = ("statusLine", "subagentStatusLine", "fileSuggestion")
# Any wildcard rule over a network verb lets the agent reach arbitrary hosts; over a shell or interpreter it is
# only dangerous when the wildcard is the whole argument list (`python *`, `bash -c *`, not `python -m pytest *`).
_NETWORK_VERBS = frozenset(
    "curl wget ssh scp nc ncat iwr irm invoke-webrequest invoke-restmethod".split()
)
_SHELL_VERBS = frozenset(
    "sh bash zsh dash eval exec sudo python python3 py node perl ruby php powershell pwsh cmd iex "
    "invoke-expression npx uvx pipx bunx xargs".split()
)


def _host(value: str) -> str:
    try:
        return _safe(urlsplit(value).hostname or "", 60) or "(none)"
    except ValueError:
        return "(unparseable)"


def _iter_hooks(data: dict):
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict):
                continue
            inner = g.get("hooks")
            for h in inner if isinstance(inner, list) else [g]:
                if isinstance(h, dict):
                    yield _safe(event, 30), h


def _handler_command(h: dict) -> str:
    cmd = h.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return ""
    args = h.get("args")
    return " ".join([cmd, *(str(a) for a in args)]) if isinstance(args, list) else cmd


_FILE_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})
# A path outside the project in a permission rule: `//abs`, `~/..`, `../..`, a drive letter, or a `..` segment.
# (A single leading `/` is project-root-relative in Claude Code's rule syntax, so it is not outside.)
_OUTSIDE_SPEC = re.compile(
    r"^(?:~|//|\.\.|[A-Za-z]:[\\/]|\$\{?HOME\}?|%USERPROFILE%|\$env:USERPROFILE)|[\\/]\.\.(?:[\\/]|$)",
    re.I,
)


def _rule_severity(rule: str, *, bare_ok: bool, in_settings: bool = False) -> str | None:
    """high for an unrestricted shell grant or a write outside the project, medium for a wildcard over a
    shell/network/interpreter verb, for unrestricted file/WebFetch/MCP grants, and for reads outside the project.

    `in_settings` is true for permissions.allow in settings files, where a bare `Edit`, `Read`, `WebFetch` or
    `mcp__*` is a standing approval chosen by whoever wrote the file; in a skill's allowed-tools those bare names
    are ordinary, so only paths outside the project count there.
    """
    m = re.fullmatch(r"([A-Za-z_][\w.\-*]*)(?:\((.*)\))?|\*", rule.strip(), re.S)
    if not m:
        return None
    tool, spec = m.group(1), m.group(2)
    if tool is None:
        return "high"
    low = tool.lower()
    if low in _FILE_WRITE_TOOLS or low == "read":
        s = (spec or "").strip()
        if _OUTSIDE_SPEC.search(s):
            return "high" if low in _FILE_WRITE_TOOLS else "medium"
        if in_settings and (spec is None or s in ("", "*", "**", "/**", "**/*")):
            return "medium"
        return None
    if in_settings and low == "webfetch":
        s = (spec or "").strip().lower()
        return "medium" if spec is None or s in ("", "*", "domain:*") else None
    if in_settings and low.startswith("mcp__*"):
        return "medium"  # every tool of every MCP server (`mcp__<server>__*` is an ordinary per-server grant)
    if low not in ("bash", "powershell"):
        return None
    if spec is None:
        return "high" if bare_ok else None
    s = spec.strip()
    if s in ("", "*", "**", ":*"):
        return "high"
    parts = re.split(r"[\s:]+", s, maxsplit=1)
    verb = _exe_base(parts[0].lstrip("./\\"))
    rest = parts[1] if len(parts) > 1 else ""
    if verb in _NETWORK_VERBS and "*" in s:
        return "medium"
    if verb in _SHELL_VERBS and re.fullmatch(r"(?:-(?:c|e|command)\s+)?\*+", rest.strip(), re.I):
        return "medium"
    return None


def _parse_json(text: str) -> tuple[object, str]:
    """(data, "") or (None, reason). Python's parser, made to accept what Node's JSON.parse accepts as far as it
    can: integers of any length (Python 3.11+ refuses over 4300 digits by default) become floats, and the
    failures a hostile file can cause (nesting deeper than the recursion limit, memory) are reported, not raised."""
    try:
        return json.loads(text.lstrip("\ufeff"), parse_int=float), ""
    except (ValueError, RecursionError, MemoryError) as exc:
        return None, type(exc).__name__


def _command_values(val: object, depth: int = 0) -> list[str]:
    """The command lines in a settings value: a string, a {command, args} object, an argv list or a list of those."""
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, dict):
        cmd = _handler_command(val)
        return [cmd] if cmd else []
    if isinstance(val, list) and depth < 3:
        if val and all(isinstance(v, str) for v in val):
            joined = " ".join(val)
            return [joined] if joined.strip() else []
        return [c for v in val[:20] for c in _command_values(v, depth + 1)]
    return []


_LOCAL_DETAIL = {
    "tracked": "personal local settings file is committed: Claude Code loads it with the same authority as settings.json",
    "untracked": "personal local settings file is not ignored by git, so it would be committed with the repository",
    "unknown": "personal local settings file present and git cannot say whether it is committed: it loads like settings.json",
}


def _local_state(repo: _Repo, rel: str) -> str:
    """ "" for a shared settings file. For `settings.local.json`: "ignored" (git says untracked and ignored, so it
    is the user's own), "tracked" and "untracked" (it is part of the repository) or "unknown" (git cannot tell:
    treated like the repository's, never as the user's own)."""
    if not rel.lower().endswith("settings.local.json"):
        return ""
    git = repo.git_files()
    if git is None:
        return "unknown"
    if rel in git[0]:
        return "tracked"
    return "untracked" if rel in git[1] else "ignored"


def _env_text(val: object) -> str | None:
    if isinstance(val, str):
        return val
    return str(val) if isinstance(val, (int, float, bool)) else None


def _path_entries(value: str) -> list[str]:
    if ";" in value:
        return value.split(";")
    if re.match(r"[A-Za-z]:[\\/]", value):
        return [value]
    return value.split(":")


def _relative_path_entry(entry: str) -> bool:
    e = entry.strip().strip("\"'")
    if any(v in e for v in ("CLAUDE_PROJECT_DIR", "$PWD", "${PWD}", "%CD%")):
        return True
    return not e or not (e.startswith(("/", "~", "$", "%")) or re.match(r"[A-Za-z]:[\\/]", e))


def _check_settings(repo: _Repo, rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    # A settings.local.json that is part of the clone (or that git cannot place) is the repository's, not the
    # user's own choice: it is flagged and its findings keep full severity. Only git saying "untracked and
    # ignored" (a file Claude Code wrote on this machine) lowers permission findings to low.
    state = _local_state(repo, rel)
    if state in _LOCAL_DETAIL:
        out.append(_finding("medium", "local-file-tracked", rel, _LOCAL_DETAIL[state]))
    data, why = _parse_json(text)
    if not isinstance(data, dict):
        reason = why or "not a JSON object"
        out.append(
            _finding(
                "medium",
                "config-unparseable",
                rel,
                f"not read as a JSON object ({reason}): not audited; review it by hand",
            )
        )
        return out

    events: list[str] = []
    unvetted: list[str] = []
    for event, h in _iter_hooks(data):
        events.append(event)
        if h.get("type") == "http" or ("url" in h and "command" not in h):
            url = h.get("url")
            out.append(
                _finding(
                    "high",
                    "hook-http",
                    rel,
                    f"{event} hook posts event data to {_host(url) if isinstance(url, str) else '(unknown)'}",
                )
            )
        cmd = _handler_command(h)
        if cmd:
            found = _command_findings(repo, cmd, rel, f"{event} hook")
            out.extend(found)
            if not found and not _clearly_local(cmd):
                unvetted.append(event)
    if events:
        names = ", ".join(sorted(set(events))[:6])
        out.append(
            _finding(
                "info",
                "hook-defined",
                rel,
                f"defines {len(events)} hook(s) ({names}); they run with your privileges once trusted",
            )
        )
    if unvetted:
        names = ", ".join(sorted(set(unvetted))[:6])
        out.append(
            _finding(
                "medium",
                "hook-defined",
                rel,
                f"{len(unvetted)} hook command(s) are not clearly local and benign ({names}): "
                "read them before you trust this folder",
            )
        )

    for key in _COMMAND_UI_KEYS:
        for cmd in _command_values(data.get(key)):
            what = "on every refresh" if key == "statusLine" else "repeatedly while you work"
            out.append(
                _finding("medium", "statusline-command", rel, f"{key} runs a command {what}")
            )
            out.extend(_command_findings(repo, cmd, rel, key))
    for key in _CREDENTIAL_HELPER_KEYS:
        for cmd in _command_values(data.get(key)):
            out.append(
                _finding(
                    "high",
                    "credential-helper",
                    rel,
                    f"{key} runs a command with your privileges (credentials, proxy auth, process wrapping)",
                )
            )
            out.extend(_command_findings(repo, cmd, rel, key))

    env = data.get("env")
    for key, raw in env.items() if isinstance(env, dict) else ():
        val = _env_text(raw)
        if not isinstance(key, str) or val is None:
            continue
        k = _safe(key, 40)
        up = key.upper()
        if _ENDPOINT_ENV.match(key):
            out.append(
                _finding(
                    "high",
                    "env-endpoint-override",
                    rel,
                    f"env sets {k} to host {_host(val)}: traffic (and credentials) would go there",
                )
            )
        elif _CREDENTIAL_ENV.match(key):
            out.append(
                _finding(
                    "high",
                    "env-credential-override",
                    rel,
                    f"env sets {k}: it overrides your login and bills or authenticates another account",
                )
            )
        elif up.startswith("CLAUDE_HR_"):
            out.append(
                _finding(
                    "high",
                    "env-guard-override",
                    rel,
                    f"env sets {k}: a cloned repo would switch claude-hr guards, gates or configuration",
                )
            )
        elif up == "PATH":
            relative = any(_relative_path_entry(p) for p in _path_entries(val))
            out.append(
                _finding(
                    "high" if relative else "medium",
                    "env-code-injection",
                    rel,
                    "env sets PATH with a repo-relative or empty entry: the next program Claude starts "
                    "could be a repo-supplied binary"
                    if relative
                    else "env sets PATH: it chooses which programs every process Claude starts runs",
                )
            )
        elif (
            up in _INJECTION_ENV
            or up.startswith(_INJECTION_PREFIXES)
            or _INJECTION_PATTERN.match(up)
        ):
            out.append(
                _finding(
                    "high",
                    "env-code-injection",
                    rel,
                    f"env sets {k}: loads code into, or redirects, child processes",
                )
            )
        elif _secretish_key(key) and not _placeholder(val) and len(val) >= 8:
            if not _token_shaped(val):  # token-shaped values are reported by the secret scan
                out.append(
                    _finding(
                        "medium",
                        "settings-literal-secret",
                        rel,
                        f"env sets {k} to a literal value ({_mask(val)})",
                    )
                )

    if data.get("enableAllProjectMcpServers") is True:
        out.append(
            _finding(
                "high",
                "mcp-auto-approve",
                rel,
                "enableAllProjectMcpServers=true auto-approves every server in .mcp.json",
            )
        )
    elif isinstance(data.get("enabledMcpjsonServers"), list) and data["enabledMcpjsonServers"]:
        out.append(
            _finding(
                "medium",
                "mcp-auto-approve",
                rel,
                "enabledMcpjsonServers pre-approves project MCP servers without asking",
            )
        )
    if data.get("disableAllHooks") is True:
        out.append(
            _finding("high", "hooks-disabled", rel, "disableAllHooks=true turns every hook off")
        )

    plugins, markets = data.get("enabledPlugins"), data.get("extraKnownMarketplaces")
    n_plugins = sum(1 for v in plugins.values() if v) if isinstance(plugins, dict) else 0
    n_markets = len(markets) if isinstance(markets, (dict, list)) else 0
    if n_plugins or n_markets:
        out.append(
            _finding(
                "medium",
                "plugin-auto-install",
                rel,
                f"project settings enable {n_plugins} plugin(s) and add {n_markets} marketplace(s) chosen by "
                "the repository; plugins bring their own hooks, skills and MCP servers",
            )
        )

    perms = data.get("permissions")
    if isinstance(perms, dict):

        def cap(sev: str) -> str:
            return (
                "low" if state == "ignored" else sev
            )  # a file Claude Code wrote here, ignored by git

        rules = perms.get("allow")
        by_sev: dict[str, list[str]] = {}
        for rule in rules if isinstance(rules, list) else ():
            if isinstance(rule, str):
                sev = _rule_severity(rule, bare_ok=True, in_settings=True)
                if sev:
                    by_sev.setdefault(cap(sev), []).append(_safe(rule, 40))
        for sev, names in by_sev.items():
            out.append(
                _finding(
                    sev,
                    "permissions-broad-allow",
                    rel,
                    f"permissions.allow grants without asking: {', '.join(names[:5])}"
                    + (f" (+{len(names) - 5} more)" if len(names) > 5 else ""),
                )
            )
        if perms.get("defaultMode") == "bypassPermissions":
            out.append(
                _finding(
                    cap("high"),
                    "permissions-bypass",
                    rel,
                    "defaultMode=bypassPermissions skips every prompt",
                )
            )
    return out


# ---------------------------------------------------------------------------------------------
# (1) .mcp.json
# ---------------------------------------------------------------------------------------------
_GIT_SPEC = re.compile(r"^(?:git\+|git@|github:|gitlab:|bitbucket:|git://)|\.git(?:[#@]|$)", re.I)
_VALUE_FLAGS = frozenset(
    "--with --python -c --call --registry --cache --prefix -w --workspace --index --index-url -i "
    "--env-file --directory --project --config".split()
)
_SPEC_FLAGS = frozenset({"-p", "--package", "--from", "--spec"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _runner_spec(args: list[str]) -> str | None:
    i = 0
    while i < len(args):
        a = args[i]
        if a in _SPEC_FLAGS:
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith(("--package=", "--from=", "--spec=")):
            return a.split("=", 1)[1]
        if a in _VALUE_FLAGS:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a
    return None


def _spec_problem(kind: str, spec: str) -> str | None:
    s = spec.strip()
    if _GIT_SPEC.search(s):
        if re.search(r"[@#][0-9a-f]{40}\b", s, re.I):
            return None
        return "installs from a git URL without a commit SHA"
    if s.startswith(("http://", "https://", "file:", "/", "./", "../")) or re.match(
        r"[A-Za-z]:[\\/]", s
    ):
        return None
    if kind == "js":
        _, _, ver = (s[1:] if s.startswith("@") else s).partition("@")
        if re.fullmatch(r"=?v?\d+\.\d+\.\d+(?:[-+][\w.+-]+)?", ver):
            return None
    elif re.search(r"==\s*\d[\w.!+-]*$|@\d[\w.!+-]*$", s):
        return None
    return "runs a package without a pinned version"


def _runner_problems(tokens: list[str]) -> list[str]:
    bases = [_exe_base(t) for t in tokens]
    for i, base in enumerate(bases):
        nxt = bases[i + 1] if i + 1 < len(bases) else ""
        nxt2 = bases[i + 2] if i + 2 < len(bases) else ""
        rest = tokens[i + 1 :]
        if base in ("npx", "bunx", "pnpx"):
            kind, args = "js", rest
        elif base in ("pnpm", "yarn") and nxt == "dlx":
            kind, args = "js", rest[1:]
        elif base == "npm" and nxt in ("exec", "x"):
            kind, args = "js", rest[1:]
        elif base == "bun" and nxt == "x":
            kind, args = "js", rest[1:]
        elif base == "uvx":
            kind, args = "py", rest
        elif base == "uv" and nxt == "tool" and nxt2 == "run":
            kind, args = "py", rest[2:]
        elif base == "pipx" and nxt == "run":
            kind, args = "py", rest[1:]
        else:
            continue
        spec = _runner_spec(args)
        problem = _spec_problem(kind, spec) if spec else None
        return [f"{base} {problem}"] if problem else []
    return []


def _mcp_tokens(command: str, args: object) -> list[str]:
    parts = [command, *(str(a) for a in args)] if isinstance(args, list) else [command]
    out: list[str] = []
    for part in parts:
        out.extend(_TOKEN_RE.findall(part) if " " in part.strip() else [part])
    return [t.strip("\"'") for t in out if t]


def _check_mcp(repo: _Repo, rel: str, text: str) -> list[Finding]:
    data, why = _parse_json(text)
    if not isinstance(data, dict):
        reason = why or "not a JSON object"
        return [
            _finding(
                "medium",
                "config-unparseable",
                rel,
                f"not read as a JSON object ({reason}): not audited; review it by hand",
            )
        ]
    servers = data.get("mcpServers", data.get("servers"))
    out: list[Finding] = []
    for name, spec in servers.items() if isinstance(servers, dict) else ():
        if not isinstance(spec, dict):
            continue
        sname = _safe(name, 40)
        command = spec.get("command")
        if isinstance(command, str) and command.strip():
            tokens = _mcp_tokens(command, spec.get("args"))
            out.extend(_command_findings(repo, " ".join(tokens), rel, f"MCP server {sname}"))
            for problem in _runner_problems(tokens):
                out.append(
                    _finding(
                        "medium", "mcp-unpinned-package", rel, f"MCP server {sname}: {problem}"
                    )
                )
        url = spec.get("url")
        if isinstance(url, str):
            try:
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
            except ValueError:
                parts, host = None, ""
            if parts and parts.scheme.lower() in ("http", "ws") and host not in _LOOPBACK:
                out.append(
                    _finding(
                        "medium",
                        "mcp-insecure-url",
                        rel,
                        f"MCP server {sname} uses {parts.scheme.lower()}:// to {_host(url)}",
                    )
                )
        for field in ("env", "headers"):
            table = spec.get(field)
            for key, val in table.items() if isinstance(table, dict) else ():
                if not isinstance(key, str) or not isinstance(val, str):
                    continue
                keyed = _secretish_key(key) or key.lower() in ("authorization", "x-api-key")
                if keyed and not _placeholder(val) and len(val) >= 8 and not _token_shaped(val):
                    out.append(
                        _finding(
                            "medium",
                            "mcp-literal-secret",
                            rel,
                            f"MCP server {sname}: {field} {_safe(key, 30)} holds a literal value ({_mask(val)})",
                        )
                    )
    return out


# ---------------------------------------------------------------------------------------------
# (1) skills, agents, commands, rules
# ---------------------------------------------------------------------------------------------
_FM = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_TOOL_RE = re.compile(r"[A-Za-z_][\w.\-*]*(?:\([^)\n]{0,300}\))?|\*")
# `[ \t]*` and `(?:-[ \t]*)?` (not `\s*-?\s*`): blank lines and two adjacent \s* made the old form cubic on a
# frontmatter of empty lines; the value is taken whole and stripped in Python.
_FM_COMMAND = re.compile(r"(?m)^[ \t]*(?:-[ \t]*)?command[ \t]*:(.*)$")
_INLINE_CMD = re.compile(r"!`([^`\n]+)`")
_FENCE_CMD = re.compile(r"(?m)^[ \t]*```!\s*\n(.*?)^[ \t]*```", re.S)
_ARG_INTERP = re.compile(r"\$ARGUMENTS|\$\d|\$\{")

_IGNORE_RULES = [
    re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding|system)"
        r"\s+(?:instructions?|prompts?|rules?|messages?|directions?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|"
        r"earlier|system|safety)\s+(?:instructions?|prompts?|rules?|guidelines?|polic(?:y|ies))\b",
        re.I,
    ),
    re.compile(
        r"\b(?:do\s+not|don'?t|never)\s+(?:tell|inform|mention|reveal|disclose|alert|notify|show)\b"
        r"[^.\n]{0,40}\b(?:user|human|operator|developer)\b",
        re.I,
    ),
    re.compile(
        r"\bwithout\s+(?:telling|informing|notifying|alerting)\s+the\s+(?:user|human)\b", re.I
    ),
]
_SKILL_ONLY_RULES = [
    re.compile(
        r"\b(?:never|don'?t|do\s+not)\s+(?:ask|prompt)\s+(?:the\s+user\s+)?(?:for\s+)?"
        r"(?:permission|confirmation|approval)\b",
        re.I,
    ),
    re.compile(
        r"\bwithout\s+(?:asking|requiring)\s+(?:for\s+)?(?:permission|confirmation|approval)\b",
        re.I,
    ),
]


def _frontmatter_text(text: str) -> str:
    m = _FM.match(text)
    return m.group(1) if m else ""


def _fm_value(fm: str, key: str) -> str:
    m = re.search(r"(?ms)^" + re.escape(key) + r"\s*:\s*(.*?)(?=^[A-Za-z_][\w-]*\s*:|\Z)", fm)
    return m.group(1) if m else ""


def _check_skill(repo: _Repo, rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    fm = _frontmatter_text(text)
    names: dict[str, list[str]] = {}
    for key in ("allowed-tools", "allowed_tools", "tools"):
        for tok in _TOOL_RE.findall(_fm_value(fm, key)):
            if key == "tools" and "(" not in tok:
                continue
            sev = _rule_severity(tok, bare_ok=key != "tools")
            if sev:
                names.setdefault(sev, []).append(_safe(tok, 40))
    for sev, toks in names.items():
        out.append(
            _finding(
                sev,
                "skill-broad-tools",
                rel,
                f"grants tools without asking: {', '.join(dict.fromkeys(toks[:5]))}",
            )
        )
    for raw in _FM_COMMAND.findall(fm):
        cmd = raw.strip().strip("\"'")
        if cmd:
            out.extend(_command_findings(repo, cmd, rel, "frontmatter hook", scripts=False))

    lines_by_sev: dict[str, list[int]] = {}
    labels: dict[str, set[str]] = {}
    inline = [(m.start(), m.group(1)) for m in _INLINE_CMD.finditer(text)]
    inline += [(m.start(1), m.group(1)) for m in _FENCE_CMD.finditer(text)]
    newlines = [i for i, ch in enumerate(text) if ch == "\n"] if inline else []
    for pos, cmd in inline:
        flagged = _analyze_command(cmd)
        if flagged:
            sev = "high" if any(s == "high" for _, s, _ in flagged) else "medium"
            labels.setdefault(sev, set()).update(label for _, _, label in flagged)
        elif _ARG_INTERP.search(cmd):
            sev = "medium"
            labels.setdefault(sev, set()).add("interpolates arguments into a shell command")
        else:
            sev = "low"
        lines_by_sev.setdefault(sev, []).append(bisect_left(newlines, pos) + 1)
    for sev, lines in lines_by_sev.items():
        what = (
            "runs a shell command when the skill loads"
            if sev == "low"
            else "; ".join(sorted(labels.get(sev, ())))
        )
        out.append(
            _finding(
                sev,
                "skill-inline-command",
                rel,
                f"inline !`command` at line(s) {_first_lines(lines)}: {what}",
                lines[0],
            )
        )

    split = text.split("\n")
    hits = _phrase_lines(split, [*_IGNORE_RULES, *_SKILL_ONLY_RULES])
    if hits:
        out.append(
            _finding(
                "medium",
                "skill-ignore-rules",
                rel,
                f"agent instruction file tells the agent to ignore rules or hide actions (line(s) {_first_lines(hits)})",
                hits[0],
            )
        )
    return out


def _phrase_lines(lines: list[str], patterns: list[re.Pattern[str]]) -> list[int]:
    return [n for n, line in enumerate(lines, 1) if any(p.search(line) for p in patterns)]


def _check_agent_config(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if SETTINGS_RE.search(rel):
            check, severity = _check_settings, "medium"
        elif MCP_RE.search(rel):
            check, severity = _check_mcp, "medium"
        elif SKILL_RE.search(rel) or AGENT_FILE_RE.search(rel):
            check, severity = _check_skill, "medium"
        elif CLAUDE_MD_RE.search(rel):
            check, severity = (
                None,
                "medium",
            )  # unreadable is reported; the text checks run in their own groups
        elif README_RE.search(rel) or LLMS_RE.search(rel):
            check, severity = None, "info"
        else:
            continue
        # one hostile file (deeply nested JSON, ...) must not discard the findings of the others
        try:
            text = _load(repo, rel, out, severity)
            if text is not None and check is not None:
                out.extend(check(repo, rel, text))
        except Exception as exc:  # noqa: BLE001 - RecursionError, MemoryError, ValueError ...
            out.append(_crash_finding(rel, exc, surface=True))
    for rel in repo.escaped:
        if _is_agent_surface(rel):
            out.append(
                _finding(
                    "medium", "symlink-outside-root", rel, "symlink resolves outside the repository"
                )
            )
    return out


# ---------------------------------------------------------------------------------------------
# (2) hidden Unicode
# ---------------------------------------------------------------------------------------------
# Characters that render as nothing (or reorder text) and are used to hide instructions from a human reader while an
# LLM still reads them: zero-width and joiner characters, bidi controls, soft hyphen, fillers, variation selectors,
# and the Unicode "tag" block (an invisible copy of ASCII). Ordinary uses are let through by `_benign_hidden`.
_HIDDEN_OTHER = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180e\u200b-\u200f\u2028\u2029\u202a-\u202e"
    "\u2060-\u2064\u2066-\u206f\u2800\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb"
    "\U0001d173-\U0001d17a\U000e0100-\U000e01ef]"
)
_TAGS = re.compile("[\U000e0000-\U000e007f]+")
# The only tag sequences with a use: the three RGI subdivision flags (England, Scotland, Wales) = black flag + the
# tag letters of gbeng/gbsct/gbwls + cancel tag. Anything else made of tag characters is hidden text, however it
# is wrapped (a bare "black flag ... cancel tag" around a payload is not a flag).
_FLAG_SEQ = re.compile(
    "\U0001f3f4\U000e0067\U000e0062"
    "(?:\U000e0065\U000e006e\U000e0067|\U000e0073\U000e0063\U000e0074|\U000e0077\U000e006c\U000e0073)"
    "\U000e007f"
)
_HIDDEN_SCOPE = (
    SETTINGS_RE,
    MCP_RE,
    SKILL_RE,
    AGENT_FILE_RE,
    CLAUDE_MD_RE,
    README_RE,
    LLMS_RE,
    COPILOT_RE,
)

_Ranges = tuple[tuple[int, int], ...]
_EMOJI_RANGES: _Ranges = (
    (0x1F000, 0x1FAFF),
    (0x2190, 0x23FF),
    (0x2600, 0x27BF),
    (0x2900, 0x297F),
    (0x2B00, 0x2BFF),
)
_RTL: _Ranges = (
    (0x0590, 0x08FF),
    (0xFB1D, 0xFDFF),
    (0xFE70, 0xFEFF),
    (0x10800, 0x10FFF),
    (0x1E800, 0x1EFFF),
)
_SEA: _Ranges = ((0x0E00, 0x0EFF), (0x0F00, 0x0FFF), (0x1000, 0x109F), (0x1780, 0x17FF))
# scripts that really use ZWNJ/ZWJ/CGJ: Persian/Arabic/Hebrew, the Indic scripts, Thai, Lao, Tibetan, Myanmar, Khmer
_JOINING: _Ranges = (*_RTL, (0x0900, 0x0DFF), *_SEA, (0x1800, 0x18AF), (0x11000, 0x11FFF))
_MONGOLIAN: _Ranges = ((0x1800, 0x18AF),)
_KHMER: _Ranges = ((0x1780, 0x17FF),)
_JAMO: _Ranges = ((0x1100, 0x11FF), (0xA960, 0xA97F), (0xD7B0, 0xD7FF))
_BRAILLE: _Ranges = ((0x2801, 0x28FF),)
# neighbours that make one invisible mark ordinary: (script ranges, how many characters either side to look)
_BENIGN_CONTEXT: dict[int, tuple[_Ranges, int]] = {
    0x034F: (_JOINING, 1),
    0x061C: (_RTL, 2),
    0x200E: (_RTL, 2),
    0x200F: (_RTL, 2),
    0x180B: (_MONGOLIAN, 1),
    0x180C: (_MONGOLIAN, 1),
    0x180D: (_MONGOLIAN, 1),
    0x17B4: (_KHMER, 1),
    0x17B5: (_KHMER, 1),
    0x115F: (_JAMO, 1),
    0x1160: (_JAMO, 1),
    0x2800: (_BRAILLE, 1),
}
# characters that take an emoji presentation selector without being in the emoji blocks
_EMOJI_BASES = frozenset(
    {0xA9, 0xAE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2, 0x3030, 0x303D, 0x3297, 0x3299}
    | set(range(0x25AA, 0x25FF))
)
_BIDI = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x2070)})
_FILLERS = frozenset(
    {0x00AD, 0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0xFFF9, 0xFFFC))
    | set(range(0x1D173, 0x1D17B))
)


def _in_ranges(cp: int, ranges: _Ranges) -> bool:
    return any(a <= cp <= b for a, b in ranges)


def _emoji_base(cp: int) -> bool:
    return _in_ranges(cp, _EMOJI_RANGES) or cp in _EMOJI_BASES


def _emojiish(cp: int) -> bool:
    return _emoji_base(cp) or 0xFE00 <= cp <= 0xFE0F


def _hidden_category(cp: int) -> str:
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or 0x180B <= cp <= 0x180D:
        return "variation-selector"
    if cp in _BIDI:
        return "bidi-control"
    if cp in _FILLERS:
        return "invisible-filler"
    if cp in (0x2028, 0x2029):
        return "line-separator"
    return "zero-width"


def _near(text: str, idx: int, ranges: _Ranges, span: int) -> bool:
    """A visible character of the script within `span` characters. Other invisible characters do not count (a run
    of U+115F is not "Hangul context" for itself), so a run is only ever excused at its edges."""
    for j in range(max(0, idx - span), min(len(text), idx + span + 1)):
        ch = text[j]
        if j != idx and not _HIDDEN_OTHER.match(ch) and _in_ranges(ord(ch), ranges):
            return True
    return False


def _benign_hidden(text: str, idx: int, cp: int) -> bool:
    """An invisible character in an ordinary role: a leading BOM, the joiners emoji sequences and Persian/Indic
    scripts need, the variation selector after an emoji, direction marks next to right-to-left text, a lone
    zero-width space between Thai/Khmer/Lao/Myanmar letters. In a run, or between ASCII letters, none of that holds."""
    if cp == 0xFEFF:
        return idx == 0  # a byte-order mark
    if cp in (0x200C, 0x200D):
        for j in (idx - 1, idx + 1):
            if not 0 <= j < len(text):
                continue
            ch = text[j]
            c = ord(ch)
            if 0xFE00 <= c <= 0xFE0F:
                return True  # emoji sequences put the presentation selector next to the joiner
            if not _HIDDEN_OTHER.match(ch) and (_emojiish(c) or _in_ranges(c, _JOINING)):
                return True
        return False
    ctx = _BENIGN_CONTEXT.get(cp)
    if ctx:
        return _near(text, idx, *ctx)
    if cp == 0x200B:
        return (
            0 < idx < len(text) - 1
            and _in_ranges(ord(text[idx - 1]), _SEA)
            and _in_ranges(ord(text[idx + 1]), _SEA)
        )
    if cp in (0xFE0E, 0xFE0F):
        if idx == 0:
            return False
        prev = ord(text[idx - 1])
        if prev in (0x23, 0x2A) or 0x30 <= prev <= 0x39:  # keycap: digit, FE0F, U+20E3
            return idx + 1 < len(text) and ord(text[idx + 1]) == 0x20E3
        return _emoji_base(prev)
    return False


def hidden_unicode(text: str) -> dict[str, tuple[int, list[int], set[int]]]:
    """category -> (count, sorted lines, code points).

    Benign uses (a leading BOM, emoji joiners and presentation selectors, direction marks inside right-to-left
    text, the England/Scotland/Wales flags) are ignored; every other tag character is reported, including the
    ones inside a flag-shaped wrapper. At most MAX_HIDDEN_MATCHES characters are examined (counts saturate).
    """
    counts: dict[str, int] = {}
    lines: dict[str, set[int]] = {}
    seen: dict[str, set[int]] = {}
    newlines: list[int] | None = None

    def line_of(off: int) -> int:
        nonlocal newlines
        if newlines is None:
            newlines = [m.start() for m in re.finditer("\n", text)]
        return bisect_left(newlines, off) + 1

    def add(cat: str, n: int, off: int, cps) -> None:
        counts[cat] = counts.get(cat, 0) + n
        lines.setdefault(cat, set()).add(line_of(off))
        seen.setdefault(cat, set()).update(cps)

    for m in itertools.islice(_HIDDEN_OTHER.finditer(text), MAX_HIDDEN_MATCHES):
        idx, cp = m.start(), ord(m.group())
        if not _benign_hidden(text, idx, cp):
            add(_hidden_category(cp), 1, idx, (cp,))
    # blank out the genuine flags (same length, so offsets and line numbers stay valid), flag every tag left
    scan = (
        _FLAG_SEQ.sub(lambda m: "\ufffd" * len(m.group()), text) if "\U0001f3f4" in text else text
    )
    for m in itertools.islice(_TAGS.finditer(scan), MAX_HIDDEN_MATCHES):
        add("tag-character", m.end() - m.start(), m.start(), set(map(ord, m.group())))
    return {k: (n, sorted(lines[k]), seen[k]) for k, n in counts.items()}


def _check_hidden_unicode(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if not any(p.search(rel) for p in _HIDDEN_SCOPE):
            continue
        try:
            text, reason = repo.read(rel)
            if text is None:
                continue
            cats = hidden_unicode(text)
            if reason == "nul" and rel in repo.nul:
                count, line = repo.nul[rel]
                cats["nul-byte"] = (
                    count,
                    [line],
                    {0},
                )  # dropped for scanning, but never ordinary in text
            if not cats:
                continue
            parts = []
            first = 10**9
            for cat in sorted(cats):
                n, lns, cps = cats[cat]
                first = min(first, lns[0])
                codes = ", ".join(f"U+{cp:04X}" for cp in sorted(cps)[:6])
                parts.append(f"{cat} x{n} at line(s) {_first_lines(lns)} ({codes})")
            out.append(
                _finding(
                    "high",
                    "hidden-unicode",
                    rel,
                    "invisible characters (not shown): " + "; ".join(parts),
                    first,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one hostile file must not hide the others
            out.append(_crash_finding(rel, exc, surface=True))
    return out


# ---------------------------------------------------------------------------------------------
# (3) agent-directed text
# ---------------------------------------------------------------------------------------------
_ADDRESS = [
    re.compile(
        r"\bif\s+you(?:'re|\s+are)\s+(?:an?\s+|the\s+)?(?:[\w-]+\s+){0,2}"
        r"(?:coding\s+agent|ai\b|a\.i\.|llm\b|language\s+model|autonomous\s+agent|agentic|claude\b|"
        r"chatgpt\b|copilot\b|codex\b|gemini\b)",
        re.I,
    ),
    re.compile(r"\bfor\s+(?:ai|llm|coding|autonomous)\s+agents?\b", re.I),
    re.compile(
        r"\b(?:instructions?|message|note|notice|attention)\s+(?:to|for)\s+(?:the\s+)?"
        r"(?:ai\b|llm\b|coding\s+agents?|assistants?)",
        re.I,
    ),
    re.compile(r"\bwhen\s+(?:an?\s+)?(?:ai|llm|coding)\s+agent\s+(?:reads|sees|finds|is)\b", re.I),
]
_ACTION = re.compile(
    r"\b(?:install(?:ing|ation)?|set\s?up|run\s+(?:the\s+following|this|these)|execute|download|"
    r"sign[\s-]?in|log[\s-]?in|login|authenticat\w+|paste\s+(?:your|the)|api[_\s-]?key|token|"
    r"credentials?|password|npx|pipx?\s+install|npm\s+(?:i|install)|brew\s+install|apt(?:-get)?\s+install|"
    r"git\s+clone|curl|wget|iex|irm)\b",
    re.I,
)
_TEXT_SCOPE = (README_RE, LLMS_RE, COPILOT_RE, CLAUDE_MD_RE)


def _in_text_scope(rel: str) -> bool:
    if any(p.search(rel) for p in _TEXT_SCOPE):
        return True
    low = rel.lower()
    return low.startswith("docs/") and low.endswith(".md")


def _agent_text_findings(rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    lines = text.split("\n")
    directed: list[int] = []
    piped = False
    address_only: list[int] = []
    for i, line in enumerate(lines):
        if not any(p.search(line) for p in _ADDRESS):
            continue
        window = "\n".join(lines[max(0, i - 2) : i + 7])
        if _pipe_to_shell(window):
            piped = True
            directed.append(i + 1)
        elif _ACTION.search(window):
            directed.append(i + 1)
        else:
            address_only.append(i + 1)
    if directed:
        extra = "; one nearby instruction pipes a download into a shell" if piped else ""
        out.append(
            _finding(
                "medium",
                "agent-directed-instruction",
                rel,
                f"addresses AI agents near install/run/sign-in/download instructions (line(s) "
                f"{_first_lines(directed)}){extra}. Informational: this is common in legitimate "
                "repos (llms.txt, AGENTS.md); read the instruction yourself before an agent follows it",
                directed[0],
            )
        )
    if address_only:
        out.append(
            _finding(
                "info",
                "agent-directed-address",
                rel,
                f"addresses AI agents directly (line(s) {_first_lines(address_only)})",
                address_only[0],
            )
        )
    # skills, agents and commands get the same phrase check (plus a few more) from _check_skill
    if not (SKILL_RE.search(rel) or AGENT_FILE_RE.search(rel)):
        hits = _phrase_lines(lines, _IGNORE_RULES)
        if hits:
            out.append(
                _finding(
                    "medium",
                    "prompt-injection-phrase",
                    rel,
                    "classic injection wording (ignore previous instructions / hide this from the user) "
                    f"at line(s) {_first_lines(hits)}. Informational: this also appears in security "
                    "docs and examples",
                    hits[0],
                )
            )
    return out


def _check_agent_text(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if not _in_text_scope(rel):
            continue
        try:
            text, _ = repo.read(rel)
            if text is not None:
                out.extend(_agent_text_findings(rel, text))
        except Exception as exc:  # noqa: BLE001 - one hostile file must not hide the others
            out.append(_crash_finding(rel, exc, surface=True))
    return out


# ---------------------------------------------------------------------------------------------
# (4) GitHub workflows (plain-text lint, no YAML library)
# ---------------------------------------------------------------------------------------------
_USES = re.compile(r"""^\s*(?:-\s+)?uses\s*:\s*["']?([^\s"'#]+)["']?\s*$""")
_PRIVILEGED = ("pull_request_target", "issue_comment", "workflow_run")
_REF_KEY = re.compile(r"\bref\s*:")
_HEAD_TAIL = re.compile(
    r"pull_request\.head|head_ref|refs/pull/|head\.sha|head\.ref|workflow_run\.head", re.I
)
_HEAD_OTHER = re.compile(r"\bgh\s+pr\s+checkout\b|refs/pull/|\bpull/\d+/head\b")
_SECRET_USE = re.compile(r"\bsecrets\.(?!GITHUB_TOKEN\b)\w+|\bsecrets\s*:\s*inherit\b", re.I)
_UNTRUSTED_CONTEXT = re.compile(
    r"\bgithub\.head_ref\b|\bgithub\.event\.(?:[\w*-]+\.){0,6}"
    r"(?:title|body|ref|label|message|page_name|head_branch|default_branch|description|email|"
    r"author\.name|committer\.name|pusher\.name)\b"
)
_SECRET_EXPR = re.compile(r"\bsecrets\.|\btojson\(\s*secrets\s*\)")
_BRACKET = re.compile(r"""\[\s*(?:'([^'\[\]]{0,64})'|"([^"\[\]]{0,64})"|(\d{1,6}))\s*\]""")
_RUN_LINE = re.compile(r"^(\s*)(?:-\s+)?(run|script)\s*:\s*(.*)$")
MAX_EXPR_CHARS = 400  # an expression is examined up to this length (longer ones are cut)
MAX_BLOCK_CHARS = 2_000_000  # run/script text examined per workflow file


def _is_head_checkout(line: str) -> bool:
    """`ref: ...pull_request.head...`, `gh pr checkout`, `refs/pull/N/head`. Linear: after the first `ref:` the
    rest of the line is searched once (a later `ref:` sees a subset of it)."""
    m = _REF_KEY.search(line)
    return bool(m and _HEAD_TAIL.search(line, m.end())) or bool(_HEAD_OTHER.search(line))


def _expressions(body: str) -> list[str]:
    """The `${{ ... }}` expressions of a block, stripped and lower-cased with `['a']`/`[0]` rewritten as `.a`/`.0`
    (GitHub contexts are case-insensitive and accept both spellings). Linear: starts are found with str.find,
    ends by bisection over the `}}` positions, and each expression is cut at MAX_EXPR_CHARS."""
    ends = [m.start() for m in re.finditer(r"\}\}", body)]
    out: list[str] = []
    i = body.find("${{")
    while i != -1 and ends and len(out) < 5000:
        j = bisect_left(ends, i + 3)
        if j == len(ends):
            break  # nothing closes this start, so nothing closes a later one either
        expr = body[i + 3 : min(ends[j], i + 3 + MAX_EXPR_CHARS)].strip().lower()
        out.append(_BRACKET.sub(lambda m: "." + (m.group(1) or m.group(2) or m.group(3)), expr))
        i = body.find("${{", ends[j] + 2)
    return out


def _strip_comment(line: str) -> str:
    return re.sub(r"(?:^|\s)#.*$", "", line) if "#" in line else line


def _workflow_triggers(code: list[str]) -> set[str]:
    for i, line in enumerate(code):
        m = re.match(r"""^(?:on|"on"|'on'|true)\s*:\s*(.*)$""", line)
        if not m:
            continue
        block = [m.group(1)]
        for nxt in code[i + 1 :]:
            if nxt.strip() and nxt[0] not in " \t-":
                break
            block.append(nxt)
        return set(re.findall(r"[A-Za-z_]+", "\n".join(block)))
    return set()


def _run_blocks(code: list[str]) -> list[tuple[int, str]]:
    """(line, text) of every `run:` body and every `script:` body (actions/github-script runs it as JavaScript).

    Index-based (a slice per block was quadratic in the number of `run:` lines), and the total text taken is
    bounded; an outer block always contains the blocks nested in it, so the bound never hides a later one.
    """
    blocks = []
    budget = MAX_BLOCK_CHARS
    n = len(code)
    for i, line in enumerate(code):
        m = _RUN_LINE.match(line)
        if not m:
            continue
        col = m.start(2)
        rest = m.group(3).strip()
        body = [] if re.match(r"^[|>][+-]?\d*$", rest) else [rest]
        j = i + 1
        while j < n and budget > 0:
            nxt = code[j]
            if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= col:
                break
            body.append(nxt)
            budget -= len(nxt) + 1
            j += 1
        blocks.append((i + 1, "\n".join(body)))
        if budget <= 0:
            break
    return blocks


def _jobs_without_permissions(code: list[str]) -> tuple[int, int] | None:
    start = next((i for i, ln in enumerate(code) if re.match(r"^jobs\s*:\s*$", ln)), None)
    if start is None:
        return None
    job_indent: int | None = None
    child_indent: int | None = None
    jobs: list[bool] = []
    for ln in code[start + 1 :]:
        if not ln.strip():
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent == 0:
            break
        if job_indent is None:
            job_indent = indent
        if indent == job_indent and re.match(r"^\s*[\w.\-'\"]+\s*:", ln):
            jobs.append(False)
            child_indent = None
            continue
        if jobs:
            if child_indent is None and indent > job_indent:
                child_indent = indent
            if indent == child_indent and re.match(r"^\s*permissions\s*:", ln):
                jobs[-1] = True
    return (jobs.count(False), len(jobs)) if jobs else None


def _workflow_findings(rel: str, text: str) -> list[Finding]:
    code = [_strip_comment(ln) for ln in text.split("\n")]
    out: list[Finding] = []

    unpinned: dict[str, list[tuple[str, int]]] = {"medium": [], "low": []}
    for n, ln in enumerate(code, 1):
        m = _USES.match(ln)
        if not m or m.group(1).startswith(("./", "../", "docker://")) or "@" not in m.group(1):
            continue
        name, _, ref = m.group(1).partition("@")
        if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            continue
        sev = "low" if name.split("/")[0].lower() == "actions" else "medium"
        unpinned[sev].append((f"{_safe(name, 40)}@{_safe(ref, 20)}", n))
    for sev, refs in unpinned.items():
        if refs:
            shown = ", ".join(r for r, _ in refs[:4]) + (", ..." if len(refs) > 4 else "")
            out.append(
                _finding(
                    sev,
                    "workflow-unpinned-action",
                    rel,
                    f"{len(refs)} action(s) not pinned to a 40-character commit SHA: {shown}",
                    refs[0][1],
                )
            )

    triggers = sorted(t for t in _workflow_triggers(code) if t in _PRIVILEGED)
    if triggers:
        head = next((n for n, ln in enumerate(code, 1) if _is_head_checkout(ln)), 0)
        secret = next((n for n, ln in enumerate(code, 1) if _SECRET_USE.search(ln)), 0)
        names = " + ".join(triggers)
        if head or secret:
            why = []
            if head:
                why.append(f"checks out PR-controlled code (line {head})")
            if secret:
                why.append(f"uses secrets (line {secret})")
            out.append(
                _finding(
                    "high",
                    "workflow-privileged-trigger",
                    rel,
                    f"{names} runs with a write token and {' and '.join(why)}",
                    head or secret,
                )
            )
        else:
            out.append(
                _finding(
                    "low",
                    "workflow-privileged-trigger",
                    rel,
                    f"{names} runs with a write token; fine only while nothing from the PR is checked out or run",
                )
            )

    if not re.search(r"(?m)^permissions\s*:", "\n".join(code)):
        jobs = _jobs_without_permissions(code)
        if jobs and jobs[0]:
            out.append(
                _finding(
                    "medium",
                    "workflow-missing-permissions",
                    rel,
                    f"no top-level permissions and {jobs[0]} of {jobs[1]} job(s) set none: the token may default to write access",
                )
            )
    for n, ln in enumerate(code, 1):
        if re.match(r"^\s*permissions\s*:\s*[\"']?write-all\b", ln):
            out.append(_finding("high", "workflow-write-all", rel, "permissions: write-all", n))
        if re.search(r"\ballowed_non_write_users\b", ln):
            out.append(
                _finding(
                    "high",
                    "workflow-non-write-users",
                    rel,
                    "allowed_non_write_users lets non-collaborators trigger the agent",
                    n,
                )
            )

    for start, body in _run_blocks(code):
        # run: bodies and github-script `script:` bodies; wrapped (format(), fromJSON()), bracketed and
        # mixed-case spellings of an untrusted context are caught because the expression is normalised first
        exprs = _expressions(body)
        injected = [m.group(0) for e in exprs if (m := _UNTRUSTED_CONTEXT.search(e))]
        secrets = [e for e in exprs if _SECRET_EXPR.search(e)]
        if injected:
            extra = " together with an inline secret" if secrets else ""
            out.append(
                _finding(
                    "high",
                    "workflow-script-injection",
                    rel,
                    f"run step expands attacker-controllable context ({_safe(injected[0], 60)}) "
                    f"inline{extra}",
                    start,
                )
            )
        elif secrets:
            out.append(
                _finding(
                    "medium",
                    "workflow-secret-inline",
                    rel,
                    f"run step expands {_safe(secrets[0], 40)} inline",
                    start,
                )
            )
        if _pipe_to_shell(body):
            out.append(
                _finding(
                    "medium",
                    "workflow-pipe-to-shell",
                    rel,
                    "run step pipes a download into a shell",
                    start,
                )
            )
    return out


def _check_workflows(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if WORKFLOW_RE.match(rel):
            try:
                text, reason = repo.read(rel)
                if text is not None:
                    out.extend(_workflow_findings(rel, text))
                elif reason in ("too-large", "unreadable"):
                    out.append(
                        _finding(
                            "medium",
                            "file-not-scanned",
                            rel,
                            f"not scanned: {_WHY.get(reason, reason)}",
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - one hostile file must not hide the others
                out.append(_crash_finding(rel, exc, surface=True))
    return out


# ---------------------------------------------------------------------------------------------
# (5) secrets and (6) hygiene
# ---------------------------------------------------------------------------------------------
_ENV_EXAMPLE = (
    ".example",
    ".sample",
    ".template",
    ".dist",
    ".tmpl",
    ".tpl",
    ".default",
    ".defaults",
)
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def _is_env_file(rel: str) -> bool:
    name = posixpath.basename(rel).lower()  # `.ENV` is the same file as `.env` on Windows and macOS
    if not (name == ".env" or name.startswith(".env.") or name.endswith(".env")):
        return False
    return not name.endswith(_ENV_EXAMPLE) and not name.endswith(".schema")


_INLINE_COMMENT = re.compile(r"\s#")


def _env_values(text: str) -> list[tuple[int, str, str]]:
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        m = _ENV_LINE.match(line.rstrip("\r"))
        if m and not line.lstrip().startswith("#"):
            value = m.group(2)
            c = _INLINE_COMMENT.search(
                value
            )  # ` # comment`; `\s+#.*$` was quadratic on long whitespace runs
            if c:
                value = value[: c.start()]
            out.append((n, m.group(1), value.strip().strip("\"'")))
    return out


def _looks_sensitive(key: str, value: str) -> bool:
    return (
        _token_shaped(value)
        or _secret_like_value(value)
        or (_secretish_key(key) and len(value.strip("\"'")) >= 8)
    )


def _candidates(repo: _Repo) -> list[str]:
    """Files that are, or would be, committed: tracked plus untracked-not-ignored (all files without git)."""
    git = repo.git_files()
    if git is None:
        return repo.files
    return [f for f in repo.files if f in git[0] or f in git[1]]


def _secret_hits(text: str) -> list[tuple[int, str, str]]:
    """(line, label, matched text) for up to 20 token-shaped secrets, scanning line by line in bounded windows."""
    low_has_aws = "aws_secret" in text.lower()
    if not (low_has_aws or any(tok in text for tok in _QUICK)):
        return []
    patterns = _token_patterns()
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, str, str]] = []
    for line_no, off, chunk in _windows(text):
        if len(hits) >= 20:
            break
        if not (any(tok in chunk for tok in _QUICK) or "aws_secret" in chunk.lower()):
            continue
        for pat, label in patterns:
            for m in pat.finditer(chunk):
                span = (off + m.start(), off + m.end())
                if len(hits) >= 20 or any(a < span[1] and span[0] < b for a, b in taken):
                    continue
                taken.append(span)
                hits.append((line_no, label, m.group(0)))
    return hits


def _scan_file_for_secrets(rel: str, text: str) -> list[Finding]:
    out = [
        _finding("high", "secret-token", rel, f"{label} {_mask(value)}", line)
        for line, label, value in _secret_hits(text)
    ]
    if _is_env_file(rel):
        for n, key, value in _env_values(text):
            if _secretish_key(key) and _secret_like_value(value) and not _token_shaped(value):
                out.append(
                    _finding(
                        "high",
                        "secret-env-value",
                        rel,
                        f"{_safe(key, 40)} holds a high-entropy value {_mask(value)}",
                        n,
                    )
                )
    return out


def _check_secrets(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    budget = MAX_SCAN_BYTES
    skipped_large = 0
    n_secrets = 0
    for rel in _candidates(repo):
        if n_secrets >= MAX_SECRET_FINDINGS:
            out.append(
                _finding(
                    "info",
                    "scan-truncated",
                    rel,
                    f"stopped after {MAX_SECRET_FINDINGS} secret findings",
                )
            )
            break
        if posixpath.splitext(rel)[1].lower() in BINARY_EXTS:
            continue
        if budget <= 0 or repo.expired():
            why = "scan budget (64 MB)" if budget <= 0 else "time budget"
            out.append(
                _finding(
                    "medium",
                    "scan-truncated",
                    rel,
                    f"{why} used up; remaining files were not scanned for secrets",
                )
            )
            break
        try:
            text, reason = repo.read(rel, cache=False)
            if text is None:
                skipped_large += reason == "too-large"
                continue
            budget -= len(text)
            found = _scan_file_for_secrets(rel, text)
        except Exception as exc:  # noqa: BLE001 - one hostile file must not hide the others
            out.append(_crash_finding(rel, exc, surface=False))
            continue
        n_secrets += sum(1 for f in found if f.kind == "secret-token")
        out.extend(found)
    if skipped_large:
        out.append(
            _finding(
                "info",
                "files-skipped",
                ".",
                f"{skipped_large} file(s) over 512 KB were not scanned for secrets",
            )
        )
    return out


def _gitignore_covers(text: str, name: str) -> bool:
    ignored = False
    for raw in text.split("\n"):
        pat = raw.strip()
        if not pat or pat.startswith("#") or pat.endswith("/"):
            continue
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        pat = pat.lstrip("/")
        if pat.startswith("**/"):
            pat = pat[3:]
        if fnmatch.fnmatchcase(name, pat):
            ignored = not neg
    return ignored


def _tracked_env_finding(repo: _Repo, rel: str) -> Finding:
    text, _ = repo.read(rel)
    values = [(k, v) for _, k, v in _env_values(text or "") if not _placeholder(v)]
    secrets = [1 for k, v in values if _looks_sensitive(k, v)]
    if secrets or (text and _token_shaped(text)):
        sev, what = (
            "high",
            f"tracked by git with {max(len(secrets), 1)} secret-looking value(s)",
        )
    elif values:
        sev, what = (
            "medium",
            f"tracked by git with {len(values)} value(s), none look like secrets",
        )
    else:
        sev, what = "low", "tracked by git (values look like placeholders)"
    return _finding(sev, "env-file-tracked", rel, what)


def _check_hygiene(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    git = repo.git_files()
    has_git = os.path.exists(os.path.join(str(repo.root), ".git"))
    env_files = [f for f in repo.files if _is_env_file(f)]
    if git is not None:
        tracked = git[0]
        for rel in env_files:
            if rel not in tracked:
                continue
            try:
                out.append(_tracked_env_finding(repo, rel))
            except Exception as exc:  # noqa: BLE001 - one hostile file must not hide the others
                out.append(_crash_finding(rel, exc, surface=False))
        for rel in repo.files:
            # (a tracked .claude/settings.local.json is reported by the agent-config check, where the preflight sees it)
            if rel in tracked and posixpath.basename(rel).lower() == "claude.local.md":
                out.append(
                    _finding(
                        "medium", "local-file-tracked", rel, "personal local file is committed"
                    )
                )
    elif has_git:
        out.append(
            _finding(
                "info", "git-unavailable", ".", "git ls-files failed: tracked-file checks skipped"
            )
        )
    if has_git:
        gi, _ = repo.read(".gitignore")
        gi = gi or ""
        if not _gitignore_covers(gi, ".env"):
            present = bool(env_files)
            out.append(
                _finding(
                    "medium" if present else "low",
                    "gitignore-missing-env",
                    ".gitignore",
                    ".env is not ignored" + (" and a .env file exists here" if present else ""),
                )
            )
        if not _gitignore_covers(gi, "CLAUDE.local.md"):
            present = repo.lookup("CLAUDE.local.md") is not None
            out.append(
                _finding(
                    "medium" if present else "low",
                    "gitignore-missing-claude-local",
                    ".gitignore",
                    "CLAUDE.local.md is not ignored"
                    + (" and the file exists here" if present else ""),
                )
            )
    return out


# ---------------------------------------------------------------------------------------------
# entry points: scan
# ---------------------------------------------------------------------------------------------
_CHECKS = (
    ("agent-config", _check_agent_config),
    ("hidden-unicode", _check_hidden_unicode),
    ("agent-text", _check_agent_text),
    ("workflow", _check_workflows),
    ("secrets", _check_secrets),
    ("hygiene", _check_hygiene),
)


def _scan(root: Path | str, groups: frozenset[str]) -> list[Finding]:
    try:
        root = Path(root)
        if not root.is_dir():
            return [_root_finding("info", "root-missing", root, "not a directory")]
        repo = _Repo(root)
        repo.walk()
    except Exception as exc:  # noqa: BLE001 - never raises; the audit just reports that it could not run
        return [
            _root_finding(
                "medium",
                "audit-error",
                root,
                f"could not read the repository: {type(exc).__name__}",
            )
        ]
    out: list[Finding] = []
    if repo.truncated:
        why = "; ".join(_TRUNCATION[k].format(n=MAX_WALK_ENTRIES) for k in sorted(repo.trunc))
        out.append(
            _finding(
                "medium",
                "scan-truncated",
                ".",
                f"the scan is incomplete: {why}",
            )
        )
    for group, check in _CHECKS:
        if group not in groups:
            continue
        try:
            out.extend(check(repo))
        except Exception as exc:  # fail open: one broken check must not hide the others
            out.append(
                _finding(
                    "medium", "audit-error", ".", f"{group} check failed: {type(exc).__name__}"
                )
            )
    return _sorted(out)


def scan_repo(root: Path | str) -> list[Finding]:
    """Every check, sorted by severity. Reads files only; never raises."""
    return _scan(root, ALL_GROUPS)


def preflight(root: Path | str) -> list[Finding]:
    """High and medium findings of the agent-config, hidden-Unicode and agent-text checks.

    The question it answers is "is this repo safe to run agent automation in?", so it skips workflow lint,
    secrets and hygiene, and it is cheap enough for the verify gate.
    """
    return [f for f in _scan(root, PREFLIGHT_GROUPS) if f.severity in ("high", "medium")]


# ---------------------------------------------------------------------------------------------
# entry points: external scanners
# ---------------------------------------------------------------------------------------------
_INSTALL_HINTS = {
    "gitleaks": "install gitleaks (brew install gitleaks, scoop install gitleaks, or a release binary "
    "from github.com/gitleaks/gitleaks/releases)",
    "pip-audit": "install pip-audit (uv tool install pip-audit, or pipx install pip-audit)",
    "npm audit": "install Node.js (npm audit ships with npm)",
    "osv-scanner": "install osv-scanner (brew install osv-scanner, or see "
    "google.github.io/osv-scanner/installation)",
}
# A repo's own config can expand variables (`${NAME}` in .npmrc) and point a scanner at a host of its choosing, so a
# scanner gets a scrubbed copy of the environment: names that look like credentials or belong to a cloud/CI/agent
# product are dropped, and so is any value that carries credentials in a URL (DATABASE_URL, a proxy with a login).
_ENV_DENY = re.compile(
    r"(?i)token|secret|pass|pwd|key|cred|auth|cookie|dsn|connection|private|cert"
    r"|^(?:ANTHROPIC|CLAUDE_CODE|AWS|AZURE|GOOGLE|GCP|GITHUB|GH|NPM)_|^DATABASE_URL$|^SSH_AUTH_SOCK$"
    r"|^GIT_ASKPASS$|^SSH_ASKPASS$"
)
# what a toolchain needs, even when the name trips the deny pattern above (GIT_AUTHOR_* contains AUTH, the CA
# bundle variables contain CERT: paths, not secrets)
_ENV_KEEP = re.compile(
    r"(?i)^(?:GIT_AUTHOR_\w+|GIT_COMMITTER_\w+|SSL_CERT_FILE|SSL_CERT_DIR|NODE_EXTRA_CA_CERTS)$"
)
_CRED_URL = re.compile(r"://[^/\s:@]+:[^/\s@]+@")


def _tool_env() -> dict[str, str]:
    """Scanners read the repo's own config (.npmrc registry, ...): never hand them credentials."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not (_ENV_DENY.search(k) and not _ENV_KEEP.match(k)) and not _CRED_URL.search(v)
    }
    env.update(
        NO_COLOR="1",
        NO_UPDATE_NOTIFIER="1",
        NPM_CONFIG_IGNORE_SCRIPTS="true",
        NPM_CONFIG_UPDATE_NOTIFIER="false",
    )
    return env


def _json(text: str) -> object | None:
    try:
        return json.loads(text)
    except (ValueError, RecursionError, MemoryError):
        return None


def _last_line(proc: _Proc) -> str:
    for blob in (proc.err, proc.out):
        lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
        if lines:
            return _safe(_redact(lines[-1]), 160)
    return f"exit code {proc.rc}"


def _missing(name: str) -> ToolRun:
    return ToolRun(name, "not installed", "", 0, _INSTALL_HINTS[name])


def _failed(name: str, proc: _Proc) -> ToolRun:
    if proc.rc == 124:
        return ToolRun(name, "timeout", "killed after the time limit")
    if proc.truncated:
        return ToolRun(name, "error", "output over the size cap; not parsed")
    return ToolRun(name, "error", _last_line(proc))


def _exec(root: Path, argv: list[str], timeout_s: float) -> _Proc:
    return _run_capped(argv, cwd=root, timeout_s=timeout_s, env=_tool_env())


def _run_gitleaks(root: Path, timeout_s: float) -> ToolRun:
    name = "gitleaks"
    exe = _which("gitleaks", os.path.realpath(root))
    if not exe:
        return _missing(name)
    # `dir` replaced `detect --no-git` in v8.19; --redact keeps secrets out of the report, stdout carries it
    flags = [
        "--no-banner",
        "--redact",
        "--report-format",
        "json",
        "--report-path",
        "-",
        "--exit-code",
        "0",
        "--log-level",
        "error",
        "--max-target-megabytes",
        "5",
    ]
    probe = _exec(root, [exe, "dir", "--help"], min(timeout_s, 20))
    argv = (
        [exe, "dir", ".", *flags]
        if probe.rc == 0
        else [exe, "detect", "--source", ".", "--no-git", *flags]
    )
    proc = _exec(root, argv, timeout_s)
    if proc.rc != 0 or proc.truncated:
        return _failed(name, proc)
    data = _json(proc.out) if proc.out.strip() else []
    if not isinstance(data, list):
        return ToolRun(name, "error", "unexpected output")
    if not data:
        return ToolRun(name, "ok", "no leaks found in the working tree (history not scanned)")
    rules = Counter(_safe(d.get("RuleID", "?"), 40) for d in data if isinstance(d, dict))
    where = [
        f"{_safe(d.get('File', '?'), 80)}:{d.get('StartLine', 0)}"
        for d in data[:3]
        if isinstance(d, dict)
    ]
    top = ", ".join(f"{r} x{n}" for r, n in rules.most_common(3))
    return ToolRun(
        name,
        "findings",
        f"{len(data)} potential secret(s), values redacted ({top}); e.g. {', '.join(where)}",
        len(data),
    )


def _run_pip_audit(root: Path, timeout_s: float) -> ToolRun:
    name = "pip-audit"
    reqs = sorted(
        p.name
        for p in root.glob("requirements*.txt")
        if p.is_file() and re.fullmatch(r"requirements[\w.-]*\.txt", p.name)
    )[:6]
    if not reqs:
        if (root / "pyproject.toml").is_file():
            return ToolRun(
                name,
                "skipped",
                "pyproject.toml only: pip-audit would build the project (runs its build backend); "
                "audit a pinned requirements file instead",
            )
        return ToolRun(name, "skipped", "no requirements*.txt or pyproject.toml")
    exe = _which("pip-audit", os.path.realpath(root))
    if not exe:
        return _missing(name)
    argv = [exe]
    for r in reqs:
        argv += ["-r", r]
    # --no-deps/--disable-pip: audit exactly the pinned versions, never resolve or build anything
    argv += ["--no-deps", "--disable-pip", "--format", "json", "--progress-spinner", "off"]
    proc = _exec(root, argv, timeout_s)
    data = _json(proc.out) if not proc.truncated else None
    deps = data.get("dependencies") if isinstance(data, dict) else data
    if not isinstance(deps, list):
        res = _failed(name, proc)
        if res.status == "error":
            res.summary += " (requirements must be fully pinned: name==version)"
        return res
    vulns = [
        v
        for d in deps
        if isinstance(d, dict)
        for v in (d.get("vulns") or [])
        if isinstance(v, dict)
    ]
    pkgs = sum(1 for d in deps if isinstance(d, dict) and d.get("vulns"))
    if not vulns:
        return ToolRun(name, "ok", f"no known vulnerabilities in {len(deps)} pinned package(s)")
    ids = ", ".join(_safe(v.get("id", "?"), 30) for v in vulns[:3])
    return ToolRun(
        name,
        "findings",
        f"{len(vulns)} known vulnerabilit(ies) in {pkgs} package(s): {ids}",
        len(vulns),
    )


def _run_npm_audit(root: Path, timeout_s: float) -> ToolRun:
    name = "npm audit"
    if not (root / "package-lock.json").is_file():
        return ToolRun(name, "skipped", "no package-lock.json")
    try:
        with open(root / ".npmrc", "rb") as fh:
            npmrc = fh.read(64 * 1024)
    except OSError:
        npmrc = b""
    if b"${" in npmrc:
        # `registry=https://host/${NAME}` or `_authToken=${NAME}` sends an environment variable wherever the repo
        # says; the scrubbed environment already removes the obvious ones, so do not run npm at all
        return ToolRun(
            name,
            "skipped",
            "the repository's .npmrc expands environment variables (${...}); not run, review it first",
        )
    exe = _which("npm", os.path.realpath(root))
    if not exe:
        return _missing(name)
    proc = _exec(root, [exe, "audit", "--omit=dev", "--package-lock-only", "--json"], timeout_s)
    data = _json(proc.out) if not proc.truncated else None
    if not isinstance(data, dict):
        return _failed(name, proc)
    if isinstance(data.get("error"), dict):
        err = data["error"]
        return ToolRun(
            name,
            "error",
            _safe(_redact(f"{err.get('code', '')} {err.get('summary', '')}".strip()), 160),
        )
    meta = (
        (data.get("metadata") or {}).get("vulnerabilities")
        if isinstance(data.get("metadata"), dict)
        else None
    )
    if isinstance(meta, dict):
        total = int(meta.get("total", 0) or 0)
        detail = ", ".join(f"{k} {meta.get(k, 0)}" for k in ("critical", "high", "moderate", "low"))
    else:
        vulns = data.get("vulnerabilities")
        total = len(vulns) if isinstance(vulns, dict) else 0
        detail = "severity breakdown unavailable"
    if not total:
        return ToolRun(name, "ok", "no known vulnerabilities in production dependencies")
    return ToolRun(name, "findings", f"{total} vulnerable package(s) ({detail})", total)


def _run_osv(root: Path, timeout_s: float) -> ToolRun:
    name = "osv-scanner"
    exe = _which("osv-scanner", os.path.realpath(root))
    if not exe:
        return _missing(name)
    proc = _exec(root, [exe, "scan", "--format", "json", "-r", "."], timeout_s)
    if proc.rc == 128:
        return ToolRun(name, "skipped", "no supported lockfiles found")
    data = _json(proc.out) if not proc.truncated else None
    if not isinstance(data, dict) or proc.rc not in (0, 1):
        return _failed(name, proc)
    ids: set[str] = set()
    pkgs = 0
    for res in data.get("results") or []:
        for pkg in res.get("packages") or [] if isinstance(res, dict) else []:
            vulns = pkg.get("vulnerabilities") or [] if isinstance(pkg, dict) else []
            pkgs += bool(vulns)
            ids.update(_safe(v.get("id", "?"), 30) for v in vulns if isinstance(v, dict))
    if not ids:
        return ToolRun(name, "ok", "no known vulnerabilities in the lockfiles found")
    return ToolRun(
        name,
        "findings",
        f"{len(ids)} known vulnerability id(s) in {pkgs} package(s): {', '.join(sorted(ids)[:3])}",
        len(ids),
    )


def run_tools(root: Path | str, *, timeout_s: float = 60) -> list[ToolRun]:
    """gitleaks, pip-audit, npm audit and osv-scanner when installed (never installs anything).

    `timeout_s` applies to each tool. These tools reach their own services (npm registry, OSV): that is theirs,
    this module opens no connection itself.
    """
    out: list[ToolRun] = []
    for name, fn in (
        ("gitleaks", _run_gitleaks),
        ("pip-audit", _run_pip_audit),
        ("npm audit", _run_npm_audit),
        ("osv-scanner", _run_osv),
    ):
        try:
            out.append(fn(Path(root), float(timeout_s)))
        except Exception as exc:  # fail open
            out.append(ToolRun(name, "error", type(exc).__name__))
    return out


# ---------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------
def _unchecked(tools: list[ToolRun]) -> str:
    items = [
        "whether a flagged command is really malicious (these are pattern checks, not proof)",
        "git history (only the working tree is read)",
        "the code of installed plugins and MCP servers (never fetched or run)",
    ]
    if not tools:
        items.append(
            "dependency and secret scanners were not run (gitleaks, pip-audit, npm audit, osv-scanner)"
        )
    for t in tools:
        if t.status in ("not installed", "skipped", "error", "timeout"):
            items.append(f"{t.name} ({t.status})")
    return "Cannot be checked here: " + "; ".join(items) + "."


def report(
    root: Path | str,
    *,
    tools: bool = False,
    as_json: bool = False,
    strict: bool = False,
    timeout_s: float = 60,
) -> tuple[str, int]:
    """What `claude-hr-ctl audit` prints and its exit code.

    Exit 2 (with or without `strict`): the audit could not run (no such directory, unreadable root, a whole check
    crashed), see `could_not_run`: a vacuous "no findings" must not read as a pass. Otherwise, under `strict`, exit 1
    when there is a high or medium finding (which includes an incomplete scan and agent files that could not be
    parsed) or an external scanner reported findings; else 0 (a plain audit that ran is informational).
    """
    root = Path(root)
    findings = scan_repo(root)
    runs = run_tools(root, timeout_s=timeout_s) if tools else []
    text = render(findings, runs, as_json=as_json, root=root)
    if could_not_run(findings):
        return text, 2
    failing = bool(blocking(findings)) or any(r.status == "findings" for r in runs)
    return text, 1 if strict and failing else 0


def render(
    findings: list[Finding],
    tools: list[ToolRun] | None = None,
    *,
    as_json: bool = False,
    root: Path | str | None = None,
) -> str:
    """Human text grouped by severity then kind (with the fix hint), or the stable JSON schema."""
    ordered = _sorted(findings)
    tools = list(tools or [])
    tally = counts(ordered)
    if as_json:
        return json.dumps(
            {
                "root": str(root) if root is not None else "",
                "counts": tally,
                "findings": [asdict(f) for f in ordered],
                "tools": [asdict(t) for t in tools],
            },
            indent=2,
        )
    head = f"claude-hr audit: {root}" if root is not None else "claude-hr audit"
    lines = [
        head,
        "Findings: "
        + ", ".join(f"{tally[s]} {s}" for s in SEVERITIES)
        + f" ({len(ordered)} total)",
    ]
    if not ordered:
        lines += ["", "No findings."]
    for sev in SEVERITIES:
        group = [f for f in ordered if f.severity == sev]
        if not group:
            continue
        lines += ["", sev.upper()]
        for kind in sorted({f.kind for f in group}):
            lines.append(f"  {kind}  fix: {hint(kind)}")
            for f in group:
                if f.kind == kind:
                    loc = f"{f.path}:{f.line}" if f.line else f.path
                    lines.append(f"    {loc}  {f.detail}")
    if tools:
        lines += ["", "External scanners"]
        for t in tools:
            status = t.status + (f" ({t.count})" if t.status == "findings" else "")
            extra = t.summary or (f"hint: {t.hint}" if t.hint else "")
            lines.append(f"  {t.name:<12} {status:<14} {extra}".rstrip())
    lines += ["", _unchecked(tools)]
    return "\n".join(lines)
