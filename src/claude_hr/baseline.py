"""The managed settings.json baseline. Built from config so every value is overridable.

Rules for what may appear here (learned the hard way):
  * Nothing that can break plain `claude` if claude-hr/Headroom is down -> NO ANTHROPIC_BASE_URL.
  * Nothing that changes model behaviour/quality without the user asking (no model, effort,
    thinking-token or output-length overrides).
  * Only documented keys (code.claude.com/docs/en/settings, env-vars).
  * `--dangerously-skip-permissions` must keep working -> never set disableBypassPermissionsMode
    unless the user opts into `security.profile = "strict"`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import paths
from .config import Config

# Secret-bearing paths Claude should never read through its file tools. `.env.example` stays readable.
SECRET_READ_DENY = [
    "Read(**/.env)",
    "Read(**/.env.local)",
    "Read(**/.env.*.local)",
    "Read(**/.env.production)",
    "Read(**/.env.development)",
    "Read(~/.ssh/**)",
    "Read(~/.aws/**)",
    "Read(~/.gnupg/**)",
    "Read(~/.config/gh/**)",
    "Read(~/.claude/.credentials.json)",
    "Read(~/.netrc)",
    "Read(~/.git-credentials)",
    "Read(~/.docker/config.json)",
    "Read(~/.kube/config)",
    "Read(~/.npmrc)",
]
# Bash cannot be fully policed by deny rules (they are not a security boundary); the plugin's
# PreToolUse hook is the second layer. These two are cheap and catch the common leak vectors.
SECRET_BASH_DENY = ["Bash(printenv *)", "Bash(env)"]

# The unattended verify gate runs the commands listed in .claude/hr-verify.toml (trusted by hash), and project settings can
# carry hooks: editing either is a change the user should see, so Claude asks first. Zero tokens, no process spawn.
TAMPER_ASK = [
    "Edit(**/.claude/hr-verify.toml)",
    "Write(**/.claude/hr-verify.toml)",
    "Edit(**/.claude/settings.json)",
    "Write(**/.claude/settings.json)",
    "Edit(**/.claude/settings.local.json)",
    "Write(**/.claude/settings.local.json)",
    # trusting a verify recipe is what lets commands run unattended: only the user does that
    "Bash(claude-hr-ctl verify trust*)",
    "Bash(claude-hr ctl verify trust*)",
    "Bash(claude-hr-ctl verify enable*)",
    "Bash(claude-hr ctl verify enable*)",
]

# claude-hr's own files: state/ holds the verify trust record and the gate counters (Claude never needs to edit it: denied)
# and config.toml can switch the gates, the repo-tool trust and the launch flags (a person edits it: Claude asks first).
# The rules are built from the home that is in use (CLAUDE_HR_HOME), plus the default ~/.claude-hr, so a relocated home is
# protected and an install whose runtime falls back to the default one still is.
DEFAULT_HOME_RULE = "~/.claude-hr"


def _relative_to_home(path: str) -> str | None:
    """`path` below the user's home directory as a forward-slash relative path ("" for the home itself), else None."""
    home = os.path.abspath(paths.home())
    a, h = os.path.normcase(path), os.path.normcase(home)
    if a == h:
        return ""
    prefix = h.rstrip("\\/") + os.sep
    if a.startswith(prefix):
        return path[len(prefix) :].replace("\\", "/").rstrip("/")
    return None


def rule_path(p: Path | str) -> str:
    """How a Claude Code permission rule spells the absolute path `p`: `~/rel` below the user's home, otherwise
    `//abs/path` (Windows: `//c/rest`, the POSIX form Claude matches against), always with forward slashes."""
    ap = os.path.abspath(p)
    rel = _relative_to_home(ap)
    if rel is not None:
        return f"~/{rel}" if rel else "~"
    posix = ap.replace("\\", "/")
    drive = re.match(r"([A-Za-z]):/(.*)", posix)
    if drive:
        return f"//{drive.group(1).lower()}/{drive.group(2).rstrip('/')}"
    return "//" + posix.lstrip("/").rstrip("/")


def _home_bases() -> list[str]:
    bases = [rule_path(paths.hr_home())]
    if DEFAULT_HOME_RULE not in bases:
        bases.append(DEFAULT_HOME_RULE)
    return bases


def state_deny() -> list[str]:
    """Edit/Write of claude-hr's state directory is denied."""
    return [f"{tool}({b}/state/**)" for b in _home_bases() for tool in ("Edit", "Write")]


def config_ask() -> list[str]:
    """Edit/Write of claude-hr's config.toml asks first."""
    return [f"{tool}({b}/config.toml)" for b in _home_bases() for tool in ("Edit", "Write")]


