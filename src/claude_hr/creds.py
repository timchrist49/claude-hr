"""Credential hygiene checks and a transcript secret scan. Never prints a secret: only a masked hint."""

from __future__ import annotations

import dataclasses
import errno
import json
import math
import os
import re
import stat
import time
from collections import Counter
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


def _terminal_safe(text: str) -> str:
    """Text from a configuration file or a transcript is not trusted: an escape sequence or control character in it is
    shown written out (\\x1b), the way every other untrusted text claude-hr prints is (verify.safe_text)."""
    try:
        from .verify import safe_text
    except Exception:  # noqa: BLE001 - printing a finding must never fail
        return "".join(c if c.isprintable() else "?" for c in text)
    return safe_text(text)


def mask(s: str) -> str:
    """The first characters only, never more than a quarter of the value, and its length."""
    return f"{_terminal_safe(s[: min(4, len(s) // 4)])}…({len(s)} chars)"


# ---------------------------------------------------------------------------------------------
# the transcript secret scan
# ---------------------------------------------------------------------------------------------
@dataclass
class TranscriptScan:
    """What the transcript scan found and how much of the window it covered: a scan that stopped at its byte budget
    must not read as a clean one."""

    hits: list[tuple[str, str, int]]  # (file, kind, count), most first
    days: int  # the window asked for
    budget_mb: float
    scanned: int = 0  # files read
    total: int = 0  # files inside the window
    main_scanned: int = 0
    main_total: int = 0
    sub_scanned: int = 0
    sub_total: int = 0
    stopped: bool = False  # the byte budget ran out before every file was read
    # every file newer than this many days was read (None = the whole window)
    horizon_days: float | None = None
    main_back_days: float | None = None  # how far back the main sessions that were read reach
    sub_back_days: float | None = None  # ... and the subagent and workflow files
    unreadable: int = 0  # files (or folders) that could not be read

    @property
    def complete(self) -> bool:
        return not self.stopped and not self.unreadable

    def coverage_lines(self) -> list[str]:
        """What was covered, in words. One line when everything was read; more when it was not."""
        if not self.stopped:
            lines = [f"scanned {self.scanned} transcript file(s) from the last {self.days} day(s)"]
        else:
            horizon = self.days if self.horizon_days is None else min(self.horizon_days, self.days)
            lines = [
                f"scanned {self.scanned} of {self.total} transcript file(s), covering the last {horizon:.1f} day(s) "
                f"of {self.days}; stopped at the {self.budget_mb:g} MB budget"
            ]
            for label, done, total, back in (
                ("main sessions", self.main_scanned, self.main_total, self.main_back_days),
                ("subagent/workflow files", self.sub_scanned, self.sub_total, self.sub_back_days),
            ):
                if not total:
                    continue
                if done >= total:
                    reach = ""
                elif back is None:
                    reach = ", none read"
                else:
                    reach = f", read back to {back:.1f} day(s)"
                lines.append(f"  {label}: {done} of {total} file(s){reach}")
        if self.unreadable:
            lines.append(f"{self.unreadable} transcript file(s) could not be read")
        return lines


