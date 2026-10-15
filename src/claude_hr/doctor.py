"""`claude-hr-ctl doctor` / `status`: one place that says whether this machine is healthy and what to do next."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from . import claudemd, creds, paths, procutil, resources, schedule, sysinfo
from .checks import FAIL, INFO, OK, WARN, Check
from .claude_setup import ClaudeSetup
from .config import Config
from .headroom import Headroom
from .jsonio import JsonFileError, read_json


def run(cfg: Config, cwd: Path, deep: bool = False) -> list[Check]:
    out: list[Check] = []
    hr = Headroom(cfg)
    out.append(Check("platform", INFO, f"{platform.system()} {platform.release()} / python {sys.version.split()[0]}"))

    # ---- Claude Code ----------------------------------------------------------------------
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        out.append(Check("claude", FAIL, "Claude Code not found", "curl -fsSL https://claude.ai/install.sh | bash"))
    else:
        v = procutil.run([str(claude), "--version"], timeout=30).stdout.strip()
        out.append(Check("claude", OK, f"{v} at {claude}"))
        try:
            settings = read_json(paths.claude_settings(), {}) or {}
        except JsonFileError as exc:  # report it; never crash the tool whose job is to find problems
            settings = {}
            out.append(Check("settings.json", FAIL, str(exc), "fix the JSON by hand (claude-hr never overwrites an unparsable file)"))
        env = settings.get("env") or {}
        if cfg.flag("claude.manage_updates", True) and env.get("DISABLE_AUTOUPDATER") != "1":
            out.append(Check("claude updates", WARN, "built-in auto-updater is still on (claude-hr stages upgrades itself)", "claude-hr-ctl setup"))
        base = env.get("ANTHROPIC_BASE_URL")
        if base:
            alive = procutil.http_json(base.rstrip("/") + "/health", timeout=0.6)[0] == 200
            out.append(Check("global ANTHROPIC_BASE_URL", OK if alive else FAIL,
                             f"settings.json routes ALL claude sessions to {base} ({'reachable' if alive else 'DOWN - claude cannot reach the API'})",
                             "remove env.ANTHROPIC_BASE_URL from ~/.claude/settings.json (claude-hr routes per launch instead)" if not alive else ""))
        if os.environ.get("CLAUDE_HR_DIRECT") == "1":
            out.append(Check("routing override", WARN, "CLAUDE_HR_DIRECT=1 is set: Headroom is bypassed"))

    # ---- tools ----------------------------------------------------------------------------
    out.append(Check("uv", OK if procutil.cmd_exists("uv") else FAIL,
                     "found" if procutil.cmd_exists("uv") else "missing",
                     "" if procutil.cmd_exists("uv") else "https://docs.astral.sh/uv/getting-started/installation/"))
    out.append(Check("git", OK if procutil.cmd_exists("git") else FAIL, "found" if procutil.cmd_exists("git") else "missing"))

    # ---- Headroom -------------------------------------------------------------------------
    if cfg.flag("headroom.enabled", True):
        b = hr.binary()
        if not b:
            out.append(Check("headroom", FAIL, "not installed", "claude-hr-ctl setup  (or: claude-hr-ctl headroom install)"))
        else:
            ver = hr.version() or "?"
            h = hr.health()
            out.append(Check("headroom", OK, f"{ver} at {b}"))
            if h.ok:
                snap_rss = max([sysinfo.process_rss_mb(p) for p in hr._listener_pids()] or [0])
                out.append(Check("headroom proxy", OK, f"healthy on {hr.url} (v{h.version}, ~{snap_rss:.0f} MB RSS)"))
            else:
                out.append(Check("headroom proxy", WARN if cfg.flag("headroom.fail_open", True) else FAIL,
                                 f"not serving on {hr.url} ({h.status}); launches fall back to direct Claude",
                                 "claude-hr-ctl headroom start   (cold start takes up to ~1 min)"))
            reg = read_json(paths.claude_global_json(), {}) or {}
            have = set((reg.get("mcpServers") or {}).keys())
            if cfg.flag("headroom.mcp", True) and "headroom" not in have:
                out.append(Check("headroom MCP", WARN, "headroom_retrieve tool not registered (compressed output cannot be expanded)", "claude-hr-ctl setup --only mcp"))
            if cfg.get("headroom.code_memory") == "serena" and cfg.get("headroom.code_memory_scope") == "user" and "serena" not in have:
                out.append(Check("serena MCP", WARN, "code-memory server not registered", "claude-hr-ctl setup --only mcp"))

    # ---- config drift ---------------------------------------------------------------------
    st = read_json(paths.state_dir() / "settings-merge.json", None)
    out.append(Check("managed settings", OK if st else WARN,
                     f"{len(st.get('leaves', {}))} keys managed" if st else "baseline not applied",
                     "" if st else "claude-hr-ctl setup"))
    md = paths.claude_user_md()
    has_block = md.exists() and "CLAUDE-HR:BEGIN" in md.read_text(encoding="utf-8", errors="ignore")
    out.append(Check("CLAUDE.md baseline", OK if has_block else WARN, "imported" if has_block else "missing", "" if has_block else "claude-hr-ctl setup"))
    rep = claudemd.lint(cwd, cfg)
    fails = [f for f in rep.findings if f.severity == "fail"]
    warns = [f for f in rep.findings if f.severity == "warn"]
    out.append(Check("CLAUDE.md lint", FAIL if fails else (WARN if warns else OK),
                     f"{rep.always_loaded_lines} always-loaded lines, ~{rep.est_tokens} tokens, {len(fails)} fail / {len(warns)} warn",
                     "claude-hr-ctl claudemd lint" if (fails or warns) else ""))

    # ---- plugins --------------------------------------------------------------------------
    if claude:
        setup = ClaudeSetup(cfg, hr, claude)
        installed = {p["id"].split("@")[0] for p in setup._installed_plugins() if p.get("scope") == "user"}
        plan = setup.plugin_plan()
        try:
            wanted = {w.split("@")[0] for w in setup.profile_plugins(plan, str(cfg.get("plugins.profile", "standard")))}
        except KeyError:
            wanted = set()
        missing = sorted(wanted - installed)
        out.append(Check("plugins", OK if not missing else WARN,
                         f"profile '{cfg.get('plugins.profile')}': {len(wanted) - len(missing)}/{len(wanted)} installed",
                         f"claude-hr-ctl plugins sync   (missing: {', '.join(missing)})" if missing else ""))

    # ---- resources ------------------------------------------------------------------------
    total, avail = sysinfo.memory_gb()
    adm = resources.admit(cfg)
    out.append(Check("memory", OK if avail >= float(cfg.get("resources.min_free_gb", 2)) else WARN, f"{avail:.1f} GB free of {total:.1f} GB"))
    out.append(Check("sessions", OK, f"{adm.running} running / limit {adm.limit}"))
    free = sysinfo.disk_free_gb(paths.home())
    out.append(Check("disk", OK if free >= float(cfg.get("monitor.disk_warn_gb", 10)) else WARN, f"{free:.0f} GB free"))
    if deep:
        cd, complete = sysinfo.dir_size_info(paths.claude_home(), budget_s=30.0)
        big = cd >= float(cfg.get("monitor.claude_dir_warn_gb", 10))
        out.append(Check("~/.claude size", WARN if big else OK, f"{'at least ' if not complete else ''}{cd:.1f} GB",
                         "claude-hr-ctl cleanup" if big else ""))

    # ---- schedule + credentials -----------------------------------------------------------
    jobs = schedule.status()
    out.append(Check("scheduled jobs", OK if jobs and "no scheduled" not in jobs[0] else WARN,
                     "; ".join(j.strip() for j in jobs[:4])[:200], "claude-hr-ctl schedule install" if "no scheduled" in jobs[0] else ""))
    out += creds.run_checks(cfg)
    return out


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
