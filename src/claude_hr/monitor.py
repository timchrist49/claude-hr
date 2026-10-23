"""Periodic health snapshot + self-healing + de-duplicated alerting. Run by the scheduler (`claude-hr-ctl monitor tick`)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass

from . import cgroups, paths, procutil, resources, sysinfo
from .config import Config
from .headroom import Headroom
from .jsonio import JsonFileError, atomic_write_text, read_json, write_json


@dataclass
class Alert:
    key: str
    title: str
    body: str


def _claude_dir_info(max_age_s: float = 6 * 3600) -> tuple[float, bool]:
    """(GB, complete) of ~/.claude, recomputed at most every 6h and never walked for more than 20s. Incomplete means the walk
    ran out of time (a folder with hundreds of thousands of files): the size is then a lower bound."""
    cache = paths.state_dir() / "claude-dir-size.json"
    c = read_json(cache, {}) or {}
    if time.time() - float(c.get("ts", 0)) < max_age_s and "gb" in c:
        return float(c["gb"]), bool(c.get("complete", True))
    gb, complete = sysinfo.dir_size_info(paths.claude_home(), budget_s=20.0)
    write_json(cache, {"ts": time.time(), "gb": round(gb, 2), "complete": complete})
    return round(gb, 2), bool(complete)


def _claude_dir_gb(max_age_s: float = 6 * 3600) -> float:
    return _claude_dir_info(max_age_s)[0]


def _last_snapshot() -> dict:
    """The previous tick's health.json. Anything unreadable is 'no previous tick': a damaged file must not stop the monitor,
    which is what rewrites it."""
    try:
        data = read_json(paths.state_dir() / "health.json", {})
    except (JsonFileError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _count(value: object) -> int:
    """A counter read back from health.json; a damaged value is 'no previous reading', never an error."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _scope_rows(prev: dict) -> list[dict]:
    """The running Claude scopes with their memory events, and how many of each event happened since the previous tick.
    A scope the previous tick did not know is NEW: everything its counters hold happened inside the window, so it counts.
    Only when there was no previous reading of the scopes at all (the first tick, or one from an older release) are the
    counters of scopes that were already running history rather than news."""
    seen = prev.get("scopes")
    tracked = isinstance(seen, list)
    before = {r.get("name"): r for r in (seen if tracked else []) if isinstance(r, dict)}
    rows = []
    for sc in cgroups.scopes():
        old = before.get(sc.name) or {}
        row = {
            "name": sc.name,
            "current_gb": round(sc.current_gb, 2),
            "used_gb": round(sc.used_gb, 2),
            "high_gb": round(sc.high_gb, 2),
            "max_gb": round(sc.max_gb, 2),
            "pids": sc.pids,
            "events_high": sc.events.get("high", 0),
            "events_max": sc.events.get("max", 0),
            "oom_kill": sc.events.get("oom_kill", 0),
        }
        for key, field in (
            ("high_new", "events_high"),
            ("max_new", "events_max"),
            ("oom_new", "oom_kill"),
        ):
            row[key] = max(0, row[field] - _count(old.get(field))) if tracked else 0
        rows.append(row)
    return rows


def _stall_share(prev: dict, now: float, totals: dict[str, float]) -> dict[str, float]:
    """The share of the time since the previous tick that tasks were stalled on memory / I/O, from the kernel's cumulative
    stall counters. The 60 second average the kernel also keeps says nothing about the other 14 minutes of a 15 minute tick;
    this does. {} on the first tick, after a reboot (the counters restarted) or when the last reading is stale."""
    before = prev.get("stall_us")
    try:
        dt = now - float(prev["ts"])
    except (KeyError, TypeError, ValueError):
        return {}
    if not isinstance(before, dict) or not 0 < dt <= 6 * 3600:
        return {}
    out: dict[str, float] = {}
    for kind, total in totals.items():
        delta = total - float(before.get(kind, total) or 0)
        if delta >= 0:
            out[kind] = round(min(100.0, delta / (dt * 1e6) * 100), 1)
    return out


