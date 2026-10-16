"""claude-hr-ctl: management CLI for the claude-hr distribution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path

from . import (
    __version__,
    claudemd,
    creds,
    doctor,
    hooks,
    maintenance,
    monitor,
    paths,
    plugins_audit,
    procutil,
    schedule,
    updater,
    usage,
    worktree,
)
from . import (
    config as config_mod,
)
from .checks import FAIL
from .claude_setup import ClaudeSetup, Step
from .headroom import Headroom
from .jsonio import atomic_write_text, read_json, write_json


def _color() -> bool:
    """Evaluated lazily: under pythonw.exe (scheduled jobs) sys.stdout is None at import time."""
    return bool(sys.stdout) and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


ALL_STEPS = ["headroom", "settings", "claude-md", "mcp", "plugins", "schedule"]


def _utf8_stdio() -> None:
    """Child tools print symbols (check marks, arrows); a cp1252 Windows console would crash on them."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def out(msg: str = "") -> None:
    print(msg, flush=True)


def _print_step(step: Step) -> None:
    mark = "ok " if step.ok else "ERR"
    out(f"[{mark}] {step.name}{' (changed)' if step.changed else ''}")
    for ln in step.lines:
        out(f"       {ln}")


# ---------------------------------------------------------------------------------------------
# setup / uninstall
# ---------------------------------------------------------------------------------------------
def _schedule_line(msg: str) -> tuple[str, bool]:
    """Format one scheduler message; failures are reported as errors instead of '[ok ] ... FAILED'."""
    bad = "FAILED" in msg or "could not" in msg
    return f"[{'ERR' if bad else 'ok '}] schedule: {msg}", bad


def cmd_setup(a, cfg) -> int:
    paths.ensure_dirs()
    steps = [s for s in ALL_STEPS if (not a.only or s in a.only) and s not in (a.skip or [])]
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    hr = Headroom(cfg)
    setup = ClaudeSetup(cfg, hr, claude, dry=a.dry_run)
    failed = 0
    if a.profile:
        cfg.data.setdefault("plugins", {})["profile"] = a.profile
        if not a.dry_run:
            # a profile chosen at install time must stick: doctor, `plugins sync` and every later `setup` read the config
            ok, msg = _persist_config("plugins.profile", a.profile)
            out(
                f"[{'ok ' if ok else 'ERR'}] plugin profile: {msg if not ok else f'{a.profile} saved to {paths.user_config_file()}'}"
            )
            failed += 0 if ok else 1
    if not a.dry_run and str(cfg.get("headroom.footprint", "auto")) == "lite":
        # the lite choice (often made with an environment variable for the installer) decides which Headroom extras later
        # reinstalls and upgrades use, so it must outlive this process
        ok, msg = _persist_config("headroom.footprint", "lite")
        out(
            f"[{'ok ' if ok else 'ERR'}] headroom footprint: {msg if not ok else 'lite saved to ' + str(paths.user_config_file())}"
        )
        failed += 0 if ok else 1
    if "headroom" in steps and cfg.flag("headroom.enabled", True):
        st = Step("headroom")
        cur = hr.version()
        want = str(cfg.get("headroom.version", "") or "")
        if cur and (not want or want == cur):
            st.add(f"installed {cur}")
        elif a.dry_run:
            st.add(f"would install {hr.install_spec()}")
        elif a.no_headroom_install:
            st.add("skipped install (--no-headroom-install)")
        else:
            st.changed = True
            st.add(
                f"installing {hr.install_spec()} (large download; first start prefetches models)"
            )
            r = hr.install(want or None)
            if r.returncode != 0:
                st.ok = False
                st.add("install failed: " + (r.stderr or r.stdout).strip()[-300:])
            else:
                st.add(f"installed {hr.version()}")
        if st.ok and not a.dry_run and hr.binary():
            h = hr.ensure(wait_s=0, announce=lambda m: st.add(m))
            st.add(
                "proxy healthy" if h.ok else "proxy starting in the background (cold start ~1 min)"
            )
        _print_step(st)
        failed += 0 if st.ok else 1
    for name, fn in (
        ("settings", lambda: setup.apply_settings(force=a.force)),
        ("claude-md", setup.sync_claude_md),
        ("mcp", setup.register_mcp),
        ("plugins", lambda: setup.sync_plugins(prune=a.prune)),
    ):
        if name in steps:
            if name in {"mcp", "plugins"} and not claude:
                out(f"[ERR] {name}: claude not found - install Claude Code first")
                failed += 1
                continue
            st = fn()
            _print_step(st)
            failed += 0 if st.ok else 1
    if "schedule" in steps and not a.dry_run:
        for m in schedule.install(cfg):
            line, bad = _schedule_line(m)
            out(line)
            failed += 1 if bad else 0
    st_file = paths.state_dir() / "install.json"
    if not a.dry_run:
        write_json(
            st_file,
            {"version": __version__, "ts": time.time(), "profile": cfg.get("plugins.profile")},
        )
    out(
        "\nDone. Open a NEW terminal, then run: claude-hr"
        if not failed
        else f"\n{failed} step(s) failed - see above; re-run after fixing."
    )
    return 1 if failed else 0


def _rmtree_force(path: Path) -> None:
    """rmtree that also removes read-only files (git objects on Windows make plain rmtree fail silently)."""
    import shutil
    import stat

    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onerror)


