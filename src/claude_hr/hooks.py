"""Hook / status line entry points. Must be fast (spawned per tool call) and fail OPEN on any error."""

from __future__ import annotations

import json
import math
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
    (
        re.compile(rf"(?ism)(?:^|[;&|(\s]){_READERS}\b[^\n;&|]*{_SECRET_FILE}"),
        "reads a credential file",
    ),
    (
        re.compile(
            r"(?ism)(?:^|[;&|(])\s*(?:env|printenv|set|export\s+-p|compgen\s+-e|Get-ChildItem\s+Env:|gci\s+Env:|dir\s+Env:)\s*(?:$|[;&|>])"
        ),
        "dumps the whole environment",
    ),
    (
        re.compile(rf"(?ism)(?:^|[;&|(])\s*printenv\s+\w*{_SECRET_NAME}"),
        "prints a secret-looking variable",
    ),
    (re.compile(r"(?is)/proc/(?:self|\d+)/environ"), "reads process environment"),
    (
        re.compile(
            rf"(?is)\b(?:echo|printf|Write-Output|Write-Host)\b[^\n;&|]*\$\{{?(?:env:)?\w*{_SECRET_NAME}\w*\}}?"
        ),
        "prints a secret-looking variable",
    ),
    (
        re.compile(rf"(?ism)(?:^|[;&|(])\s*\$env:\w*{_SECRET_NAME}\w*\s*(?:$|[;&|])"),
        "prints a secret-looking variable",
    ),
    (re.compile(r"(?is)\bgh\s+auth\s+token\b"), "prints the GitHub token"),
    (
        re.compile(r"(?is)\bgh\s+auth\s+status\b[^\n;&|]*(?:\s-t\b|--show-token\b)"),
        "prints the GitHub token",
    ),
    (re.compile(r"(?is)\bop\s+(?:read|item\s+get)\b(?![^\n]*\|)"), "prints a 1Password secret"),
    (
        re.compile(r"(?is)\bsecurity\s+find-(?:generic|internet)-password\b[^\n]*\s-w\b"),
        "prints a keychain secret",
    ),
]


# Git operations that are hard to undo or that skip safety checks. The user is ASKED (permissionDecision "ask"), not
# blocked: these are legitimate when intended. Under security.profile = strict the baseline sets CLAUDE_HR_GIT_GUARD=deny.
# A regex is not a security boundary (`git -C . push`, `sh -c ...` get past it): server-side branch protection is.
# `git` must be in command position (start of a command segment, optionally after VAR=x / sudo / time), so that
# `echo git reset --hard` or a commit message that mentions it is not mistaken for the command itself.
_GIT = r"(?:^|[;&|(])\s*(?:\w+=\S+\s+)*(?:(?:sudo|command|time|nohup)\s+)*git\b[^\n;&|]*?"
_PROTECTED = r"(?:main|master|trunk|release[\w./-]*|production|prod)"
GIT_RISKS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(rf"(?ism){_GIT}\s--no-verify\b"),
        "skips the repository's git hooks (--no-verify)",
    ),
    (
        re.compile(
            rf"(?ism){_GIT}\bpush\b[^\n;&|]*(?:\s--force\b|\s-f\b|\s\+[\w./-]+)[^\n;&|]*\b{_PROTECTED}\b|"
            rf"{_GIT}\bpush\b[^\n;&|]*\b{_PROTECTED}\b[^\n;&|]*(?:\s--force\b|\s-f\b)|"
            rf"{_GIT}\bpush\b[^\n;&|]*\s\+(?:[\w./-]+:)?{_PROTECTED}\b"
        ),
        "force-pushes a protected branch",
    ),
    (
        re.compile(rf"(?ism){_GIT}\breset\b[^\n;&|]*\s--hard\b"),
        "discards uncommitted work (git reset --hard)",
    ),
    (
        re.compile(rf"(?ism){_GIT}\bclean\b[^\n;&|]*\s-[a-z]*f"),
        "deletes untracked files (git clean -f)",
    ),
    (
        re.compile(rf"(?ism){_GIT}\b(?:checkout\s+(?:--\s+)?\.|restore\s+(?:--\S+\s+)*\.)(?:\s|$)"),
        "discards all uncommitted changes in the working tree",
    ),
]


# Trusting a verify recipe is what lets a repository's own commands run unattended at the end of every turn, so only the
# user does it (in their own terminal). The CLI refuses a Claude Code shell; this refuses the agent that tries anyway,
# in every permission mode, as long as hooks run. Like every pattern here it is not a boundary (`sh -c "$(echo ...)"`).
_VERIFY_TRUST = re.compile(
    r"(?ism)claude[-_]hr[\w.-]*\b[^\n;&|]*?\bverify\s+(?:(?:un)?trust|enable|disable)\b"
    r"|\bverify-(?:trust|offer)\.json\b"
    r"|\bclaude_hr\b[^\n]*\bverify\b[^\n]*\b(?:un)?trust\s*\("
    r"|\bclaude_hr\b[^\n]*\bgate_offer\b[^\n]*\b(?:enable|disable|record|forget|offer_now)\s*\("
)