def scan_transcripts(days: int = 7, max_mb: float = 400) -> TranscriptScan:
    """Looks for token-shaped secrets in the transcripts of the last `days` days, up to `max_mb` of them.

    The main sessions are read first (newest first) and the subagent and workflow transcripts with what is left of the
    budget: what a subagent printed is as plaintext on disk as anything else, but there are several times more of
    those files and they must not crowd the sessions out. The result says how much was covered."""
    res = TranscriptScan([], days, max_mb)
    root = paths.claude_home() / "projects"
    if not root.is_dir():
        return res
    now = time.time()
    problems: list[str] = []
    files = usage.list_transcripts(root, now - days * 86400, True, problems)
    files.sort(key=lambda t: t.kind != "main")  # stable: newest first inside each kind
    res.unreadable = len(problems)
    res.total = len(files)
    res.main_total = sum(1 for t in files if t.kind == "main")
    res.sub_total = res.total - res.main_total
    budget = int(max_mb * 1024 * 1024)
    hits: dict[tuple[str, str], int] = {}
    oldest = {"main": now, "subagent": now}  # the oldest file read, per kind
    newest_left = 0.0  # the newest file not read: everything newer than it was
    for t in files:
        if budget <= 0:
            res.stopped = True
            newest_left = max(newest_left, t.mtime)
            continue
        budget -= t.size
        try:
            with open(usage.long_path(t.path), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for pat, kind in TOKEN_PATTERNS:
                        if pat.search(line):
                            hits[(str(t.path), kind)] = hits.get((str(t.path), kind), 0) + 1
        except OSError:
            res.unreadable += 1
            continue
        res.scanned += 1
        if t.kind == "main":
            res.main_scanned += 1
        else:
            res.sub_scanned += 1
        oldest[t.kind] = min(oldest[t.kind], t.mtime)
    if res.stopped:
        res.horizon_days = max(0.0, (now - newest_left) / 86400)
        if res.main_scanned:
            res.main_back_days = (now - oldest["main"]) / 86400
        if res.sub_scanned:
            res.sub_back_days = (now - oldest["subagent"]) / 86400
    res.hits = [(f, k, n) for (f, k), n in sorted(hits.items(), key=lambda kv: -kv[1])]
    return res


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
# a value that is a reference, not the secret: ${VAR}, $VAR, %VAR%, {{ template }}, <placeholder>, and the pointers
# the secret managers use (op://vault/item/field, keychain:name, vault:path#field, file:/run/secrets/x, an ARN)
_REFERENCE = re.compile(
    r"\$\{|^\$[A-Za-z_(]|\{\{|^%[A-Za-z_]\w*%$|^<[^<>]*>$"
    r"|^(?:op|keychain|vault|file|bw|aws-sm|aws-ssm|ssm|secret|secrets|env|1password|gcp-sm|akv|sops):"
    r"|^arn:aws[\w-]*:",
    re.I,
)
_PLACEHOLDER_WORDS = ("your", "example", "changeme", "placeholder", "xxxx", "todo", "replace")
_ARG_KV = re.compile(r"^(?:-{1,2})?([A-Za-z][\w.-]*)=(.*)$", re.S)
_ARG_FLAG = re.compile(r"^-{1,2}([A-Za-z][\w.-]*)$")
# user:password@ right after a scheme://
_USERINFO = re.compile(r"[^/\s:@]*:([^/\s@]+)@")
_URL_QUERY = re.compile(r"[?&]([\w.-]+)=([^&#\s]*)")
_MAX_VALUE = 4096  # characters looked at: the JWT pattern backtracks on a long look-alike run
# `Authorization: Bearer x`, `X-Api-Key: x` written as one string (an argument after --header or -H, a variable)
_HEADER_LINE = re.compile(r"^\s*([A-Za-z][\w-]*)\s*:\s*(\S.*)$", re.S)
_SCHEME_VALUE = re.compile(r"^(Bearer|Basic)\s+(\S{8,})$", re.I)


# what a value under a secret-looking name can be without being a secret
_PATH_START = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~(?:[\\/]|$)|\.{1,2}[\\/])")
_FILE_NAME = re.compile(
    r"^[\w@%+=.~-]+\.(?:pem|key|crt|cer|p12|pfx|jks|json|ya?ml|toml|ini|cfg|conf|env|txt|token|secret|pub|kdbx|gpg|asc)$",
    re.I,
)
_PATH_WORDS = re.compile(r"^[\w.@~ +-]+(?:[/\\][\w.@~ +-]+)+[/\\]?$")
_URL_ANY = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WORD = re.compile(r"^(?:[A-Za-z][A-Za-z'.,:;!?()-]*|\d+)$")
# one word, or words joined by - _ . (client_secret_basic, Authorization, my-secret-name): a name or a setting
_IDENTIFIER = re.compile(r"^[A-Z]?[a-z]+(?:[-_.][A-Z]?[a-z]+)*$")
# a UUID or a hash (a commit, a digest, an integrity string): an identifier, not a secret, under a name that says nothing
_ID_LIKE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}|[0-9a-fA-F]{32,128}|sha(?:1|256|384|512)[-:].*)$"
)
_RANDOM_RUN = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")


def _safe(text: object, n: int) -> str:
    """Config text is not trusted (a cloned repo's .mcp.json names its own servers and variables): shown with every
    escape sequence and control character written out, and short."""
    t = _terminal_safe(str(text)[: n * 4])
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


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _mixed(s: str) -> bool:
    """Lower case, upper case and a digit in one word: how a random token looks, not how a path segment does."""
    return bool(re.search("[a-z]", s) and re.search("[A-Z]", s) and re.search("[0-9]", s))


