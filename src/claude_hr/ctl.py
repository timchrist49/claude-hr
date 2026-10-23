"""claude-hr-ctl: management CLI for the claude-hr distribution."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import (
    __version__,
    claude_setup,
    claudemd,
    creds,
    doctor,
    gate_offer,
    hooks,
    human,
    maintenance,
    monitor,
    paths,
    planmeter,
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
    for ln in step.needs:
        out(f"       needs you: {ln}")


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
    # outcome of the steps that make up the managed baseline (see applied-version.json)
    baseline_ok: dict[str, bool] = {}
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
                baseline_ok[name] = False
                continue
            st = fn()
            _print_step(st)
            failed += 0 if st.ok else 1
            baseline_ok[name] = st.ok
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
        # Remember, per step, which release each part of the baseline came from (and which Claude config directory it went
        # to), so doctor and the daily update job can tell when a newer release (installed unattended) has not been applied
        # yet. Only the steps that ran in THIS invocation and succeeded: `--only plugins` must not mark the settings as
        # applied, a failed step stays visible, and a skipped one stays skipped (the job re-applies recorded steps only).
        applied = [n for n in claude_setup.BASELINE_STEPS if baseline_ok.get(n)]
        if applied:
            try:
                claude_setup.record_applied_version(*applied)
            except OSError as exc:
                out(f"[ERR] could not record the applied version: {exc}")
                failed += 1
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
    step_failed = False
    # a step that cannot run (an unparsable settings.json or ~/.claude.json is never rewritten) is reported and skipped: the
    # rest of the uninstall must still happen, and the exit status says it was not complete
    reverted = setup.revert_settings()
    for st in (reverted, setup.remove_claude_md(), setup.unregister_mcp()):
        _print_step(st)
        step_failed = step_failed or not st.ok
    if not a.dry_run:
        # whatever else happens below: the machine is no longer "set up", so no later update run re-applies the baseline
        claude_setup.clear_setup_markers()
    if claude and not a.dry_run:
        for pid in ("hr-core@claude-hr", "hr-gates@claude-hr"):
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
            if not reverted.ok:
                # the ownership record in state/ is what lets a later uninstall remove exactly the keys claude-hr added
                out(
                    f"[warn] {paths.hr_home()} was kept: it holds the record needed to revert settings.json "
                    "(fix that file, then run `claude-hr-ctl uninstall --purge` again)"
                )
            else:
                _rmtree_force(paths.hr_home())
                if paths.hr_home().exists():
                    out(
                        f"[warn] could not delete everything under {paths.hr_home()} (files in use?); remove it by hand"
                    )
                else:
                    out("[ok ] purged ~/.claude-hr and uninstalled Headroom")
    if step_failed:
        out(
            "Uninstall is INCOMPLETE: a step above is marked ERR (it was skipped, everything else ran). Fix what it "
            "names and run `claude-hr-ctl uninstall` again; your own settings, plugins and CLAUDE.md content were left untouched."
        )
    else:
        out(
            "Uninstalled managed configuration. Your own settings, plugins and CLAUDE.md content were left untouched."
        )
    if sched_failed:
        out(
            "[warn] the scheduled jobs could not be removed (see above); remove them with `claude-hr-ctl schedule remove` or in Task Scheduler / systemd"
        )
    return 1 if (step_failed or sched_failed) else 0


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
    h = hr.health_patient()
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
    # extra usage is cash: show it, and say so when the window is full
    meter = planmeter.read_local()
    if meter and planmeter.extra_text(meter):
        out(f"  Extra usage: {planmeter.extra_text(meter)} this month")
    flag = planmeter.warning(meter) if meter else None
    if flag and flag[0] == "warn":
        out(f"  WARNING    : {flag[1]}")
    out(
        f"  Sessions   : {snap['sessions']} running / limit {snap['session_limit']}   RAM free {snap['ram_free_gb']} of {snap['ram_total_gb']} GB   disk free {snap['disk_free_gb']} GB"
    )
    last_update = updater.last_update_summary()
    if last_update:
        out(f"  Last update: {last_update}")
    led = usage.collect(1, max_seconds=15)  # `status` stays quick: a slow disk gives a lower bound
    t = led.total
    out(
        f"  Last 24h   : {t.messages} model calls, {usage._n(t.output)} output tokens, cache hit {t.cache_hit * 100:.0f}%"
        + (" (partial: the scan was cut short)" if led.partial else "")
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


def _running_sessions() -> int:
    """How many claude-hr sessions are running (the launcher's session registry); 0 when that cannot be told."""
    try:
        from . import resources

        return len(resources.live_sessions())
    except Exception:  # noqa: BLE001 - a warning is never worth failing the command it is for
        return 0


def cmd_headroom(a, cfg) -> int:
    hr = Headroom(cfg)
    if a.action == "status":
        h = hr.health_patient()
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
        running = _running_sessions()
        if running:
            out(
                f"warning: {running} claude-hr session{'s' if running != 1 else ''} running; "
                "they are without Headroom until the proxy is back (up to ~1 minute). Restarting anyway."
            )
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
        out(
            usage.format_tool_use(
                usage.collect_tool_use(a.days, subagents=not a.no_subagents), a.days
            )
        )
        return 0
    led = usage.collect(a.days, subagents=not a.no_subagents)
    if a.snapshot:
        res = usage.write_snapshot(led)
        for line in res.lines():
            out(line)
        return 1 if res.skipped else 0
    out(usage.format_ledger(led, a.by))
    return 0


def _claude_only_update(cfg) -> int:
    """`update --claude-only`: Claude Code's own `claude update`, with nothing of claude-hr's staging around it."""
    claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))
    if not claude:
        out("claude not found")
        return 127
    return subprocess.call([str(claude), "update"])


