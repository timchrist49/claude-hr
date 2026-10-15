"""Hook / status line entry points. Must be fast (spawned per tool call) and fail OPEN on any error."""

from __future__ import annotations

import json
import os
import re
import sys
import time

from . import paths, procutil

_B = r"(?<![\w.-])"  # path boundary: `.env` must start a path component, so `process.env` / `import.meta.env` don't match
_SECRET_NAME = r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)"
_SECRET_FILE = (
    rf"(?:{_B}\.env(?!\.(?:example|sample|template|dist)\b)(?:\.[\w-]+)?\b|"
    r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b(?!\.pub)|[\w.-]*(?:key|priv|secret)[\w.-]*\.pem\b|\.p12\b|\.pfx\b|"
    r"\.aws[/\\]credentials|\.netrc|\.git-credentials|\.docker[/\\]config\.json|\.kube[/\\]config|"
    r"\.claude[/\\]\.credentials\.json|\.npmrc|\.config[/\\]gh[/\\]hosts\.yml|"
    r"\.ssh[/\\](?!config\b|known_hosts|authorized_keys|[\w.-]*\.pub\b)[\w.-]+)"
)
# Programs that PRINT or EXFILTRATE a file. cp / mv / tar are deliberately absent: they put nothing in the transcript
# and `cp .env.example .env` is a normal setup step.
_READERS = (
    r"(?:cat|less|more|head|tail|type|bat|nl|tac|sed|awk|grep|rg|strings|xxd|od|base64|Get-Content|gc|jq|cut|sort|uniq|"
    r"column|paste|diff|vi|vim|nano|view|curl|wget|scp|rsync|nc|ncat|Invoke-WebRequest|Invoke-RestMethod|"
    r"git\s+(?:log|show|diff|cat-file))"
)
RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"(?ism)(?:^|[;&|(\s]){_READERS}\b[^\n;&|]*{_SECRET_FILE}"), "reads a credential file"),
    (re.compile(r"(?ism)(?:^|[;&|(])\s*(?:env|printenv|set|export\s+-p|compgen\s+-e|Get-ChildItem\s+Env:|gci\s+Env:|dir\s+Env:)\s*(?:$|[;&|>])"),
     "dumps the whole environment"),
    (re.compile(rf"(?ism)(?:^|[;&|(])\s*printenv\s+\w*{_SECRET_NAME}"), "prints a secret-looking variable"),
    (re.compile(r"(?is)/proc/(?:self|\d+)/environ"), "reads process environment"),
    (re.compile(rf"(?is)\b(?:echo|printf|Write-Output|Write-Host)\b[^\n;&|]*\$\{{?(?:env:)?\w*{_SECRET_NAME}\w*\}}?"),
     "prints a secret-looking variable"),
    (re.compile(rf"(?ism)(?:^|[;&|(])\s*\$env:\w*{_SECRET_NAME}\w*\s*(?:$|[;&|])"), "prints a secret-looking variable"),
    (re.compile(r"(?is)\bgh\s+auth\s+token\b"), "prints the GitHub token"),
    (re.compile(r"(?is)\bop\s+(?:read|item\s+get)\b(?![^\n]*\|)"), "prints a 1Password secret"),
    (re.compile(r"(?is)\bsecurity\s+find-(?:generic|internet)-password\b[^\n]*\s-w\b"), "prints a keychain secret"),
]


def main(argv: list[str] | None = None) -> int:
    """`claude-hr-hook <bash-guard|statusline>`: a tiny entry point so hooks do not import the whole control CLI (~250 ms)."""
    argv = sys.argv[1:] if argv is None else argv
    name = argv[0] if argv else ""
    if name == "bash-guard":
        return bash_guard()
    if name == "statusline":
        return statusline()
    sys.stderr.write("usage: claude-hr-hook bash-guard | statusline\n")
    return 2


def bash_guard() -> int:
    """PreToolUse(Bash). Exit 2 blocks and returns stderr to the model; anything else lets it run."""
    if os.environ.get("CLAUDE_HR_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = str(payload.get("tool_input", {}).get("command", ""))
    except Exception:
        return 0
    if not command:
        return 0
    for pattern, why in RULES:
        if pattern.search(command):
            sys.stderr.write(
                f"Blocked by the claude-hr secret guard: this command {why}, which would put credentials into the "
                "session transcript. Use the configured credential helper (gh auth, op run) or ask the user to "
                "run it themselves.\n"
            )
            return 2
    return 0


# ---------------------------------------------------------------------------------------------
# status line:  model | ctx 41% | $0.83 | ✂ 183k saved 40% | 5h 3%
# ---------------------------------------------------------------------------------------------
def _fmt_tokens(n: float) -> str:
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(int(n))


def _headroom_summary(port: int) -> dict | None:
    cache = paths.state_dir() / "statusline-headroom.json"
    try:
        if time.time() - cache.stat().st_mtime < 20:
            return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    status, body = procutil.http_json(f"http://127.0.0.1:{port}/stats?cached=1", timeout=0.4)
    if status != 200 or not isinstance(body, dict):
        return None
    life = (body.get("persistent_savings") or {}).get("lifetime") or {}
    disp = body.get("display_session") or {}
    out = {
        "saved": (body.get("savings") or {}).get("total_tokens") or life.get("tokens_saved") or 0,
        "pct": disp.get("savings_percent"),
        "five_hour": (((body.get("subscription_window") or {}).get("latest") or {}).get("five_hour") or {}).get("utilization_pct"),
    }
    try:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return out


def statusline() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    parts: list[str] = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(str(model))
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {float(ctx):.0f}%")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost:
        parts.append(f"${float(cost):.2f}")
    try:
        from . import config

        port = int(config.load().get("headroom.port", 8787))
        if os.environ.get("ANTHROPIC_BASE_URL", "").endswith(f":{port}"):
            hs = _headroom_summary(port)
            if hs and hs.get("saved"):
                pct = f" {float(hs['pct']):.0f}%" if hs.get("pct") else ""
                parts.append(f"✂ {_fmt_tokens(hs['saved'])} saved{pct}")
            if hs and hs.get("five_hour") is not None:
                parts.append(f"5h {float(hs['five_hour']):.0f}%")
    except Exception:
        pass
    sys.stdout.write(" | ".join(parts) + "\n")
    return 0