def _random_token(v: str) -> bool:
    """A long run of random-looking characters (base64, base62, URL-safe base64), whatever it is called. Not a
    UUID or a hash, which are identifiers; not a camelCase word either: the case changes far less often in those."""
    if _ID_LIKE.match(v) or not _RANDOM_RUN.match(v) or not _mixed(v):
        return False
    classes = [
        "l" if c.islower() else "u" if c.isupper() else "d" if c.isdigit() else "p" for c in v
    ]
    changes = sum(1 for a, b in zip(classes, classes[1:], strict=False) if a != b)
    return changes / (len(v) - 1) >= 0.45 and _entropy(v) >= 4.0


def _path_like(v: str) -> bool:
    """A file or folder, written as a path (/etc/x, ~/x, ./x, C:\\x, \\\\server\\x, a\\b), a bare file name with a
    well-known extension (key.pem) or a relative path of plain words (prod/db/credentials). Nothing here touches the
    disk: a value in an untrusted file may name a share that would be contacted by looking at it."""
    if _PATH_START.match(v) or "\\" in v or _FILE_NAME.match(v):
        return True
    return bool("/" in v and _PATH_WORDS.match(v) and not any(_mixed(seg) for seg in v.split("/")))


def _word_list(v: str) -> bool:
    """Words: prose or a quantity ('Enter password for user', '3600 seconds') or a name made of words
    ('client_secret_basic', 'Authorization')."""
    if _IDENTIFIER.match(v):
        return True
    words = v.split()
    return (
        len(words) >= 2 and all(_WORD.match(w) for w in words) and any(w.isalpha() for w in words)
    )


def _not_a_secret(v: str) -> bool:
    """A value under a secret-looking name that is no secret: a path, a URL (one with a password is handled before),
    or prose. A long random run is kept: a base64 key can contain '/'."""
    if _random_token(v):
        return False
    return _path_like(v) or bool(_URL_ANY.match(v)) or _word_list(v)


def _credential_like(v: str) -> bool:
    """A value that merely follows `Bearer` or `Basic` in a variable with an ordinary name: it must look like a
    credential, not like a word ('Bearer authentication')."""
    return (
        len(v) >= 12
        and bool(re.search(r"\d", v) or (re.search("[a-z]", v) and re.search("[A-Z]", v)))
        and not _not_a_secret(v)
    )


def _url_password(s: str) -> str | None:
    """The password of the first `scheme://user:password@host` in `s` (a connection string in an argument, a variable
    or a URL field), or None. Found from the `://`, not by a pattern that starts at every letter: the text is not trusted."""
    pos = s.find("://")
    while pos > 0:
        if s[pos - 1].isalnum() or s[pos - 1] in "+-.":
            m = _USERINFO.match(s, pos + 3)
            if m:
                return m.group(1)
        pos = s.find("://", pos + 3)
    return None


def _url_secret(url: str) -> tuple[str, str, bool] | None:
    """A password in `scheme://user:password@host` (masked on its own: the user and host stay out of the report), or
    a secret in the query string of the URL."""
    pw = _url_password(url)
    if pw is not None:
        if (
            len(pw) >= 8
            and not _REFERENCE.search(pw)
            and not any(w in pw.lower() for w in _PLACEHOLDER_WORDS)
        ):
            return "password", mask(pw), False
    for k, val in _URL_QUERY.findall(url):
        got = _classify(k, val, _query=True)
        if got:
            return got
    return None


