"""`claude-hr-ctl doctor` / `status`: one place that says whether this machine is healthy and what to do next."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from . import (
    baseline,
    claude_setup,
    claudemd,
    creds,
    paths,
    procutil,
    resources,
    schedule,
    sysinfo,
)
from .checks import FAIL, INFO, OK, WARN, Check
from .claude_setup import ClaudeSetup
from .config import Config
from .headroom import Headroom
from .jsonio import JsonFileError, read_json


def _is_own_proxy(base: str, port: int) -> bool:
    """True if `base` points at claude-hr's own Headroom proxy (loopback host and the configured port)."""
    from urllib.parse import urlparse

    try:
        u = urlparse(base)
        return (u.hostname or "") in ("127.0.0.1", "localhost", "::1") and (u.port or 80) == int(
            port
        )
    except ValueError:
        return False


def _desktop_markers(own_port: int) -> list[str]:
    """Signs of Headroom Desktop (a paid tray app that routes Claude Code through its own proxy on :6767)."""
    found: list[str] = []
    try:
        if (paths.claude_home() / "hooks" / "headroom-rtk-rewrite.sh").exists():
            found.append("hook ~/.claude/hooks/headroom-rtk-rewrite.sh")
        for name in (".bashrc", ".zshrc", ".profile", ".bash_profile"):
            f = paths.home() / name
            if (
                f.is_file()
                and f.stat().st_size < 1_000_000
                and "# >>> headroom:" in f.read_text(encoding="utf-8", errors="replace")
            ):
                found.append(f"~/{name} block")
                break
        if own_port != 6767 and procutil.port_listening("127.0.0.1", 6767):
            found.append("something is listening on 127.0.0.1:6767")
    except OSError:
        pass
    return found


_RESTART = "apply them when no Claude session is running: claude-hr-ctl headroom restart"


def _proxy_settings_check(hr: Headroom) -> Check | None:
    """A WARN when the running shared proxy was started with other settings than the config asks for now.

    The proxy keeps the flags and the environment it was started with until it is restarted, so a changed setting, or a
    release that changed a default, reaches a machine only at its next login or reboot. WARN, never FAIL: it works."""
    try:
        state = hr.settings_state()
    except Exception:  # noqa: BLE001 - an optional comparison must never break doctor
        return None
    if state == "changed":
        return Check(
            "headroom settings",
            WARN,
            "the running proxy was started with other settings than the configuration asks for now",
            f"Headroom is running with older settings; {_RESTART}",
        )
    if state == "older":
        return Check(
            "headroom settings",
            WARN,
            "the running proxy was started by an older claude-hr, which did not record its settings",
            f"Headroom was started by an older claude-hr; {_RESTART}",
        )
    return None


def _security_guidance() -> dict | None:
    """security-guidance's log summary, or None (plugin not installed, no log, or footprint missing)."""
    try:
        from .footprint import security_guidance_health, security_log_path
    except ImportError:  # tolerate a partial install
        return None
    return security_guidance_health(security_log_path())


def _verify_state(cwd: Path) -> str | None:
    """no-config | invalid | untrusted | modified | trusted for the repository that contains `cwd` (the end-of-turn gate and
    `verify status` both use the git top level, so doctor run from a subdirectory must too); None if it cannot be told."""
    try:
        from . import verify, worktree

        return verify.trust_state(worktree.repo_root(cwd) or cwd, paths.state_dir())
    except Exception:  # noqa: BLE001 - doctor must never crash on an optional check
        return None


def state_inside_repo(cwd: Path) -> bool:
    """Is `cwd` in a git work tree that contains the state folder? The end-of-turn hook then ignores the trust record (see
    paths.state_inside), whatever `verify status` would otherwise say. False outside a git work tree and when that cannot
    be told: doctor and `verify` must never fail on it."""
    try:
        from . import worktree

        top = worktree.repo_root(cwd)
        return top is not None and paths.state_inside(top, cwd)
    except Exception:  # noqa: BLE001 - an optional check must never break doctor
        return False


def state_inside_warning() -> str:
    return (
        f"the state folder {paths.state_dir()} is inside this repository, so the end-of-turn hook ignores its trust "
        "record (set CLAUDE_HR_HOME outside the repository)"
    )


