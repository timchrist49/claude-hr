from pathlib import Path

from claude_hr import config, paths, sysinfo
from claude_hr.headroom import Headroom


def hr(monkeypatch=None, **env):
    return Headroom(config.load())


def test_proxy_args_are_safe_for_parallel_agents(sandbox, monkeypatch):
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


def test_memory_is_off_by_default_because_in_cache_mode_it_is_pure_overhead(sandbox):
    args = hr().proxy_args()
    assert "--memory" not in args and "--memory-db-path" not in args


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


def test_install_spec_pins(sandbox, monkeypatch):
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
