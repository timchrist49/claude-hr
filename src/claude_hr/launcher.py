"""claude-hr: drop-in replacement for `claude`.

Everything after the command name is passed to Claude Code untouched, so
    claude-hr agents --dangerously-skip-permissions
    claude-hr -p "..." --output-format json
    claude-hr --resume <id>
all behave exactly like `claude ...`, plus: Headroom routing, resource governor, session registry.

claude-hr-only switches (stripped before Claude sees the args):
    --hr-direct      skip Headroom for this launch      --hr-wrap      use `headroom wrap claude`
    --hr-force       ignore the RAM / session limits    --hr-no-limits no admission/limits
    --hr-dry-run     print the plan and exit            --hr-verbose   explain decisions on stderr
    --hr-wait N      seconds to wait for a cold proxy
Management lives in `claude-hr ctl ...` (same as the `claude-hr-ctl` command).

All claude-hr chatter goes to stderr so `-p --output-format json` pipelines stay clean. The one thing that ever asks a
question is the one-time offer of the end-of-turn test gate (gate_offer.py): only for a plain interactive session with a
terminal, once per git folder, never in -p / agents / subcommands / --hr-dry-run; Ctrl-C, no answer within five minutes
or any error just starts Claude.

Design rule: nothing in here may stop `claude` from starting. Bookkeeping (session registry, launch ring, caches) is
best-effort and every failure degrades to "launch anyway".
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from . import paths, procutil

# NOTE: config / resources / headroom / jsonio are imported inside the functions that need them. This module runs on
# every `claude` invocation and eager imports cost ~190 ms on Windows; passthrough commands need none of them.

# Fallbacks used only when `claude --help` cannot be parsed (the real list is read from the installed CLI and cached).
PASSTHROUGH_SUBCOMMANDS = {
    "mcp",
    "plugin",
    "plugins",
    "update",
    "upgrade",
    "install",
    "doctor",
    "config",
    "setup-token",
    "auth",
    "migrate-installer",
    "completion",
    "logs",
    "stop",
    "kill",
    "rm",
    "respawn",
    "attach",
    "daemon",
    "purge",
    "import",
    "ultrareview",
    "auto-mode",
    "project",
    "gateway",
    "eval",
    "help",
}
PASSTHROUGH_FLAGS = {"-v", "--version", "-h", "--help"}
HEADLESS_FLAGS = {"-p", "--print"}
DAEMON_FLAGS = {"--bg", "--background"}
NEST_LIMIT = 4


def _split_hr_flags(argv: list[str]) -> tuple[dict, list[str]]:
    opts: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":  # everything after a bare -- belongs to claude verbatim
            rest.extend(argv[i:])
            break
        if a.startswith("--hr-"):
            name = a[len("--hr-") :]
            if name == "wait":
                if i + 1 < len(argv):
                    try:
                        opts["wait"] = float(argv[i + 1])
                    except ValueError:
                        pass  # ignore a malformed value rather than crash before claude starts
                    i += 2
                    continue
            elif "=" in name:
                k, _, v = name.partition("=")
                opts[k] = v
            else:
                opts[name] = True
            i += 1
            continue
        rest.append(a)
        i += 1
    return opts, rest


# ---------------------------------------------------------------------------------------------
# What does this command line mean to claude?  (subcommands and value-taking flags come from `claude --help`)
# ---------------------------------------------------------------------------------------------
def _parse_help(text: str) -> tuple[set[str], set[str], set[str]]:
    """(subcommands, value-taking flags, variadic flags) from commander-style help text."""
    import re

    subs: set[str] = set()
    value: set[str] = set()
    variadic: set[str] = set()
    section = ""
    for line in text.splitlines():
        if line.startswith("Commands:"):
            section = "c"
            continue
        if line.startswith("Options:"):
            section = "o"
            continue
        if line and not line.startswith(" "):
            section = ""
            continue
        if section == "c":
            m = re.match(
                r"^ {2}([a-z][\w-]*)(?:\|([a-z][\w-]*))?(?=\s|$)", line
            )  # exactly 2 spaces: wrapped text is deeper
            if m:
                subs.update(g for g in m.groups() if g)
        elif section == "o" and re.match(r"^ {2}-", line):
            sig = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            names = re.findall(r"(--?[\w-]+)", sig)
            if re.search(r"[<\[]", sig):
                value.update(names)
                if "..." in sig:
                    variadic.update(names)
    return subs, value, variadic


def _cli_shape(real: Path) -> tuple[set[str], set[str], set[str]] | None:
    """Cached per claude binary (path + size + mtime) so `claude --help` runs once per upgrade, not per launch."""
    try:
        st = real.stat()
    except OSError:
        return None
    key = f"{real}|{st.st_size}|{int(st.st_mtime)}"
    cache = paths.state_dir() / "claude-cli-shape.txt"
    try:
        lines = cache.read_text(encoding="utf-8").splitlines()
        if lines and lines[0] == key:
            return (
                {x[2:] for x in lines if x.startswith("S:")},
                {x[2:] for x in lines if x.startswith("V:")},
                {x[2:] for x in lines if x.startswith("W:")},
            )
    except (OSError, UnicodeDecodeError):
        pass
    r = procutil.run([str(real), "--help"], timeout=20)
    if r.returncode != 0 or "Commands:" not in (r.stdout or ""):
        return None
    subs, value, variadic = _parse_help(r.stdout)
    if not subs:
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            "\n".join(
                [
                    key,
                    *(f"S:{x}" for x in sorted(subs)),
                    *(f"V:{x}" for x in sorted(value)),
                    *(f"W:{x}" for x in sorted(variadic)),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return subs, value, variadic


def _positionals(args: list[str], value_flags: set[str], variadic: set[str]):
    """Yield (index, token) for tokens that are neither flags nor flag values."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return
        if a.startswith("-") and a != "-":
            name = a.split("=", 1)[0]
            if "=" not in a and name in value_flags:
                i += 1
                if name in variadic:
                    while i < len(args) and not args[i].startswith("-"):
                        i += 1
                else:
                    i += 1
                continue
            i += 1
            continue
        yield i, a
        i += 1