def _classify(
    name: str, value: str, header: bool = False, _query: bool = False
) -> tuple[str, str, bool] | None:
    """(kind, masked value, temporary AWS credentials) when `name = value` is a literal secret, else None.

    A secret is the value of a secret-looking name, a password inside a URL (a connection string), the value of an
    Authorization-style header, a value that follows `Bearer` or `Basic`, or a value of any name that has the shape of a
    known token or is a long random run. Not secrets: references (${VAR}, op://..., keychain:...), placeholders,
    booleans, values under 8 characters and, under a secret-looking name, paths, URLs without a password and prose."""
    v = value.strip()[:_MAX_VALUE]
    if len(v) < 8 or v.lower() in {"true", "false"}:
        return None
    hl = _HEADER_LINE.match(v)
    if hl and hl.group(1).lower() in _AUTH_HEADERS:  # "Authorization: Bearer x" in one string
        name, v, header = hl.group(1), hl.group(2).strip(), True
        if len(v) < 8:
            return None
    low = name.lower()
    scheme, payload = "", v
    sm = _SCHEME_VALUE.match(v)
    if header and low in {"authorization", "proxy-authorization"} and " " in v:
        scheme, payload = (x.strip() for x in v.split(None, 1))
    elif sm:
        scheme, payload = sm.group(1), sm.group(2)
    if _REFERENCE.search(payload):
        return None
    if not _query and "://" in payload:
        got = _url_secret(payload)
        if got:
            return got
    aws = _AWS_NAMES.get(low)
    shape = _shape(payload)
    keyed = bool(aws) or low in _AUTH_HEADERS or bool(SECRETISH_ENV.search(name))
    if shape is None:
        if any(w in payload.lower() for w in _PLACEHOLDER_WORDS):
            return None
        if keyed:
            if not aws and _not_a_secret(payload):
                return None
        elif scheme:
            if not _credential_like(payload):
                return None
        elif not _random_token(payload):
            return None
    if aws:
        kind = aws
    elif scheme.lower() == "bearer":
        kind = "bearer token"
    elif scheme.lower() == "basic":
        kind = "basic auth credentials"
    elif scheme:
        kind = "authorization header"
    elif shape:
        kind = shape
    elif keyed:
        kind = _name_kind(name)
    else:
        kind = "high-entropy string"
    return kind, mask(payload), aws == "AWS session token" or bool(_ASIA_KEY.search(payload))


class _Scan:
    def __init__(self) -> None:
        self.findings: list[SecretFinding] = []
        self.capped = False  # a finding was left out because MAX_FINDINGS was reached

    def hit(self, file: str, project: str, server: str, field: str, name: str, got) -> None:
        if got is None:
            return
        if len(self.findings) >= MAX_FINDINGS:
            self.capped = True
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
                _safe(masked, 80),
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
            hl = _HEADER_LINE.match(a)  # --header "Authorization: Bearer x", -H "X-Api-Key: x"
            label = hl.group(1) if hl and hl.group(1).lower() in _AUTH_HEADERS else f"[{i}]"
            scan.hit(file, project, server, "args", label, _classify("", a))


def _scan_url(scan: _Scan, file: str, project: str, server: str, url: object) -> None:
    if not isinstance(url, str):
        return
    before = len(scan.findings)
    pw = _url_password(url[:_MAX_VALUE])
    if pw is not None:
        scan.hit(file, project, server, "url", "password", _classify("password", pw))
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


# ---------------------------------------------------------------------------------------------
# reading the files: bounded, regular files only
# ---------------------------------------------------------------------------------------------
class _Skip(Exception):
    """A file that is there and was not scanned. `reason`: "special" (a symlink, a FIFO, a device, a folder),
    "big" (past the size limit) or "unreadable"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (a junction, a OneDrive placeholder, a WSL special file)."""
    attributes = int(getattr(st, "st_file_attributes", 0) or 0)  # only Windows has them
    return stat.S_ISLNK(st.st_mode) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _read_file(path: Path, max_bytes: int, untrusted: bool) -> bytes | None:
    """The bytes of a configuration file; None when it is not there; _Skip when it is there and must not be read.

    The files of a project folder are a cloned repository's: `untrusted` files are lstat-ed first and read only when
    they are regular files and no link (a repository can commit `.mcp.json -> /dev/zero`, or a FIFO that blocks the
    open). Both kinds are opened without blocking, checked again on the open descriptor, and read at most
    max_bytes + 1 bytes: the size from stat is no bound for a device."""
    try:
        if untrusted:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or _is_link(st):
                raise _Skip("special")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        if untrusted:
            flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError, ValueError):  # ValueError: a NUL in the path
        return None
    except OSError as exc:
        raise _Skip("special" if exc.errno == errno.ELOOP else "unreadable") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Skip("special")
        if st.st_size > max_bytes:
            raise _Skip("big")
        chunks: list[bytes] = []
        left = max_bytes + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    except OSError as exc:
        raise _Skip("unreadable") from exc
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise _Skip("big")
    return data


def _parse(data: bytes) -> object:
    return json.loads(data.decode("utf-8-sig"))