def _footprint_check(cfg: Config, listing: list[dict]) -> Check | None:
    """Estimated always-on tokens of the installed plugins, for every profile. There is no cap unless the user sets one
    (footprint.budget_standard_tokens > 0, standard profile only): then OVER is a WARN.

    Costs nothing extra: it reuses the `claude plugin list --json` data the plugins check already fetched, and reads only the
    plugin files `claude-hr-ctl footprint` would. None (no check) when there is nothing to measure or anything goes wrong."""
    if not listing:
        return None
    try:
        from . import footprint

        fps = footprint.scan_installed(listing)
        tokens = footprint.always_on_tokens(fps)
        cap = int(cfg.get("footprint.budget_standard_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 - an optional estimate must never break doctor
        return None
    shown = f"~{tokens:,} always-on tokens from {len(fps)} plugin(s)"
    if cap <= 0 or str(cfg.get("plugins.profile", "standard")) != "standard":
        return Check("footprint", OK, shown + " (measured; no cap set)")
    over = tokens > cap
    return Check(
        "footprint",
        WARN if over else OK,
        f"{shown}; cap {cap:,}" + (f": OVER by {tokens - cap:,}" if over else ""),
        "claude-hr-ctl footprint   (per-plugin table; `claude-hr-ctl plugins audit` shows what to disable)"
        if over
        else "",
    )


def _channel_checks(cfg: Config) -> list[Check]:
    """Which Claude Code release channel claude-hr's updater follows, and a WARN when claude.channel names one explicitly and
    the autoUpdatesChannel in the user's settings.json says another (Claude Code's own setting and the updater then disagree)."""
    try:
        ch = claude_setup.resolve_channel(cfg)
    except Exception:  # noqa: BLE001 - an informational line must never break doctor
        return []
    user = ch.user if ch.user is not None else "unset"
    checks = [
        Check(
            "claude update channel",
            INFO,
            f"{ch.effective} (claude.channel={ch.raw}, settings.json autoUpdatesChannel={user})",
        )
    ]
    if ch.explicit and ch.user is not None and ch.user.strip().lower() != ch.effective:
        checks.append(
            Check(
                "claude channel conflict",
                WARN,
                f'claude.channel is "{ch.effective}" but autoUpdatesChannel in settings.json is "{ch.user}": '
                "claude-hr's updater and Claude Code's own setting follow different channels",
                "claude-hr-ctl config set claude.channel auto   (follow your autoUpdatesChannel), or "
                "claude-hr-ctl setup --only settings after changing claude.channel",
            )
        )
    return checks


def _unrouted_claude_check(cfg: Config) -> Check | None:
    """INFO when Claude Code processes run that claude-hr did not start: their traffic does not go through Headroom and
    nothing here limits them. The two ways to route Claude through claude-hr are named, one per line."""
    if (
        not cfg.flag("headroom.enabled", True)
        or str(cfg.get("headroom.mode", "persistent")) == "off"
    ):
        return None
    try:
        from . import updater

        n = len(updater.unmanaged_claude_processes())
        routed = n > 0 and updater.plain_claude_uses_proxy(Headroom(cfg))
    except Exception:  # noqa: BLE001 - an optional probe must never break doctor
        return None
    if not n:
        return None
    effect = (
        "their traffic goes through Headroom because ANTHROPIC_BASE_URL points at the proxy (no session limits, not seen "
        "by the update guard; a proxy restart waits for them)"
        if routed
        else "their traffic does not go through Headroom (no compression, no session limits, not seen by the update guard)"
    )
    return Check(
        "claude outside claude-hr",
        INFO,
        f"{n} Claude Code process(es) not started by claude-hr are running; {effect}\n"
        "         use: type `claude-hr` instead of `claude`\n"
        "         or:  claude-hr-ctl shim install   (then put ~/.claude-hr/bin first on PATH)",
    )


def _plan_meter_check() -> Check | None:
    """The plan meter from Headroom's last poll: a WARN when the 7-day window is full and extra usage (billed at API prices)
    is on or nearly used up, an INFO when the window is nearly full, otherwise one OK line. None without a poll."""
    from . import planmeter

    meter = planmeter.read_local()
    if not meter:
        return None
    flag = planmeter.warning(meter)
    if flag is None:
        return Check("plan meter", OK, planmeter.line(meter))
    level, text = flag
    return Check(
        "plan meter",
        WARN if level == "warn" else INFO,
        f"{planmeter.line(meter)}\n         {text}",
        "defer heavy workflows until the window resets, or use fewer agents and Sonnet; `claude-hr-ctl status` shows the meter",
    )


REAPPLY = "claude-hr-ctl setup --only settings claude-md plugins"


def _managed_settings_check(cfg: Config, hr: Headroom, claude: Path | None) -> Check:
    """Is the managed baseline applied, and is it the one this release would apply?

    What makes an applied baseline stale: setup was last run by another claude-hr version (the unattended self-update
    replaces the code but applies nothing; the daily update job re-applies the settings and the CLAUDE.md block, never
    plugins, doctor says so until then), setup applied it to another Claude config directory than this shell uses (a
    scheduled job cannot know CLAUDE_CONFIG_DIR, so it leaves that alone), and a pending change in `setup`'s own dry run:
    a value to set (config edited since, a value added by a release) or a managed key to retire (a config change that
    removes it from the baseline). Keys the user changed (`kept`) are theirs and never counted."""
    state_file = paths.state_dir() / "settings-merge.json"
    try:
        st = read_json(state_file, None)
    except JsonFileError as exc:
        return Check(
            "managed settings",
            FAIL,
            str(exc),
            f"fix or delete {state_file}, then: claude-hr-ctl setup --only settings",
        )
    if not st:
        return Check("managed settings", WARN, "baseline not applied", "claude-hr-ctl setup")
    detail = f"{len(st.get('leaves', {}))} keys managed"
    problems: list[str] = []
    stale, applied = claude_setup.baseline_is_stale()
    if stale:
        problems.append(
            f"applied by claude-hr {applied or 'before 0.2.0'}, this is {claude_setup.running_version()}: "
            "settings, CLAUDE.md and plugins may lack what this release adds"
        )
    try:
        rep = ClaudeSetup(cfg, hr, claude, dry=True).plan_settings()[3]
    except Exception:  # noqa: BLE001 - an unreadable settings.json is reported by its own check
        rep = None
    elsewhere = claude_setup.applied_elsewhere()
    if elsewhere:
        problems.append(
            f"applied to {elsewhere}, but this shell's Claude config is {paths.claude_settings()} "
            "(the daily update job leaves it alone)"
        )
    if rep is not None and rep.set:
        shown = ", ".join(rep.set[:3]) + (" ..." if len(rep.set) > 3 else "")
        problems.append(f"{len(rep.set)} baseline setting(s) not applied yet ({shown})")
    # a managed key the config no longer wants (security.stop_review back to plugin-default, a gate turned off) is pending too
    if rep is not None and rep.removed:
        shown = ", ".join(rep.removed[:3]) + (" ..." if len(rep.removed) > 3 else "")
        problems.append(f"{len(rep.removed)} managed setting(s) to retire ({shown})")
    if not problems:
        return Check("managed settings", OK, detail)
    return Check(
        "managed settings",
        WARN,
        f"{detail}; " + "; ".join(problems),
        REAPPLY if (stale or elsewhere) else "claude-hr-ctl setup --only settings",
    )


def _read_user_json(path: Path, name: str, out: list[Check]) -> dict:
    """A JSON file Claude or claude-hr wrote. Unparsable: report it as a failed check and carry on with {} (doctor is the tool
    that finds such problems, so it must not die on them, and claude-hr never rewrites the file)."""
    try:
        data = read_json(path, {}) or {}
    except JsonFileError as exc:
        out.append(
            Check(
                name,
                FAIL,
                str(exc),
                "fix the JSON by hand or restore a backup (claude-hr never overwrites an unparsable file)",
            )
        )
        return {}
    return data if isinstance(data, dict) else {}


def run(cfg: Config, cwd: Path, deep: bool = False) -> list[Check]:
    out: list[Check] = []
    hr = Headroom(cfg)
    out.append(
        Check(
            "platform",
            INFO,
            f"{platform.system()} {platform.release()} / python {sys.version.split()[0]}",
        )
    )

    # ---- Claude Code ----------------------------------------------------------------------
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        out.append(
            Check(
                "claude",
                FAIL,
                "Claude Code not found",
                "curl -fsSL https://claude.ai/install.sh | bash",
            )
        )
    else:
        v = procutil.run([str(claude), "--version"], timeout=30).stdout.strip()
        out.append(Check("claude", OK, f"{v} at {claude}"))
        try:
            settings = read_json(paths.claude_settings(), {}) or {}
        except (
            JsonFileError
        ) as exc:  # report it; never crash the tool whose job is to find problems
            settings = {}
            out.append(
                Check(
                    "settings.json",
                    FAIL,
                    str(exc),
                    "fix the JSON by hand (claude-hr never overwrites an unparsable file)",
                )
            )
        env = settings.get("env") or {}
        if cfg.flag("claude.manage_updates", True) and env.get("DISABLE_AUTOUPDATER") != "1":
            out.append(
                Check(
                    "claude updates",
                    WARN,
                    "built-in auto-updater is still on (claude-hr stages upgrades itself)",
                    "claude-hr-ctl setup",
                )
            )
        if cfg.flag("claude.manage_updates", True):
            out += _channel_checks(cfg)
        base = env.get("ANTHROPIC_BASE_URL")
        if base:
            alive = procutil.http_json(base.rstrip("/") + "/health", timeout=0.6)[0] == 200
            mine = _is_own_proxy(base, hr.port)
            if mine or not alive:
                out.append(
                    Check(
                        "global ANTHROPIC_BASE_URL",
                        OK if alive else FAIL,
                        f"settings.json routes ALL claude sessions to {base} ({'reachable' if alive else 'DOWN - claude cannot reach the API'})",
                        "remove env.ANTHROPIC_BASE_URL from ~/.claude/settings.json (claude-hr routes per launch instead)",
                    )
                )
            else:
                # A reachable proxy that is NOT claude-hr's: a settings.json env value replaces the one claude-hr sets for
                # each launch, so sessions go through that proxy and claude-hr's routing, limits and savings do not apply.
                out.append(
                    Check(
                        "foreign proxy",
                        WARN,
                        f"settings.json routes ALL claude sessions to {base}, which is not claude-hr's proxy (port {hr.port}); "
                        "it overrides claude-hr's per-launch routing",
                        "remove env.ANTHROPIC_BASE_URL from ~/.claude/settings.json, or uninstall the tool that wrote it",
                    )
                )
        if os.environ.get("CLAUDE_HR_DIRECT") == "1":
            out.append(
                Check("routing override", WARN, "CLAUDE_HR_DIRECT=1 is set: Headroom is bypassed")
            )

    desktop = _desktop_markers(hr.port)
    if desktop:
        out.append(
            Check(
                "headroom desktop",
                WARN,
                "Headroom Desktop appears to be installed ("
                + "; ".join(desktop)
                + "). It manages its own proxy and edits "
                "~/.claude/settings.json: it and claude-hr should not both route Claude",
                "use one of them: quit Headroom Desktop and remove its entries, or keep it and `claude-hr-ctl uninstall`",
            )
        )
    if not shutil.which("claude-hr-hook"):
        out.append(
            Check(
                "hook binary",
                WARN,
                "claude-hr-hook is not on PATH in this shell: the hr-gates end-of-turn hook cannot start "
                "(Claude shows a non-blocking hook notice each turn)",
                "uv tool update-shell, then open a new terminal",
            )
        )
    if cfg.flag("gates.enabled", True) and cfg.flag("verify.gate", True):
        state = _verify_state(cwd)
        if state_inside_repo(cwd):
            out.append(
                Check(
                    "verify state folder",
                    WARN,
                    state_inside_warning(),
                    "set CLAUDE_HR_HOME to a folder outside the repository, or unset it",
                )
            )
            if state == "trusted":
                state = None  # the hook does not run this recipe: no "the gate runs it" for it
        if state == "trusted":
            out.append(
                Check(
                    "verify recipe",
                    OK,
                    ".claude/hr-verify.toml is trusted: the end-of-turn test gate runs it",
                )
            )
        elif state in ("untrusted", "modified", "invalid"):
            why = {
                "untrusted": "has not been trusted",
                "modified": "changed since you trusted it",
                "invalid": "is not valid",
            }[state]
            out.append(
                Check(
                    "verify recipe",
                    WARN,
                    f".claude/hr-verify.toml {why}: the end-of-turn test gate does nothing in this repository",
                    "in a normal terminal (not a Claude Code session): claude-hr-ctl verify enable (shows the commands and asks you)"
                    if state != "invalid"
                    else "claude-hr-ctl verify status shows what is wrong",
                )
            )
    sg = _security_guidance()
    if sg and sg.get("skipped_reviews") and not sg.get("completed_reviews"):
        out.append(
            Check(
                "security-guidance",
                WARN,
                f"its model reviews were skipped {sg['skipped_reviews']} time(s) and none ran (no API credentials found: a known "
                "issue for /login users). Its regex layer still works",
                "run /security-review or `claude-hr-ctl audit` before merging; or set ANTHROPIC_API_KEY for the plugin",
            )
        )

    if baseline.stop_review_mode(cfg) == "unknown":
        out.append(
            Check(
                "security.stop_review",
                WARN,
                f"unrecognised value {cfg.get('security.stop_review')!r}: treated like plugin-default, so "
                "security-guidance's per-turn Opus review stays ON",
                "claude-hr-ctl config set security.stop_review off   (or plugin-default to keep the review on purpose)",
            )
        )

    # ---- tools ----------------------------------------------------------------------------
    out.append(
        Check(
            "uv",
            OK if procutil.cmd_exists("uv") else FAIL,
            "found" if procutil.cmd_exists("uv") else "missing",
            ""
            if procutil.cmd_exists("uv")
            else "https://docs.astral.sh/uv/getting-started/installation/",
        )
    )
    out.append(
        Check(
            "git",
            OK if procutil.cmd_exists("git") else FAIL,
            "found" if procutil.cmd_exists("git") else "missing",
        )
    )

    # ---- Headroom -------------------------------------------------------------------------
    reg = _read_user_json(paths.claude_global_json(), ".claude.json", out)
    if cfg.flag("headroom.enabled", True):
        b = hr.binary()
        if not b:
            out.append(
                Check(
                    "headroom",
                    FAIL,
                    "not installed",
                    "claude-hr-ctl setup  (or: claude-hr-ctl headroom install)",
                )
            )
        else:
            ver = hr.version() or "?"
            h = hr.health_patient()
            out.append(Check("headroom", OK, f"{ver} at {b}"))
            if h.ok:
                snap_rss = max([sysinfo.process_rss_mb(p) for p in hr._listener_pids()] or [0])
                out.append(
                    Check(
                        "headroom proxy",
                        OK,
                        f"healthy on {hr.url} (v{h.version}, ~{snap_rss:.0f} MB RSS)",
                    )
                )
                stale = _proxy_settings_check(hr)
                if stale:
                    out.append(stale)
            else:
                out.append(
                    Check(
                        "headroom proxy",
                        WARN if cfg.flag("headroom.fail_open", True) else FAIL,
                        f"not serving on {hr.url} ({h.status}); launches fall back to direct Claude",
                        "claude-hr-ctl headroom start   (cold start takes up to ~1 min)",
                    )
                )
            have = set((reg.get("mcpServers") or {}).keys())
            if cfg.flag("headroom.mcp", True) and "headroom" not in have:
                out.append(
                    Check(
                        "headroom MCP",
                        WARN,
                        "headroom_retrieve tool not registered (compressed output cannot be expanded)",
                        "claude-hr-ctl setup --only mcp",
                    )
                )
            if (
                cfg.get("headroom.code_memory") == "serena"
                and cfg.get("headroom.code_memory_scope") == "user"
                and "serena" not in have
            ):
                out.append(
                    Check(
                        "serena MCP",
                        WARN,
                        "code-memory server not registered",
                        "claude-hr-ctl setup --only mcp",
                    )
                )

    # ---- config drift ---------------------------------------------------------------------
    out.append(_managed_settings_check(cfg, hr, claude))
    md = paths.claude_user_md()
    has_block = md.exists() and "CLAUDE-HR:BEGIN" in md.read_text(encoding="utf-8", errors="ignore")
    out.append(
        Check(
            "CLAUDE.md baseline",
            OK if has_block else WARN,
            "imported" if has_block else "missing",
            "" if has_block else "claude-hr-ctl setup",
        )
    )
    rep = claudemd.lint(cwd, cfg)
    fails = [f for f in rep.findings if f.severity == "fail"]
    warns = [f for f in rep.findings if f.severity == "warn"]
    out.append(
        Check(
            "CLAUDE.md lint",
            FAIL if fails else (WARN if warns else OK),
            f"{rep.always_loaded_lines} always-loaded lines, ~{rep.est_tokens} tokens, {len(fails)} fail / {len(warns)} warn",
            "claude-hr-ctl claudemd lint" if (fails or warns) else "",
        )
    )

    # ---- plugins --------------------------------------------------------------------------
    if claude:
        setup = ClaudeSetup(cfg, hr, claude)
        # the one `claude plugin list` call: the footprint check below reuses its answer
        listing = setup._installed_plugins()
        installed = {p["id"].split("@")[0] for p in listing if p.get("scope") == "user"}
        plan = setup.plugin_plan()
        try:
            # what `plugins sync` would install: a plugin from a marketplace that is switched off (headroom.install_plugins
            # = false) is not wanted, or the warning below could never be cleared
            wanted = {
                w.split("@")[0]
                for w in setup.effective_plugins(plan, str(cfg.get("plugins.profile", "standard")))
            }
        except KeyError:
            wanted = set()
        missing = sorted(wanted - installed)
        out.append(
            Check(
                "plugins",
                OK if not missing else WARN,
                f"profile '{cfg.get('plugins.profile')}': {len(wanted) - len(missing)}/{len(wanted)} installed",
                f"claude-hr-ctl plugins sync   (missing: {', '.join(missing)})" if missing else "",
            )
        )
        fp_check = _footprint_check(cfg, listing)
        if fp_check:
            out.append(fp_check)

    # ---- resources ------------------------------------------------------------------------
    total, avail = sysinfo.memory_gb()
    adm = resources.admit(cfg)
    out.append(
        Check(
            "memory",
            OK if avail >= float(cfg.get("resources.min_free_gb", 2)) else WARN,
            f"{avail:.1f} GB free of {total:.1f} GB",
        )
    )
    out.append(Check("sessions", OK, f"{adm.running} running / limit {adm.limit}"))
    unrouted = _unrouted_claude_check(cfg)
    if unrouted:
        out.append(unrouted)
    plan = _plan_meter_check()
    if plan:
        out.append(plan)
    if paths.IS_LINUX:
        cap, _ = resources.memory_limits_gb(cfg)
        if cap > 0 and resources.caps_available():
            out.append(
                Check(
                    "resource caps",
                    OK,
                    f"each session runs in a systemd scope, MemoryMax {cap:.0f} GB",
                )
            )
        elif cap > 0:
            out.append(
                Check(
                    "resource caps",
                    WARN,
                    "per-session memory caps are NOT active (no systemd user session bus)",
                    "install dbus-user-session (apt install dbus-user-session), log in again; on WSL also enable systemd",
                )
            )
    free = sysinfo.disk_free_gb(paths.home())
    out.append(
        Check(
            "disk",
            OK if free >= float(cfg.get("monitor.disk_warn_gb", 10)) else WARN,
            f"{free:.0f} GB free",
        )
    )
    if deep:
        cd, complete = sysinfo.dir_size_info(paths.claude_home(), budget_s=30.0)
        big = cd >= float(cfg.get("monitor.claude_dir_warn_gb", 10))
        out.append(
            Check(
                "~/.claude size",
                WARN if big else OK,
                f"{'at least ' if not complete else ''}{cd:.1f} GB",
                "claude-hr-ctl cleanup" if big else "",
            )
        )

    # ---- self-update that did not finish -----------------------------------------------------
    from . import updater

    behind = updater.installed_is_behind(paths.hr_home() / "repo")
    if behind:
        out.append(
            Check(
                "claude-hr version",
                WARN,
                f"installed {behind[1]} but the checkout holds {behind[0]}: the last self-update did not finish",
                "claude-hr-ctl update --only self   (see ~/.claude-hr/logs/self-update.log)",
            )
        )

    # ---- schedule + credentials -----------------------------------------------------------
    jobs = schedule.status()
    out.append(
        Check(
            "scheduled jobs",
            OK if jobs and "no scheduled" not in jobs[0] else WARN,
            "; ".join(j.strip() for j in jobs[:4])[:200],
            "claude-hr-ctl schedule install" if "no scheduled" in jobs[0] else "",
        )
    )
    try:
        out += creds.run_checks(cfg)
    except Exception as exc:  # noqa: BLE001 - e.g. an unparsable ~/.claude.json, already reported above
        out.append(Check("credentials", WARN, f"credential checks could not run: {exc}"))
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
