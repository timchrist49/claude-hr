import json
from pathlib import Path

import pytest

from claude_hr import config, ctl, headroom, paths, resources, sysinfo
from claude_hr.headroom import Headroom, Health


def hr(monkeypatch=None, **env):
    return Headroom(config.load())


@pytest.fixture
def full(monkeypatch):
    """The footprint these tests are about, set explicitly: `auto` reads the machine's RAM, and under 8 GB it drops the
    memory and code-aware flags, which made a test of "memory is off" pass whatever the default was."""
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__FOOTPRINT", "full")


def test_proxy_args_are_safe_for_parallel_agents(sandbox, full, monkeypatch):
    monkeypatch.setenv(
        "CLAUDE_HR_HEADROOM__MEMORY", "true"
    )  # the memory path is only built when it is switched on
    args = hr().proxy_args()
    assert args[:1] == ["proxy"]
    assert "--no-rate-limit" in args  # the default 60 req/min limiter throttles parallel sessions
    assert args[args.index("--mode") + 1] == "cache"
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert "--workers" in args
    assert "--memory-db-path" in args
    assert str(Path(args[args.index("--memory-db-path") + 1])).startswith(
        str(sandbox)
    )  # never cwd-relative


def test_memory_is_off_by_default_because_in_cache_mode_it_is_pure_overhead(
    sandbox, full, monkeypatch
):
    assert config.load().get("headroom.memory") is False  # defaults.toml itself
    args = hr().proxy_args()
    assert "--code-aware" in args, (
        "the full footprint is in effect: no memory flag is missing for lack of RAM"
    )
    assert "--memory" not in args and "--memory-db-path" not in args
    monkeypatch.setenv(
        "CLAUDE_HR_HEADROOM__MEMORY", "true"
    )  # the same footprint does build them when asked
    assert "--memory" in hr().proxy_args()


def test_lite_footprint_drops_ml_features(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__FOOTPRINT", "lite")
    h = hr()
    assert h.effective_extras() == "proxy,code,mcp"
    args = h.proxy_args()
    assert "--memory" not in args
    assert "--code-aware" not in args
    assert h.install_spec() == "headroom-ai[proxy,code,mcp]"


def test_auto_footprint_goes_lite_on_small_machines(sandbox, monkeypatch):
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (4.0, 2.0))
    assert hr().effective_extras() == "proxy,code,mcp"
    monkeypatch.setattr(sysinfo, "memory_gb", lambda: (32.0, 20.0))
    assert hr().effective_extras() == "all"


