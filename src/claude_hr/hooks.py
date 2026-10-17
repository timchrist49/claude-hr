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
# A regex or a tokeniser is not a security boundary (`git -C . push` gets past the regex; aliases, variables and
# `$(...)`-built arguments get past both): server-side branch protection is.
# `git` must be in command position (start of a command segment, optionally after VAR=x / sudo / time), so that
# `echo git reset --hard` or a commit message that mentions it is not mistaken for the command itself.
# Hook bypasses (--no-verify and its relatives) are judged by _hook_bypass() and by the pattern below, together.
_GIT = r"(?:^|[;&|(])\s*(?:\w+=\S+\s+)*(?:(?:sudo|command|time|nohup)\s+)*git\b[^\n;&|]*?"
_PROTECTED = r"(?:main|master|trunk|release[\w./-]*|production|prod)"
_NO_VERIFY_RE = re.compile(rf"(?ism){_GIT}\s--no-verify\b")
_NO_VERIFY_WHY = "skips the repository's git hooks (--no-verify)"
_COMMIT_N_WHY = "skips the repository's git hooks (git commit -n)"
_HOOKS_PATH_WHY = "redirects the repository's git hooks (core.hooksPath)"
GIT_RISKS: list[tuple[re.Pattern, str]] = [
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
#
# The gaps between the words are BOUNDED and trust_attempt() scans a bounded amount of text: with an open-ended gap the
# match time grows with the cube of the line, so one comment line of padded keywords ran the hook past its timeout, and a
# hook that is killed lets the command through. The bounds are far longer than any typed command. Every alternative
# needs the word `verify` or `gate_offer`: text without either is not looked at by a pattern at all.
_GAP = 256  # `claude-hr ... verify trust`: other words between the program name and the verb
_GAP_PY = 128  # `claude_hr ... verify` / `claude_hr ... gate_offer` inside python -c
_GAP_CALL = 64  # `verify ... trust(`
_SCAN = 16_384  # characters scanned from the start and from the end of an over-long command
_KEYWORDS = ("verify", "gate_offer")
_VERIFY_TRUST = re.compile(
    rf"(?ism)claude[-_]hr[^\n;&|]{{0,{_GAP}}}?\bverify\s+(?:(?:un)?trust|enable|disable)\b"
    r"|\bverify-(?:trust|offer)\.json\b"
    rf"|\bclaude_hr\b[^\n]{{0,{_GAP_PY}}}\bverify\b[^\n]{{0,{_GAP_CALL}}}\b(?:un)?trust\s*\("
    rf"|\bclaude_hr\b[^\n]{{0,{_GAP_PY}}}\bgate_offer\b[^\n]{{0,{_GAP_CALL}}}"
    r"\b(?:enable|disable|record|forget|offer_now)\s*\("
)


def _scan(text: str) -> bool:
    lowered = text.lower()
    if not any(word in lowered for word in _KEYWORDS):
        return False
    return bool(_VERIFY_TRUST.search(text))


def trust_attempt(command: str) -> bool:
    """Does `command` try to trust, enable or disable a verify recipe, or touch the trust or the offer record?

    A command longer than 2 * _SCAN is looked at from its first and its last _SCAN characters only, so the time this takes
    has a ceiling however the command is padded (a trust attempt only in the middle of such a command is not seen)."""
    if len(command) > 2 * _SCAN:
        return _scan(command[:_SCAN]) or _scan(command[-_SCAN:])
    return _scan(command)


# Hook bypasses are judged twice and the answers are ORed.
# (1) On words (_hook_bypass): shlex splits the command the way a shell does, so `git commit -n` (short for --no-verify), an
#     abbreviated `--no-veri`, `git -c core.hooksPath=...` and a quoted "--no-verify" are seen. shlex is imported when first
#     needed, so that this module stays light.
# (2) By the old pattern, on the command with the values of -m / --message cut out: a commit message that merely mentions
#     --no-verify is not a request to skip hooks, but every other place the pattern matched before (`$(...)`, a command
#     that does not parse, the PowerShell tool) still asks.
# A longer command is not tokenised: shlex builds each word by repeated concatenation, so its time grows faster than
# linearly on long words (about 0.1 to 0.2 s at this size, 1.7 s at 260 KB).
_TOKEN_MAX = 65_536
_DEPTH = 3  # how deep `$(...)`, `sh -c '...'` and eval are followed
_SEGMENT_END = frozenset(";&|()")
_SEPARATORS = ";&|()"
_MASK = str.maketrans(dict(zip(_SEPARATORS, "", strict=True)))
_UNMASK = str.maketrans(dict(zip("", _SEPARATORS, strict=True)))
# a backslash that ends a line (not an escaped one) joins the next line to this one
_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\r?\n")
# words that may stand in front of the command and do not change what it is
_COMMAND_PREFIX = frozenset(
    {
        "sudo",
        "command",
        "time",
        "nohup",
        "exec",
        "env",
        "then",
        "do",
        "else",
        "elif",
        "if",
        "while",
        "until",
        "{",
        "!",
    }
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_ENV_ASSIGN = re.compile(r"\w+=")
_GIT_NAMES = frozenset({"git", "git.exe"})
_GIT_VALUE_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"})
# `git commit` options whose value is the NEXT word (`-m "-n fix"` is a message, not the -n flag)
_COMMIT_VALUE_OPTIONS = frozenset(
    {
        "--message",
        "--file",
        "--author",
        "--date",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--template",
        "--cleanup",
        "--trailer",
        "--pathspec-from-file",
    }
)
_NO_VERIFY = "--no-verify"
# `-m "..."`, `-am '...'`, `--message="..."`: a commit message in quotes
_MESSAGE_VALUE = re.compile(
    r"""(?<!\S)(?:-[A-Za-z]*m|--message)(?:=|\s+)(?:"(?:[^"\\]|\\.)*"|'[^']*')""", re.S
)


def _mask_quoted(text: str) -> str:
    """`text` with the separator characters that sit inside quotes (or are escaped) replaced by private-use characters, so
    that shlex does not take `-m ";"` or `\\;` for a command separator."""
    out: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and quote != "'" and i + 1 < n:
            out.append(ch + text[i + 1].translate(_MASK))
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
                out.append(ch)
            else:
                out.append(ch.translate(_MASK))
        else:
            if ch in "'\"":
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


def _hook_bypass(command: str, depth: int = 0) -> str | None:
    """Why `command` skips or redirects the repository's git hooks, judged on words; raises ValueError for an unbalanced quote."""
    import shlex

    text = _mask_quoted(_CONTINUATION.sub(r"\1 ", command)).replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    segment: list[str] = []
    for token in lexer:
        # '' is a quoted empty word, not a separator
        if token and _SEGMENT_END.issuperset(token):
            why = _segment_bypass(segment, depth)
            if why:
                return why
            segment = []
        else:
            segment.append(token)
    return _segment_bypass(segment, depth)


def _nested_commands(words: list[str], start: int) -> list[str]:
    """Command text hidden inside the words of a segment: `$(...)` and backtick substitutions in any word, the script of
    `sh -c '...'` / `bash -lc '...'`, and what `eval` is given."""
    found: list[str] = []
    for word in words:
        text = word.translate(_UNMASK)
        if "$(" in text or "`" in text:
            found += text.split("$(")[1:] + text.split("`")[1::2]
    if start < len(words):
        name = words[start].lower().rsplit("/", 1)[-1].removesuffix(".exe")
        if name == "eval":
            found.append(" ".join(words[start + 1 :]).translate(_UNMASK))
        elif name in _SHELLS:
            for j in range(start + 1, len(words) - 1):
                option = words[j]
                # `-c`, `-lc`, `-ec`: a cluster of short options with c in it; its script is the next word
                if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
                    found.append(words[j + 1].translate(_UNMASK))
                    break
    return found


def _segment_bypass(words: list[str], depth: int = 0) -> str | None:
    i = 0
    while i < len(words) and (_ENV_ASSIGN.match(words[i]) or words[i].lower() in _COMMAND_PREFIX):
        i += 1
    if depth < _DEPTH:
        for inner in _nested_commands(words, i):
            try:
                why = _hook_bypass(inner, depth + 1)
            except Exception:  # noqa: BLE001 - a fragment that does not parse is not evidence of anything
                why = None
            if why:
                return why
    if i >= len(words) or words[i].lower().rsplit("/", 1)[-1] not in _GIT_NAMES:
        return None
    j = i + 1
    # git's own options come before the subcommand
    while j < len(words):
        option = words[j]
        if option in ("-c", "--config-env"):
            # `-c core.hooksPath=X`, `--config-env core.hooksPath=VAR`
            if j + 1 < len(words) and words[j + 1].lower().startswith("core.hookspath="):
                return _HOOKS_PATH_WHY
            j += 2
        elif option.lower().startswith("--config-env=core.hookspath="):
            return _HOOKS_PATH_WHY
        elif option in _GIT_VALUE_OPTIONS:
            j += 2
        elif option.startswith("-"):
            j += 1
        else:
            break
    if j >= len(words):
        return None
    commit = words[j] == "commit"
    args = words[j + 1 :]
    k = 0
    while k < len(args):
        arg = args[k]
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg.partition("=")[0]
            # an abbreviation is only unambiguous from `--no-veri` on (`--no-v...` could be --no-verbose: git rejects those)
            if name == _NO_VERIFY or (len(name) >= 9 and _NO_VERIFY.startswith(name)):
                return _NO_VERIFY_WHY
            if commit and arg in _COMMIT_VALUE_OPTIONS:
                k += 1
        elif commit and len(arg) > 1 and arg[0] == "-":
            # a cluster of short flags: -an, -sn, -vn, -nu
            for pos, letter in enumerate(arg[1:], 1):
                if letter == "n":
                    return _COMMIT_N_WHY
                # these take a value: attached (-mfix) or, last in the cluster, the next word
                if letter in "mFCct":
                    if pos == len(arg) - 1:
                        k += 1
                    break
                # an optional value, attached only (-umode, -SKEYID), ends the cluster
                if letter in "uS":
                    break
        k += 1
    return None


def git_risk(command: str, tool: str = "") -> str | None:
    """Why `command` deserves a confirmation (see GIT_RISKS and _hook_bypass), or None. `tool` is Claude Code's tool name
    ("Bash", "PowerShell"): PowerShell quotes differently, so the word-based check is not used for it."""
    why = _no_verify_risk(command, tool)
    if why:
        return why
    for pattern, why in GIT_RISKS:
        if pattern.search(command):
            return why
    return None


def _no_verify_risk(command: str, tool: str) -> str | None:
    # every pattern here needs the word git
    if "git" not in command.lower():
        return None
    if tool != "PowerShell" and len(command) <= _TOKEN_MAX:
        try:
            why = _hook_bypass(command)
        except Exception:  # noqa: BLE001 - an unbalanced quote or anything unforeseen: the pattern below still judges
            why = None
        if why:
            return why
    return _NO_VERIFY_WHY if _NO_VERIFY_RE.search(_MESSAGE_VALUE.sub(" -m X", command)) else None


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
        return _judge(command, str(payload.get("tool_name", ""))) if command else 0
    except Exception:
        return 0  # a guard must never break the session: an error of its own lets the command through, silently


def _judge(command: str, tool: str = "") -> int:
    # The trust check goes first: it is the one with a ceiling on its time (see _VERIFY_TRUST, a few hundredths of a second
    # for any command), so padding aimed at the slower patterns below cannot keep it from running before the hook is killed.
    if trust_attempt(command):
        sys.stderr.write(
            "Blocked by the claude-hr guard: trusting or enabling (or editing the trust record of) a verify recipe lets "
            "commands run unattended after every turn, so only the user does it. Show the user .claude/hr-verify.toml "
            "and ask them to run `claude-hr-ctl verify enable` (or `verify trust`) in their own terminal.\n"
        )
        return 2
    for pattern, why in RULES:
        if pattern.search(command):
            sys.stderr.write(
                f"Blocked by the claude-hr secret guard: this command {why}, which would put credentials into the "
                "session transcript. Use the configured credential helper (gh auth, op run) or ask the user to "
                "run it themselves.\n"
            )
            return 2
    mode = os.environ.get("CLAUDE_HR_GIT_GUARD", "ask").strip().lower()
    if mode != "off":
        why = git_risk(command, tool)
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


def _headroom_summary(port: int, named: tuple[str, ...] = ()) -> dict | None:
    """Headroom's numbers for the status line, cached for 20 s in the state folder. `named`: the folders the payload names;
    a state folder inside one of them (or inside the working folder) is the repository's to have chosen, so the cache is
    neither read nor written (paths.hook_write)."""
    cache = paths.state_dir() / "statusline-headroom.json"
    usable = not paths.inside_cwd(cache, *named)
    try:
        if usable and time.time() - cache.stat().st_mtime < 20:
            return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    status, body = procutil.http_json(f"http://127.0.0.1:{port}/stats?cached=1", timeout=0.4)
    if status != 200 or not isinstance(body, dict):
        return None
    life = (body.get("persistent_savings") or {}).get("lifetime") or {}
    disp = body.get("display_session") or {}
    sub = (body.get("subscription_window") or {}).get("latest") or {}
    out = {
        "saved": (body.get("savings") or {}).get("total_tokens") or life.get("tokens_saved") or 0,
        "pct": disp.get("savings_percent"),
        "five_hour": (sub.get("five_hour") or {}).get("utilization_pct"),
        "seven_day": _field(sub, "seven_day", "utilization_pct"),
        "extra_used": _field(sub, "extra_usage", "used_credits_usd")
        if _field(sub, "extra_usage", "is_enabled") is True
        else None,
    }
    if usable:
        paths.hook_write(cache, json.dumps(out), *named)
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


def _named_folders(data) -> tuple[str, ...]:
    """The folders a status line payload names (`cwd`, `workspace.current_dir`, `workspace.project_dir`)."""
    found = (
        _field(data, "cwd"),
        _field(data, "workspace", "current_dir"),
        _field(data, "workspace", "project_dir"),
    )
    return tuple(f for f in found if isinstance(f, str) and f)


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
        hs = _headroom_summary(port, _named_folders(data))
        saved = _num(_field(hs, "saved"))
        if saved:
            pct = _num(_field(hs, "pct"))
            share = f" {pct:.0f}%" if pct else ""
            parts.append(f"✂ {_fmt_tokens(saved)} saved{share}")
        five_hour = _num(_field(hs, "five_hour"))
        if five_hour is not None:
            parts.append(f"5h {five_hour:.0f}%")
        seven_day = _num(_field(hs, "seven_day"))
        # the window only when it matters, so that the status line stays short
        if seven_day is not None and seven_day >= 80:
            parts.append(f"7d {seven_day:.0f}%")
        extra_used = _num(_field(hs, "extra_used"))
        # a full window means extra usage is being spent, at API prices
        if extra_used and seven_day is not None and seven_day >= 100:
            parts.append(f"extra ${extra_used:,.0f}")


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