def snapshot(cfg: Config) -> dict:
    hr = Headroom(cfg)
    now = time.time()
    last = _last_snapshot()
    psi = cgroups.pressure()
    stall_us = {
        k: row["some_us"] for k, row in psi.items() if k in {"memory", "io"} and "some_us" in row
    }
    total, avail = sysinfo.memory_gb()
    # "enabled" for alerting purposes means: configured AND installed (a missing install is a doctor finding, not an outage)
    health = hr.health_patient() if cfg.flag("headroom.enabled", True) and hr.binary() else None
    proxy_rss = 0.0
    claude_dir_gb, claude_dir_complete = _claude_dir_info()
    st = read_json(paths.state_dir() / "headroom-proxy.json", {}) or {}
    for pid in {int(st.get("pid") or 0)} | hr._listener_pids():
        if pid:
            proxy_rss = max(proxy_rss, sysinfo.process_rss_mb(pid))
    snap = {
        "ts": now,
        "ram_total_gb": round(total, 1),
        "ram_free_gb": round(avail, 1),
        "disk_free_gb": round(sysinfo.disk_free_gb(paths.home()), 1),
        "claude_dir_gb": claude_dir_gb,
        "claude_dir_complete": claude_dir_complete,
        "sessions": len(resources.live_sessions()),
        "session_limit": resources.session_limit(cfg),
        "scopes": _scope_rows(last),
        "pressure": {
            k: {n: v for n, v in row.items() if not n.endswith("_us")} for k, row in psi.items()
        },
        "stall_us": stall_us,
        "stall_pct": _stall_share(last, now, stall_us),
        "proxy": {
            "enabled": health is not None,
            "ok": bool(health and health.ok),
            "status": health.status if health else "off",
            "version": health.version if health else "",
            "rss_mb": round(proxy_rss),
        },
    }
    return snap


def evaluate(cfg: Config, snap: dict) -> list[Alert]:
    m = cfg.section("monitor")
    alerts: list[Alert] = []
    if snap["disk_free_gb"] and snap["disk_free_gb"] < float(m.get("disk_warn_gb", 10)):
        alerts.append(
            Alert("disk", "Low disk space", f"{snap['disk_free_gb']} GB free on the home volume")
        )
    if snap["ram_total_gb"]:
        used_pct = 100 * (1 - snap["ram_free_gb"] / snap["ram_total_gb"])
        if used_pct > float(m.get("mem_warn_pct", 90)):
            alerts.append(
                Alert(
                    "memory",
                    "High memory use",
                    f"{used_pct:.0f}% RAM used, {snap['sessions']} Claude session(s)",
                )
            )
    if snap["claude_dir_gb"] > float(m.get("claude_dir_warn_gb", 10)):
        days = float(cfg.section("maintenance").get("jobs_retention_days", 14))
        size = (
            f"{snap['claude_dir_gb']} GB"
            if snap.get("claude_dir_complete", True)
            else f"at least {snap['claude_dir_gb']} GB (the size scan ran out of time, usually on ~/.claude/jobs)"
        )
        alerts.append(
            Alert(
                "claude-dir",
                "~/.claude is large",
                f"{size}. `claude-hr-ctl cleanup` lists what can go; finished workflow scratch under ~/.claude/jobs is "
                f"only removed after {days:.0f} days (maintenance.jobs_retention_days), so it may free little today",
            )
        )
    alerts.extend(_cap_alerts(m, snap))
    if snap["proxy"]["enabled"] and not snap["proxy"]["ok"]:
        alerts.append(Alert("proxy", "Headroom proxy down", f"status={snap['proxy']['status']}"))
    return alerts