# security.stop_review: what counts as "off". The value comes from TOML or `config set`, so `false` arrives as a bool and `0` as
# an int; compare case-insensitively on the text. Anything else that is not a known "leave the plugin as shipped" word is
# reported by doctor (a typo must not silently keep a per-turn model review running).
STOP_REVIEW_OFF = frozenset({"off", "false", "0", "no", "disabled"})
STOP_REVIEW_PLUGIN_DEFAULT = frozenset({"plugin-default", "on", "true", "1", "yes", "enabled"})


def stop_review_mode(cfg: Config) -> str:
    """How security.stop_review reads: 'off', 'plugin-default' or 'unknown' (not a word we know)."""
    value = str(cfg.get("security.stop_review", "off")).strip().lower()
    if value in STOP_REVIEW_OFF:
        return "off"
    return "plugin-default" if value in STOP_REVIEW_PLUGIN_DEFAULT else "unknown"


# Read-only GitHub CLI calls used by the hr-gh skill. Pre-approving reads only; anything that writes still prompts.
GH_READONLY_ALLOW = [
    "Bash(gh issue view *)",
    "Bash(gh issue list *)",
    "Bash(gh pr view *)",
    "Bash(gh pr list *)",
    "Bash(gh pr diff *)",
    "Bash(gh pr checks *)",
    "Bash(gh run list *)",
    "Bash(gh run view *)",
    "Bash(gh repo view *)",
]

ATOMIC_PATHS = {("statusLine",)}


def build(
    cfg: Config, statusline_command: str | None = None, process_wrapper: str | None = None
) -> tuple[dict, set[tuple[str, ...]]]:
    c = cfg.section("claude")
    env: dict[str, str] = {"CLAUDE_HR_MANAGED": "1"}
    if bool(c.get("manage_updates", True)):
        # claude-hr stages upgrades itself. DISABLE_AUTOUPDATER (not DISABLE_UPDATES) so that
        # `claude update` / `claude install <ver>` keep working for the staged updater. It also
        # pauses plugin auto-update, which claude-hr's updater performs instead.
        env["DISABLE_AUTOUPDATER"] = "1"
    env["DISABLE_ERROR_REPORTING"] = "1"
    if str(c.get("privacy", "standard")) == "strict":
        # These also switch off feature-flag fetching (/skill-doctor, plugin sync, ...): opt-in only.
        env["DISABLE_TELEMETRY"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["FORCE_AUTOUPDATE_PLUGINS"] = "1"
    if process_wrapper:
        # Caps the agent-view background service and its sessions (Linux; ignored on Windows).
        env["CLAUDE_CODE_PROCESS_WRAPPER"] = process_wrapper
    if stop_review_mode(cfg) == "off":
        # security-guidance's end-of-turn model review (Opus, once per file-changing turn) is the one hidden recurring
        # spend in the standard profile. Its regex layer and its commit/push review stay on; audit before merging with
        # /security-review or `claude-hr-ctl audit`. "plugin-default" leaves the plugin untouched.
        env["ENABLE_STOP_REVIEW"] = "0"
    sub = str(cfg.get("tokens.subagent_model", "") or "")
    if sub:
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = sub
    for key, value in (c.get("env", {}) or {}).items():  # user-declared extras from config.toml
        env[str(key)] = str(value)

    doc: dict = {"$schema": "https://json.schemastore.org/claude-code-settings.json", "env": env}

    if bool(cfg.get("credentials.block_secret_reads", True)):
        doc["permissions"] = {"deny": SECRET_READ_DENY + SECRET_BASH_DENY}
    if bool(cfg.get("gates.protect_config", True)):
        doc.setdefault("permissions", {})["ask"] = [*TAMPER_ASK, *config_ask()]
        deny = doc.setdefault("permissions", {}).setdefault("deny", [])
        deny.extend(r for r in state_deny() if r not in deny)
    if bool(cfg.get("github.readonly_allow", True)):
        doc.setdefault("permissions", {})["allow"] = list(GH_READONLY_ALLOW)

    if str(c.get("channel", "") or "") and bool(c.get("manage_updates", True)):
        # the channel the updater follows, so Claude Code's own setting never says something else. A value the user set is
        # theirs: the merge keeps it (and with "auto" the resolved channel IS that value, so there is nothing to keep apart)
        from .claude_setup import resolve_channel  # lazy: claude_setup imports this module

        doc["autoUpdatesChannel"] = resolve_channel(cfg).effective

    days = int(cfg.get("maintenance.transcript_retention_days", 30) or 30)
    if days != 30 and days >= 1:  # 30 is Claude's own default; only write a deliberate change
        doc["cleanupPeriodDays"] = days

    if statusline_command and bool(c.get("statusline", True)):
        doc["statusLine"] = {"type": "command", "command": statusline_command, "padding": 0}

    if str(cfg.get("security.profile", "standard")) == "strict":
        doc.setdefault("permissions", {})["disableBypassPermissionsMode"] = "disable"
        doc["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
        doc["env"]["CLAUDE_HR_GIT_GUARD"] = (
            "deny"  # risky git commands are refused instead of confirmed
        )

    return doc, set(ATOMIC_PATHS)