def classify(args: list[str], shape: tuple[set[str], set[str], set[str]] | None = None) -> str:
    """'passthrough' | 'agents' | 'daemon' | 'headless' | 'session'."""
    if not args:
        return "session"
    subs, value_flags, variadic = shape if shape else (set(), set(), set())
    pos = list(_positionals(args, value_flags, variadic))
    first_idx = pos[0][0] if pos else len(args)
    sub = pos[0][1] if pos else None
    # -h / -v only mean "show help/version" when they come before the prompt (`claude -p "what does --help do"`)
    if any(a in PASSTHROUGH_FLAGS for a in args[:first_idx]):
        return "passthrough"
    if sub == "agents":
        return "agents"
    if sub is not None and (
        sub in subs or sub in PASSTHROUGH_SUBCOMMANDS
    ):  # some (daemon) are hidden from --help
        return "passthrough"
    if any(a in HEADLESS_FLAGS for a in args):
        return "headless"
    if any(a in DAEMON_FLAGS for a in args):
        return "daemon"
    return "session"


# ---------------------------------------------------------------------------------------------
def _logged_in(env: dict[str, str]) -> bool:
    """Is there any credential Claude Code will use? (macOS keeps it in the keychain: assume yes.)"""
    keys = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    )
    if any(env.get(k) for k in keys) or paths.IS_MAC:
        return True
    if (paths.claude_home() / ".credentials.json").exists():
        return True
    try:
        from .jsonio import read_json

        return bool((read_json(paths.claude_settings(), {}) or {}).get("apiKeyHelper"))
    except Exception:
        return True  # unreadable settings: do not second-guess


def _is_loopback_url(url: str) -> bool:
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _configured_binary() -> str:
    """`claude.binary` from config, without paying for config parsing unless a config file actually exists."""
    if not (paths.system_config_file().exists() or paths.user_config_file().exists()):
        return ""
    try:
        from . import config as config_mod

        return str(config_mod.load().get("claude.binary", "") or "")
    except ValueError:
        return ""