def cmd_uninstall(a, cfg) -> int:
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    hr = Headroom(cfg)
    setup = ClaudeSetup(cfg, hr, claude, dry=a.dry_run)
    sched_failed = False
    for m in schedule.remove() if not a.dry_run else []:
        line, bad = _schedule_line(m)
        out(line)
        sched_failed = sched_failed or bad
    _print_step(setup.revert_settings())
    _print_step(setup.remove_claude_md())
    _print_step(setup.unregister_mcp())
    if claude and not a.dry_run:
        for pid in ("hr-core@claude-hr",):
            procutil.run(
                [str(claude), "plugin", "uninstall", pid, "--scope", "user", "-y"], timeout=120
            )
        procutil.run([str(claude), "plugin", "marketplace", "remove", "claude-hr"], timeout=120)
    if not a.dry_run:
        hr.stop()
        for f in ("claude", "claude.exe", "claude.cmd", "claude.ps1"):
            (paths.bin_dir() / f).unlink(missing_ok=True)
        if a.purge:
            procutil.run(["uv", "tool", "uninstall", "headroom-ai"], timeout=300)
            _rmtree_force(paths.hr_home())
            if paths.hr_home().exists():
                out(
                    f"[warn] could not delete everything under {paths.hr_home()} (files in use?); remove it by hand"
                )
            else:
                out("[ok ] purged ~/.claude-hr and uninstalled Headroom")
    out(
        "Uninstalled managed configuration. Your own settings, plugins and CLAUDE.md content were left untouched."
    )
    if sched_failed:
        out(
            "[warn] the scheduled jobs could not be removed (see above); remove them with `claude-hr-ctl schedule remove` or in Task Scheduler / systemd"
        )
        return 1
    return 0


# ---------------------------------------------------------------------------------------------
# simple commands
# ---------------------------------------------------------------------------------------------
def cmd_doctor(a, cfg) -> int:
    checks = doctor.run(cfg, Path.cwd(), deep=a.deep)
    if a.json:
        out(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        for c in checks:
            out(c.line(_color()))
    return doctor.exit_code(checks)


def cmd_status(a, cfg) -> int:
    hr = Headroom(cfg)
    snap = monitor.snapshot(cfg)
    h = hr.health()
    out(f"claude-hr {__version__}")
    out(
        f"  Headroom   : {'healthy' if h.ok else h.status} {('v' + h.version) if h.version else ''} @ {hr.url}  (mode={cfg.get('headroom.mode')}, profile={cfg.get('headroom.savings_profile')})"
    )
    if h.ok:
        _, body = procutil.http_json(f"{hr.url}/stats?cached=1", timeout=2.0)
        if isinstance(body, dict):
            life = (body.get("persistent_savings") or {}).get("lifetime") or {}
            sw = (body.get("subscription_window") or {}).get("latest") or {}
            out(
                f"  Savings    : {life.get('tokens_saved', 0):,} prompt tokens + {life.get('tool_tokens_saved', 0):,} tool-schema tokens "
                f"over {life.get('requests', 0)} requests (lifetime)"
            )
            if sw.get("five_hour"):
                out(
                    f"  Plan usage : 5h window {sw['five_hour'].get('utilization_pct')}%  |  7d window {(sw.get('seven_day') or {}).get('utilization_pct')}%"
                )
    out(
        f"  Sessions   : {snap['sessions']} running / limit {snap['session_limit']}   RAM free {snap['ram_free_gb']} of {snap['ram_total_gb']} GB   disk free {snap['disk_free_gb']} GB"
    )
    led = usage.collect(1)
    t = led.total
    out(
        f"  Last 24h   : {t.messages} model calls, {usage._n(t.output)} output tokens, cache hit {t.cache_hit * 100:.0f}%"
    )
    rep = claudemd.lint(Path.cwd(), cfg)
    out(
        f"  CLAUDE.md  : {rep.always_loaded_lines} always-loaded lines (~{rep.est_tokens} tokens), {len([f for f in rep.findings if f.severity == 'fail'])} fail / {len([f for f in rep.findings if f.severity == 'warn'])} warn"
    )
    out("  (details: claude-hr-ctl doctor | claude-hr-ctl plugins audit | claude-hr-ctl usage)")
    return 0


def cmd_config(a, cfg) -> int:
    if a.action == "path":
        out(str(paths.user_config_file()))
    elif a.action == "show":
        out(json.dumps(cfg.data, indent=2))
    elif a.action == "get":
        out(json.dumps(cfg.get(a.key)))
    elif a.action == "set":
        ok, msg = _persist_config(a.key, a.value)
        out(msg)
        return 0 if ok else 1
    return 0


def _persist_config(key: str, raw_value: str) -> tuple[bool, str]:
    """Write one key into the per-user config file (~/.claude-hr/config.toml). Returns (ok, message). Never raises, and does
    not touch the file at all when the value is already what was asked for (so the installer cannot strip your comments)."""
    f = paths.user_config_file()
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8-sig")) if f.exists() else {}
        cur = data
        parts = key.split(".")
        for p in parts[:-1]:
            nxt = cur.setdefault(p, {})
            if not isinstance(nxt, dict):
                return False, f"cannot set {key}: '{p}' is not a table in {f}"
            cur = nxt
        value = config_mod._parse_scalar(raw_value)
        if parts[-1] in cur and cur[parts[-1]] == value and type(cur[parts[-1]]) is type(value):
            return True, f"{key} = {value!r}  ({f}, unchanged)"
        cur[parts[-1]] = value
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_dump_toml(data), encoding="utf-8")
        return True, f"{key} = {value!r}  ({f})"
    except (
        OSError,
        ValueError,
    ) as exc:  # ValueError covers tomllib.TOMLDecodeError and UnicodeDecodeError
        return False, f"cannot edit {f}: {exc}"


_BARE_KEY = __import__("re").compile(r"[A-Za-z0-9_-]+")


def _toml_key(k) -> str:
    k = str(k)
    return k if _BARE_KEY.fullmatch(k) else _toml_string(k)


def _toml_string(s: str) -> str:
    # JSON string syntax is valid TOML except that DEL must be escaped; ensure_ascii=False keeps non-BMP characters as
    # themselves (ensure_ascii=True would write surrogate pairs, which TOML forbids)
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_val(v) -> str:
    import datetime as _dt

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_val(x)}" for k, x in v.items()) + " }"
    return _toml_string(str(v))


def _dump_toml(data: dict, prefix: tuple = ()) -> str:
    lines = (
        ["# edited by `claude-hr-ctl config set` (comments are not preserved)"]
        if not prefix
        else []
    )
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
    for k, v in data.items():
        if isinstance(v, dict):
            path = (*prefix, k)
            lines.append("")
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
            lines.append(_dump_toml(v, path))
    return "\n".join(lines).strip("\n") + "\n"


