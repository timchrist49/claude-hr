"""Read-only, zero-token audit of a repository: is it safe to point an agent at?

Nothing from the audited repo is ever executed, imported or fetched: files are only read (at most 512 KB each, never
binaries, never through a symlink that leaves the root). Findings never echo repo text: they carry a fixed label, a
sanitised identifier, line numbers and (for secrets) a masked prefix, so the report itself cannot carry an injection
into a session that reads it. External scanners (gitleaks, pip-audit, npm audit, osv-scanner) run only when they are
already on PATH, with a hard timeout, a scrubbed environment and capped output; nothing is ever installed.

Public entry points never raise: problems become `info` findings or a `ToolRun` with status "error".
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import posixpath
import re
import stat
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from . import procutil

MAX_FILE_BYTES = 512 * 1024
MAX_WALK_ENTRIES = 50_000
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SECRET_FINDINGS = 200
MAX_TOOL_OUTPUT = 2 * 1024 * 1024
PRUNE_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache"}
)
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
        "Delete it; these variables load code into every process Claude starts.",
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
        "Review the command; a repo-supplied status line runs on every refresh.",
    ),
    "permissions-broad-allow": (
        "agent-config",
        "Remove the allow rule; keep Bash approvals narrow (specific commands, never wildcards or shells).",
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
        "Fix the JSON so Claude Code and this audit can read it.",
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
        "Review the file by hand: it is binary, over 512 KB or unreadable, so it was not scanned.",
    ),
    "hidden-unicode": (
        "hidden-unicode",
        "Strip the invisible characters (find them with grep -nP on U+200B-200D, U+202A-202E, "
        "U+2066-2069, U+E0000-E007F) and review who added them.",
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
        "Never check out or run PR code in pull_request_target/issue_comment workflows or expose "
        "secrets to them; keep permissions minimal.",
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
        "Scan the rest with gitleaks; this scan stops at a fixed size and entry budget.",
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
    "audit-error": ("meta", "Report it; the other checks still ran."),
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
    """What `audit --strict` should fail on: every high and medium finding."""
    return [f for f in findings if f.severity in ("high", "medium")]


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_@./:+\-\[\]()*=#~]")


def _safe(text: object, limit: int = 60) -> str:
    """Printable ASCII, no spaces, short: identifiers from the repo (names, hosts, paths) are untrusted data,
    and without spaces they cannot read as a sentence addressed to whoever reads the report."""
    s = _SAFE_CHARS.sub("?", re.sub(r"\s+", "_", str(text).strip()))
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _mask(secret: str) -> str:
    """First characters only, and never more than a quarter of the value."""
    return secret[: min(4, len(secret) // 4)] + "\u2026"


def _first_lines(lines: list[int], n: int = 8) -> str:
    shown = ", ".join(str(x) for x in lines[:n])
    return shown + (f" (+{len(lines) - n} more)" if len(lines) > n else "")


def _finding(sev: str, kind: str, path: str, detail: str, line: int = 0) -> Finding:
    # a path stays usable (spaces kept) but cannot carry control or non-ASCII characters into the report
    return Finding(sev, kind, re.sub(r"[^\x20-\x7e]", "?", path)[:200], detail, line)


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


def _token_shaped(value: str) -> bool:
    return any(pat.search(value) for pat, _ in _token_patterns())


def _redact(text: str) -> str:
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
class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.real = os.path.realpath(root)
        self.files: list[str] = []
        self.fileset: set[str] = set()
        self.escaped: list[str] = []  # symlinks that leave the root: never read
        self.truncated = False
        self._cache: dict[str, tuple[str | None, str]] = {}
        self._git: tuple[set[str], set[str]] | None | bool = False  # False = not asked yet

    def inside(self, real: str) -> bool:
        return _within(real, self.real)

    def walk(self) -> None:
        stack: list[tuple[str, str, frozenset[str]]] = [
            (str(self.root), "", frozenset({os.path.normcase(self.real)}))
        ]
        count = 0
        while stack:
            base, prefix, ancestors = stack.pop()
            try:
                with os.scandir(base) as it:
                    entries = sorted(it, key=lambda e: e.name)
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
                    if e.name in PRUNE_DIRS:
                        continue
                    real = os.path.realpath(e.path)
                    if not self.inside(real):
                        self.escaped.append(rel)
                    elif os.path.normcase(real) not in ancestors:
                        subdirs.append((e.path, rel + "/", ancestors | {os.path.normcase(real)}))
                elif is_file and e.name != ".git":
                    if e.is_symlink() and not self.inside(os.path.realpath(e.path)):
                        self.escaped.append(rel)
                        continue
                    count += 1
                    if count > MAX_WALK_ENTRIES:
                        self.truncated = True
                        return
                    self.files.append(rel)
                    self.fileset.add(rel)
            stack.extend(reversed(subdirs))

    def read(self, rel: str, *, cache: bool = True) -> tuple[str | None, str]:
        """(text, reason): reason is ok | binary | too-large | unreadable | outside."""
        hit = self._cache.get(rel)
        if hit is not None:
            return hit
        result = self._read(rel)
        if cache:
            if len(self._cache) >= 400:
                self._cache.pop(next(iter(self._cache)))
            self._cache[rel] = result
        return result

    def _read(self, rel: str) -> tuple[str | None, str]:
        path = os.path.join(str(self.root), *rel.split("/"))
        try:
            real = os.path.realpath(path)
            if not self.inside(real):
                return None, "outside"
            if not stat.S_ISREG(os.stat(real).st_mode):
                return None, "unreadable"
            with open(real, "rb") as fh:
                data = fh.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None, "unreadable"
        if len(data) > MAX_FILE_BYTES:
            return None, "too-large"
        if b"\0" in data[:8192]:
            return None, "binary"
        return data.decode("utf-8", errors="replace"), "ok"

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
SETTINGS_RE = re.compile(r"(?:^|/)\.claude/settings(?:\.local)?\.json$")
MCP_RE = re.compile(r"(?:^|/)\.mcp\.json$")
SKILL_RE = re.compile(r"(?:^|/)\.claude/skills/[^/]+/SKILL\.md$")
AGENT_FILE_RE = re.compile(r"(?:^|/)\.claude/(?:agents|commands|rules)/.+\.md$")
CLAUDE_MD_RE = re.compile(
    r"(?:^|/)(?:CLAUDE(?:\.local)?|AGENTS|GEMINI)\.md$|(?:^|/)\.claude/.+\.md$"
)
README_RE = re.compile(r"(?:^|/)README(?:\.[A-Za-z]+)?$", re.I)
LLMS_RE = re.compile(r"(?:^|/)llms[\w.-]*\.txt$", re.I)
COPILOT_RE = re.compile(r"(?:^|/)(?:\.github/copilot-instructions\.md|\.cursorrules)$")
WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")


_SURFACE_DIR_RE = re.compile(r"(?:^|/)\.claude(?:/(?:skills|agents|commands|rules|hooks))?$")


def _is_agent_surface(rel: str) -> bool:
    return bool(
        SETTINGS_RE.search(rel)
        or MCP_RE.search(rel)
        or SKILL_RE.search(rel)
        or AGENT_FILE_RE.search(rel)
        or CLAUDE_MD_RE.search(rel)
        or _SURFACE_DIR_RE.search(rel)
    )


def _load(repo: _Repo, rel: str, out: list[Finding]) -> str | None:
    """Read an agent-facing file; a file we could not read is reported, not skipped silently."""
    text, reason = repo.read(rel)
    if text is None:
        why = {
            "binary": "binary or non-UTF-8 content",
            "too-large": "over 512 KB",
            "unreadable": "unreadable",
            "outside": "resolves outside the repository",
        }.get(reason, reason)
        out.append(_finding("info", "file-not-scanned", rel, f"not scanned: {why}"))
    return text


# ---------------------------------------------------------------------------------------------
# (1) command analysis shared by hooks, status line, helpers, MCP servers, referenced scripts, skills
# ---------------------------------------------------------------------------------------------
_FETCH = r"(?:curl|wget|invoke-webrequest|invoke-restmethod|iwr|irm)"
_PIPE_TO_SHELL = re.compile(
    r"\b" + _FETCH + r"(?:\.exe)?\b[^|\n]*\|\s*(?:sudo\s+(?:-\w+\s+)*)?"
    r"(?:(?:ba|z|da|k|c)?sh|python[\d.]*|perl|ruby|node|php|iex|invoke-expression|pwsh|powershell)\b"
    r"|\|\s*(?:iex|invoke-expression)\b"
    r"|\b(?:iex|invoke-expression)\b"
    r"|\b(?:(?:ba|z|da|k)?sh|eval|source)\s+(?:-c\s+)?[\"']?(?:\$\(|`)\s*" + _FETCH + r"\b"
    r"|\b(?:(?:ba|z)?sh|source)\s+<\(\s*" + _FETCH + r"\b",
    re.I,
)
_NETWORK = re.compile(
    r"\b"
    + _FETCH
    + r"(?:\.exe)?\b|\bnew-object\s+net\.webclient\b|\bdownload(?:string|file|data)\b"
    r"|\bstart-bitstransfer\b|\bbitsadmin\b|\bcertutil\b[^\n]*-urlcache|\b(?:ncat|netcat|socat)\b"
    r"|/dev/tcp/",
    re.I,
)
_BASE64 = re.compile(
    r"\bbase64\s+(?:-\w+\s+)*(?:-d|-D|--decode)\b|\bfrombase64string\b|\bcertutil\b[^\n]*-decode"
    r"|\bb64decode\b|\batob\s*\(|\bopenssl\s+(?:enc|base64)\b[^\n]*\s-d\b"
    r"|\s-(?:enc|encodedcommand|ec)\s+[A-Za-z0-9+/=]{16,}",
    re.I,
)
_OUTSIDE = (
    r"(?:~(?=[/\\\s\"']|$)|\$\{?HOME\}?|\$env:(?:USERPROFILE|HOME|APPDATA|LOCALAPPDATA)"
    r"|%(?:USERPROFILE|HOME|APPDATA|LOCALAPPDATA)%|/(?:etc|usr|var|root|opt|Library|boot|bin|sbin|home)\b"
    r"|\.\./|[A-Za-z]:[\\/](?:Windows|Users|ProgramData|Program Files))"
)
_WRITE_OUTSIDE = re.compile(
    "|".join(
        [
            r">>?\s*[\"']?" + _OUTSIDE,
            r"\btee\b(?:\s+-\w+)*\s+[\"']?" + _OUTSIDE,
            # cp/mv/ln: only the LAST argument is a destination
            r"\b(?:cp|mv|ln|install|rsync|copy|move|xcopy)\b(?:\s+-\S+)*(?:\s+\S+)+?\s+[\"']?"
            + _OUTSIDE
            + r"\S*[\"']?\s*(?:$|[;&|])",
            r"\b(?:set-content|add-content|out-file|new-item|copy-item|move-item)\b[^\n;|&]*?"
            + _OUTSIDE,
        ]
    ),
    re.I,
)
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
_SCRIPT_REF = re.compile(
    r"[\w$%{}./\\~:-]*[\w}%]\.(?:sh|bash|zsh|py|js|mjs|cjs|ts|ps1|cmd|bat|rb|pl)\b", re.I
)
_PROJECT_VARS = (
    "${CLAUDE_PROJECT_DIR}",
    "$CLAUDE_PROJECT_DIR",
    "%CLAUDE_PROJECT_DIR%",
    "$env:CLAUDE_PROJECT_DIR",
)


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


def _analyze_command(cmd: str) -> list[tuple[str, str, str]]:
    """(kind, severity, label) for one command line. Labels are fixed text, never the command itself."""
    out: list[tuple[str, str, str]] = []
    if _PIPE_TO_SHELL.search(cmd):
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
    if _BASE64.search(cmd):
        out.append(
            (
                "cmd-base64-decode",
                "high",
                "decodes base64 data or runs an encoded PowerShell command",
            )
        )
    m = _WRITE_OUTSIDE.search(cmd)
    if m:
        sev = "high" if _PERSIST.search(cmd[m.start() : m.end() + 60]) else "medium"
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
        for tok in dict.fromkeys(_SCRIPT_REF.findall(cmd)):
            script = _resolve_script(repo, tok)
            if script and found < 3:
                found += 1
                out.extend(_script_findings(repo, script, source))
    return out


def _resolve_script(repo: _Repo, token: str) -> str | None:
    t = token.strip("\"'")
    had_var = False
    for var in _PROJECT_VARS:
        if t.startswith(var):
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
    return norm if norm in repo.fileset else None


def _script_findings(repo: _Repo, rel: str, source: str) -> list[Finding]:
    """A hook usually just names a script: read that script (never run it) and apply the same checks."""
    text, _ = repo.read(rel)
    if text is None:
        return []
    first: dict[str, tuple[int, str, str]] = {}
    for n, line in enumerate(text.split("\n"), 1):
        for kind, sev, label in _analyze_command(line):
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
_ENDPOINT_ENV = re.compile(r"(?i)^(?:ANTHROPIC_(?:[A-Z0-9]+_)*BASE_URL|(?:HTTPS?|ALL)_PROXY)$")
_CREDENTIAL_ENV = re.compile(r"^(?:ANTHROPIC_(?:API_KEY|AUTH_TOKEN)|CLAUDE_CODE_OAUTH_TOKEN)$")
_INJECTION_ENV = frozenset(
    {"LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS", "BASH_ENV", "PYTHONSTARTUP"}
)
_HELPER_KEYS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "otelHeadersHelper")
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


def _rule_severity(rule: str, *, bare_ok: bool) -> str | None:
    """high for an unrestricted shell grant, medium for a wildcard over a shell/network/interpreter verb."""
    m = re.fullmatch(r"([A-Za-z_][\w.\-]*)(?:\((.*)\))?|\*", rule.strip(), re.S)
    if not m:
        return None
    tool, spec = m.group(1), m.group(2)
    if tool is None:
        return "high"
    if tool.lower() not in ("bash", "powershell"):
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


def _check_settings(repo: _Repo, rel: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return [_finding("info", "config-unparseable", rel, "not valid JSON object: not audited")]
    local = rel.endswith("settings.local.json")

    events: list[str] = []
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
            out.extend(_command_findings(repo, cmd, rel, f"{event} hook"))
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

    sl = data.get("statusLine")
    sl_cmd = sl.get("command") if isinstance(sl, dict) else sl
    if isinstance(sl_cmd, str) and sl_cmd.strip():
        out.append(
            _finding(
                "medium", "statusline-command", rel, "statusLine runs a command on every refresh"
            )
        )
        out.extend(_command_findings(repo, sl_cmd, rel, "statusLine"))
    for key in _HELPER_KEYS:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out.append(
                _finding(
                    "high",
                    "credential-helper",
                    rel,
                    f"{key} runs a command that handles credentials",
                )
            )
            out.extend(_command_findings(repo, val, rel, key))

    env = data.get("env")
    for key, val in env.items() if isinstance(env, dict) else ():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        k = _safe(key, 40)
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
        elif key in _INJECTION_ENV:
            out.append(
                _finding(
                    "high",
                    "env-code-injection",
                    rel,
                    f"env sets {k}: loads code into child processes",
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

    perms = data.get("permissions")
    if isinstance(perms, dict):

        def cap(sev: str) -> str:
            return "low" if local else sev  # a local file holds the user's own choices

        rules = perms.get("allow")
        by_sev: dict[str, list[str]] = {}
        for rule in rules if isinstance(rules, list) else ():
            if isinstance(rule, str):
                sev = _rule_severity(rule, bare_ok=True)
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
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return [_finding("info", "config-unparseable", rel, "not valid JSON object: not audited")]
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
_TOOL_RE = re.compile(r"[A-Za-z_][\w.\-]*(?:\([^)\n]*\))?|\*")
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
    for cmd in re.findall(r"(?m)^\s*-?\s*command\s*:\s*(.+?)\s*$", fm):
        out.extend(
            _command_findings(repo, cmd.strip("\"'"), rel, "frontmatter hook", scripts=False)
        )

    lines_by_sev: dict[str, list[int]] = {}
    labels: dict[str, set[str]] = {}
    inline = [(m.start(), m.group(1)) for m in _INLINE_CMD.finditer(text)]
    inline += [(m.start(1), m.group(1)) for m in _FENCE_CMD.finditer(text)]
    for pos, cmd in inline:
        flagged = _analyze_command(cmd)
        if flagged:
            sev = "high"
            labels.setdefault(sev, set()).update(label for _, _, label in flagged)
        else:
            sev = "medium" if _ARG_INTERP.search(cmd) else "low"
        lines_by_sev.setdefault(sev, []).append(text.count("\n", 0, pos) + 1)
    for sev, lines in lines_by_sev.items():
        what = {
            "high": "; ".join(sorted(labels.get(sev, ()))),
            "medium": "interpolates arguments into a shell command",
            "low": "runs a shell command when the skill loads",
        }[sev]
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
            text = _load(repo, rel, out)
            if text is not None:
                out.extend(_check_settings(repo, rel, text))
        elif MCP_RE.search(rel):
            text = _load(repo, rel, out)
            if text is not None:
                out.extend(_check_mcp(repo, rel, text))
        elif SKILL_RE.search(rel) or AGENT_FILE_RE.search(rel):
            text = _load(repo, rel, out)
            if text is not None:
                out.extend(_check_skill(repo, rel, text))
        elif CLAUDE_MD_RE.search(rel) or README_RE.search(rel) or LLMS_RE.search(rel):
            _load(
                repo, rel, out
            )  # reports an unreadable file; the text checks run in their own groups
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
_HIDDEN_OTHER = re.compile(
    "[\u200b-\u200d\u2060-\u2064\ufeff\u180e\u202a-\u202e\u2066-\u2069\U000e0100-\U000e01ef]"
)
_TAGS = re.compile("[\U000e0000-\U000e007f]+")
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


def _emojiish(cp: int) -> bool:
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2190 <= cp <= 0x23FF
        or 0x2600 <= cp <= 0x27BF
        or 0x2900 <= cp <= 0x297F
        or 0x2B00 <= cp <= 0x2BFF
        or 0xFE00 <= cp <= 0xFE0F
    )


def _hidden_category(cp: int) -> str:
    if 0xE0100 <= cp <= 0xE01EF:
        return "variation-selector"
    if 0x202A <= cp <= 0x202E or 0x2066 <= cp <= 0x2069:
        return "bidi-control"
    return "zero-width"


def _benign_hidden(text: str, idx: int, cp: int) -> bool:
    if cp == 0xFEFF and idx == 0:
        return True  # a byte-order mark
    if cp in (
        0x200C,
        0x200D,
    ):  # joiners are required by emoji sequences and by Persian/Indic scripts
        near = [ord(text[j]) for j in (idx - 1, idx + 1) if 0 <= j < len(text)]
        return any(_emojiish(c) or 0x0590 <= c <= 0x0DFF for c in near)
    return False


def hidden_unicode(text: str) -> dict[str, tuple[int, list[int], set[int]]]:
    """category -> (count, sorted lines, code points). Benign uses (BOM, emoji joiners, flag tags) are ignored."""
    cats: dict[str, tuple[int, set[int], set[int]]] = {}
    newlines: list[int] = []

    def line_of(off: int) -> int:
        if not newlines:
            newlines.extend(m.start() for m in re.finditer("\n", text))
        return bisect_left(newlines, off) + 1

    def add(cat: str, n: int, off: int, cps: set[int]) -> None:
        count, lines, seen = cats.get(cat, (0, set(), set()))
        lines.add(line_of(off))
        cats[cat] = (count + n, lines, seen | cps)

    for m in _HIDDEN_OTHER.finditer(text):
        cp = ord(m.group())
        if not _benign_hidden(text, m.start(), cp):
            add(_hidden_category(cp), 1, m.start(), {cp})
    for m in _TAGS.finditer(text):
        start, end = m.span()
        if start > 0 and text[start - 1] == "\U0001f3f4" and ord(text[end - 1]) == 0xE007F:
            continue  # an emoji flag sequence (England, Scotland, Wales)
        add("tag-character", end - start, start, {ord(c) for c in text[start:end]})
    return {k: (n, sorted(lines), cps) for k, (n, lines, cps) in cats.items()}


def _check_hidden_unicode(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if not any(p.search(rel) for p in _HIDDEN_SCOPE):
            continue
        text, _ = repo.read(rel)
        if text is None:
            continue
        cats = hidden_unicode(text)
        if not cats:
            continue
        parts = []
        first = 10**9
        for cat in sorted(cats):
            n, lines, cps = cats[cat]
            first = min(first, lines[0])
            codes = ", ".join(f"U+{cp:04X}" for cp in sorted(cps)[:6])
            parts.append(f"{cat} x{n} at line(s) {_first_lines(lines)} ({codes})")
        out.append(
            _finding(
                "high",
                "hidden-unicode",
                rel,
                "invisible characters (not shown): " + "; ".join(parts),
                first,
            )
        )
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
_TEXT_SCOPE = (
    README_RE,
    LLMS_RE,
    COPILOT_RE,
    re.compile(r"(?:^|/)(?:CLAUDE(?:\.local)?|AGENTS|GEMINI)\.md$"),
)


def _in_text_scope(rel: str) -> bool:
    if any(p.search(rel) for p in _TEXT_SCOPE):
        return True
    return rel.startswith("docs/") and rel.lower().endswith(".md")


def _check_agent_text(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    for rel in repo.files:
        if not _in_text_scope(rel):
            continue
        text, _ = repo.read(rel)
        if text is None:
            continue
        lines = text.split("\n")
        directed: list[int] = []
        piped = False
        address_only: list[int] = []
        for i, line in enumerate(lines):
            if not any(p.search(line) for p in _ADDRESS):
                continue
            window = "\n".join(lines[max(0, i - 2) : i + 7])
            if _PIPE_TO_SHELL.search(window):
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


# ---------------------------------------------------------------------------------------------
# (4) GitHub workflows (plain-text lint, no YAML library)
# ---------------------------------------------------------------------------------------------
_USES = re.compile(r"""^\s*(?:-\s+)?uses\s*:\s*["']?([^\s"'#]+)["']?\s*$""")
_PRIVILEGED = ("pull_request_target", "issue_comment")
_HEAD_CHECKOUT = re.compile(
    r"\bref\s*:.*(?:pull_request\.head|head_ref|refs/pull/|head\.sha|head\.ref|workflow_run\.head)"
    r"|\bgh\s+pr\s+checkout\b|refs/pull/|\bpull/\d+/head\b"
)
_SECRET_USE = re.compile(r"\bsecrets\.(?!GITHUB_TOKEN\b)\w+|\bsecrets\s*:\s*inherit\b")
_EXPR = re.compile(r"\$\{\{\s*(.+?)\s*\}\}", re.S)
_UNTRUSTED_EXPR = re.compile(
    r"^(?:github\.head_ref|github\.event\.(?:[\w\[\]*'\"-]+\.)*?"
    r"(?:title|body|ref|label|message|page_name|head_branch|default_branch|description|email|"
    r"author\.name|committer\.name|pusher\.name))(?![\w])"
)
_RUN_LINE = re.compile(r"^(\s*)(?:-\s+)?run\s*:\s*(.*)$")


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
    blocks = []
    for i, line in enumerate(code):
        m = _RUN_LINE.match(line)
        if not m:
            continue
        col = line.index("run")
        rest = m.group(2).strip()
        body = [] if re.match(r"^[|>][+-]?\d*$", rest) else [rest]
        for nxt in code[i + 1 :]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= col:
                break
            body.append(nxt)
        blocks.append((i + 1, "\n".join(body)))
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
        head = next((n for n, ln in enumerate(code, 1) if _HEAD_CHECKOUT.search(ln)), 0)
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
        exprs = _EXPR.findall(body)
        injected = [e for e in exprs if _UNTRUSTED_EXPR.match(e.strip())]
        secrets = [e for e in exprs if e.strip().startswith("secrets.")]
        if injected:
            extra = " together with an inline secret" if secrets else ""
            out.append(
                _finding(
                    "high",
                    "workflow-script-injection",
                    rel,
                    f"run step expands attacker-controllable context ({_safe(injected[0].strip(), 60)}) "
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
                    f"run step expands {_safe(secrets[0].strip(), 40)} inline",
                    start,
                )
            )
        if _PIPE_TO_SHELL.search(body):
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
            text, _ = repo.read(rel)
            if text is not None:
                out.extend(_workflow_findings(rel, text))
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
    name = posixpath.basename(rel)
    if not (name == ".env" or name.startswith(".env.") or name.endswith(".env")):
        return False
    return not name.endswith(_ENV_EXAMPLE) and not name.endswith(".schema")


def _env_values(text: str) -> list[tuple[int, str, str]]:
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        m = _ENV_LINE.match(line.rstrip("\r"))
        if m and not line.lstrip().startswith("#"):
            value = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("\"'")
            out.append((n, m.group(1), value))
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


def _check_secrets(repo: _Repo) -> list[Finding]:
    out: list[Finding] = []
    patterns = _token_patterns()
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
        if budget <= 0:
            out.append(
                _finding(
                    "info",
                    "scan-truncated",
                    rel,
                    "scan budget (64 MB) used up; remaining files not scanned",
                )
            )
            break
        text, reason = repo.read(rel, cache=False)
        if text is None:
            skipped_large += reason == "too-large"
            continue
        budget -= len(text)
        low = text.lower()
        if any(tok in text for tok in _QUICK) or "aws_secret" in low:
            taken: list[tuple[int, int]] = []
            found = 0
            for pat, label in patterns:
                for m in pat.finditer(text):
                    span = m.span()
                    if any(a < span[1] and span[0] < b for a, b in taken) or found >= 20:
                        continue
                    taken.append(span)
                    found += 1
                    n_secrets += 1
                    line = text.count("\n", 0, span[0]) + 1
                    out.append(
                        _finding(
                            "high",
                            "secret-token",
                            rel,
                            f"{label} {_mask(m.group(0))}",
                            line,
                        )
                    )
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
            out.append(_finding(sev, "env-file-tracked", rel, what))
        for rel in repo.files:
            base = posixpath.basename(rel)
            if rel in tracked and (
                base == "CLAUDE.local.md" or (base == "settings.local.json" and ".claude/" in rel)
            ):
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
            present = "CLAUDE.local.md" in repo.fileset
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
            return [_finding("info", "root-missing", str(root), "not a directory")]
        repo = _Repo(root)
        repo.walk()
    except (OSError, ValueError) as exc:
        return [
            _finding(
                "info",
                "audit-error",
                str(root),
                f"could not read the repository: {type(exc).__name__}",
            )
        ]
    out: list[Finding] = []
    if repo.truncated:
        out.append(
            _finding(
                "info",
                "scan-truncated",
                ".",
                f"stopped after {MAX_WALK_ENTRIES} files; the rest was not scanned",
            )
        )
    for group, check in _CHECKS:
        if group not in groups:
            continue
        try:
            out.extend(check(repo))
        except Exception as exc:  # fail open: one broken check must not hide the others
            out.append(
                _finding("info", "audit-error", ".", f"{group} check failed: {type(exc).__name__}")
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
_ENV_SECRETS = re.compile(
    r"(?i)token|secret|passw|credential|api[_-]?key|auth|private[_-]?key|^ANTHROPIC_|^CLAUDE_CODE_"
    r"|^AWS_|^AZURE_|^GOOGLE_|^GCP_"
)


def _tool_env() -> dict[str, str]:
    """Scanners read the repo's own config (.npmrc registry, ...): never hand them credentials."""
    env = {k: v for k, v in os.environ.items() if not _ENV_SECRETS.search(k)}
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
    except ValueError:
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

    Under `strict` the exit code is 1 when there is a high or medium finding or an external scanner reported
    findings; otherwise it is 0 (a plain audit is informational and never fails).
    """
    root = Path(root)
    findings = scan_repo(root)
    runs = run_tools(root, timeout_s=timeout_s) if tools else []
    text = render(findings, runs, as_json=as_json, root=root)
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