def _load(path: Path, max_bytes: int, skipped: list[str] | None = None) -> object:
    """The parsed JSON of one of the user's own files; None when it is missing or cannot be read. A file that exists but
    is too big, is not a regular file or is not JSON is also noted in `skipped` (the caller passes it for the files that
    matter) so it is not mistaken for clean. A link to a regular file is followed here: dotfile managers use them."""
    try:
        data = _read_file(path, max_bytes, untrusted=False)
        if data is None:
            return None
        return _parse(data)
    except _Skip as s:
        if skipped is not None:
            skipped.append(
                {
                    "big": f"{path.name} is larger than {max_bytes:,} bytes",
                    "special": f"{path.name} is not a regular file",
                    "unreadable": f"{path.name} cannot be read",
                }[s.reason]
            )
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested past what json will parse
        if skipped is not None:
            skipped.append(f"{path.name} is not valid JSON")
    return None


@dataclass
class _Coverage:
    """What the scan of the project folders did not look at."""

    projects_total: int = 0  # project folders listed in ~/.claude.json
    projects_done: int = 0  # ... whose own files were looked at
    out_of_time: bool = False
    special: int = 0  # project files that are links or not regular files
    too_big: int = 0
    unreadable: int = 0
    errors: int = 0  # a project file whose scan failed in a way nobody expected

    def notes(self) -> list[str]:
        out: list[str] = []
        if self.projects_done < self.projects_total:
            why = (
                f"time limit {SCAN_SECONDS:g} s"
                if self.out_of_time
                else f"only the {MAX_PROJECT_DIRS} most recent are read"
            )
            out.append(
                f"scan stopped after {self.projects_done} of {self.projects_total} projects ({why})"
            )
        if self.special:
            out.append(
                f"{self.special} project file(s) are symlinks or special files and were not read"
            )
        if self.too_big:
            out.append(
                f"{self.too_big} project file(s) larger than {MAX_PROJECT_FILE_BYTES:,} bytes were not read"
            )
        if self.unreadable:
            out.append(f"{self.unreadable} project file(s) could not be read")
        if self.errors:
            out.append(f"{self.errors} project file(s) could not be scanned (error)")
        return out


def _plain_folder(path: Path) -> bool | None:
    """True: a real folder. False: a link or something else. None: not there."""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _is_link(st)


def _project_file(root: Path, rel: str, cov: _Coverage) -> object:
    """The parsed JSON of one file of a project folder, or None. Nothing here raises for the content of the file."""
    path = root / rel
    if "/" in rel:  # .claude must be a folder of its own, not a link to somewhere else
        folder = _plain_folder(root / rel.split("/")[0])
        if folder is None:
            return None
        if not folder:
            cov.special += 1
            return None
    try:
        data = _read_file(path, MAX_PROJECT_FILE_BYTES, untrusted=True)
        return None if data is None else _parse(data)
    except _Skip as s:
        if s.reason == "special":
            cov.special += 1
        elif s.reason == "big":
            cov.too_big += 1
        else:
            cov.unreadable += 1
    except (ValueError, RecursionError):
        pass  # not JSON, or past what json will parse: the repository's own business
    return None


@dataclass
class ConfigScan:
    """The result of the configuration scan: the findings, and everything it could not say."""

    findings: list[SecretFinding] = dataclasses.field(default_factory=list)
    # user-level files it could not read: it cannot say "none"
    unread: list[str] = dataclasses.field(default_factory=list)
    # what it did not look at (a cap, files it must not read): "none" holds for the rest only
    notes: list[str] = dataclasses.field(default_factory=list)
    capped: bool = False  # MAX_FINDINGS was reached: there may be more