def _cap_alerts(m: dict, snap: dict) -> list[Alert]:
    """Linux only (an older snapshot, or another OS, has no `scopes` / `pressure`). The scope is shared by Claude's background
    service and all the sessions it hosts, so one heavy run (a test suite with a worker per core) squeezes every session in it.
    Counters are compared between ticks, so a stall is reported once, shortly AFTER it (within one monitor interval)."""
    out: list[Alert] = []
    scopes = [s for s in (snap.get("scopes") or []) if isinstance(s, dict)]
    hint = "`claude-hr-ctl doctor` shows the caps; bound the heavy run (for example `jest --maxWorkers=2`)."

    def ceiling(s: dict) -> float:
        return float(s.get("high_gb") or s.get("max_gb") or 0)

    def used(
        s: dict,
    ) -> float:  # the working set; a snapshot from before 0.3.5 only has the raw figure
        return float(s.get("used_gb", s.get("current_gb", 0)))

    pct = float(m.get("scope_warn_pct", 90))
    full = [s for s in scopes if ceiling(s) and used(s) >= ceiling(s) * pct / 100]
    if full:
        rows = "; ".join(
            f"{s['name']} {used(s):.1f} of {ceiling(s):.1f} GB in use, {s.get('pids', 0)} tasks"
            for s in full
        )
        why = (
            "Above the soft limit the kernel throttles everything in the scope."
            if any(s.get("high_gb") for s in full)
            else "At the hard cap the kernel kills a process."
        )
        out.append(
            Alert(
                "scope-full",
                "Claude's background sessions are near their memory cap",
                f"{rows} (reclaimable file cache not counted). {why} {hint}",
            )
        )
    killed = [s for s in scopes if s.get("oom_new")]
    if killed:
        rows = "; ".join(f"{s['name']} lost {s['oom_new']} process(es)" for s in killed)
        out.append(
            Alert(
                "scope-oom",
                "A process in Claude's background scope was OOM-killed",
                f"{rows} since the last check (the scope's own cap, or the machine running out of memory). {hint}",
            )
        )
    hit = [s for s in scopes if s.get("max_new") and not s.get("oom_new")]
    if hit:
        rows = "; ".join(f"{s['name']} reached its hard cap {s['max_new']} time(s)" for s in hit)
        out.append(
            Alert(
                "scope-capped",
                "Claude's background sessions hit their hard memory cap",
                f"{rows} since the last check. {hint}",
            )
        )
    limit = float(m.get("psi_warn_pct", 20))
    avg = {
        k: float(v.get("some", 0))
        for k, v in (snap.get("pressure") or {}).items()
        if k in {"memory", "io"}
    }
    share = {k: float(v) for k, v in (snap.get("stall_pct") or {}).items() if k in {"memory", "io"}}
    rows = []
    for kind in sorted(set(avg) | set(share)):
        if share.get(kind, 0) >= limit:
            rows.append(f"{kind} {share[kind]:.0f}% of the time since the last check")
        elif avg.get(kind, 0) >= limit:
            rows.append(f"{kind} {avg[kind]:.0f}% of the last minute")
    if rows:
        out.append(
            Alert(
                "pressure",
                "The machine is stalling",
                f"tasks waited for {', '.join(rows)}. Heavy parallel work (a test suite, a build) is the usual cause: "
                "look at `top` and the Claude scopes in `claude-hr-ctl doctor`.",
            )
        )
    return out


def heal_caps(cfg: Config) -> str:
    """Linux: keep the caps on Claude's background sessions right for THIS machine. The process wrapper is written once, by
    `setup`; a VM that was resized since (or a changed `resources.*`) left the old caps in place, and the background service
    keeps the scope it started in for days. So: re-write a stale wrapper (new scopes get it), and raise, never lower, the
    caps of the scope that is already running. The two steps do not depend on each other: a wrapper that cannot be written
    must not keep a squeezed service from being raised. Never raises."""
    if not (paths.IS_LINUX and cfg.flag("monitor.heal_resource_caps", True)):
        return ""
    notes: list[str] = []
    try:
        note = resources.refresh_wrapper(cfg)
        if note:
            notes.append(note)
    except Exception as exc:  # noqa: BLE001 - a failed refresh must never cost the alerts of this tick
        notes.append(f"wrapper refresh failed: {type(exc).__name__}")
    try:
        lim = resources.wrapper_limits(cfg)
        if lim is not None:
            for sc in cgroups.scopes():
                raised = cgroups.raise_caps(sc, lim.max_mb, lim.high_mb)
                if raised:
                    notes.append(raised)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"cap raise failed: {type(exc).__name__}")
    return "; ".join(notes)


