"""Periodic health snapshot + self-healing + de-duplicated alerting. Run by the scheduler (`claude-hr-ctl monitor tick`)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass

from . import paths, procutil, resources, sysinfo
from .config import Config
from .headroom import Headroom
from .jsonio import atomic_write_text, read_json, write_json


@dataclass
class Alert:
    key: str
    title: str
    body: str


def _claude_dir_gb(max_age_s: float = 6 * 3600) -> float:
    """~/.claude size, recomputed at most every 6h and never walked for more than 20s."""
    cache = paths.state_dir() / "claude-dir-size.json"
    c = read_json(cache, {}) or {}
    if time.time() - float(c.get("ts", 0)) < max_age_s and "gb" in c:
        return float(c["gb"])
    gb, complete = sysinfo.dir_size_info(paths.claude_home(), budget_s=20.0)
    write_json(cache, {"ts": time.time(), "gb": round(gb, 2), "complete": complete})
    return round(gb, 2)


def snapshot(cfg: Config) -> dict:
    hr = Headroom(cfg)
    total, avail = sysinfo.memory_gb()
    # "enabled" for alerting purposes means: configured AND installed (a missing install is a doctor finding, not an outage)
    health = hr.health() if cfg.flag("headroom.enabled", True) and hr.binary() else None
    proxy_rss = 0.0
    st = read_json(paths.state_dir() / "headroom-proxy.json", {}) or {}
    for pid in {int(st.get("pid") or 0)} | hr._listener_pids():
        if pid:
            proxy_rss = max(proxy_rss, sysinfo.process_rss_mb(pid))
    snap = {
        "ts": time.time(),
        "ram_total_gb": round(total, 1),
        "ram_free_gb": round(avail, 1),
        "disk_free_gb": round(sysinfo.disk_free_gb(paths.home()), 1),
        "claude_dir_gb": _claude_dir_gb(),
        "sessions": len(resources.live_sessions()),
        "session_limit": resources.session_limit(cfg),
        "proxy": {"enabled": health is not None, "ok": bool(health and health.ok), "status": health.status if health else "off",
                  "version": health.version if health else "", "rss_mb": round(proxy_rss)},
    }
    return snap


def evaluate(cfg: Config, snap: dict) -> list[Alert]:
    m = cfg.section("monitor")
    alerts: list[Alert] = []
    if snap["disk_free_gb"] and snap["disk_free_gb"] < float(m.get("disk_warn_gb", 10)):
        alerts.append(Alert("disk", "Low disk space", f"{snap['disk_free_gb']} GB free on the home volume"))
    if snap["ram_total_gb"]:
        used_pct = 100 * (1 - snap["ram_free_gb"] / snap["ram_total_gb"])
        if used_pct > float(m.get("mem_warn_pct", 90)):
            alerts.append(Alert("memory", "High memory use", f"{used_pct:.0f}% RAM used, {snap['sessions']} Claude session(s)"))
    if snap["claude_dir_gb"] > float(m.get("claude_dir_warn_gb", 10)):
        alerts.append(Alert("claude-dir", "~/.claude is large", f"{snap['claude_dir_gb']} GB; run `claude-hr-ctl cleanup`"))
    if snap["proxy"]["enabled"] and not snap["proxy"]["ok"]:
        alerts.append(Alert("proxy", "Headroom proxy down", f"status={snap['proxy']['status']}"))
    return alerts


def heal(cfg: Config, snap: dict) -> str:
    """Restart the shared proxy if it died. Rate-limited to once per 10 minutes."""
    if not (snap["proxy"]["enabled"] and not snap["proxy"]["ok"] and cfg.flag("monitor.restart_proxy", True)):
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
    if procutil.port_listening(hr.host, hr.port) and not hr.health().ok:
        hr.stop()
        conflict = getattr(hr, "last_conflict", None)
        if conflict and procutil.port_listening(hr.host, hr.port):
            return f"port {hr.port} is held by another program (pid {conflict}); change headroom.port"
    hr.ensure(wait_s=0)
    return "proxy restart requested"


def deliver(cfg: Config, alert: Alert) -> None:
    m = cfg.section("monitor")
    env = dict(os.environ, CLAUDE_HR_ALERT_TITLE=alert.title, CLAUDE_HR_ALERT_BODY=alert.body)
    if m.get("alert_command"):
        # Split into argv (no shell): the alert text reaches the command only via CLAUDE_HR_ALERT_* env vars.
        try:
            argv = shlex.split(str(m["alert_command"]), posix=not paths.IS_WINDOWS)
            subprocess.run(argv, env=env, timeout=30, check=False)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if m.get("alert_ntfy_url"):
        _post(str(m["alert_ntfy_url"]), alert.body.encode(), {"Title": alert.title, "Priority": "high"})
    if m.get("alert_webhook_url"):
        _post(str(m["alert_webhook_url"]), json.dumps({"title": alert.title, "text": alert.body}).encode(),
              {"Content-Type": "application/json"})
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
    snap = snapshot(cfg)
    note = heal(cfg, snap)
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
    write_json(paths.state_dir() / "health.json", {**snap, "alerts": [a.key for a in alerts], "note": note})
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
