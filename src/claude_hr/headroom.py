"""Headroom lifecycle: install/upgrade, one persistent shared proxy, client env, MCP registration.

Design notes (all verified against headroom-ai 0.41.0 on Windows):
  * `headroom wrap claude` costs ~6s per start (it re-registers MCP servers every time) and its
    proxy dies with the session, so claude-hr runs ONE long-lived proxy and registers MCP servers once.
  * A cold proxy start takes 40-70s (imports, model prefetch, embedder warm-up) -> it must be
    started early (login / first launch) and never on a hot path without a visible wait.
  * The proxy's built-in rate limiter (60 req/min) throttles parallel agents -> --no-rate-limit.
  * A proxy started by `headroom proxy` (not `wrap`) never self-exits.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import paths, procutil, sysinfo
from .config import Config
from .filelock import LockTimeout, locked
from .jsonio import atomic_write_text, read_json

# Quiet-CLI defaults copied in spirit from headroom wrap: they only silence zero-signal chatter
# (progress bars, funding banners, version nags, telemetry pings), never diffs/errors/results.
QUIET_CLI_ENV = {
    "GIT_PAGER": "cat",
    "PIP_QUIET": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "npm_config_fund": "false",
    "npm_config_audit": "false",
    "npm_config_progress": "false",
    "npm_config_update_notifier": "false",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "NEXT_TELEMETRY_DISABLED": "1",
    "HOMEBREW_NO_ENV_HINTS": "1",
}
# The variables of Headroom.server_env() that a setting decides: they are fixed when the proxy starts, so they are part
# of what settings_hash() records.
SETTINGS_ENV = (
    "HEADROOM_SAVINGS_PROFILE",
    "HEADROOM_OUTPUT_SHAPER",
    "HEADROOM_OUTPUT_HOLDOUT",
    "HEADROOM_BEACON",
    "HEADROOM_UPDATE_CHECK",
)


def pick_release(
    releases: dict, cooldown_days: float, fallback: str | None = None, now: float | None = None
) -> str | None:
    """Newest X.Y.Z release whose earliest file is >= cooldown_days old, ignoring pre-releases and yanked builds."""
    from datetime import datetime

    now = now if now is not None else time.time()
    best: tuple[int, ...] | None = None
    best_name: str | None = None
    for ver, files in releases.items():
        parts = ver.split(".")
        if (
            len(parts) != 3
            or not all(p.isdigit() for p in parts)
            or not files
            or all(f.get("yanked") for f in files)
        ):
            continue
        times = []
        for f in files:
            raw = f.get("upload_time_iso_8601") or f.get("upload_time")
            try:
                times.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        if not times:
            continue
        key = tuple(int(p) for p in parts)
        if (now - min(times)) / 86400 >= cooldown_days and (best is None or key > best):
            best, best_name = key, ver
    if best_name is None and cooldown_days <= 0:
        return fallback
    return best_name


@dataclass
class Health:
    ok: bool
    status: str = "down"
    version: str = ""
    detail: str = ""


class Headroom:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.h = cfg.section("headroom")

    # ---- discovery ------------------------------------------------------------------------
    @property
    def port(self) -> int:
        return int(self.h.get("port", 8787))

    @property
    def host(self) -> str:
        return str(self.h.get("host", "127.0.0.1"))

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def binary(self) -> Path | None:
        exe = "headroom.exe" if paths.IS_WINDOWS else "headroom"
        env = os.environ.get("CLAUDE_HR_HEADROOM_BIN")
        if env and Path(env).is_file():
            return Path(env)
        found = procutil.which_all("headroom")
        if found:
            return found[0]
        for d in self._uv_tool_bin_dirs():
            cand = d / exe
            if cand.is_file():
                return cand
        return None

    @staticmethod
    def _uv_tool_bin_dirs() -> list[Path]:
        out = []
        if os.environ.get("UV_TOOL_BIN_DIR"):
            out.append(Path(os.environ["UV_TOOL_BIN_DIR"]))
        out += [paths.home() / ".local" / "bin"]
        return out

    def version(self) -> str | None:
        b = self.binary()
        if not b:
            return None
        r = procutil.run([str(b), "--version"], timeout=60)
        if r.returncode != 0:
            return None
        return (r.stdout.strip().split()[-1] if r.stdout.strip() else None) or None

    # ---- install / upgrade ----------------------------------------------------------------
    def effective_extras(self) -> str:
        """`footprint = auto` picks a lighter extras set on small machines (<8 GB RAM)."""
        extras = str(self.h.get("extras", "all"))
        if str(self.h.get("footprint", "auto")) == "lite":
            return "proxy,code,mcp"
        if str(self.h.get("footprint", "auto")) == "auto" and extras == "all":
            total, _ = sysinfo.memory_gb()
            if 0 < total < 8:
                return "proxy,code,mcp"
        return extras

    def install_spec(self, version: str | None = None) -> str:
        ver = version if version is not None else str(self.h.get("version", "") or "")
        base = f"headroom-ai[{self.effective_extras()}]"
        return f"{base}=={ver}" if ver else base

    def install(
        self, version: str | None = None, upgrade: bool = True, exclude_newer: str | None = None
    ) -> subprocess.CompletedProcess:
        """`uv tool install` into an isolated env. exclude_newer (ISO date) also cools every transitive dependency."""
        uv = procutil.which_all("uv")
        if not uv:
            return subprocess.CompletedProcess([], 127, "", "uv not found on PATH")
        cmd = [str(uv[0]), "tool", "install", "--python", str(self.h.get("python", "3.13"))]
        if upgrade:
            cmd.append("--upgrade")
        if exclude_newer:
            cmd += ["--exclude-newer", exclude_newer]
        cmd += ["--force", self.install_spec(version)]
        return procutil.run(cmd, timeout=1800)

    def latest_version(self, timeout: float = 15.0, cooldown_days: float = 0.0) -> str | None:
        """Newest stable, non-yanked release at least `cooldown_days` old (0 = just the newest)."""
        status, body = procutil.http_json(
            "https://pypi.org/pypi/headroom-ai/json", timeout=timeout, loopback=False
        )
        if status != 200 or not isinstance(body, dict):
            return None
        return pick_release(
            body.get("releases") or {}, cooldown_days, body.get("info", {}).get("version")
        )

    # ---- proxy ----------------------------------------------------------------------------
    def health(self, timeout: float = 0.6) -> Health:
        status, body = procutil.http_json(f"{self.url}/health", timeout=timeout)
        if status != 200 or not isinstance(body, dict):
            return Health(False, "down", detail=f"HTTP {status}" if status else "no listener")
        ok = body.get("status") == "healthy" and bool(body.get("ready"))
        return Health(ok, str(body.get("status")), str(body.get("version", "")))

    def health_patient(self, patience_s: float = 4.0) -> Health:
        """health() for callers that can afford to wait a little before saying "down".

        A proxy nobody has asked for a while has its memory trimmed by the OS, so its first answer can take 0.4-0.8 s, past the
        0.6 s of health(); the next one takes a millisecond (measured on Windows: every monitor tick after 15 idle minutes
        failed the quick probe and raised a false "Headroom proxy down"). When something is listening but the quick probe got no
        usable answer, ask once more with `patience_s`. Nothing listening (refused) is down at once."""
        h = self.health()
        if h.ok or patience_s <= 0.6:
            return h
        if procutil.port_listening(self.host, self.port):
            return self.health(timeout=patience_s)
        return h

    def proxy_args(self) -> list[str]:
        h = self.h
        args = ["proxy", "--host", self.host, "--port", str(self.port), "--workers", "1"]
        args += ["--mode", str(h.get("proxy_mode", "cache")), "--no-rate-limit"]
        footprint_lite = self.effective_extras() == "proxy,code,mcp"
        if bool(h.get("memory", False)) and not footprint_lite:
            args += [
                "--memory",
                "--memory-db-path",
                str(paths.hr_home() / "headroom" / "memory.db"),
                "--memory-storage",
                "project",
            ]
        if bool(h.get("learn", False)) and not footprint_lite:
            args.append("--learn")
        if not footprint_lite:
            args.append("--code-aware")
        if h.get("budget_usd_daily"):
            args += ["--budget", str(h["budget_usd_daily"]), "--budget-period", "daily"]
        if not bool(h.get("subscription_tracking", True)):
            args.append("--no-subscription-tracking")
        args += [str(a) for a in h.get("extra_proxy_args", [])]
        return args

    def server_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base if base is not None else os.environ)
        h = self.h
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONSAFEPATH"] = "1"  # never let a cwd containing `headroom/` shadow the wheel
        env["HEADROOM_SAVINGS_PROFILE"] = str(h.get("savings_profile", "coding"))
        env["HEADROOM_WORKSPACE_DIR"] = str(paths.hr_home() / "headroom")
        env.setdefault("HEADROOM_AGENT_TYPE", "claude")
        if bool(h.get("output_shaper", False)):
            env["HEADROOM_OUTPUT_SHAPER"] = "1"
        holdout = float(h.get("output_holdout", 0) or 0)
        if holdout > 0:
            env["HEADROOM_OUTPUT_HOLDOUT"] = str(holdout)
        if not bool(h.get("telemetry", False)):
            env["HEADROOM_BEACON"] = "off"
            env["DO_NOT_TRACK"] = "1"
        if not bool(h.get("update_check", False)):
            env["HEADROOM_UPDATE_CHECK"] = "off"
        if self._offline_models():
            env["HF_HUB_OFFLINE"] = "1"
        # Do not mark this proxy as wrap-owned: it must outlive every session.
        env.pop("HEADROOM_WRAP_OWNED", None)
        return env

    def settings_hash(self) -> str:
        """A short hash of what start() launches the proxy with: its argument list plus the environment that comes from
        the settings (the output shaper, the savings profile ...). Not the ambient environment and not what depends on the
        machine's state (`offline_models = auto` flips once a model is cached): only a settings change should differ."""
        import hashlib

        env = self.server_env({})
        basis = json.dumps(
            [self.proxy_args(), {k: env.get(k) for k in SETTINGS_ENV}], sort_keys=True
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    def settings_state(self) -> str:
        """Was the running proxy started with the settings it would get now?

        "current"; "changed" (started with other arguments: a setting was changed, or a release changed a default);
        "older" (the record of the start has no hash: an older claude-hr started it); "unknown" (no record of a start by
        claude-hr, or one that cannot be read: a proxy somebody else started is not judged)."""
        try:
            st = read_json(self._state_file(), {}) or {}
        except Exception:  # noqa: BLE001 - a corrupt record is "unknown", never a reason to stop doctor
            return "unknown"
        if not isinstance(st, dict) or not st:
            return "unknown"
        recorded = st.get("args")
        if not recorded:
            return "older"
        return "current" if recorded == self.settings_hash() else "changed"

    def _offline_models(self) -> bool:
        """true / false / "auto" (default): go offline once the Kompress model is cached, which removes a
        ~10s HuggingFace round-trip from every cold start and makes the proxy independent of huggingface.co."""
        mode = self.h.get("offline_models", "auto")
        if isinstance(mode, bool):
            return mode
        if str(mode).lower() in {"true", "1", "yes", "on"}:
            return True
        if str(mode).lower() != "auto":
            return False
        hf_home = os.environ.get("HF_HOME") or str(paths.home() / ".cache" / "huggingface")
        hub = Path(os.environ.get("HF_HUB_CACHE") or Path(hf_home) / "hub")
        return (hub / "models--chopratejas--kompress-v2-base").is_dir()

    def _state_file(self) -> Path:
        return paths.state_dir() / "headroom-proxy.json"

    def start(self) -> subprocess.Popen | None:
        """Spawn the shared proxy detached from this process and from any Job Object."""
        b = self.binary()
        if not b:
            return None
        paths.ensure_dirs()
        settings = self.settings_hash()  # before the spawn: a started proxy always gets its record
        try:
            log = self._open_log()
        except (
            OSError
        ):  # e.g. a symlink planted at the log path: run without a log rather than not at all
            log = None
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": log if log is not None else subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "env": self.server_env(),
            "cwd": str(paths.hr_home()),
        }
        try:
            if paths.IS_WINDOWS:
                flags = (
                    procutil.windows_background_flags()
                )  # no window, ever: see there for why not DETACHED_PROCESS
                try:
                    proc = subprocess.Popen(
                        [str(b), *self.proxy_args()],
                        creationflags=flags | procutil.CREATE_BREAKAWAY_FROM_JOB,
                        **kwargs,
                    )
                except OSError:  # CREATE_BREAKAWAY_FROM_JOB not permitted by the parent job
                    proc = subprocess.Popen(
                        [str(b), *self.proxy_args()], creationflags=flags, **kwargs
                    )
            else:
                proc = subprocess.Popen(
                    [str(b), *self.proxy_args()], start_new_session=True, **kwargs
                )
        finally:
            if log is not None:
                log.close()  # the child has its own copy of the handle
        atomic_write_text(
            self._state_file(),
            json.dumps(
                {
                    "pid": proc.pid,
                    "port": self.port,
                    "started": time.time(),
                    "args": settings,  # doctor compares it with what the config asks for now
                }
            )
            + "\n",
            follow_symlinks=False,
        )
        return proc

    @staticmethod
    def _open_log():
        """Proxy stdout/stderr log: owner-only (it can contain project paths and request metadata) and never opened
        through a symlink. Raises OSError if the path is a symlink."""
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(paths.logs_dir() / "headroom-proxy.log", flags, 0o600)
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)  # a log created by an older version was 0644
            except OSError:
                pass
        return os.fdopen(fd, "ab")

    def ensure(self, wait_s: float = 0.0, announce=None) -> Health:
        """Make sure a healthy proxy is serving. wait_s=0 starts it in the background and returns."""
        health = self.health()
        if health.ok:
            return health
        proc = None
        try:
            with locked(paths.state_dir() / "headroom-start.lock", timeout=max(wait_s, 5.0)):
                health = self.health()
                if health.ok:
                    return health
                listening = procutil.port_listening(self.host, self.port)
                st = read_json(self._state_file(), {}) or {}
                starting = (
                    bool(st.get("pid"))
                    and sysinfo.pid_alive(int(st["pid"]))
                    and (time.time() - float(st.get("started", 0)) < 180)
                )
                if listening and not starting:
                    # something is bound to the port but is not a healthy proxy we just started: don't wait for it
                    return Health(
                        False, "port-busy", detail=f"port {self.port} is held by another process"
                    )
                if not listening and not starting:
                    if self.binary() is None:
                        return Health(False, "not-installed", detail="headroom is not installed")
                    if announce:
                        announce("starting the Headroom proxy (cold start takes up to ~1 min)")
                    try:
                        proc = self.start()
                    except OSError as exc:  # Windows refuses to start the file: an error 5 is not a crash of claude-hr
                        return Health(
                            False, "refused", detail=f"the proxy could not be started: {exc}"
                        )
        except LockTimeout:
            pass
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            health = self.health()
            if health.ok:
                return health
            if proc is not None and proc.poll() is not None:
                return Health(
                    False,
                    "crashed",
                    detail=f"proxy exited with code {proc.returncode} (see headroom-proxy.log)",
                )
            time.sleep(1.0)
        return self.health()

    @staticmethod
    def _is_headroom_process(pid: int) -> bool:
        """Never signal a PID on faith: stale state files and reused PIDs would otherwise kill unrelated programs."""
        try:
            if paths.IS_LINUX:
                return b"headroom" in Path(f"/proc/{pid}/cmdline").read_bytes().lower()
            if paths.IS_WINDOWS:
                r = procutil.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                    ],
                    timeout=20,
                )
            else:
                r = procutil.run(["ps", "-o", "args=", "-p", str(int(pid))], timeout=10)
            return "headroom" in (r.stdout or "").lower()
        except (OSError, ValueError):
            return False

    def stop(self) -> bool:
        """Stop OUR proxy. A foreign process on the port (another tool's dev server) is left alone and reported."""
        st = read_json(self._state_file(), {}) or {}
        pid = int(st.get("pid") or 0)
        killed = False
        self.last_conflict: int | None = None
        for target in self._listener_pids() | ({pid} if pid else set()):
            if not (target and sysinfo.pid_alive(target)):
                continue
            if self._is_headroom_process(target):
                killed |= self._terminate_tree(target)
            elif target != pid:
                self.last_conflict = target
        try:
            self._state_file().unlink()
        except OSError:
            pass
        return killed

    def _listener_pids(self) -> set[int]:
        """PIDs listening on the proxy port. netstat/ss are ~100ms; PowerShell's Get-NetTCPConnection is seconds."""
        port = self.port
        pids: set[int] = set()
        if paths.IS_WINDOWS:
            r = procutil.run(["netstat", "-ano", "-p", "TCP"], timeout=15)
            for line in r.stdout.splitlines():
                cols = line.split()
                if (
                    len(cols) >= 5
                    and cols[0] == "TCP"
                    and cols[3] == "LISTENING"
                    and cols[1].rsplit(":", 1)[-1] == str(port)
                ):
                    if cols[-1].isdigit():
                        pids.add(int(cols[-1]))
            return pids
        if procutil.cmd_exists("ss"):
            r = procutil.run(["ss", "-H", "-ltnp", f"sport = :{port}"], timeout=10)
            for part in r.stdout.replace(",", " ").split():
                if part.startswith("pid="):
                    digits = "".join(c for c in part[4:] if c.isdigit())
                    if digits:
                        pids.add(int(digits))
            if pids:
                return pids
        if procutil.cmd_exists("lsof"):
            r = procutil.run(["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=10)
            pids |= {int(x) for x in r.stdout.split() if x.isdigit()}
        return pids

    @staticmethod
    def _terminate_tree(pid: int) -> bool:
        if paths.IS_WINDOWS:
            return (
                procutil.run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20).returncode == 0
            )
        try:
            os.killpg(os.getpgid(pid), 15)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, 15)
                return True
            except OSError:
                return False

    # ---- client side ----------------------------------------------------------------------
    def client_env(self, cwd: Path | None = None) -> dict[str, str]:
        """Environment that routes one Claude Code process through the proxy."""
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": self.url}
        ts = str(self.h.get("tool_search", "true"))
        if ts:
            env["ENABLE_TOOL_SEARCH"] = ts
        cwd = cwd or Path.cwd()
        name = urllib.parse.quote(cwd.name.strip() or "root", safe="-_.() ")
        headers = [f"X-Headroom-Project: {name}"]
        try:
            headers.append(
                f"X-Headroom-Cwd: {urllib.parse.quote(str(cwd.resolve()), safe='/:._-~()')}"
            )  # same encoding as `headroom wrap`
        except OSError:
            pass
        existing = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        env["ANTHROPIC_CUSTOM_HEADERS"] = (
            "\n".join([existing, *headers]) if existing else "\n".join(headers)
        )
        if bool(self.h.get("one_m_context", False)):
            env.setdefault("ANTHROPIC_MODEL", os.environ.get("ANTHROPIC_MODEL", "opus[1m]"))
        if bool(self.h.get("quiet_cli", True)):
            for k, v in QUIET_CLI_ENV.items():
                if k not in os.environ:
                    env[k] = v
        return env

    # ---- one-time registration (replaces wrap's per-session work) -------------------------
    def mcp_server_spec(self) -> dict:
        b = self.binary()
        return {
            "type": "stdio",
            "command": str(b) if b else "headroom",
            "args": ["mcp", "serve"],
            # same workspace as the proxy: without it the MCP tool keeps its plaintext store in ~/.headroom, outside the
            # folder that `uninstall --purge` removes and that claude-hr keeps owner-only
            "env": {
                "HEADROOM_PROXY_URL": self.url,
                "HEADROOM_WORKSPACE_DIR": str(paths.hr_home() / "headroom"),
            },
        }

    def serena_pin(self) -> str:
        """Pinned serena-agent version: explicit config wins, else the one resolved (with cooldown) at setup."""
        pin = str(self.h.get("serena_version", "") or "")
        if pin:
            return pin
        try:
            return (paths.state_dir() / "serena-pin.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def resolve_serena_pin(self, cooldown_days: float = 5.0) -> str:
        """`uvx serena-agent` without a version pulls whatever is newest on every start: pin it once, reviewably."""
        status, body = procutil.http_json(
            "https://pypi.org/pypi/serena-agent/json", timeout=15.0, loopback=False
        )
        if status != 200 or not isinstance(body, dict):
            return ""
        ver = (
            pick_release(
                body.get("releases") or {}, cooldown_days, body.get("info", {}).get("version")
            )
            or ""
        )
        if ver:
            try:
                paths.state_dir().mkdir(parents=True, exist_ok=True)
                atomic_write_text(
                    paths.state_dir() / "serena-pin.txt", ver + "\n", follow_symlinks=False
                )
            except OSError:
                pass
        return ver

    def serena_spec(self) -> dict:
        pin = self.serena_pin()
        src = f"serena-agent=={pin}" if pin else "serena-agent"
        return {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "--from",
                src,
                "serena",
                "start-mcp-server",
                "--project-from-cwd",
                "--context",
                "claude-code",
                "--open-web-dashboard",
                "False",
            ],
            "env": {},
        }