def _say(msg: str) -> None:
    try:
        print(f"[claude-hr] {msg}", file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        print(f"[claude-hr] {msg}".encode("ascii", "replace").decode(), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _best_effort(fn, *args, **kwargs):
    """Bookkeeping must never block or break a launch."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# a proxy that is installed but broken must not stall every launch for the full wait
# ---------------------------------------------------------------------------------------------
def _failure_stamp() -> Path:
    return paths.state_dir() / "headroom-failed.txt"


def _recently_failed(window_s: float = 600.0) -> bool:
    try:
        return (time.time() - _failure_stamp().stat().st_mtime) < window_s
    except OSError:
        return False


def _mark_failed(failed: bool) -> None:
    try:
        if failed:
            paths.state_dir().mkdir(parents=True, exist_ok=True)
            _failure_stamp().write_text(str(time.time()), encoding="utf-8")
        else:
            _failure_stamp().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# daemon ownership (background sessions inherit the env of whichever client first started the daemon)
# ---------------------------------------------------------------------------------------------
def _daemon_status(claude: Path) -> dict:
    """workers == -1 means "running but could not parse the count" (callers must not treat that as idle)."""
    r = procutil.run([str(claude), "daemon", "status"], timeout=20)
    info = {"running": False, "pid": 0, "starter_pid": 0, "workers": 0}
    text = r.stdout or ""
    if "not running" in text.lower() or r.returncode != 0 or "pid:" not in text:
        return info
    info["running"] = True
    info["workers"] = -1
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("pid:"):
            info["pid"] = int("".join(c for c in s.split(":", 1)[1] if c.isdigit()) or 0)
        elif s.startswith("origin:") and "(pid" in s:
            tail = s.split("(pid", 1)[1]
            info["starter_pid"] = int("".join(c for c in tail.split(")")[0] if c.isdigit()) or 0)
        elif s.startswith("bg workers:"):
            digits = "".join(c for c in s.split(":", 1)[1].split("running")[0] if c.isdigit())
            if digits:
                info["workers"] = int(digits)
    return info


def _ring_path() -> Path:
    return paths.state_dir() / "launch-ring.json"


def _remember_launch(pid: int, proxy_url: str | None) -> None:
    import json

    from .jsonio import atomic_write_text, read_json

    paths.tighten_existing()  # a few chmod calls; fixes installs upgraded from a release that left these world-readable
    try:
        ring = read_json(_ring_path(), []) or []
    except Exception:
        ring = []
    ring.append({"pid": pid, "ts": time.time(), "proxy": proxy_url})
    atomic_write_text(_ring_path(), json.dumps(ring[-200:]) + "\n")


def _daemon_is_ours(status: dict, proxy_url: str) -> bool:
    from .jsonio import read_json

    try:
        ring = read_json(_ring_path(), []) or []
    except Exception:
        return False
    return any(
        r.get("pid") == status.get("starter_pid") and r.get("proxy") == proxy_url for r in ring
    )


def _prepare_daemon(cfg, claude: Path, proxy_url: str, verbose: bool) -> None:
    policy = str(cfg.get("agents.foreign_daemon", "idle"))
    st = _daemon_status(claude)
    if not st["running"] or _daemon_is_ours(st, proxy_url):
        return
    if policy == "never":
        _say(
            "a Claude background service is already running without Headroom; its sessions will not be compressed"
        )
        return
    if policy == "always" or st["workers"] == 0:  # -1 (unknown) is deliberately NOT treated as idle
        procutil.run([str(claude), "daemon", "stop", "--any"], timeout=30)
        if verbose:
            _say("restarted the idle Claude background service so it inherits the Headroom route")
        return
    n = st["workers"]
    _say(
        f"a Claude background service with {n if n > 0 else 'unknown'} running session(s) was started without Headroom; "
        "new background sessions will NOT be compressed. Stop it when idle: `claude daemon stop`."
    )


# ---------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "ctl":
        from . import ctl

        return ctl.main(argv[1:])

    opts, args = _split_hr_flags(argv)

    nest = (
        int(os.environ.get("CLAUDE_HR_NEST", "0") or 0)
        if os.environ.get("CLAUDE_HR_NEST", "0").isdigit()
        else 0
    )
    if os.environ.get("CLAUDE_HR_SESSION") == "1":
        # Claude spawning `claude` itself (hooks, plugin commands, self-update relaunch): the outer launch already
        # routed, limited and registered this session. Pass straight through - but never loop through a shim forever.
        if nest >= NEST_LIMIT:
            _say(
                "claude-hr is invoking itself in a loop (a `claude` shim resolves to itself). "
                "Set CLAUDE_HR_CLAUDE_BIN to the real Claude Code binary or run `claude-hr-ctl shim remove`."
            )
            return 126
        os.environ["CLAUDE_HR_NEST"] = str(nest + 1)
        real = _absolute(procutil.find_claude())
        if real is None:
            _say("Claude Code was not found.")
            return 127
        if paths.IS_WINDOWS:
            import subprocess

            return subprocess.call([str(real), *args])
        os.execv(str(real), [str(real), *args])

    verbose = bool(opts.get("verbose")) or os.environ.get("CLAUDE_HR_VERBOSE") == "1"
    real = _absolute(procutil.find_claude(_configured_binary()))
    if real is None:
        _say(
            "Claude Code was not found. Install it (https://claude.ai/install.sh) or run `claude-hr-ctl setup`."
        )
        return 127

    first = next((a for a in args if not a.startswith("-")), None)
    shape = None
    if args and not (
        args[0] in PASSTHROUGH_SUBCOMMANDS or args[0] in PASSTHROUGH_FLAGS or args[0] == "agents"
    ):
        shape = (
            _best_effort(_cli_shape, real)
            if (first is not None or args[0].startswith("-"))
            else None
        )
    kind = classify(args, shape)

    if kind == "passthrough" and not opts.get("dry-run") and not verbose:
        # Fast path (mcp / plugin / --version / ...): no routing, limits or session accounting needed.
        if paths.IS_WINDOWS:
            import subprocess

            try:
                return subprocess.call([str(real), *args])
            except KeyboardInterrupt:
                return 130
        os.execv(str(real), [str(real), *args])

    from . import config as config_mod
    from . import resources
    from .headroom import Headroom

    user_config_ok = True
    try:
        cfg = config_mod.load()
    except ValueError as exc:  # broken user config must not lock the user out of claude
        _say(f"ignoring unreadable config: {exc}")
        cfg = config_mod.load(with_user=False)
        user_config_ok = False  # the defaults stand in for what the person set: whether they switched the offer off is unknown
    configured = str(cfg.get("claude.binary", "") or "")
    claude = (_absolute(procutil.find_claude(configured)) if configured else None) or real

    default_flags = (
        [str(x) for x in (cfg.get("claude.default_flags", []) or [])]
        if kind in {"session", "headless"}
        else []
    )
    cmd = [str(claude), *default_flags, *args]
    op_file = str(cfg.get("credentials.op_env_file", "") or "")
    # resolve_exe (absolute PATH entries only), never shutil.which / a bare "op": on Windows both look in the current
    # directory first, and the current directory of a launch is often a freshly cloned, untrusted repository.
    op_exe = procutil.resolve_exe("op") if op_file and kind != "passthrough" else None
    if op_exe:
        # --no-masking: masking would pipe the child's stdout and break the interactive TUI
        cmd = [op_exe, "run", "--no-masking", "--env-file", op_file, "--", *cmd]
    env = dict(os.environ)
    env["CLAUDE_HR_SESSION"] = "1"
    env["CLAUDE_HR_NEST"] = "0"

    routed_url: str | None = None
    hr = Headroom(cfg)
    want_route = (
        kind != "passthrough"
        and bool(cfg.get("headroom.enabled", True))
        and str(cfg.get("headroom.mode", "persistent")) != "off"
        and not opts.get("direct")
        and os.environ.get("CLAUDE_HR_DIRECT") != "1"
    )
    plan: list[str] = [f"kind={kind}", f"claude={claude}"]
    user_base = os.environ.get("ANTHROPIC_BASE_URL", "")

    if want_route and opts.get("wrap") and hr.binary():
        # Upstream behaviour: per-session proxy, code-graph watcher, MCP re-registration (~6s start).
        cmd = [
            str(hr.binary()),
            "wrap",
            "claude",
            "--port",
            str(hr.port),
            *(["--memory"] if cfg.get("headroom.memory", False) else []),
            *(["--code-graph"] if cfg.get("headroom.code_graph", True) else []),
            "--",
            *args,
        ]
        plan.append("route=headroom-wrap")
        want_route = False
    elif want_route and user_base and not _is_loopback_url(user_base):
        # The user already routes through their own gateway; silently replacing it would break their setup.
        plan.append("route=direct (ANTHROPIC_BASE_URL is already set to a gateway)")
        if verbose:
            _say(
                "ANTHROPIC_BASE_URL points at your own gateway: leaving it alone (no Headroom for this launch)"
            )
        want_route = False
    elif want_route and not _logged_in(env):
        # The first-run login flow behind a custom base URL is unverified; log in direct, route from the next launch.
        plan.append("route=direct (no login yet)")
        _say(
            "no Claude login found: starting without Headroom so you can log in. Run claude-hr again afterwards."
        )
        want_route = False
    elif want_route:
        wait = float(opts.get("wait", cfg.get("headroom.start_wait_s", 90)))
        if _recently_failed():
            wait = 0.0  # it failed to come up moments ago: don't stall every launch for the full wait again
        if hr.binary() is None:
            plan.append("route=direct (headroom not installed; run `claude-hr-ctl setup`)")
            _say(
                "Headroom is not installed - running Claude directly. Run `claude-hr-ctl setup` to install it."
            )
            want_route = False
        else:
            # a short extra wait for a proxy that is listening but slow to answer its first request after idling: without it
            # every launch after a quiet spell printed "waiting for the Headroom proxy (up to 90s ...)" for a proxy that was fine
            health = hr.health_patient(patience_s=1.5)
            if not health.ok and not opts.get("dry-run"):
                if wait > 0:
                    _say(
                        f"waiting for the Headroom proxy (up to {int(wait)}s; only after boot/updates)..."
                    )
                health = hr.ensure(wait_s=wait, announce=lambda m: _say(m))
                _mark_failed(not health.ok and wait > 0)
            if health.ok:
                env.update(hr.client_env())
                routed_url = hr.url
                plan.append(f"route=headroom {hr.url} v{health.version}")
            elif bool(cfg.get("headroom.fail_open", True)):
                plan.append(f"route=direct (proxy {health.status})")
                _say(
                    f"Headroom proxy is not healthy ({health.status}); continuing WITHOUT compression. "
                    "Diagnose: `claude-hr-ctl doctor`."
                )
            else:
                _say(
                    f"Headroom proxy is not healthy ({health.status}) and fail_open=false; aborting."
                )
                return 3
    else:
        plan.append("route=direct")

    # Auth shadowing: an exported API key silently overrides the subscription login.
    if (
        env.get("ANTHROPIC_API_KEY")
        and (paths.claude_home() / ".credentials.json").exists()
        and verbose
    ):
        _say(
            "note: ANTHROPIC_API_KEY is set and overrides your subscription login for this session"
        )

    if kind in {"agents", "daemon"} and routed_url:
        _best_effort(_prepare_daemon, cfg, claude, routed_url, verbose)

    track = kind in {"session", "headless", "agents", "daemon"}
    if track and not opts.get("no-limits"):
        adm = _best_effort(resources.admit, cfg)
        if adm is not None:
            plan.append(f"sessions={adm.running}/{adm.limit} free_ram={adm.free_gb:.1f}GB")
            if not adm.allowed and not opts.get("force") and not opts.get("dry-run"):
                # Too many sessions only warns (a stricter machine can set resources.enforce_sessions = "block");
                # critically low RAM blocks interactive/agent launches because the machine would thrash.
                block_sessions = (
                    str(cfg.get("resources.enforce_sessions", "warn")) == "block"
                    and kind != "headless"
                )
                if (
                    adm.reason_kind == "ram"
                    and kind != "headless"
                    or (adm.reason_kind == "sessions" and block_sessions)
                ):
                    _say(f"{adm.reason}. Start anyway with --hr-force, or close a session.")
                    return 75
                _say(f"warning: {adm.reason}")
        env = _best_effort(resources.child_env, cfg, env, kind) or env

    if opts.get("dry-run") or verbose:
        _say("plan: " + "; ".join(plan))
        if opts.get("dry-run"):
            _say("command: " + " ".join(cmd))
            return 0

    if kind == "session" and user_config_ok:
        # The one-time offer of the end-of-turn test gate: only a plain interactive session, only in a normal terminal,
        # once per folder, and not while the user's config file cannot be read (it may be where the offer was switched
        # off). Imported here (it pulls in nothing heavy unless a question is really due) and wrapped: whatever happens
        # in there, Claude starts.
        try:
            from . import gate_offer

            gate_offer.maybe_offer(args, cfg)
        except Exception:
            pass

    # ---- launch ---------------------------------------------------------------------------
    cwd = os.getcwd()
    if not paths.IS_WINDOWS:
        if track:
            _best_effort(resources.register_session, os.getpid(), cwd, kind)  # pid survives exec
            _best_effort(_remember_launch, os.getpid(), routed_url)
        full = cmd
        if track and not opts.get("no-limits"):
            full = _best_effort(resources.wrap_command_posix, cfg, cmd) or cmd
        launch = _resolved_command(full, env)
        if launch is None and full != cmd:
            launch = _resolved_command(
                cmd, env
            )  # the limiter (systemd-run, nice) vanished: run unlimited, not never
        if launch is None:
            _say(f"could not start Claude Code: {cmd[0]} was not found on PATH")
            return 126
        try:
            os.execve(launch[0], launch, env)
        except OSError as exc:
            _say(f"could not start Claude Code: {exc}")
            return 126
    # Windows has no exec(): keep a thin parent that owns the Job Object.
    launch = _resolved_command(cmd, env)
    if launch is None:
        _say(f"could not start Claude Code: {cmd[0]} was not found on PATH")
        return 126
    child_pids: list[int] = []
    try:
        return _run_windows(
            cfg, launch, env, cwd, kind, routed_url, track and not opts.get("no-limits"), child_pids
        )
    finally:
        for pid in child_pids:
            _best_effort(resources.unregister_session, pid)


def _absolute(binary: Path | None) -> Path | None:
    """A located Claude Code binary, safe to start. A bare name (find_claude returns one when `claude.binary` or
    CLAUDE_HR_CLAUDE_BIN names a file in the current directory, and pathlib drops a leading `./`) is looked up through
    absolute PATH entries only - never in the current directory, which may be an untrusted repository - and not
    accepted when that finds a claude-hr shim of this very launcher. None means "not found"."""
    if binary is None:
        return None
    exe = procutil.resolve_exe(str(binary))
    if exe is None or (exe != str(binary) and procutil._is_shim(Path(exe))):
        return None
    return Path(exe)


def _resolved_command(cmd: list[str], env: dict[str, str] | None = None) -> list[str] | None:
    """`cmd` with its program resolved to an absolute path, or None when a bare program name is not on PATH.

    Every program the launcher starts (claude, `op`, the limiter prefix, `headroom wrap`) goes through here at the
    point of the spawn. A bare name is resolved through absolute PATH entries only (procutil.resolve_exe): Windows
    CreateProcess searches the current directory first - a freshly cloned repository that commits `op.exe` would run
    it before Claude starts - and an empty PATH entry means the current directory to execvp on POSIX.
    """
    exe = procutil.resolve_exe(str(cmd[0]), env)
    return None if exe is None else [exe, *cmd[1:]]


def _run_windows(cfg, cmd, env, cwd, kind, routed_url, limits: bool, pids: list[int]) -> int:
    import subprocess

    from . import resources

    try:
        proc = subprocess.Popen(cmd, env=env)
    except OSError as exc:  # same message and code as the POSIX exec failure above
        _say(f"could not start Claude Code: {exc}")
        return 126
    pids.append(proc.pid)
    if kind in {"session", "headless", "agents", "daemon"}:
        _best_effort(resources.register_session, proc.pid, cwd, kind)
        _best_effort(_remember_launch, proc.pid, routed_url)
    job = None
    # `agents` / `--bg` hand work to a detached daemon that MUST outlive this process, so no
    # kill-on-close Job Object for those; interactive and headless sessions do get one.
    if limits and kind in {"session", "headless"} and bool(cfg.get("resources.enabled", True)):
        try:
            job = resources.WindowsJob(
                resources.memory_limits_gb(cfg)[0], bool(cfg.get("resources.kill_on_exit", True))
            )
            job.assign(proc.pid)
        except OSError:
            job = None
        if bool(cfg.get("resources.low_priority", False)):
            _best_effort(resources.set_windows_priority_below_normal, proc.pid)
    try:
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:
                continue  # Ctrl-C reaches claude through the shared console; keep waiting for it
    finally:
        if job:
            job.close()


if __name__ == "__main__":
    raise SystemExit(main())
