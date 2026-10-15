"""Staged upgrades: check -> snapshot -> upgrade -> smoke test -> keep or roll back.

Components: claude (native build), headroom (uv tool), plugins (marketplace refresh), self (this repo).
Rules that keep it safe to run unattended:
  * never downgrade unless a pin says so; never upgrade the shared proxy or Claude Code while sessions run
  * every upgrade is smoke-tested; a failed smoke test restores the previous version
  * every decision is appended to logs/update.jsonl
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import paths, procutil, resources
from .claude_setup import ClaudeSetup
from .config import Config
from .headroom import Headroom
from .jsonio import read_json, write_json

CLAUDE_RELEASES = "https://downloads.claude.ai/claude-code-releases"


def vtuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v)
    return tuple(int(x) for x in m.groups()) if m else ()


@dataclass
class Result:
    component: str
    current: str = ""
    target: str = ""
    action: str = "none"  # none | updated | deferred | rolled-back | failed | available
    detail: str = ""

    def line(self) -> str:
        arrow = f"{self.current or '?'} -> {self.target}" if self.target and self.target != self.current else self.current
        return f"{self.component:<9} {self.action:<11} {arrow}  {self.detail}".rstrip()


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(r.action in {"failed", "rolled-back"} for r in self.results)


def _log(entry: dict) -> None:
    try:
        paths.ensure_dirs()
        with open(paths.logs_dir() / "update.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}) + "\n")
    except OSError:
        pass


class Updater:
    def __init__(self, cfg: Config, dry: bool = False, force: bool = False, allow_downgrade: bool = False):
        self.cfg, self.dry, self.force, self.allow_downgrade = cfg, dry, force, allow_downgrade
        self.hr = Headroom(cfg)
        self.claude = procutil.find_claude(str(cfg.get("claude.binary", "") or ""))

    # ---- guards ---------------------------------------------------------------------------
    def _busy(self) -> str:
        """Why an upgrade must wait. On Windows replacing the tool env under a running launcher fails and can leave a
        half-installed package, so this guards claude-hr's own update as well."""
        if self.force:
            return ""
        n = len(resources.live_sessions())
        if n:
            return f"{n} claude-hr session(s) running"
        if self.claude:  # daemon-hosted `agents` / `--bg` workers are not in claude-hr's registry
            from . import launcher

            st = launcher._best_effort(launcher._daemon_status, self.claude) or {}
            if st.get("running") and st.get("workers", 0) != 0:
                return "Claude background sessions are running"
        return ""

    # ---- claude ---------------------------------------------------------------------------
    def claude_version(self) -> str:
        if not self.claude:
            return ""
        r = procutil.run([str(self.claude), "--version"], timeout=30)
        m = re.search(r"\d+\.\d+\.\d+\S*", r.stdout or "")
        return m.group(0) if m else ""

    def is_native_install(self) -> bool:
        """`claude install <version>` only manages the native build (~/.local/bin/claude -> ~/.local/share/claude/...)."""
        if not self.claude:
            return False
        try:
            real = str(self.claude.resolve()).replace("\\", "/").lower()
        except OSError:
            return False
        home = str(paths.home()).replace("\\", "/").lower()
        return "/.local/share/claude/" in real or real.startswith(f"{home}/.local/bin/") or "/.claude/local/" in real

    def claude_target(self) -> str:
        pin = str(self.cfg.get("claude.pin", "") or "")
        if pin:
            return pin
        channel = str(self.cfg.get("claude.channel", "stable"))
        try:
            import urllib.request

            with urllib.request.urlopen(f"{CLAUDE_RELEASES}/{channel}", timeout=10) as resp:  # noqa: S310
                return resp.read().decode().strip()
        except Exception:
            return ""

    def update_claude(self) -> Result:
        cur, tgt = self.claude_version(), self.claude_target()
        res = Result("claude", cur, tgt)
        if not self.claude:
            res.action, res.detail = "failed", "claude not installed"
            return res
        if not tgt:
            res.action, res.detail = "failed", "could not read release channel"
            return res
        if not self.cfg.flag("claude.manage_updates", True):
            res.detail = "managed updates disabled in config"
            return res
        if not self.is_native_install():
            res.detail = "not the native build (npm/Homebrew/WinGet): update it with that package manager"
            return res
        if vtuple(tgt) == vtuple(cur):
            return res
        if vtuple(tgt) < vtuple(cur) and not self.allow_downgrade and not self.cfg.get("claude.pin"):
            res.detail = f"installed version is newer than channel '{self.cfg.get('claude.channel')}'; not downgrading"
            return res
        if self.dry:
            res.action = "available"
            return res
        busy = self._busy()
        if busy:
            res.action, res.detail = "deferred", busy
            return res
        r = procutil.run([str(self.claude), "install", tgt, "--force"], timeout=900)
        new = self.claude_version()
        smoke = procutil.run([str(self.claude), "plugin", "list", "--json"], timeout=60)
        if r.returncode == 0 and vtuple(new) == vtuple(tgt) and smoke.returncode == 0:
            res.action = "updated"
        else:
            procutil.run([str(self.claude), "install", cur, "--force"], timeout=900)
            res.action = "rolled-back"
            res.detail = (r.stderr or r.stdout or "smoke test failed").strip()[-160:]
        return res

    # ---- headroom -------------------------------------------------------------------------
    def update_headroom(self) -> Result:
        cur = self.hr.version() or ""
        pin = str(self.cfg.get("headroom.version", "") or "")
        cooldown = 0.0 if (self.force or pin) else float(self.cfg.get("updates.cooldown_days", 5))
        tgt = pin or self.hr.latest_version(cooldown_days=cooldown) or ""
        res = Result("headroom", cur, tgt)
        if not self.cfg.flag("headroom.enabled", True):
            res.detail = "disabled"
            return res
        if not tgt:
            res.action, res.detail = "failed", "could not read PyPI"
            return res
        if cur and vtuple(tgt) == vtuple(cur):
            return res
        if cur and vtuple(tgt) < vtuple(cur) and not (self.allow_downgrade or pin):
            res.detail = "installed version is newer than target; not downgrading"
            return res
        if self.dry:
            res.action = "available"
            return res
        busy = self._busy()
        if busy:
            res.action, res.detail = "deferred", busy
            return res
        was_running = self.hr.health().ok
        self.hr.stop()
        # a new release may need a newer model revision: allow the network for this one start
        online_h = {**self.hr.h, "offline_models": False}
        self.hr.h, saved_h = online_h, self.hr.h
        cutoff = None
        if cooldown > 0:  # cool the whole dependency tree, not just headroom-ai itself
            cutoff = (datetime.now(UTC) - timedelta(days=cooldown)).strftime("%Y-%m-%dT%H:%M:%SZ")
        inst = self.hr.install(tgt, exclude_newer=cutoff)
        ok = inst.returncode == 0 and self.hr.version() is not None and vtuple(self.hr.version()) == vtuple(tgt)
        if ok:
            ok = self.smoke_headroom()
        if ok:
            res.action = "updated"
        else:
            res.action = "rolled-back" if cur else "failed"
            res.detail = (inst.stderr or "smoke test failed").strip()[-160:]
            if cur:
                self.hr.stop()
                # restore with the SAME dependency cutoff (an unconstrained reinstall would pull today's newest
                # transitive packages - possibly the very breakage being rolled back) and prove it works again
                back = self.hr.install(cur, exclude_newer=cutoff)
                if back.returncode != 0 or not self.smoke_headroom():
                    res.detail += " | WARNING: rollback could not be verified, run `claude-hr-ctl headroom bench`"
        self.hr.h = saved_h
        if was_running or res.action == "updated":
            self.hr.stop()  # restart so the next start uses the normal (possibly offline) settings
            self.hr.ensure(wait_s=0)
        return res

    def smoke_headroom(self, wait_s: float = 150.0) -> bool:
        """Start the proxy, require healthy, then push a repetitive tool result through /v1/compress."""
        health = self.hr.ensure(wait_s=wait_s)
        if not health.ok:
            return False
        import urllib.request

        rows = [{"id": i, "status": "ok", "message": "heartbeat received from node-a", "latency_ms": 12} for i in range(300)]
        body = json.dumps({
            "model": "claude-sonnet-5-5",
            "messages": [
                {"role": "user", "content": "check the nodes"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "nodes"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(rows)}]},
            ],
        }).encode()
        req = urllib.request.Request(f"{self.hr.url}/v1/compress", data=body, headers={"content-type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=60) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
            return resp.status == 200 and int(data.get("tokens_saved", 0)) > 0
        except Exception:
            return False

    # ---- plugins --------------------------------------------------------------------------
    def update_plugins(self) -> Result:
        res = Result("plugins")
        if not self.claude:
            res.action, res.detail = "failed", "claude not installed"
            return res
        if self.dry:
            res.detail = "dry run"
            return res
        setup = ClaudeSetup(self.cfg, self.hr, self.claude)
        before = {p["id"]: p.get("version") for p in setup._installed_plugins()}
        step = setup.update_marketplaces()
        after = {p["id"]: p.get("version") for p in setup._installed_plugins()}
        changed = [f"{k} {before.get(k)}->{v}" for k, v in after.items() if before.get(k) != v]
        res.action = "updated" if changed else "none"
        res.detail = "; ".join(changed) or "already current"
        if not step.ok:
            res.action, res.detail = "failed", "; ".join(step.lines)[-160:]
        return res

    # ---- self (claude-hr itself) ----------------------------------------------------------------------
    def update_self(self) -> Result:
        repo = paths.hr_home() / "repo"
        res = Result("self")
        git = procutil.which_all("git")
        if not (repo / ".git").exists() or not git:
            res.detail = "not a managed checkout (dev install?)"
            return res
        g = str(git[0])
        cur = procutil.run([g, "-C", str(repo), "describe", "--tags", "--always"], timeout=30).stdout.strip()
        fetched = procutil.run([g, "-C", str(repo), "fetch", "--tags", "--prune", "origin"], timeout=120)
        if fetched.returncode != 0:
            # surfaces the usual private-repo problem (no credentials on this VM) instead of silently never updating
            res.action, res.detail = "failed", "git fetch failed: " + (fetched.stderr or fetched.stdout).strip()[-160:]
            return res
        ref = str(self.cfg.get("self.update_ref", "stable"))
        if ref == "stable":
            tags = procutil.run([g, "-C", str(repo), "tag", "--list", "v*"], timeout=30).stdout.split()
            # release tags only: vX.Y.Z (a v1.3.0-rc1 must never outrank v1.3.0, nor be installed unattended)
            tags = sorted((t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)), key=vtuple)
            tgt = tags[-1] if tags else ""
        else:
            tgt = f"origin/{ref}"
        res.current, res.target = cur, tgt
        if not tgt:
            res.detail = "no release tag yet"
            return res
        head = procutil.run([g, "-C", str(repo), "rev-parse", "HEAD"], timeout=30).stdout.strip()
        want = procutil.run([g, "-C", str(repo), "rev-parse", tgt + "^{commit}"], timeout=30).stdout.strip()
        if not want or head == want:
            res.target = res.current
            return res
        # only ever move forward: a checkout of `main` that is ahead of the newest tag must not be rewound to it
        if procutil.run([g, "-C", str(repo), "merge-base", "--is-ancestor", head, want], timeout=30).returncode != 0:
            res.target = res.current
            res.detail = f"installed commit is not behind {tgt}; not moving"
            return res
        if self.dry:
            res.action = "available"
            return res
        busy = self._busy()
        if busy:  # replacing the tool env under a running launcher fails on Windows and can leave a broken install
            res.action, res.detail = "deferred", busy
            return res
        co = procutil.run([g, "-C", str(repo), "checkout", "--detach", "--quiet", tgt], timeout=60)
        if co.returncode != 0:
            res.action, res.detail = "failed", "git checkout failed: " + (co.stderr or co.stdout).strip()[-160:]
            return res
        uv = procutil.which_all("uv")
        if not uv:
            res.action, res.detail = "failed", "uv missing"
            return res
        import subprocess

        # Re-installing replaces the files this process runs from, so hand it to a detached helper.
        cmd = [str(uv[0]), "tool", "install", "--python", "3.13", "--force", "--reinstall", str(repo)]
        flags = 0x08000000 | 0x00000008 if paths.IS_WINDOWS else 0
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         creationflags=flags, start_new_session=not paths.IS_WINDOWS)
        write_json(paths.state_dir() / "self-update.json", {"from": head, "to": want, "ts": time.time()})
        res.action, res.detail = "updated", "reinstalling in the background; run `claude-hr-ctl setup` afterwards if config changed"
        return res

    # ---- orchestration --------------------------------------------------------------------
    def run(self, components: list[str] | None = None) -> Report:
        order = components or ["claude", "headroom", "self", "plugins"]  # self before plugins: the plugin comes from the repo
        fns = {
            "claude": self.update_claude,
            "headroom": self.update_headroom,
            "plugins": self.update_plugins,
            "self": self.update_self,
        }
        report = Report()
        for name in order:
            try:
                res = fns[name]()
            except Exception as exc:  # one broken component must not block the others
                res = Result(name, action="failed", detail=f"{type(exc).__name__}: {exc}")
            report.results.append(res)
            _log({"component": res.component, "from": res.current, "to": res.target, "action": res.action, "detail": res.detail})
        st = read_json(paths.state_dir() / "update-state.json", {}) or {}
        st["last_run"] = time.time()
        st["last_ok"] = report.ok
        write_json(paths.state_dir() / "update-state.json", st)
        return report