def cmd_headroom(a, cfg) -> int:
    hr = Headroom(cfg)
    if a.action == "status":
        h = hr.health()
        out(
            f"{'healthy' if h.ok else h.status} {h.version} {hr.url}  binary={hr.binary()}  installed={hr.version()}"
        )
        return 0 if h.ok else 1
    if a.action in {"start", "warm"}:
        h = hr.ensure(wait_s=0 if a.action == "warm" else a.wait, announce=out)
        out("healthy" if h.ok else f"not ready yet ({h.status})")
        return 0 if (h.ok or a.action == "warm") else 1
    if a.action == "stop":
        out("stopped" if hr.stop() else "nothing to stop")
        return 0
    if a.action == "restart":
        hr.stop()
        h = hr.ensure(wait_s=a.wait, announce=out)
        out("healthy" if h.ok else f"not ready ({h.status})")
        return 0 if h.ok else 1
    if a.action in {"install", "upgrade"}:
        r = hr.install(a.version or None)
        out((r.stdout + r.stderr).strip()[-600:])
        return r.returncode
    if a.action == "logs":
        f = paths.logs_dir() / "headroom-proxy.log"
        out(
            "\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-a.lines :])
            if f.exists()
            else "no log yet"
        )
        return 0
    if a.action == "bench":
        ok = updater.Updater(cfg).smoke_headroom(wait_s=a.wait)
        out("compression smoke test: " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    return 2


def cmd_plugins(a, cfg) -> int:
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        out("claude not found")
        return 127
    hr = Headroom(cfg)
    setup = ClaudeSetup(cfg, hr, claude, dry=getattr(a, "dry_run", False))
    if a.action == "sync":
        st = setup.sync_plugins(a.profile, prune=a.prune)
        _print_step(st)
        return 0 if st.ok else 1
    if a.action == "update":
        _print_step(setup.update_marketplaces())
        return 0
    if a.action == "audit":
        rows = plugins_audit.audit_plugins(cfg, claude)
        out(plugins_audit.format_plugins(rows))
        return 0
    if a.action == "list":
        for p in setup._installed_plugins():
            out(
                f"{p['id']:<56} {p.get('scope', ''):<8} {'on ' if p.get('enabled') else 'off'} {p.get('version', '')}"
            )
        return 0
    if a.action == "suggest":
        import tomllib as _t

        with (paths.package_data_dir() / "plugins.toml").open("rb") as fh:
            plan = _t.load(fh)
        cwd = Path.cwd()
        shown = False
        for pid, meta in plan.get("project_scoped", {}).items():
            if any((cwd / m).exists() for m in meta["markers"]):
                shown = True
                out(
                    f"{pid}: this looks like a matching project -> `claude plugin install {pid} --scope project`\n    {meta['why']}"
                )
        if not shown:
            out("no project-scoped plugin suggestions for this directory")
        return 0
    return 2


def cmd_skills(a, cfg) -> int:
    msgs, stats = plugins_audit.audit_skills(cfg, Path.cwd())
    for m in msgs:
        out(m)
    out(
        f"skills: {stats['user_project_skills']} user/project, {stats['plugin_skills']} from plugins; "
        f"description text that loads every session: {stats['description_chars']:,} chars (~{stats['description_chars'] // 4:,} tokens)"
    )
    if stats["description_chars"] > 12000:
        out(
            "WARN: large skill listing - Claude drops descriptions of the least-used skills when over budget. "
            "Disable unused plugins/skills (disable-model-invocation: true)."
        )
    return 1 if any(m.startswith("FAIL") for m in msgs) else 0


def cmd_claudemd(a, cfg) -> int:
    if a.action == "lint":
        rep = claudemd.lint(Path(a.path).resolve() if a.path else Path.cwd(), cfg)
        out(rep.to_json() if a.json else claudemd.format_report(rep))
        return 1 if rep.has_fail() else 0
    if a.action == "learn":
        hr = Headroom(cfg)
        b = hr.binary()
        if not b:
            out("headroom not installed")
            return 1
        cmd = [str(b), "learn", *(["--apply"] if a.apply else [])]
        out(
            "Running `headroom learn` "
            + ("(writing to CLAUDE.local.md)" if a.apply else "(dry run: nothing written)")
        )
        r = procutil.run(cmd, timeout=600, env=hr.server_env())
        out((r.stdout + r.stderr).strip()[-3000:])
        if a.apply:
            rep = claudemd.lint(Path.cwd(), cfg)
            over = [f for f in rep.findings if f.rule == "L1" and "auto-learned" in f.message]
            for f in over:
                out(f"WARN {f.message}")
        return r.returncode
    return 2


def cmd_usage(a, cfg) -> int:
    if a.tools:
        out(usage.format_tool_use(usage.collect_tool_use(a.days), a.days))
        return 0
    led = usage.collect(a.days)
    if a.snapshot:
        d = paths.state_dir() / "usage"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{time.strftime('%Y-%m')}.json"
        cur = read_json(f, {}) or {}
        for day, b in led.by_day.items():
            cur[day] = {
                "input": b.input,
                "output": b.output,
                "cache_write": b.cache_write,
                "cache_read": b.cache_read,
                "messages": b.messages,
            }
        write_json(f, cur)
        out(f"snapshot written: {f}")
        return 0
    out(usage.format_ledger(led, a.by))
    return 0


def cmd_update(a, cfg) -> int:
    u = updater.Updater(cfg, dry=a.check, force=a.force, allow_downgrade=a.allow_downgrade)
    rep = u.run(a.only or None)
    for r in rep.results:
        out(r.line())
    return 0 if rep.ok else 1


def cmd_schedule(a, cfg) -> int:
    msgs = {
        "install": lambda: schedule.install(cfg),
        "remove": schedule.remove,
        "status": schedule.status,
    }[a.action]()
    for m in msgs:
        out(m)
    return 1 if a.action != "status" and any(_schedule_line(m)[1] for m in msgs) else 0


def cmd_monitor(a, cfg) -> int:
    if a.action == "tick":
        res = monitor.tick(cfg)
        out(json.dumps({"alerts": res["alerts"], "fired": res["fired"], "note": res["note"]}))
        return 0
    f = paths.state_dir() / "health.json"
    out(json.dumps(read_json(f, {}), indent=2))
    return 0


def cmd_cleanup(a, cfg) -> int:
    p = maintenance.plan(cfg)
    for it in sorted(p.items, key=lambda i: -i.size_mb)[:40]:
        out(f"{it.size_mb:9.1f} MB  {it.kind:<12} {it.path}  ({it.reason})")
    for s in p.skipped[:10]:
        out(f"  skipped {s}")
    out(f"\n{len(p.items)} item(s), {p.total_mb / 1024:.2f} GB reclaimable")
    if a.apply:
        n, freed = maintenance.apply(p)
        out(f"removed {n} item(s), freed {freed / 1024:.2f} GB")
    elif p.items:
        out("dry run - re-run with --apply to delete")
    return 0


def cmd_creds(a, cfg) -> int:
    if a.action == "doctor":
        checks = creds.run_checks(cfg)
        for c in checks:
            out(c.line(_color()))
        return 1 if any(c.status == FAIL for c in checks) else 0
    hits, scanned = creds.scan_transcripts(a.days)
    out(f"scanned {scanned} transcript file(s) from the last {a.days} day(s)")
    for f, kind, n in hits[:40]:
        out(f"  {n:4d} x {kind:<26} {f}")
    if hits:
        out(
            "\nSecrets in transcripts are plaintext on disk. Rotate each affected credential, then delete the file "
            "(or `claude purge`). Values are never printed here."
        )
    else:
        out("no token-shaped secrets found")
    return 1 if hits else 0


def cmd_shim(a, cfg) -> int:
    b = paths.bin_dir()
    b.mkdir(parents=True, exist_ok=True)
    sibling = Path(sys.executable).with_name("claude-hr.exe" if paths.IS_WINDOWS else "claude-hr")
    found = [sibling] if sibling.is_file() else procutil.which_all("claude-hr")
    target = str(found[0]) if found else "claude-hr"
    if a.action == "remove":
        for f in ("claude", "claude.exe", "claude.cmd", "claude.ps1"):
            (b / f).unlink(missing_ok=True)
        out("shim removed")
        return 0
    if paths.IS_WINDOWS:
        # A real .exe, NOT a .cmd: cmd.exe re-parses arguments, so `claude -p "a&b"`, `|`, `>`, `%VAR%`, `^` and
        # multi-line prompts would be mangled (or executed) on the way through a batch file.
        if not found:
            out("claude-hr.exe not found; run the installer first")
            return 1
        import shutil

        shutil.copy2(target, b / "claude.exe")
        (b / "claude.cmd").unlink(missing_ok=True)
        out(f"created {b / 'claude.exe'} (copy of the claude-hr launcher)")
        out(
            f'Put it first on PATH (user): [Environment]::SetEnvironmentVariable("Path", "{b};" + [Environment]::GetEnvironmentVariable("Path","User"), "User")'
        )
    else:
        f = b / "claude"
        f.write_text(f'#!/bin/sh\nexec "{target}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)
        out(f"created {f}")
        out(f'Put it first on PATH, e.g. add to your shell profile:  export PATH="{b}:$PATH"')
    out(
        "Plain `claude` now goes through claude-hr. `--hr-direct` or CLAUDE_HR_DIRECT=1 bypasses Headroom."
    )
    return 0


def cmd_wt(a, cfg) -> int:
    if a.action == "new":
        rc, msg = worktree.new(a.name, base=a.base)
        out(msg)
        return rc
    if a.action == "list":
        for w in worktree.list_all():
            out(f"{w.get('worktree', '?'):<70} {w.get('branch', w.get('HEAD', ''))}")
        return 0
    rc, msg = worktree.remove(a.name, force=a.force, delete_branch=a.delete_branch)
    out(msg)
    return rc


def cmd_workflows(a, cfg) -> int:
    """Saved Claude Workflow scripts (opt-in multi-agent orchestration). Plugins cannot ship them, so they are copied
    into a project's .claude/workflows/ on request. Running one spawns many agents: claude-hr never does that on its own."""
    src_dir = paths.package_data_dir() / "workflows"
    scripts = sorted(src_dir.glob("*.js")) if src_dir.is_dir() else []
    if a.action == "list":
        for s in scripts:
            first = next(
                (ln for ln in s.read_text(encoding="utf-8").splitlines() if "description:" in ln),
                "",
            )
            summary = first.split("description:", 1)[-1].strip().strip(",").strip("'")
            out(f"{s.stem:<24} {summary}")
        out(
            "\nInstall into a project:  claude-hr-ctl workflows install    then in Claude: ask it to run the 'hr-review-changes' or 'hr-plan-execute' workflow"
        )
        return 0
    root = worktree.repo_root(Path(a.dir) if a.dir else None) or (
        Path(a.dir) if a.dir else Path.cwd()
    )
    dest = root / ".claude" / "workflows"
    dest.mkdir(parents=True, exist_ok=True)
    for s in scripts:
        target = dest / s.name
        if (
            target.exists()
            and target.read_text(encoding="utf-8") != s.read_text(encoding="utf-8")
            and not a.force
        ):
            out(f"{target}: exists and differs; kept (use --force to overwrite)")
            continue
        target.write_text(s.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        out(f"installed {target}")
    return 0


def cmd_daemon(a, cfg) -> int:
    from . import launcher

    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        return 127
    hr = Headroom(cfg)
    st = launcher._daemon_status(claude)
    ours = launcher._daemon_is_ours(st, hr.url) if st["running"] else False
    if a.action == "status":
        out(json.dumps({**st, "routed_through_headroom": ours}, indent=2))
        return 0
    procutil.run([str(claude), "daemon", "stop", "--any"], timeout=30)
    out(
        "Claude background service stopped; the next `claude-hr agents` / `--bg` starts one routed through Headroom."
    )
    return 0


def cmd_job(a, cfg) -> int:
    paths.ensure_dirs()
    log = paths.logs_dir() / f"job-{a.name}.log"
    t0 = time.time()
    msg = ""
    rc = 0
    try:
        if a.name == "warm":
            hr = Headroom(cfg)
            if cfg.flag("headroom.enabled", True) and hr.binary():
                h = hr.ensure(wait_s=0)
                msg = "proxy healthy" if h.ok else "proxy start requested"
        elif a.name == "monitor":
            res = monitor.tick(cfg)
            msg = f"alerts={res['alerts']} note={res['note']}"
        elif a.name == "update":
            if cfg.flag("updates.enabled", True):
                rep = updater.Updater(cfg, dry=not cfg.flag("updates.auto_apply", True)).run()
                msg = " | ".join(r.line() for r in rep.results)
                rc = 0 if rep.ok else 1
        elif a.name == "maintain":
            p = maintenance.plan(cfg)
            n, freed = maintenance.apply(p)
            msg = f"cleanup removed {n} items ({freed / 1024:.2f} GB)"
            try:
                from . import auditlog

                dropped = auditlog.prune(int(cfg.get("gates.trail_days", 90)))
                if dropped:
                    msg += f"; audit trail: dropped {dropped} old month file(s)"
            except Exception:  # noqa: BLE001 - housekeeping must not fail the job
                pass
            if cfg.flag("maintenance.usage_snapshot", True):
                led = usage.collect(3)
                d = paths.state_dir() / "usage"
                d.mkdir(parents=True, exist_ok=True)
                f = d / f"{time.strftime('%Y-%m')}.json"
                cur = read_json(f, {}) or {}
                for day, b in led.by_day.items():
                    cur[day] = {
                        "input": b.input,
                        "output": b.output,
                        "cache_write": b.cache_write,
                        "cache_read": b.cache_read,
                        "messages": b.messages,
                    }
                write_json(f, cur)
            hr_learn = Headroom(cfg)
            if (
                cfg.flag("headroom.enabled", True)
                and cfg.flag("maintenance.learn_report", True)
                and hr_learn.binary()
            ):
                # read-only (no --apply): auto-written instructions can lower accuracy, so a human decides
                r = procutil.run(
                    [str(hr_learn.binary()), "learn", "--all"],
                    timeout=600,
                    env=hr_learn.server_env(),
                )
                atomic_write_text(
                    paths.state_dir() / "learn-report.txt",
                    (r.stdout or "") + (r.stderr or ""),
                    follow_symlinks=False,
                )
                msg += " | learn report written"
            hits, scanned = creds.scan_transcripts(7)
            if hits:
                monitor.deliver(
                    cfg,
                    monitor.Alert(
                        "transcript-secrets",
                        "Secrets found in Claude transcripts",
                        f"{len(hits)} finding(s) in {scanned} files; run `claude-hr-ctl creds scan`",
                    ),
                )
        else:
            return 2
    except Exception as exc:  # a scheduled job must never raise into the scheduler
        msg = f"EXCEPTION {type(exc).__name__}: {exc}"
        rc = 1
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} rc={rc} {time.time() - t0:.1f}s {msg}\n")
    return rc


# ---------------------------------------------------------------------------------------------
# ---------------------------------------------------------------------------------------------
# dev workflow: format / verify / audit / scaffold / footprint / trail
# ---------------------------------------------------------------------------------------------
def _repo_root() -> Path:
    return worktree.repo_root(Path.cwd()) or Path.cwd()


def _not_a_person() -> str | None:
    """Why nobody can be assumed to be at the keyboard (None = a person plausibly is).

    Used for the two decisions that let a repository's own commands run: `verify trust` and `verify run` of an
    untrusted recipe. An agent that was talked into running them must not be able to approve itself: there is no
    --yes flag, and a shell that belongs to a Claude Code session or has no terminal is refused. This is a speed bump
    (a determined agent can allocate a pty); the permission prompts and the bash guard are the other layers.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "this shell belongs to a Claude Code session"
    if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
        return "there is no interactive terminal"
    return None


def _show_recipe(root: Path, recipe) -> None:
    for tier in ("fast", "full"):
        shown = list(getattr(recipe, tier))
        if tier == "full":
            fast = {c.name for c in recipe.fast}
            shown = [
                c for c in shown if c.name not in fast
            ]  # `full` repeats `fast`: show only the additions
        for c in shown:
            from . import verify

            out(
                f"  [{tier}] {c.name}: {verify.command_line(c)}   (timeout {c.timeout_s}s, cwd {c.cwd})"
            )


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} type 'yes': ").strip().lower() == "yes"
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_fmt(a, cfg) -> int:
    """Format (or, with --check, only report) files with a formatter the repo ALREADY configures. No arguments: say which."""
    from . import fmt, stop_gate

    root = _repo_root()
    trusted = stop_gate.repo_tools_allowed(cfg, root)
    if not a.paths and not a.changed:
        found = fmt.describe(root, trusted=trusted)
        if not found:
            out(
                "no formatter is configured in this repository (claude-hr only uses one the repo already sets up: "
                "ruff/black, prettier/biome, gofmt, rustfmt)"
            )
            return 0
        for d in found:
            refused = "" if d.get("usable", True) else f"   NOT USED: {d.get('reason', '')}"
            out(
                f"{d['name']:<9} {' '.join(d.get('extensions', []))}   "
                f"(configured by {d.get('configured_by', '?')}; {d.get('exe', '')}){refused}"
            )
        return 0
    kw = {
        "timeout_s": max(
            float(cfg.get("format.timeout_s", 30)), 120.0
        ),  # you asked for it: no hurry
        "max_kb": int(cfg.get("format.max_file_kb", 512)),
        "skip_globs": tuple(cfg.get("format.skip_globs", []) or ()),
        "allow": list(cfg.get("format.allow", []) or []) or None,
        "check": bool(a.check),
        "clean_gate": False,  # deliberate command: format the whole file even if it had drifted
        "trusted": trusted,
    }
    if a.paths:
        rep = fmt.format_paths(root, [Path(os.path.abspath(x)) for x in a.paths], **kw)
    else:
        rep = fmt.format_changed(root, max_files=500, **kw)
    for p in rep.formatted:
        out(f"{'would reformat' if a.check else 'reformatted'}: {p}")
    for p, why in rep.skipped:
        out(f"skipped: {p} ({why})")
    for p, why in rep.errors:
        out(f"error: {p}: {why}")
    out(
        f"{len(rep.formatted)} {'need formatting' if a.check else 'reformatted'}, "
        f"{len(rep.errors)} error(s), {len(rep.skipped)} skipped"
        + (f" ({', '.join(rep.formatters)})" if rep.formatters else "")
    )
    if any("verify trust" in why for _, why in rep.skipped):
        out(
            "some files were skipped because their formatter or its configuration runs code from this repository: "
            "read it, then `claude-hr-ctl verify trust` in a terminal (or set format.repo_tools = always)"
        )
    if rep.errors:
        return 2
    return 1 if (a.check and rep.formatted) else 0


def cmd_verify(a, cfg) -> int:
    """The repo's verify recipe (.claude/hr-verify.toml): propose it, trust it, run it."""
    from . import verify

    root = _repo_root()
    cp = verify.config_path(root)
    if a.action == "init":
        recipe = verify.discover(root)
        text = verify.render_toml(recipe)
        if not a.write:
            out(text)
            out(
                f"# dry run: `claude-hr-ctl verify init --write` saves this to {cp}; review it, then run "
                "`claude-hr-ctl verify trust` in a terminal"
            )
            return 0
        if cp.exists() and not a.force:
            out(f"{cp} already exists (use --force to overwrite); review it, then trust it")
            return 1
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(text, encoding="utf-8", newline="\n")
        out(
            f"wrote {cp}\nReview it (these commands run at the end of each Claude turn), then, in a terminal "
            "outside Claude Code: claude-hr-ctl verify trust"
        )
        return 0
    try:
        recipe = verify.load(root)
    except ValueError as exc:
        out(f"{cp}: {exc}")
        return 2
    state = verify.trust_state(root, paths.state_dir())
    if a.action == "status":
        if recipe is None:
            out(f"no recipe at {cp}  ->  `claude-hr-ctl verify init --write`")
            return 0
        meaning = {
            "trusted": "TRUSTED: the end-of-turn gate runs these checks",
            "untrusted": "NOT trusted: the gate does nothing until a person runs `claude-hr-ctl verify trust`",
            "modified": "CHANGED since it was trusted: the gate does nothing until it is reviewed and trusted again",
        }.get(state, state)
        out(f"{cp}: {meaning}")
        _show_recipe(root, recipe)
        return 0
    if a.action == "untrust":
        removed = verify.untrust(root, paths.state_dir())
        out(
            "trust removed: the end-of-turn gate will not run this repository's commands"
            if removed
            else "this repository was not trusted"
        )
        return 0
    if a.action == "trust":
        if recipe is None:
            out(f"no {cp}; create one with `claude-hr-ctl verify init --write`")
            return 1
        why = _not_a_person()
        if why:
            out(
                f"refusing to trust a verify recipe: {why}.\n"
                "Trusting lets the end-of-turn gate run these commands unattended, so a person has to do it: open a "
                f"normal terminal (not a Claude Code session), cd to {root} and run `claude-hr-ctl verify trust`."
            )
            return 1
        out(f"The end-of-turn gate will run these commands without asking, in {root}:")
        _show_recipe(root, recipe)
        try:
            from . import audit

            findings = audit.preflight(root)
            if findings:
                out("\nThe repository's agent surface has findings you should read first:")
                out(audit.render(findings, [], root=root))
        except Exception:  # noqa: BLE001 - the preflight is advice; it must not block the decision
            pass
        if not _confirm("\nTrust exactly this file?"):
            out("not trusted")
            return 1
        try:
            digest = verify.trust(root, paths.state_dir())
        except ValueError as exc:
            out(str(exc))
            return 1
        out(f"trusted ({digest[:12]}...): the end-of-turn gate may now run these commands")
        return 0
    # ---- run -------------------------------------------------------------------------------------------------
    if recipe is None:
        recipe = verify.discover(root)
        out("(no .claude/hr-verify.toml: these are the commands that would be proposed)")
    if state != "trusted":
        # a recipe nobody approved is the repository's own text: it only runs with a person's say-so, never for an agent
        why = _not_a_person()
        if why:
            out(
                f"refusing to run an untrusted recipe: {why}.\n"
                "Ask the user to review `.claude/hr-verify.toml` and run `claude-hr-ctl verify trust` in a terminal."
            )
            return 1
        out(f"These commands come from {root} and have not been trusted:")
        _show_recipe(root, recipe)
        if not _confirm("\nRun them once now?"):
            return 1
    checks = list(recipe.full if (a.tier == "full" and recipe.full) else recipe.fast)
    if not checks:
        out("nothing to run: no checks were found for this repository")
        return 1
    res = verify.run(root, checks, total_timeout_s=float(sum(c.timeout_s for c in checks)))
    for r in res.results:
        flag = "ok  " if r.ok else "FAIL"
        out(f"[{flag}] {r.name} ({r.seconds:.1f}s, exit {r.rc})")
        if not r.ok and r.output:
            out("\n".join("      " + ln for ln in r.output.splitlines()))
    for name in res.skipped:
        out(f"[skip] {name} (not started: time budget)")
    if cfg.flag("verify.test_guard", True):
        for note in verify.test_weakening(root):
            out(f"[warn] possible test weakening: {note}")
    return 0 if res.ok else 1


def cmd_audit(a, cfg) -> int:
    """Read-only repository audit: agent-config attack surface, workflow lint, secrets. Zero tokens, runs nothing from the repo."""
    from . import audit

    root = Path(os.path.abspath(a.path)) if a.path else _repo_root()
    text, code = audit.report(
        root,
        tools=bool(a.tools and cfg.flag("audit.tools", True)),
        as_json=a.json,
        strict=a.strict,
        timeout_s=float(cfg.get("audit.timeout_s", 60)),
    )
    out(text)
    return code


def cmd_init_repo(a, cfg) -> int:
    """Scaffold issue templates, PR template, CODEOWNERS, Dependabot, docs/plans, REVIEW.md, .editorconfig. Dry run by default."""
    from . import initrepo

    root = Path(os.path.abspath(a.path)) if a.path else _repo_root()
    commands = None
    try:  # the CI skeleton runs the same checks the end-of-turn gate would
        from . import verify

        commands = [c.argv for c in verify.discover(root).fast] or None
    except Exception:  # noqa: BLE001
        pass
    try:
        actions, notes = initrepo.plan_with_notes(root, only=a.only, commands=commands)
    except ValueError as exc:  # an unknown group name; the message lists the valid ones
        out(str(exc))
        return 2
    out(initrepo.format_plan(actions, notes))
    if not a.apply:
        out(
            "dry run: nothing written. `claude-hr-ctl init-repo --apply` creates the files marked new "
            "(existing files are never overwritten); review with git diff, then commit."
        )
        return 0
    res = initrepo.apply_detailed(root, actions)
    for p in res.written:
        out(f"created {p}")
    for p, why in res.errors:
        out(f"failed {p}: {why}")
    out("nothing was committed or pushed: review with `git status` / `git diff`.")
    return 1 if res.errors else 0


def cmd_footprint(a, cfg) -> int:
    """What the installed plugins cost in always-on context, including what `claude plugin details` does not count."""
    from . import footprint

    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        out("Claude Code not found")
        return 1
    r = procutil.run([str(claude), "plugin", "list", "--json"], timeout=60)
    try:
        listing = json.loads(r.stdout or "[]")
    except ValueError:
        out("could not read `claude plugin list --json`")
        return 1
    if isinstance(listing, dict):
        listing = listing.get("plugins", [])
    fps = footprint.scan_installed(listing if isinstance(listing, list) else [])
    # the budget is for the standard profile; other profiles add plugins on purpose
    budget = (
        int(cfg.get("footprint.budget_standard_tokens", 1500))
        if str(cfg.get("plugins.profile", "standard")) == "standard"
        else None
    )
    out(footprint.render(fps, budget))
    over = budget is not None and footprint.always_on_tokens(fps) > budget
    if a.probe:
        out(
            "\nprobe: one real `claude -p` turn in an empty temp directory (this spends real tokens) ..."
        )
        res = footprint.probe(str(claude))
        out(footprint.render_probe(res))
        if not res.get("ok"):
            return 1
    return 1 if (a.strict and over) else 0


def cmd_trail(a, cfg) -> int:
    """The metadata-only audit trail of the end-of-turn gate (what ran, when, pass/fail; never code or output)."""
    from . import auditlog

    if a.prune:
        n = auditlog.prune(int(cfg.get("gates.trail_days", 90)))
        out(f"removed {n} old month file(s) from {auditlog.audit_dir()}")
        return 0
    rows = auditlog.tail(a.tail)
    if not rows:
        out(f"no audit records yet ({auditlog.audit_dir()})")
        return 0
    for r in rows:
        extra = " ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts", "event"))
        out(f"{r.get('ts', '?')} {r.get('event', '?'):<6} {extra}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-hr-ctl", description="Manage the claude-hr distribution."
    )
    p.add_argument("--version", action="version", version=f"claude-hr {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="install/repair everything (idempotent)")
    s.add_argument("--only", nargs="+", choices=ALL_STEPS)
    s.add_argument("--skip", nargs="+", choices=ALL_STEPS)
    s.add_argument(
        "--profile", choices=["minimal", "standard", "process", "audit", "full", "lean-output"]
    )
    s.add_argument("--dry-run", action="store_true")
    s.add_argument(
        "--force", action="store_true", help="overwrite settings keys you changed yourself"
    )
    s.add_argument(
        "--prune", action="store_true", help="uninstall user-scope plugins outside the profile"
    )
    s.add_argument("--no-headroom-install", action="store_true")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("uninstall", help="remove managed config (your own settings are kept)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument(
        "--purge", action="store_true", help="also uninstall Headroom and delete ~/.claude-hr"
    )
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("doctor", help="health check with fixes")
    s.add_argument("--json", action="store_true")
    s.add_argument("--deep", action="store_true", help="also measure ~/.claude size")
    s.set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="dashboard").set_defaults(fn=cmd_status)

    s = sub.add_parser("config")
    s.add_argument("action", choices=["show", "path", "get", "set"])
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("headroom", help="manage the shared proxy")
    s.add_argument(
        "action",
        choices=[
            "status",
            "start",
            "warm",
            "stop",
            "restart",
            "install",
            "upgrade",
            "logs",
            "bench",
        ],
    )
    s.add_argument("--wait", type=float, default=150.0)
    s.add_argument("--version", default="")
    s.add_argument("--lines", type=int, default=60)
    s.set_defaults(fn=cmd_headroom)

    s = sub.add_parser("plugins")
    s.add_argument("action", choices=["list", "sync", "update", "audit", "suggest"])
    s.add_argument("--profile")
    s.add_argument("--prune", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_plugins)
    sub.add_parser("skills", help="skill hygiene audit").set_defaults(fn=cmd_skills)

    s = sub.add_parser("claudemd")
    s.add_argument("action", choices=["lint", "learn"])
    s.add_argument("--path")
    s.add_argument("--json", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_claudemd)

    s = sub.add_parser("usage", help="token ledger from local transcripts")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--by", choices=["day", "model", "project"], default="day")
    s.add_argument("--snapshot", action="store_true")
    s.add_argument(
        "--tools",
        action="store_true",
        help="which skills / subagents / MCP servers were actually used",
    )
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser("update", help="staged upgrades with smoke tests and rollback")
    s.add_argument("--check", action="store_true", help="report only")
    s.add_argument("--only", nargs="+", choices=["claude", "headroom", "plugins", "self"])
    s.add_argument("--force", action="store_true", help="upgrade even while sessions are running")
    s.add_argument("--allow-downgrade", action="store_true")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("schedule")
    s.add_argument("action", choices=["install", "remove", "status"])
    s.set_defaults(fn=cmd_schedule)
    s = sub.add_parser("monitor")
    s.add_argument("action", choices=["tick", "show"])
    s.set_defaults(fn=cmd_monitor)
    s = sub.add_parser("cleanup", help="reclaim disk Claude's own sweep misses")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_cleanup)
    s = sub.add_parser("creds")
    s.add_argument("action", choices=["doctor", "scan"])
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(fn=cmd_creds)
    s = sub.add_parser("shim", help="make plain `claude` run claude-hr")
    s.add_argument("action", choices=["install", "remove"])
    s.set_defaults(fn=cmd_shim)

    s = sub.add_parser("workflows", help="saved multi-agent Workflow scripts (opt-in, token-heavy)")
    s.add_argument("action", choices=["list", "install"])
    s.add_argument("--dir", help="project directory (default: the current git repo)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_workflows)

    s = sub.add_parser("wt", help="git worktrees for parallel agents")
    s.add_argument("action", choices=["new", "list", "rm"])
    s.add_argument("name", nargs="?")
    s.add_argument("--base", default="HEAD")
    s.add_argument("--force", action="store_true")
    s.add_argument("--delete-branch", action="store_true")
    s.set_defaults(fn=cmd_wt)
    s = sub.add_parser("daemon", help="Claude background service routing")
    s.add_argument("action", choices=["status", "restart"])
    s.set_defaults(fn=cmd_daemon)

    s = sub.add_parser(
        "fmt", help="format with the repo's own configured formatter (--check to only report)"
    )
    s.add_argument(
        "paths", nargs="*", help="files to format; none = list the formatters this repo configures"
    )
    s.add_argument("--changed", action="store_true", help="the files changed in the working tree")
    s.add_argument(
        "--check", action="store_true", help="do not write; exit 1 if anything needs formatting"
    )
    s.set_defaults(fn=cmd_fmt)

    s = sub.add_parser(
        "verify",
        help="the repo's test/lint recipe (.claude/hr-verify.toml): init, trust, run, status",
    )
    s.add_argument("action", choices=["init", "run", "trust", "untrust", "status"])
    s.add_argument(
        "--write", action="store_true", help="init: save the proposal to .claude/hr-verify.toml"
    )
    s.add_argument("--force", action="store_true", help="init --write: overwrite an existing file")
    s.add_argument("--tier", choices=["fast", "full"], default="fast", help="run: which checks")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser(
        "audit", help="read-only repo audit: agent-config attack surface, workflow lint, secrets"
    )
    s.add_argument("path", nargs="?", help="repository to audit (default: the current one)")
    s.add_argument("--json", action="store_true")
    s.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on any high or medium finding (or a scanner that reported findings)",
    )
    s.add_argument(
        "--tools",
        action="store_true",
        help="also run gitleaks / pip-audit / npm audit / osv-scanner if installed",
    )
    s.set_defaults(fn=cmd_audit)

    s = sub.add_parser(
        "init-repo",
        help="scaffold issue/PR templates, CODEOWNERS, Dependabot, docs/plans (dry run by default)",
    )
    s.add_argument("path", nargs="?", help="repository (default: the current one)")
    s.add_argument("--apply", action="store_true", help="write the files that do not exist yet")
    s.add_argument(
        "--only",
        metavar="GROUPS",
        help="comma-separated: github,docs,editor,dependabot,codeowners,review,ci (default: all)",
    )
    s.set_defaults(fn=cmd_init_repo)

    s = sub.add_parser(
        "footprint",
        help="always-on token cost of the installed plugins, incl. hidden hook/MCP costs",
    )
    s.add_argument(
        "--probe", action="store_true", help="also measure one real first message (spends tokens)"
    )
    s.add_argument("--strict", action="store_true", help="exit 1 when over the profile's budget")
    s.set_defaults(fn=cmd_footprint)

    s = sub.add_parser("trail", help="metadata-only audit trail of the end-of-turn gate")
    s.add_argument("--tail", type=int, default=20)
    s.add_argument("--prune", action="store_true")
    s.set_defaults(fn=cmd_trail)

    s = sub.add_parser("job", help=argparse.SUPPRESS)
    s.add_argument("name", choices=list(schedule.JOBS))
    s.set_defaults(fn=cmd_job)
    sub.add_parser("statusline", help=argparse.SUPPRESS).set_defaults(
        fn=lambda a, c: hooks.statusline()
    )
    s = sub.add_parser("hook", help=argparse.SUPPRESS)
    s.add_argument("name", choices=["bash-guard"])
    s.set_defaults(fn=lambda a, c: hooks.bash_guard())
    return p