def git_risk(command: str) -> str | None:
    """Why `command` deserves a confirmation (see GIT_RISKS), or None."""
    for pattern, why in GIT_RISKS:
        if pattern.search(command):
            return why
    return None


def main(argv: list[str] | None = None) -> int:
    """`claude-hr-hook <bash-guard|stop|statusline>`: a tiny entry point so hooks do not import the whole control CLI (~250 ms)."""
    argv = sys.argv[1:] if argv is None else argv
    name = argv[0] if argv else ""
    if name == "bash-guard":
        return bash_guard()
    if name == "stop":
        try:
            from . import stop_gate

            return stop_gate.run()
        except Exception:
            return 0  # a gate must never break the session
    if name == "statusline":
        return statusline()
    sys.stderr.write("usage: claude-hr-hook bash-guard | stop | statusline\n")
    return 2


def read_stdin_json() -> dict:
    """The hook payload from stdin ({} when it is empty or not a JSON object; raises ValueError for broken JSON).

    Claude Code sends UTF-8. Read the bytes and decode them as UTF-8 ourselves: Python decodes a Windows pipe with the
    console code page (mangling non-ASCII commands), and Windows PowerShell 5.1 can prepend a BOM when it pipes text to a
    native program, which made json.loads fail and the secret guard silently allow everything.
    """
    stream = sys.stdin
    buf = getattr(stream, "buffer", None)
    raw = buf.read() if buf is not None else stream.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw.lstrip("﻿")
    data = json.loads(text.strip() or "{}")
    return data if isinstance(data, dict) else {}


def bash_guard() -> int:
    """PreToolUse(Bash). Exit 2 blocks and returns stderr to the model; anything else lets it run."""
    if os.environ.get("CLAUDE_HR_GUARD_OFF") == "1":
        return 0
    try:
        payload = read_stdin_json()
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
    if _VERIFY_TRUST.search(command):
        sys.stderr.write(
            "Blocked by the claude-hr guard: trusting or enabling (or editing the trust record of) a verify recipe lets "
            "commands run unattended after every turn, so only the user does it. Show the user .claude/hr-verify.toml "
            "and ask them to run `claude-hr-ctl verify enable` (or `verify trust`) in their own terminal.\n"
        )
        return 2
    mode = os.environ.get("CLAUDE_HR_GIT_GUARD", "ask").strip().lower()
    if mode != "off":
        why = git_risk(command)
        if why:
            decision = "deny" if mode == "deny" else "ask"
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": decision,
                            "permissionDecisionReason": f"claude-hr: this command {why}.",
                        }
                    }
                )
            )
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
        "five_hour": (
            ((body.get("subscription_window") or {}).get("latest") or {}).get("five_hour") or {}
        ).get("utilization_pct"),
    }
    try:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return out


def _num(value) -> float | None:
    """A JSON number (or a numeric string) as a finite float; None for anything else (a wrongly typed field is just absent)."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _field(data, *keys):
    """data[k1][k2]... where any level that is not an object (null, a string, a number, a list) reads as absent."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _statusline_parts(data, parts: list[str]) -> None:
    """Append the segments of the status line to `parts` one at a time, so a failure only loses the segments after it."""
    model = _field(data, "model", "display_name")
    if isinstance(model, str) and model.strip():
        parts.append(model.strip())
    ctx = _num(_field(data, "context_window", "used_percentage"))
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    cost = _num(_field(data, "cost", "total_cost_usd"))
    if cost:
        parts.append(f"${cost:.2f}")
    from . import config

    port = int(config.load().get("headroom.port", 8787))
    if os.environ.get("ANTHROPIC_BASE_URL", "").endswith(f":{port}"):
        hs = _headroom_summary(port)
        saved = _num(_field(hs, "saved"))
        if saved:
            pct = _num(_field(hs, "pct"))
            share = f" {pct:.0f}%" if pct else ""
            parts.append(f"✂ {_fmt_tokens(saved)} saved{share}")
        five_hour = _num(_field(hs, "five_hour"))
        if five_hour is not None:
            parts.append(f"5h {five_hour:.0f}%")


def statusline() -> int:
    """Print one status line, always, and exit 0: this runs on every refresh of the prompt, so a payload field of the wrong
    type, a broken stdin, an unreadable config or a closed stdout may cost segments but never a traceback or a failing exit."""
    parts: list[str] = []
    try:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            data = read_stdin_json()
        except Exception:
            data = {}
        try:
            _statusline_parts(data, parts)
        except Exception:
            pass  # whatever was built before the failure is still printed
        sys.stdout.write(" | ".join(parts) + "\n")
    except Exception:
        pass
    return 0