def _update_progress():
    """A line per component on stderr when a person is watching. The run prints its one report only at the end, a minute
    or more later, and a silent terminal looks like a command that did nothing. None when stderr is not a terminal (the
    scheduled job, a pipe): those get the report and the log, as before."""
    stream = sys.stderr
    try:
        if stream is None or not stream.isatty():
            return None
    except (AttributeError, ValueError):
        return None

    def say(line: str) -> None:
        try:
            print(line, file=stream, flush=True)
        except (OSError, ValueError):  # a closed or broken terminal must not stop an update
            pass

    return say


def cmd_update(a, cfg) -> int:
    """Update everything (see updater.py), print the one report, and exit with 0 ok | 1 failed | 75 deferred (sessions run,
    or another update is running) | 2 refused."""
    if a.claude_only:
        # Claude Code's own updater takes none of claude-hr's switches: refuse them rather than ignore them (a real
        # `claude update` under --check would be a surprise)
        clash = [
            flag
            for flag, given in (
                ("--check", a.check),
                ("--only", bool(a.only)),
                ("--force", a.force),
                ("--allow-downgrade", a.allow_downgrade),
                ("--no-restart", a.no_restart),
                ("--post", a.post),
            )
            if given
        ]
        if clash:
            out(
                f"--claude-only runs Claude Code's own `claude update` and takes none of {', '.join(clash)}: "
                "leave --claude-only out to use them"
            )
            return updater.EXIT_REFUSED
        return _claude_only_update(cfg)
    if a.post and (a.check or a.only):
        out("--post (the second half of a self-update) takes no --check or --only")
        return updater.EXIT_REFUSED
    u = updater.Updater(
        cfg,
        dry=a.check,
        force=a.force,
        allow_downgrade=a.allow_downgrade,
        restart=not a.no_restart,
    )
    say = None if a.post else _update_progress()
    if say:
        say("claude-hr update: each step is listed as it runs; the report follows at the end.")
    u.progress = say
    try:
        rep = u.run_post() if a.post else u.run(a.only or None)
    except updater.UpdateInProgress:
        out("another update is running (state/update.lock): try again when it has finished")
        return updater.EXIT_DEFERRED
    for line in updater.format_report(rep):
        out(line)
    return rep.exit_code()