def heal(cfg: Config, snap: dict) -> str:
    """Restart the shared proxy if it died. Rate-limited to once per 10 minutes."""
    if not (
        snap["proxy"]["enabled"]
        and not snap["proxy"]["ok"]
        and cfg.flag("monitor.restart_proxy", True)
    ):
        return ""
    stamp = paths.state_dir() / "last-proxy-restart"
    try:
        if time.time() - stamp.stat().st_mtime < 600:
            return "proxy restart skipped (tried <10 min ago)"
    except OSError:
        pass
    atomic_write_text(stamp, str(time.time()))
    hr = Headroom(cfg)
    if hr.binary() is None:
        return "headroom not installed"
    # a wedged proxy of ours may still hold the port; stop() only ever signals Headroom processes
    if procutil.port_listening(hr.host, hr.port) and not hr.health_patient().ok:
        hr.stop()
        conflict = getattr(hr, "last_conflict", None)
        if conflict and procutil.port_listening(hr.host, hr.port):
            return (
                f"port {hr.port} is held by another program (pid {conflict}); change headroom.port"
            )
    hr.ensure(wait_s=0)
    return "proxy restart requested"


def deliver(cfg: Config, alert: Alert) -> None:
    m = cfg.section("monitor")
    env = dict(os.environ, CLAUDE_HR_ALERT_TITLE=alert.title, CLAUDE_HR_ALERT_BODY=alert.body)
    if m.get("alert_command"):
        # Split into argv (no shell): the alert text reaches the command only via CLAUDE_HR_ALERT_* env vars.
        try:
            argv = shlex.split(str(m["alert_command"]), posix=not paths.IS_WINDOWS)
            extra = {"creationflags": procutil.CREATE_NO_WINDOW} if paths.IS_WINDOWS else {}
            subprocess.run(
                argv, env=env, timeout=30, check=False, stdin=subprocess.DEVNULL, **extra
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if m.get("alert_ntfy_url"):
        _post(
            str(m["alert_ntfy_url"]),
            alert.body.encode(),
            {"Title": alert.title, "Priority": "high"},
        )
    if m.get("alert_webhook_url"):
        _post(
            str(m["alert_webhook_url"]),
            json.dumps({"title": alert.title, "text": alert.body}).encode(),
            {"Content-Type": "application/json"},
        )
    with open(paths.logs_dir() / "alerts.log", "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {alert.key} {alert.title}: {alert.body}\n")


def _post(url: str, data: bytes, headers: dict[str, str]) -> None:
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10).read()  # noqa: S310
    except Exception:
        pass


def tick(cfg: Config) -> dict:
    paths.ensure_dirs()
    caps_note = heal_caps(
        cfg
    )  # before the snapshot, so a scope that was just raised is not reported as squeezed
    snap = snapshot(cfg)
    note = "; ".join(n for n in (heal(cfg, snap), caps_note) if n)
    alerts = evaluate(cfg, snap)
    state_f = paths.state_dir() / "alerts.json"
    sent = read_json(state_f, {}) or {}
    cooldown = float(cfg.get("monitor.alert_cooldown_hours", 6)) * 3600
    now = time.time()
    fired = []
    for a in alerts:
        if now - float(sent.get(a.key, 0)) >= cooldown:
            deliver(cfg, a)
            sent[a.key] = now
            fired.append(a.key)
    for key in list(sent):  # a cleared condition re-arms its alert immediately
        if key not in {a.key for a in alerts}:
            del sent[key]
    write_json(state_f, sent)
    write_json(
        paths.state_dir() / "health.json", {**snap, "alerts": [a.key for a in alerts], "note": note}
    )
    line = json.dumps({**snap, "alerts": [a.key for a in alerts]})
    log = paths.logs_dir() / "monitor.jsonl"
    try:
        if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
            log.replace(log.with_suffix(".jsonl.old"))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return {"snapshot": snap, "alerts": [a.key for a in alerts], "fired": fired, "note": note}