def test_install_spec_pins(sandbox, full, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__VERSION", "0.41.0")
    assert hr().install_spec() == "headroom-ai[all]==0.41.0"
    assert hr().install_spec("0.40.0") == "headroom-ai[all]==0.40.0"


def test_server_env_is_private_by_default(sandbox):
    env = hr().server_env({"PATH": "x"})
    assert env["HEADROOM_BEACON"] == "off"
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HEADROOM_UPDATE_CHECK"] == "off"
    assert env["HEADROOM_SAVINGS_PROFILE"] == "coding"
    assert (
        "HEADROOM_OUTPUT_SHAPER" not in env
    )  # off by default: it edits every request's system prompt
    assert "HEADROOM_WRAP_OWNED" not in env  # a wrap-owned proxy would self-terminate


def test_output_shaper_can_be_switched_on(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__OUTPUT_SHAPER", "true")
    assert hr().server_env({"PATH": "x"})["HEADROOM_OUTPUT_SHAPER"] == "1"


def test_client_env_keeps_tool_search_and_labels_project(sandbox, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    proj = tmp_path / "my proj"
    proj.mkdir()
    env = hr().client_env(proj)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8787"
    assert env["ENABLE_TOOL_SEARCH"] == "true"
    headers = env["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Headroom-Project: my proj" in headers  # label encoding identical to `headroom wrap`
    assert "X-Headroom-Cwd: " in headers
    assert (
        "%20" in headers.split("X-Headroom-Cwd: ")[1]
    )  # full path is percent-encoded (space, backslashes)


def test_client_env_preserves_user_headers(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Team: blue")
    env = hr().client_env(tmp_path)
    assert env["ANTHROPIC_CUSTOM_HEADERS"].startswith("X-Team: blue\n")


def test_offline_models_auto_requires_cached_model(sandbox, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    h = hr()
    assert h._offline_models() is False
    (tmp_path / "hf" / "hub" / "models--chopratejas--kompress-v2-base").mkdir(parents=True)
    assert h._offline_models() is True
    assert h.server_env({})["HF_HUB_OFFLINE"] == "1"


def test_mcp_specs(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__SERENA_VERSION", "2.0.0")
    h = hr()
    assert h.mcp_server_spec()["args"] == ["mcp", "serve"]
    assert h.mcp_server_spec()["env"]["HEADROOM_PROXY_URL"] == h.url
    # same workspace as the proxy, so the MCP tool never keeps a plaintext store in ~/.headroom
    assert h.mcp_server_spec()["env"]["HEADROOM_WORKSPACE_DIR"] == str(paths.hr_home() / "headroom")
    assert "serena-agent==2.0.0" in h.serena_spec()["args"]
    assert "--project-from-cwd" in h.serena_spec()["args"]


def test_health_down_when_nothing_listens(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__PORT", "1")  # nothing can listen here
    h = hr().health(timeout=0.3)
    assert not h.ok
    assert h.status == "down"


# ---- the settings the running proxy was started with -------------------------------------------------------------


class FakeProc:
    pid = 4242


@pytest.fixture
def spawn(sandbox, full, monkeypatch):
    """start() against a stand-in binary and a stand-in Popen: what it spawned, and nothing else."""
    spawned = []
    monkeypatch.setattr(Headroom, "binary", lambda self: Path("/fake/headroom"))

    def popen(cmd, **kwargs):
        spawned.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr(headroom.subprocess, "Popen", popen)
    return spawned


def record():
    return json.loads((paths.state_dir() / "headroom-proxy.json").read_text(encoding="utf-8"))


def test_start_records_a_hash_of_the_settings_it_used(spawn):
    h = hr()
    assert h.start() is not None
    rec = record()
    assert rec["pid"] == 4242 and rec["port"] == h.port and rec["started"] > 0
    assert rec["args"] == h.settings_hash() and len(rec["args"]) == 12
    cmd, _kwargs = spawn[0]
    assert cmd[1:] == h.proxy_args()  # what is hashed is what was spawned


def test_start_on_windows_asks_for_no_window_and_never_detaches(spawn, monkeypatch):
    """DETACHED_PROCESS overrides CREATE_NO_WINDOW: uv's headroom.exe trampoline then has no console, the python.exe it starts
    gets a NEW visible one (an empty Windows Terminal tab titled with the path of headroom.exe), and closing it kills the proxy."""
    monkeypatch.setattr(headroom.paths, "IS_WINDOWS", True)
    assert hr().start() is not None
    flags = spawn[0][1]["creationflags"]
    assert flags & 0x08000000, "CREATE_NO_WINDOW"
    assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP"
    assert flags & 0x01000000, "CREATE_BREAKAWAY_FROM_JOB is tried first"
    assert not flags & 0x00000008, "DETACHED_PROCESS must never be set"


def test_start_on_windows_falls_back_without_breakaway_but_still_without_a_window(
    sandbox, full, monkeypatch
):
    monkeypatch.setattr(Headroom, "binary", lambda self: Path("/fake/headroom"))
    monkeypatch.setattr(headroom.paths, "IS_WINDOWS", True)
    calls = []

    def popen(cmd, **kwargs):
        calls.append(kwargs["creationflags"])
        if len(calls) == 1:
            raise OSError("the parent job does not allow breakaway")
        return FakeProc()

    monkeypatch.setattr(headroom.subprocess, "Popen", popen)
    assert hr().start() is not None
    assert len(calls) == 2 and calls[0] & 0x01000000 and not calls[1] & 0x01000000
    assert all(c & 0x08000000 and not c & 0x00000008 for c in calls)


@pytest.mark.parametrize(
    "name, value",
    [
        ("MEMORY", "true"),
        ("OUTPUT_SHAPER", "true"),
        ("OUTPUT_HOLDOUT", "0.1"),
        ("SAVINGS_PROFILE", "balanced"),
        ("PROXY_MODE", "token"),
        ("PORT", "18787"),
        ("LEARN", "true"),
        ("EXTRA_PROXY_ARGS", '["--some-flag"]'),
        ("TELEMETRY", "true"),
    ],
)
def test_the_hash_changes_with_every_setting_that_reaches_the_proxy(
    sandbox, full, monkeypatch, name, value
):
    before = hr().settings_hash()
    monkeypatch.setenv(f"CLAUDE_HR_HEADROOM__{name}", value)
    assert hr().settings_hash() != before


def test_the_hash_ignores_what_is_not_a_setting(sandbox, full, monkeypatch, tmp_path):
    before = hr().settings_hash()
    monkeypatch.setenv(
        "HEADROOM_OUTPUT_SHAPER", "1"
    )  # the shell this process runs in is not the configuration
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    (tmp_path / "hf" / "hub" / "models--chopratejas--kompress-v2-base").mkdir(parents=True)
    assert hr()._offline_models() is True  # the model got cached since the proxy started
    assert hr().settings_hash() == before


def write_record(data):
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (paths.state_dir() / "headroom-proxy.json").write_text(text, encoding="utf-8")


def test_settings_state(sandbox, full):
    h = hr()
    assert h.settings_state() == "unknown", "no record of a start by claude-hr: not judged"
    write_record({"pid": 1, "port": 8787, "started": 1.0})
    assert h.settings_state() == "older", "a record without a hash: an older claude-hr started it"
    write_record({"pid": 1, "port": 8787, "started": 1.0, "args": "0123456789ab"})
    assert h.settings_state() == "changed"
    write_record({"pid": 1, "port": 8787, "started": 1.0, "args": h.settings_hash()})
    assert h.settings_state() == "current"
    for broken in ("{not json", "[]", '"x"'):
        write_record(broken)
        assert h.settings_state() == "unknown"


def test_a_proxy_started_with_the_0_2_0_defaults_is_not_current(sandbox, full, monkeypatch):
    """0.2.0 started the proxy with --memory and the output shaper on; the flipped defaults must not read as "current"."""
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__MEMORY", "true")
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__OUTPUT_SHAPER", "true")
    write_record({"pid": 1, "port": 8787, "started": 1.0, "args": hr().settings_hash()})
    assert hr().settings_state() == "current"
    monkeypatch.delenv("CLAUDE_HR_HEADROOM__MEMORY")
    monkeypatch.delenv("CLAUDE_HR_HEADROOM__OUTPUT_SHAPER")
    assert hr().settings_state() == "changed"


# ---- `headroom restart` with sessions running ----------------------------------------------------------------------


@pytest.fixture
def restart(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(Headroom, "stop", lambda self: calls.append("stop") or True)
    monkeypatch.setattr(
        Headroom,
        "ensure",
        lambda self, wait_s=0.0, announce=None: calls.append("ensure") or Health(True),
    )
    return calls


def test_restart_warns_with_the_number_of_running_sessions_but_restarts(
    restart, monkeypatch, capsys
):
    monkeypatch.setattr(resources, "live_sessions", lambda: [{"pid": 1}, {"pid": 2}])
    assert ctl.main(["headroom", "restart"]) == 0
    out = capsys.readouterr().out
    assert "warning: 2 claude-hr sessions running" in out and "healthy" in out
    assert restart == ["stop", "ensure"]


def test_restart_warning_is_singular_for_one_session(restart, monkeypatch, capsys):
    monkeypatch.setattr(resources, "live_sessions", lambda: [{"pid": 1}])
    ctl.main(["headroom", "restart"])
    assert "warning: 1 claude-hr session running" in capsys.readouterr().out


def test_restart_without_sessions_says_nothing_extra(restart, monkeypatch, capsys):
    monkeypatch.setattr(resources, "live_sessions", lambda: [])
    assert ctl.main(["headroom", "restart"]) == 0
    assert "warning" not in capsys.readouterr().out and restart == ["stop", "ensure"]


def test_restart_still_restarts_when_the_session_registry_cannot_be_read(
    restart, monkeypatch, capsys
):
    def boom():
        raise OSError("registry unreadable")

    monkeypatch.setattr(resources, "live_sessions", boom)
    assert ctl.main(["headroom", "restart"]) == 0
    assert "warning" not in capsys.readouterr().out and restart == ["stop", "ensure"]
