"""Credential hygiene checks and a transcript secret scan. Never prints a secret: only a masked hint."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import paths, procutil, usage
from .checks import FAIL, INFO, OK, WARN, Check
from .config import Config

# Strong, low-false-positive token shapes only (the CLAUDE.md linter also checks weaker "password=" forms).
TOKEN_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{50,}"), "GitHub fine-grained PAT"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{30,}"), "Anthropic key/token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    (re.compile(r"\bsk_live_[0-9A-Za-z]{24,}\b"), "Stripe live key"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "npm token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
]
SECRETISH_ENV = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)"
)


def mask(s: str) -> str:
    """The first characters only, never more than a quarter of the value, and its length."""
    return f"{s[: min(4, len(s) // 4)]}…({len(s)} chars)"


def scan_transcripts(days: int = 7, max_mb: int = 400) -> tuple[list[tuple[str, str, int]], int]:
    """Returns ([(file, kind, count)], files_scanned). Scans newest transcripts first, up to max_mb.

    Subagent and workflow transcripts are read too: what a subagent printed is as plaintext on disk as anything else."""
    root = paths.claude_home() / "projects"
    if not root.is_dir():
        return [], 0
    files = usage.list_transcripts(root, time.time() - days * 86400)
    budget = max_mb * 1024 * 1024
    hits: dict[tuple[str, str], int] = {}
    scanned = 0
    for t in files:
        if budget <= 0:
            break
        budget -= t.size
        scanned += 1
        try:
            with t.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for pat, kind in TOKEN_PATTERNS:
                        if pat.search(line):
                            hits[(str(t.path), kind)] = hits.get((str(t.path), kind), 0) + 1
        except OSError:
            continue
    return [(f, k, n) for (f, k), n in sorted(hits.items(), key=lambda kv: -kv[1])], scanned


# ---------------------------------------------------------------------------------------------
# literal secrets in Claude's configuration: MCP servers (env, headers, args, url) and settings `env` tables
# ---------------------------------------------------------------------------------------------
MAX_CONFIG_BYTES = 20 * 1024 * 1024  # ~/.claude.json is ~100 KB; a file this big is not read
MAX_PROJECT_FILE_BYTES = (
    1024 * 1024
)  # a project's .mcp.json or settings file, or the user's settings
MAX_PROJECT_DIRS = 60  # project folders whose own files are read (the most recently added ones)
MAX_FINDINGS = 100
SCAN_SECONDS = 3.0  # the whole scan: a project folder on a dead network share must not stall doctor

_ASIA_KEY = re.compile(r"\bASIA[0-9A-Z]{16}\b")  # an STS (temporary) AWS access key id
_AWS_NAMES = {
    "aws_access_key_id": "AWS access key id",
    "aws_secret_access_key": "AWS secret access key",
    "aws_session_token": "AWS session token",
    "aws_security_token": "AWS session token",
}
_AUTH_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-access-token",
    "cookie",
}
# a value that is a reference, not the secret: ${VAR}, $VAR, %VAR%, {{ template }}, <placeholder>, op://vault/item/field
_REFERENCE = re.compile(r"\$\{|^\$[A-Za-z_(]|\{\{|^%[A-Za-z_]\w*%$|^op://|^<[^<>]*>$")
_PLACEHOLDER_WORDS = ("your", "example", "changeme", "placeholder", "xxxx", "todo", "replace")
_ARG_KV = re.compile(r"^(?:-{1,2})?([A-Za-z][\w.-]*)=(.*)$", re.S)
_ARG_FLAG = re.compile(r"^-{1,2}([A-Za-z][\w.-]*)$")
_URL_PASSWORD = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:([^/\s@]+)@")
_URL_QUERY = re.compile(r"[?&]([\w.-]+)=([^&#\s]*)")
_MAX_VALUE = 4096  # characters looked at: the JWT pattern backtracks on a long look-alike run


def _safe(text: object, n: int) -> str:
    """Config text is not trusted (a cloned repo's .mcp.json names its own servers and variables): printable, short."""
    t = "".join(c if c.isprintable() else "?" for c in str(text))
    return t if len(t) <= n else t[: n - 1] + "…"


def _short(path: str, n: int = 48) -> str:
    p = _safe(path, 4096)
    return p if len(p) <= n else "…" + p[-(n - 1) :]


@dataclass(frozen=True)
class SecretFinding:
    """One literal secret found in a configuration file. It holds where it is and a masked hint, never the value."""

    file: str  # .claude.json, settings.json, settings.local.json or .mcp.json
    project: str  # the project path (the key in ~/.claude.json, or the folder the file is in); "" = user level
    server: str  # the MCP server; "" for a settings `env` table
    field: str  # env, headers, args, url or command
    name: str  # the variable, header or argument
    kind: str  # AWS access key id, AWS secret access key, AWS session token, bearer token, GitHub token ...
    masked: str  # mask() of the value
    temporary: bool = False  # temporary AWS credentials (STS): probably expired already

    @property
    def where(self) -> str:
        scope = f"project {_short(self.project)}" if self.project else "user"
        server = f"{self.server} " if self.server else ""
        return f"{self.file} [{scope}] {server}{self.field}.{self.name}"

    @property
    def fix(self) -> str:
        if self.temporary:
            return (
                "temporary AWS credentials (STS): probably expired already, so just delete them from the file "
                "(`aws sso login` or an AWS profile gets new ones when needed)"
            )
        if self.kind.startswith("AWS"):
            return (
                "use an AWS profile or a secret manager (`op run`) instead, or delete it; "
                "rotate the key if it was ever shared or committed"
            )
        return (
            "move it to a secret manager (`op run`, ${VAR} expansion or OAuth) or delete it; "
            "rotate it if it was ever shared or committed"
        )


def _shape(value: str) -> str | None:
    for pat, kind in TOKEN_PATTERNS:
        if pat.search(value):
            return kind
    return "AWS access key id" if _ASIA_KEY.search(value) else None


def _name_kind(name: str) -> str:
    low = name.lower()
    for rx, kind in (
        (r"password|passwd", "password"),
        (r"private[_-]?key", "private key"),
        (r"api[_-]?key", "API key"),
        (r"token", "token"),
        (r"secret", "secret"),
        (r"credential", "credential"),
    ):
        if re.search(rx, low):
            return kind
    return "secret"


def _classify(name: str, value: str, header: bool = False) -> tuple[str, str, bool] | None:
    """(kind, masked value, temporary AWS credentials) when `name = value` is a literal secret, else None.

    A secret is the value of a secret-looking name, or a value of any name that has the shape of a known token.
    References (${VAR}, op://...), placeholders, booleans and values under 8 characters are not."""
    v = value.strip()[:_MAX_VALUE]
    if len(v) < 8 or v.lower() in {"true", "false"}:
        return None
    low = name.lower()
    scheme, payload = "", v
    if header and low in {"authorization", "proxy-authorization"} and " " in v:
        scheme, payload = (x.strip() for x in v.split(None, 1))
    if _REFERENCE.search(payload):
        return None
    aws = _AWS_NAMES.get(low)
    shape = _shape(payload)
    keyed = bool(aws) or low in _AUTH_HEADERS or bool(SECRETISH_ENV.search(name))
    if not keyed and shape is None:
        return None
    if shape is None and any(w in payload.lower() for w in _PLACEHOLDER_WORDS):
        return None
    if aws:
        kind = aws
    elif scheme.lower() == "bearer":
        kind = "bearer token"
    elif scheme.lower() == "basic":
        kind = "basic auth credentials"
    elif scheme:
        kind = "authorization header"
    else:
        kind = shape or _name_kind(name)
    return kind, mask(payload), aws == "AWS session token" or bool(_ASIA_KEY.search(payload))


class _Scan:
    def __init__(self) -> None:
        self.findings: list[SecretFinding] = []

    def hit(self, file: str, project: str, server: str, field: str, name: str, got) -> None:
        if got is None or len(self.findings) >= MAX_FINDINGS:
            return
        kind, masked, temporary = got
        self.findings.append(
            SecretFinding(
                file,
                _safe(project, 4096),
                _safe(server, 60),
                field,
                _safe(name, 60),
                kind,
                masked,
                temporary,
            )
        )


def _scan_table(
    scan: _Scan, file: str, project: str, server: str, field: str, table: object
) -> None:
    for k, v in table.items() if isinstance(table, dict) else ():
        if isinstance(k, str) and isinstance(v, str):
            scan.hit(file, project, server, field, k, _classify(k, v, header=field == "headers"))


def _scan_args(scan: _Scan, file: str, project: str, server: str, args: object) -> None:
    if not isinstance(args, list):
        return
    skip = False
    for i, a in enumerate(args):
        if skip or not isinstance(a, str):
            skip = False
            continue
        m, flag = _ARG_KV.match(a), _ARG_FLAG.match(a)
        if m:
            scan.hit(file, project, server, "args", m.group(1), _classify(m.group(1), m.group(2)))
        elif flag and SECRETISH_ENV.search(flag.group(1)) and i + 1 < len(args):
            nxt = args[i + 1]
            got = _classify(flag.group(1), nxt) if isinstance(nxt, str) else None
            scan.hit(file, project, server, "args", flag.group(1), got)
            skip = got is not None  # that argument is the value just reported
        else:
            scan.hit(file, project, server, "args", f"[{i}]", _classify("", a))


def _scan_url(scan: _Scan, file: str, project: str, server: str, url: object) -> None:
    if not isinstance(url, str):
        return
    before = len(scan.findings)
    m = _URL_PASSWORD.match(url)
    if m:
        scan.hit(file, project, server, "url", "password", _classify("password", m.group(1)))
    for k, v in _URL_QUERY.findall(url[:_MAX_VALUE]):
        scan.hit(file, project, server, "url", f"?{k}", _classify(k, v))
    if len(scan.findings) == before:  # a token-shaped string anywhere else in it
        scan.hit(file, project, server, "url", "url", _classify("", url))


def _scan_servers(scan: _Scan, file: str, project: str, servers: object) -> None:
    for name, spec in servers.items() if isinstance(servers, dict) else ():
        if not isinstance(spec, dict):
            continue
        server = str(name) or "(unnamed)"
        for field in ("env", "headers"):
            _scan_table(scan, file, project, server, field, spec.get(field))
        _scan_args(scan, file, project, server, spec.get("args"))
        _scan_url(scan, file, project, server, spec.get("url"))
        cmd = spec.get("command")
        if isinstance(cmd, str):
            scan.hit(file, project, server, "command", "command", _classify("", cmd))


def _scan_settings(scan: _Scan, file: str, project: str, data: object) -> None:
    """A settings file: an `env` table and, rarely, mcpServers."""
    if isinstance(data, dict):
        _scan_table(scan, file, project, "", "env", data.get("env"))
        _scan_servers(scan, file, project, data.get("mcpServers"))


def _scan_mcp_json(scan: _Scan, file: str, project: str, data: object) -> None:
    """A project's .mcp.json: {"mcpServers": {...}}."""
    if isinstance(data, dict):
        _scan_servers(scan, file, project, data.get("mcpServers"))


def _load(path: Path, max_bytes: int, skipped: list[str] | None = None) -> object:
    """The parsed JSON of `path`; None when it is missing or cannot be read. A file that exists but is too big or is
    not JSON is also noted in `skipped` (the caller passes it for the files that matter) so it is not mistaken for clean."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        if size > max_bytes:
            if skipped is not None:
                skipped.append(f"{path.name} is larger than {max_bytes:,} bytes")
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        if skipped is not None:
            skipped.append(f"{path.name} cannot be read")
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested past what json will parse
        if skipped is not None:
            skipped.append(f"{path.name} is not valid JSON")
    return None


def scan_config_secrets(skipped: list[str] | None = None) -> list[SecretFinding]:
    """Literal secrets in the MCP servers and settings `env` tables Claude Code reads, user level and per project:
    ~/.claude.json (top-level and per-project mcpServers), the user settings.json and settings.local.json and, for
    the projects listed in ~/.claude.json, their .mcp.json and .claude/settings*.json. Capped in file size,
    projects and time; never raises; never keeps a value (see SecretFinding). A user-level file it could not read
    is named in `skipped`."""
    scan = _Scan()
    deadline = time.monotonic() + SCAN_SECONDS
    try:
        gj = _load(paths.claude_global_json(), MAX_CONFIG_BYTES, skipped)
        if isinstance(gj, dict):
            _scan_servers(scan, ".claude.json", "", gj.get("mcpServers"))
            projects = gj.get("projects")
            items = list(projects.items()) if isinstance(projects, dict) else []
            for key, proj in items:
                if isinstance(key, str) and isinstance(proj, dict):
                    _scan_servers(scan, ".claude.json", key, proj.get("mcpServers"))
            for key, _proj in items[-MAX_PROJECT_DIRS:]:
                root = Path(key) if isinstance(key, str) else None
                if root is None or not root.is_absolute():
                    continue
                for rel, how in (
                    (".mcp.json", _scan_mcp_json),
                    (".claude/settings.json", _scan_settings),
                    (".claude/settings.local.json", _scan_settings),
                ):
                    if time.monotonic() > deadline:
                        break
                    data = _load(root / rel, MAX_PROJECT_FILE_BYTES)
                    if data is not None:
                        how(scan, Path(rel).name, key, data)
        for name in ("settings.json", "settings.local.json"):
            data = _load(paths.claude_home() / name, MAX_PROJECT_FILE_BYTES, skipped)
            _scan_settings(scan, name, "", data)
    except Exception:  # noqa: BLE001 - a credential scan must never break doctor
        pass
    # every AWS value next to a session token or an ASIA key id belongs to the same expired set
    temp = {(f.file, f.project, f.server) for f in scan.findings if f.temporary}
    return [
        replace(
            f,
            temporary=f.temporary
            or (f.kind.startswith("AWS") and (f.file, f.project, f.server) in temp),
        )
        for f in scan.findings
    ]


def format_findings(found: list[SecretFinding]) -> list[str]:
    """The per-finding report of `creds doctor`: where, what kind, a masked value, what to do. Never a value."""
    if not found:
        return []
    out = [f"{len(found)} literal secret(s) in Claude's configuration (values are never printed):"]
    for n, f in enumerate(found, 1):
        out.append(f"  {n:>2}. {f.where}")
        if f.project:
            out.append(f"      project: {f.project}")
        out.append(
            f"      kind: {f.kind}{' (temporary)' if f.temporary else ''}   value: {f.masked}"
        )
        out.append(f"      fix: {f.fix}")
    return out


def run_checks(cfg: Config) -> list[Check]:
    out: list[Check] = []
    env = os.environ
    cred = paths.claude_home() / ".credentials.json"
    # 1. which auth is actually in force
    if env.get("ANTHROPIC_API_KEY"):
        out.append(
            Check(
                "auth: ANTHROPIC_API_KEY",
                WARN,
                "an exported API key overrides your subscription login (and bills the API)",
                "unset ANTHROPIC_API_KEY unless API billing is intended",
            )
        )
    elif env.get("ANTHROPIC_AUTH_TOKEN") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        out.append(
            Check(
                "auth: token env",
                INFO,
                "a token is supplied through the environment (headless style)",
            )
        )
    elif cred.exists():
        out.append(Check("auth: subscription login", OK, "OAuth login present"))
    else:
        out.append(
            Check(
                "auth",
                INFO,
                "no credentials file found - run `claude` once to log in (or `claude setup-token` on a headless VM)",
            )
        )
    # 2. credentials file permissions (POSIX)
    if cred.exists() and not paths.IS_WINDOWS:
        mode = cred.stat().st_mode & 0o777
        if mode & 0o077:
            out.append(Check("credentials file mode", FAIL, oct(mode), f"chmod 600 {cred}"))
        else:
            out.append(Check("credentials file mode", OK, oct(mode)))
    # 3 + 4. literal secrets in settings `env` tables and in MCP server definitions: user level and per project
    unread: list[str] = []
    found = scan_config_secrets(unread)
    if unread:  # said, not skipped silently: "none" from a scan that could not read the file would be false comfort
        out.append(
            Check(
                "credentials",
                WARN,
                "the credential scan could not read: " + "; ".join(unread),
                "fix or remove the file so it can be scanned",
            )
        )
    env_hits = [f for f in found if not f.server]
    mcp_hits = [f for f in found if f.server]
    names = [
        f.name + (f" (project {_short(f.project, 40)})" if f.project else "") for f in env_hits
    ]
    out.append(
        Check(
            "settings.json env secrets",
            WARN if env_hits else OK,
            (
                "secret-looking variable(s) stored in plaintext: "
                + ", ".join(names[:6])
                + (f", +{len(names) - 6} more" if len(names) > 6 else "")
            )
            if env_hits
            else "none",
            "use `op run`/a keyring helper; settings.json env is plaintext and reaches every subprocess"
            if env_hits
            else "",
        )
    )
    out.append(
        Check(
            "MCP literal secrets",
            WARN if mcp_hits else OK,
            (
                f"{len(mcp_hits)} literal secret(s) found: "
                + "; ".join(f.where for f in mcp_hits[:2])
                + (f"; +{len(mcp_hits) - 2} more" if len(mcp_hits) > 2 else "")
            )
            if mcp_hits
            else "none",
            "`claude-hr-ctl creds doctor` lists them masked; move them to a secret manager (`op run`, ${VAR} "
            "expansion or OAuth) or delete them"
            + (
                ". Temporary AWS credentials (an ASIA key id or a session token) are probably expired: "
                "just delete them"
                if any(f.temporary for f in mcp_hits)
                else ""
            )
            if mcp_hits
            else "",
        )
    )
    # 5. GitHub
    if procutil.cmd_exists("gh"):
        r = procutil.run(["gh", "auth", "status"], timeout=20)
        text = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            out.append(
                Check(
                    "github: gh auth",
                    INFO,
                    "not logged in",
                    "gh auth login (or set GH_TOKEN from a secret manager)",
                )
            )
        else:
            plaintext = bool(re.search(r"hosts\.yml", text)) and "keyring" not in text.lower()
            out.append(
                Check(
                    "github: gh auth",
                    WARN if plaintext else OK,
                    "token stored in plaintext hosts.yml"
                    if plaintext
                    else "logged in (OS credential store)",
                    "re-run `gh auth login` on a machine with a keyring, or use a fine-grained PAT with expiry"
                    if plaintext
                    else "",
                )
            )
    else:
        out.append(Check("github: gh", INFO, "GitHub CLI not installed"))
    # 6. secret tools
    out.append(
        Check(
            "secret manager",
            OK if procutil.cmd_exists("op") else INFO,
            "1Password CLI available" if procutil.cmd_exists("op") else "no `op` CLI (optional)",
        )
    )
    out.append(
        Check(
            "gitleaks",
            OK if procutil.cmd_exists("gitleaks") else INFO,
            "installed"
            if procutil.cmd_exists("gitleaks")
            else "not installed (optional: pre-commit secret scanning)",
        )
    )
    # 7. headroom CCR store holds plaintext originals
    store = paths.hr_home() / "headroom"
    if store.exists() and not paths.IS_WINDOWS:
        mode = store.stat().st_mode & 0o777
        out.append(
            Check(
                "headroom data dir mode",
                OK if mode & 0o077 == 0 else WARN,
                oct(mode),
                f"chmod 700 {store}  (it holds plaintext copies of compressed tool output)",
            )
        )
    return out