def scan_config() -> ConfigScan:
    """Literal secrets in the MCP servers and settings `env` tables Claude Code reads, user level and per project:
    ~/.claude.json (top-level and per-project mcpServers), the user settings.json and settings.local.json and, for
    the projects listed in ~/.claude.json, their .mcp.json and .claude/settings*.json. Capped in file size,
    projects, findings and time, and the result says which cap was reached; never raises; never keeps a value (see
    SecretFinding). The files of a project folder are a repository's: see _read_file."""
    res = ConfigScan()
    scan, cov = _Scan(), _Coverage()
    deadline = time.monotonic() + SCAN_SECONDS
    items: list[tuple[object, object]] = []
    try:
        gj = _load(paths.claude_global_json(), MAX_CONFIG_BYTES, res.unread)
        if isinstance(gj, dict):
            _scan_servers(scan, ".claude.json", "", gj.get("mcpServers"))
            projects = gj.get("projects")
            items = list(projects.items()) if isinstance(projects, dict) else []
    except Exception as exc:  # noqa: BLE001 - a credential scan must never break doctor, and must say it fell short
        res.unread.append(f".claude.json caused an error ({type(exc).__name__})")
    # the user's own files before any project's: a cap must not leave them unread
    for name in ("settings.json", "settings.local.json"):
        try:
            data = _load(paths.claude_home() / name, MAX_PROJECT_FILE_BYTES, res.unread)
            _scan_settings(scan, name, "", data)
        except Exception as exc:  # noqa: BLE001
            res.unread.append(f"{name} caused an error ({type(exc).__name__})")
    try:
        for key, proj in items:
            if isinstance(key, str) and isinstance(proj, dict):
                _scan_servers(scan, ".claude.json", key, proj.get("mcpServers"))
        roots = [k for k, _ in items if isinstance(k, str) and Path(k).is_absolute()]
        cov.projects_total = len(roots)
        for key in roots[-MAX_PROJECT_DIRS:]:
            if time.monotonic() > deadline:
                cov.out_of_time = True
                break
            cov.projects_done += 1
            for rel, how in (
                (".mcp.json", _scan_mcp_json),
                (".claude/settings.json", _scan_settings),
                (".claude/settings.local.json", _scan_settings),
            ):
                try:  # each file on its own: one bad file does not take the rest of the scan with it
                    data = _project_file(Path(key), rel, cov)
                    if data is not None:
                        how(scan, Path(rel).name, key, data)
                except Exception:  # noqa: BLE001
                    cov.errors += 1
    except Exception as exc:  # noqa: BLE001
        res.unread.append(f"the scan stopped early ({type(exc).__name__})")
    # every AWS value next to a session token or an ASIA key id belongs to the same expired set
    temp = {(f.file, f.project, f.server) for f in scan.findings if f.temporary}
    res.findings = [
        replace(
            f,
            temporary=f.temporary
            or (f.kind.startswith("AWS") and (f.file, f.project, f.server) in temp),
        )
        for f in scan.findings
    ]
    res.notes = cov.notes()
    res.capped = scan.capped
    if scan.capped:
        res.notes.append(f"findings are capped at {MAX_FINDINGS}; there may be more")
    return res


def scan_config_secrets(
    skipped: list[str] | None = None, notes: list[str] | None = None
) -> list[SecretFinding]:
    """The findings of scan_config(); a user-level file it could not read is named in `skipped` and what it did not
    look at (a cap that was reached, project files it must not read) in `notes`."""
    res = scan_config()
    if skipped is not None:
        skipped.extend(res.unread)
    if notes is not None:
        notes.extend(res.notes)
    return res.findings


def format_findings(found: list[SecretFinding], capped: bool = False) -> list[str]:
    """The per-finding report of `creds doctor`: where, what kind, a masked value, what to do. Never a value."""
    if not found:
        return []
    out = [
        f"{len(found)}{'+' if capped else ''} literal secret(s) in Claude's configuration (values are never printed):"
    ]
    for n, f in enumerate(found, 1):
        out.append(f"  {n:>2}. {f.where}")
        if f.project:
            out.append(f"      project: {f.project}")
        out.append(
            f"      kind: {f.kind}{' (temporary)' if f.temporary else ''}   value: {f.masked}"
        )
        out.append(f"      fix: {f.fix}")
    return out


def run_checks(cfg: Config, scan: ConfigScan | None = None) -> list[Check]:
    """The credential checks. `scan` is a configuration scan already made (`creds doctor` lists its findings too)."""
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
    scan = scan or scan_config()
    unread, found = scan.unread, scan.findings
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
    # what the scan did not look at is said next to its answer: "none" from a scan that stopped at a cap is not "none"
    not_scanned = f" (not everything was scanned: {'; '.join(scan.notes)})" if scan.notes else ""
    out.append(
        Check(
            "MCP literal secrets",
            WARN if mcp_hits else (INFO if scan.notes else OK),
            (
                f"{len(mcp_hits)}{'+' if scan.capped else ''} literal secret(s) found: "
                + "; ".join(f.where for f in mcp_hits[:2])
                + (f"; +{len(mcp_hits) - 2} more" if len(mcp_hits) > 2 else "")
                + not_scanned
            )
            if mcp_hits
            else "none" + not_scanned,
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