UPDATE_DESCRIPTION = """Update everything in this setup, in one go, and say what happened in one table:
  claude-hr itself (when a newer release exists), Claude Code (the native build only; staged: smoke-tested, rolled back on
  failure), Headroom (staged the same way), plugins and their marketplaces (user scope; never answers a prompt for you,
  adds no marketplace and installs no plugin: that is `claude-hr-ctl plugins sync`), then the managed settings and
  CLAUDE.md block (re-applied only on a machine `setup` ran on, for an older release), and the Headroom proxy (restarted
  only when it runs stale settings and nothing uses it). A full run ends with a health verdict; when claude-hr itself
  updates, its reinstall, the last steps and the verdict run in the background after this command
  (logs/self-update.log). claude-hr itself, Claude Code, Headroom and the proxy wait while claude-hr sessions or Claude
  background sessions run (exit 75); plugins and marketplaces do not.

Exit codes: 0 ok, already current, or a preview that found nothing wrong | 1 a component failed or was rolled back
            (with --check: could not be checked) | 2 usage error or unreadable configuration |
            75 something waits (claude-hr or Claude background sessions run, or another update is running):
            close the sessions and run it again, or use --force.
A Claude Code installed with npm, Homebrew or WinGet is left to that package manager; `claude-hr update --claude-only`
runs Claude Code's own `claude update` instead of all of this (it takes none of the switches below).
Through the launcher, `update` (or `upgrade`) must be the first argument, or follow plain flags only (--verbose ...):
`claude-hr --model opus update` is Claude Code's own updater."""


def _add_update_args(s: argparse.ArgumentParser) -> None:
    s.add_argument(
        "--check",
        action="store_true",
        help="preview only: nothing is installed, restarted or re-applied (it does fetch claude-hr's newest "
        "release tags into its checkout); plugins are not previewed",
    )
    s.add_argument(
        "--only",
        nargs="+",
        choices=list(updater.COMPONENTS),
        metavar="STEP",
        help="just these, always in the order of a full run (any of: "
        + ", ".join(updater.COMPONENTS)
        + ")",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="update even while claude-hr sessions are running (claude-hr itself too, except that on Windows its "
        "reinstall is skipped while a session still runs), and skip the Headroom release cooldown",
    )
    s.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="also move Claude Code and Headroom to an older release (claude-hr itself never moves back; "
        "claude.pin and headroom.version need no flag)",
    )
    s.add_argument(
        "--no-restart",
        action="store_true",
        help="never restart the Headroom proxy: a Headroom upgrade and a restart for stale settings are skipped "
        "(--force does not override this)",
    )
    s.add_argument(
        "--claude-only",
        action="store_true",
        help="only Claude Code's own `claude update` (no staging, no claude-hr; no other switch)",
    )
    # the second half of a self-update: the helper starts it after the reinstall (see updater.py)
    s.add_argument("--post", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_update)