def _log_crash(argv: list[str]) -> None:
    """Scheduled jobs run windowless (pythonw / systemd / cron) where a traceback would vanish."""
    import traceback

    try:
        paths.ensure_dirs()
        with open(paths.logs_dir() / "crash.log", "a", encoding="utf-8") as fh:
            fh.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} claude-hr-ctl {' '.join(argv)}\n{traceback.format_exc()}\n"
            )
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for name in (
        "stdout",
        "stderr",
    ):  # pythonw.exe starts with no console: print() must not blow up
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
    _utf8_stdio()
    # hooks / statusline must be fast and must never fail on a broken user config
    if argv[:2] == ["hook", "bash-guard"]:
        return hooks.bash_guard()
    if argv[:1] == ["statusline"]:
        return hooks.statusline()
    try:
        return _run(argv)
    except SystemExit:
        raise
    except Exception:
        _log_crash(argv)
        raise


def _run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config_mod.load()
    except ValueError as exc:
        out(f"config error: {exc}")
        return 2
    if args.cmd == "wt" and args.action in {"new", "rm"} and not args.name:
        out("wt new/rm need a name")
        return 2
    if args.cmd == "config" and args.action in {"get", "set"} and not args.key:
        out("config get/set need a key")
        return 2
    if args.cmd == "config" and args.action == "set" and args.value is None:
        out("config set needs a key and a value")
        return 2
    paths.tighten_existing()
    return int(args.fn(args, cfg) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
