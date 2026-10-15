"""Credential hygiene checks and a transcript secret scan. Never prints a secret: only a masked hint."""

from __future__ import annotations

import os
import re
import time

from . import paths, procutil
from .checks import FAIL, INFO, OK, WARN, Check
from .config import Config
from .jsonio import JsonFileError, read_json

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
SECRETISH_ENV = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)")


def mask(s: str) -> str:
    return f"{s[:4]}…({len(s)} chars)"


def scan_transcripts(days: int = 7, max_mb: int = 400) -> tuple[list[tuple[str, str, int]], int]:
    """Returns ([(file, kind, count)], files_scanned). Scans newest transcripts first, up to max_mb."""
    root = paths.claude_home() / "projects"
    if not root.is_dir():
        return [], 0
    cutoff = time.time() - days * 86400
    files = []
    for f in root.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            files.append((st.st_mtime, st.st_size, f))
    files.sort(reverse=True)
    budget = max_mb * 1024 * 1024
    hits: dict[tuple[str, str], int] = {}
    scanned = 0
    for _mt, size, f in files:
        if budget <= 0:
            break
        budget -= size
        scanned += 1
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    for pat, kind in TOKEN_PATTERNS:
                        if pat.search(line):
                            hits[(str(f), kind)] = hits.get((str(f), kind), 0) + 1
        except OSError:
            continue
    return [(f, k, n) for (f, k), n in sorted(hits.items(), key=lambda kv: -kv[1])], scanned


def run_checks(cfg: Config) -> list[Check]:
    out: list[Check] = []
    env = os.environ
    cred = paths.claude_home() / ".credentials.json"
    # 1. which auth is actually in force
    if env.get("ANTHROPIC_API_KEY"):
        out.append(Check("auth: ANTHROPIC_API_KEY", WARN,
                         "an exported API key overrides your subscription login (and bills the API)",
                         "unset ANTHROPIC_API_KEY unless API billing is intended"))
    elif env.get("ANTHROPIC_AUTH_TOKEN") or env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        out.append(Check("auth: token env", INFO, "a token is supplied through the environment (headless style)"))
    elif cred.exists():
        out.append(Check("auth: subscription login", OK, "OAuth login present"))
    else:
        out.append(Check("auth", INFO, "no credentials file found - run `claude` once to log in (or `claude setup-token` on a headless VM)"))
    # 2. credentials file permissions (POSIX)
    if cred.exists() and not paths.IS_WINDOWS:
        mode = cred.stat().st_mode & 0o777
        if mode & 0o077:
            out.append(Check("credentials file mode", FAIL, oct(mode), f"chmod 600 {cred}"))
        else:
            out.append(Check("credentials file mode", OK, oct(mode)))
    # 3. secrets in settings.json env
    try:
        settings = read_json(paths.claude_settings(), {}) or {}
    except JsonFileError:
        settings = {}  # doctor reports the corrupt file; here we just skip the env-secret scan
    bad = [k for k, v in (settings.get("env") or {}).items()
           if SECRETISH_ENV.search(k) and v and not str(v).startswith("$") and k not in {"DISABLE_ERROR_REPORTING"}]
    out.append(Check("settings.json env secrets", WARN if bad else OK,
                     ("secret-looking variable(s) stored in plaintext: " + ", ".join(bad)) if bad else "none",
                     "use `op run`/a keyring helper; settings.json env is plaintext and reaches every subprocess" if bad else ""))
    # 4. literal secrets in MCP server env
    gj = read_json(paths.claude_global_json(), {}) or {}
    lit = []
    for name, spec in (gj.get("mcpServers") or {}).items():
        for k, v in (spec.get("env") or {}).items():
            if SECRETISH_ENV.search(k) and v and not str(v).startswith("${"):
                lit.append(f"{name}.{k}")
    out.append(Check("MCP literal secrets", WARN if lit else OK, ", ".join(lit) if lit else "none",
                     "use ${VAR} expansion or OAuth instead of literal values" if lit else ""))
    # 5. GitHub
    if procutil.cmd_exists("gh"):
        r = procutil.run(["gh", "auth", "status"], timeout=20)
        text = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            out.append(Check("github: gh auth", INFO, "not logged in", "gh auth login (or set GH_TOKEN from a secret manager)"))
        else:
            plaintext = bool(re.search(r"hosts\.yml", text)) and "keyring" not in text.lower()
            out.append(Check("github: gh auth", WARN if plaintext else OK,
                             "token stored in plaintext hosts.yml" if plaintext else "logged in (OS credential store)",
                             "re-run `gh auth login` on a machine with a keyring, or use a fine-grained PAT with expiry" if plaintext else ""))
    else:
        out.append(Check("github: gh", INFO, "GitHub CLI not installed"))
    # 6. secret tools
    out.append(Check("secret manager", OK if procutil.cmd_exists("op") else INFO,
                     "1Password CLI available" if procutil.cmd_exists("op") else "no `op` CLI (optional)"))
    out.append(Check("gitleaks", OK if procutil.cmd_exists("gitleaks") else INFO,
                     "installed" if procutil.cmd_exists("gitleaks") else "not installed (optional: pre-commit secret scanning)"))
    # 7. headroom CCR store holds plaintext originals
    store = paths.hr_home() / "headroom"
    if store.exists() and not paths.IS_WINDOWS:
        mode = store.stat().st_mode & 0o777
        out.append(Check("headroom data dir mode", OK if mode & 0o077 == 0 else WARN, oct(mode),
                         f"chmod 700 {store}  (it holds plaintext copies of compressed tool output)"))
    return out