def update_main(argv: list[str], prog: str = "claude-hr-ctl update") -> int:
    """`claude-hr update` (through the launcher) and `claude-hr-ctl update`: one parser, one implementation. Returns the
    exit code (a usage error is 2, --help is 0) instead of exiting, so the launcher can hand it on."""
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        prog=prog,
        description=UPDATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_update_args(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    try:
        cfg = config_mod.load()
    except ValueError as exc:
        out(f"config error: {exc}")
        return 2
    try:
        paths.tighten_existing()
        return int(args.fn(args, cfg) or 0)
    except Exception:
        _log_crash(["update", *argv])
        raise


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
        scan = creds.scan_config()  # once: the checks and the listing below must agree
        checks = creds.run_checks(cfg, scan)
        for c in checks:
            out(c.line(_color()))
        listing = creds.format_findings(scan.findings, scan.capped)
        if listing:
            out("")
            for line in listing:
                out(line)
        return 1 if any(c.status == FAIL for c in checks) else 0
    res = creds.scan_transcripts(a.days)
    for line in res.coverage_lines():
        out(line)
    for f, kind, n in res.hits[:40]:
        out(f"  {n:4d} x {kind:<26} {f}")
    if res.hits:
        out(
            "\nSecrets in transcripts are plaintext on disk. Rotate each affected credential, then delete the file "
            "(or `claude purge`). Values are never printed here."
        )
    else:
        out(
            "no token-shaped secrets found"
            + ("" if res.complete else " in the part that was scanned")
        )
    return 1 if res.hits else 0


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
        # --hr-shim: the launcher cannot see the name this script has, and `claude update` through the shim is not
        # the whole-setup update for a script or an IDE (launcher._wants_update_everything)
        f.write_text(f'#!/bin/sh\nexec "{target}" --hr-shim "$@"\n', encoding="utf-8")
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
                # auto_apply = false is a report-only job: it still records what it found, and a component it could not
                # check (or that failed) still fails the job
                try:
                    rep = updater.Updater(
                        cfg, dry=not cfg.flag("updates.auto_apply", True), record=True
                    ).run()
                    msg = " | ".join(r.line() for r in rep.results)
                    # exit 75 (sessions are running, so something waits) is how the command says "nothing is broken": the
                    # scheduler must not call that a failure of the job
                    rc = 0 if rep.exit_code() in (updater.EXIT_OK, updater.EXIT_DEFERRED) else 1
                except updater.UpdateInProgress:
                    msg = "another update is running; nothing to do"
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
                # 8 days back: the job runs weekly, and the first and the last day of a window are only partly in it, so
                # the seven whole days before today are what gets stored
                snap = usage.write_snapshot(usage.collect(8))
                msg += (
                    f"; usage snapshot: {len(snap.written)} day(s) stored"
                    if not snap.skipped
                    else f"; usage snapshot not written: {snap.skipped}"
                )
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
            scan = creds.scan_transcripts(7)
            if not scan.complete:  # a scan that did not cover the week says so in the job log
                msg += " | transcript scan: " + "; ".join(
                    ln.strip() for ln in scan.coverage_lines()
                )
            if scan.hits:
                monitor.deliver(
                    cfg,
                    monitor.Alert(
                        "transcript-secrets",
                        "Secrets found in Claude transcripts",
                        f"{len(scan.hits)} finding(s) in {scan.scanned} of {scan.total} files"
                        + ("" if scan.complete else " (the scan did not cover everything)")
                        + "; run `claude-hr-ctl creds scan`",
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
    """Why nobody can be assumed to be at the keyboard (None = a person plausibly is): see human.not_a_person.

    Used for the decisions that let a repository's own commands run: `verify trust`, `verify enable` and `verify run` of
    an untrusted recipe. A thin wrapper so that a test (or a caller) can replace it in this module."""
    return human.not_a_person()


def _show_recipe(
    root: Path, recipe, gate_timeout_s: float | None = None, gate_tier: str = "fast"
) -> None:
    """Every check of both tiers, in full, exactly as the gate would run it (see gate_offer.recipe_lines)."""
    for line in gate_offer.recipe_lines(recipe, gate_timeout_s, gate_tier, root=root):
        out(line)


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
            # `describe` lists only a formatter that is configured AND installed, so an empty answer does not mean the repo
            # has no configuration: say both, or the user is told "none configured" about a repo that names ruff
            out(
                "no formatter is configured and installed in this repository (claude-hr only uses one the repo already "
                "configures AND that is installed: ruff/black, prettier/biome, gofmt, rustfmt). If the repo does configure "
                "one (a [tool.ruff] table in pyproject.toml, a prettier config, go.mod, Cargo.toml ...) but nothing is "
                "listed here, its tool is not installed: put it on PATH, in .venv or in node_modules/.bin"
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
    # what the checks of one end-of-turn run can really get: the setting, and never more than the hook has left
    gate_budget, gate_tier = gate_offer.gate_limits(cfg)
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
        try:
            # never through a link: the repository may commit .claude/hr-verify.toml (or .claude) as a symlink to a
            # file of yours (see verify.write_config)
            verify.write_config(root, text, force=bool(a.force))
        except verify.ConfigExists:
            out(f"{cp} already exists (use --force to overwrite); review it, then trust it")
            return 1
        except (ValueError, OSError) as exc:
            out(f"cannot write {cp}: {verify.safe_text(str(exc))}")
            return 1
        out(
            f"wrote {cp}\nReview it (these commands run at the end of each Claude turn), then, in a terminal "
            "outside Claude Code: claude-hr-ctl verify trust"
        )
        return 0
    if a.action == "untrust":
        # before the recipe is loaded: revoking must work while the file is missing, unreadable or invalid
        removed = verify.untrust(root, paths.state_dir())
        out(
            "trust removed: the end-of-turn gate will not run this repository's commands"
            if removed
            else "this repository was not trusted"
        )
        out(
            "(the launcher may offer this folder's checks again when you next start Claude here; `claude-hr-ctl verify disable` stops that too)"
        )
        return 0
    if a.action == "disable":
        # same as untrust, and remember "no" so the launcher never asks again for this folder; the file is kept
        removed = gate_offer.disable(root)
        out(
            "turned off: the end-of-turn gate will not run this folder's checks"
            if removed
            else "this folder's checks were not on"
        )
        out(
            f"{cp} was kept (if there is one). claude-hr will not ask again in this folder unless its check list changes. "
            "Turn it on again: claude-hr-ctl verify enable"
        )
        return 0
    if a.action == "enable":
        why = _not_a_person()
        if why:
            out(
                f"refusing to turn the checks on: {why}.\n"
                "They run commands from this folder unattended after every turn, so a person has to say yes: open a "
                f"normal terminal (not a Claude Code session), cd to {root} and run `claude-hr-ctl verify enable`."
            )
            return 1
        if worktree.repo_root(Path.cwd()) is None:
            out(
                "this folder is not inside a git repository, and the end-of-turn gate only runs in git repositories "
                "(`git init` first, or cd into one)"
            )
            return 1
        if not (cfg.flag("gates.enabled", True) and cfg.flag("verify.gate", True)):
            out(
                "note: gates.enabled or verify.gate is off in your config, so the end-of-turn gate will not run until you turn it on"
            )
        if str(cfg.get("plugins.profile", "standard")).strip().lower() == "minimal":
            out(
                "note: the minimal plugin profile does not install hr-gates (the end-of-turn hook): claude-hr-ctl setup --profile standard"
            )
        outcome = gate_offer.offer_now(root, cfg, out, explicit=True)
        return {"enabled": 0, "already-on": 0, "unavailable": 2}.get(outcome, 1)
    # ONE read of the file: the recipe shown, the trust verdict and (for `trust`) the digest that is approved all come
    # from these bytes, so what a person sees is what gets trusted or run
    try:
        loaded = verify.load_checked(root)
    except ValueError as exc:
        out(f"{cp}: {verify.safe_text(str(exc))}")
        return 2
    recipe = loaded.recipe
    state = verify.trust_state_of(root, paths.state_dir(), loaded.data)
    if a.action == "status":
        hint = gate_offer.status_hint(root, state, cfg)
        if recipe is None:
            out(f"no recipe at {cp}  ->  `claude-hr-ctl verify enable` (or `verify init --write`)")
            if hint:
                out(hint)
            return 0
        when_trusted = "TRUSTED: the end-of-turn gate runs these checks"
        if state == "trusted" and doctor.state_inside_repo(Path.cwd()):
            # the hook refuses a state folder inside the repository: the record is valid and the gate still does not run
            when_trusted = (
                "NOT RUNNING: the state folder is inside this repository, so the end-of-turn hook ignores its trust "
                f"record (set CLAUDE_HR_HOME outside the repository; the state folder is {paths.state_dir()})"
            )
        meaning = {
            "trusted": when_trusted,
            "untrusted": "NOT trusted: the gate does nothing until a person approves it (`claude-hr-ctl verify enable`)",
            "modified": "CHANGED since it was trusted: the gate does nothing until it is reviewed and trusted again (`claude-hr-ctl verify enable`)",
        }.get(state, state)
        out(f"{cp}: {meaning}")
        _show_recipe(root, recipe, gate_budget, gate_tier)
        if hint:
            out(hint)
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
        _show_recipe(root, recipe, gate_budget, gate_tier)
        try:
            from . import audit

            findings = audit.preflight(root)
            if findings:
                out("\nThe repository's agent surface has findings you should read first:")
                out(audit.render(findings, [], root=root))
        except Exception:  # noqa: BLE001 - the preflight is advice; it must not block the decision
            pass
        out(f"\nfile sha256: {loaded.sha256}")
        if doctor.state_inside_repo(Path.cwd()):
            out(f"\nwarning: {doctor.state_inside_warning()}")
        if not _confirm("\nTrust exactly this file?"):
            out("not trusted")
            return 1
        try:
            # refuses when the file no longer hashes to what was shown above
            digest = verify.trust(root, paths.state_dir(), expect_sha256=loaded.sha256)
        except ValueError as exc:
            out(verify.safe_text(str(exc)))
            return 1
        gate_offer.record(
            root, "enabled", recipe_sha=digest
        )  # so the launcher never asks about this folder
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
        _show_recipe(root, recipe)  # `verify run` honours every timeout_s: no gate budget to show
        if not _confirm("\nRun them once now?"):
            return 1
    checks = list(recipe.full if (a.tier == "full" and recipe.full) else recipe.fast)
    if not checks:
        out("nothing to run: no checks were found for this repository")
        return 1
    # the same environment the gate gives the checks (verify.env_allow)
    allowed_env = cfg.get("verify.env_allow", []) or []
    res = verify.run(
        root,
        checks,
        total_timeout_s=float(sum(c.timeout_s for c in checks)),
        env_allow=allowed_env,
    )
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
    try:
        groups = initrepo.parse_groups(a.only)
    except ValueError as exc:  # an unknown group name; the message lists the valid ones
        out(str(exc))
        return 2
    if not root.is_dir():
        out(f"{root} is not a directory")
        return 2
    where, top = initrepo.locate(root)
    if where in {"none", "gitdir"}:
        # .github/, .editorconfig and .gitattributes written here would belong to no repository (or, in a bare
        # repository's git directory, sit next to HEAD and objects/): operate on the top level of a work tree only
        what = (
            "a git directory (a bare repository, or .git itself), not a work tree"
            if where == "gitdir"
            else "not inside a git work tree"
        )
        out(
            f"warning: {root} is {what}, so nothing was scaffolded. init-repo writes the top-level files of a git "
            "repository (.github/, .editorconfig, .gitattributes, docs/): run `git init` first, or pass the top-level "
            "directory of the repository."
        )
        return 2
    if where == "subdir":
        skipped = [g for g in groups if g in initrepo.ROOT_ONLY_GROUPS]
        if skipped:
            # these belong to the top level: GitHub reads .github/ and reviewers read REVIEW.md only there, and a nested
            # .editorconfig / .gitattributes would silently override the repository's own for this whole subtree
            groups = [g for g in groups if g not in skipped]
            out(
                f"warning: {root} is a subdirectory of the git repository at {top}. {', '.join(skipped)} are not "
                f"scaffolded here: they belong to the top level (GitHub reads .github/ only there, and a nested "
                f".editorconfig or .gitattributes would override the repository's own for this subtree). "
                f"Run `claude-hr-ctl init-repo {top}` for them."
            )
            if not groups:
                out("nothing left to scaffold in this directory.")
                return 2
    commands = None
    try:  # the CI skeleton runs the same checks the end-of-turn gate would
        from . import verify

        commands = [c.argv for c in verify.discover(root).fast] or None
    except Exception:  # noqa: BLE001
        pass
    actions, notes = initrepo.plan_with_notes(root, only=groups, commands=commands, discovered=True)
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
    if r.returncode != 0 or not (r.stdout or "").strip():
        # a timeout (124), an old claude without --json, a crash: nothing was measured, so never print "0 tokens, budget OK"
        why = (r.stderr or "").strip()[:200]
        out(f"`claude plugin list --json` failed (exit {r.returncode}){': ' + why if why else ''}")
        return 1
    try:
        listing = json.loads(r.stdout)
    except ValueError:
        out("could not read `claude plugin list --json`")
        return 1
    if isinstance(listing, dict):
        listing = listing.get("plugins", [])
    fps = footprint.scan_installed(listing if isinstance(listing, list) else [])
    # no cap unless one is set: the numbers are for choosing, not for refusing
    cap = int(cfg.get("footprint.budget_standard_tokens", 0) or 0)
    budget = cap if cap > 0 else None
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
    s.add_argument(
        "--no-subagents",
        action="store_true",
        help="count the main sessions only (the numbers before subagent and workflow transcripts were read)",
    )
    s.set_defaults(fn=cmd_usage)

    s = sub.add_parser(
        "update",
        help="update everything: claude-hr, Claude Code, Headroom, plugins, baseline, proxy",
        description=UPDATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_update_args(s)

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
        help="the repo's test/lint recipe (.claude/hr-verify.toml): enable, disable, status, init, trust, untrust, run",
        description=(
            "The end-of-turn test gate. 'enable' turns it on for this folder (runs the checks once, keeps the ones "
            "that pass, asks you first; a person at a terminal only); 'disable' turns it off and stops the launcher "
            "from asking again; 'status' shows where this folder stands. 'init' / 'trust' / 'untrust' are the manual "
            "way (write the file, approve it, revoke the approval); 'run' runs the checks once now."
        ),
    )
    s.add_argument(
        "action", choices=["enable", "disable", "status", "init", "trust", "untrust", "run"]
    )
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
    s.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when over footprint.budget_standard_tokens (only when a cap > 0 is set; the default is no cap)",
    )
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
