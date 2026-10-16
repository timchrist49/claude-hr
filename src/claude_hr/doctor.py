"""`claude-hr-ctl doctor` / `status`: one place that says whether this machine is healthy and what to do next."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from . import claudemd, creds, paths, procutil, resources, schedule, sysinfo
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


def _security_guidance() -> dict | None:
    """security-guidance's log summary, or None (plugin not installed, no log, or footprint missing)."""
    try:
        from .footprint import security_guidance_health, security_log_path
    except ImportError:  # tolerate a partial install
        return None
    return security_guidance_health(security_log_path())


def _verify_state(cwd: Path) -> str | None:
    """no-config | invalid | untrusted | modified | trusted for the repository in `cwd`; None if it cannot be told."""
    try:
        from . import verify

        return verify.trust_state(cwd, paths.state_dir())
    except Exception:  # noqa: BLE001 - doctor must never crash on an optional check
        return None


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
                    "review the file, then in a normal terminal (not a Claude Code session): claude-hr-ctl verify trust"
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
            h = hr.health()
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
            else:
                out.append(
                    Check(
                        "headroom proxy",
                        WARN if cfg.flag("headroom.fail_open", True) else FAIL,
                        f"not serving on {hr.url} ({h.status}); launches fall back to direct Claude",
                        "claude-hr-ctl headroom start   (cold start takes up to ~1 min)",
                    )
                )
            reg = read_json(paths.claude_global_json(), {}) or {}
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
    st = read_json(paths.state_dir() / "settings-merge.json", None)
    out.append(
        Check(
            "managed settings",
            OK if st else WARN,
            f"{len(st.get('leaves', {}))} keys managed" if st else "baseline not applied",
            "" if st else "claude-hr-ctl setup",
        )
    )
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
        installed = {
            p["id"].split("@")[0] for p in setup._installed_plugins() if p.get("scope") == "user"
        }
        plan = setup.plugin_plan()
        try:
            wanted = {
                w.split("@")[0]
                for w in setup.profile_plugins(plan, str(cfg.get("plugins.profile", "standard")))
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
    out += creds.run_checks(cfg)
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
