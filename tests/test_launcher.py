import pytest

from claude_hr import launcher


@pytest.mark.parametrize(
    "args,kind",
    [
        ([], "session"),
        (["--resume", "abc"], "session"),
        (["--model", "sonnet", "fix the bug"], "session"),
        (["-p", "hello"], "headless"),
        (["--print", "hi", "--output-format", "json"], "headless"),
        (["agents"], "agents"),
        (["agents", "--dangerously-skip-permissions"], "agents"),
        (["--dangerously-skip-permissions", "agents"], "agents"),
        (["agents", "--json"], "agents"),
        (["--bg", "do it"], "daemon"),
        (["mcp", "list"], "passthrough"),
        (["plugin", "install", "x"], "passthrough"),
        (["update"], "passthrough"),
        (["doctor"], "passthrough"),
        (["--version"], "passthrough"),
        (["-v"], "passthrough"),
        (["--help"], "passthrough"),
        (["logs", "abc"], "passthrough"),
        (["-p", "explain what --help does"], "headless"),  # --help inside the prompt is not a flag
    ],
)
def test_classify(args, kind):
    assert launcher.classify(args) == kind


def test_split_hr_flags_only_strips_hr_switches():
    opts, rest = launcher._split_hr_flags(
        ["--hr-verbose", "agents", "--hr-wait", "5", "--dangerously-skip-permissions", "--hr-dry-run"]
    )
    assert opts == {"verbose": True, "wait": 5.0, "dry-run": True}
    assert rest == ["agents", "--dangerously-skip-permissions"]


def test_double_dash_passes_everything_after_to_claude():
    opts, rest = launcher._split_hr_flags(["--hr-direct", "--", "--hr-weird", "x"])
    assert opts == {"direct": True}
    assert rest == ["--", "--hr-weird", "x"]


def test_dry_run_plans_without_launching(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: __import__("pathlib").Path("/bin/claude"))
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    rc = launcher.main(["--hr-dry-run", "agents", "--dangerously-skip-permissions"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "kind=agents" in err and "agents --dangerously-skip-permissions" in err and "route=direct" in err


def test_missing_claude_returns_127(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: None)
    assert launcher.main(["hello"]) == 127


def test_not_logged_in_goes_direct(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: __import__("pathlib").Path("/bin/claude"))
    monkeypatch.setattr(launcher.paths, "IS_MAC", False)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert launcher.main(["--hr-dry-run", "hello"]) == 0
    err = capsys.readouterr().err
    assert "route=direct (no login yet)" in err


def test_logged_in_detection(sandbox, monkeypatch):
    monkeypatch.setattr(launcher.paths, "IS_MAC", False)
    assert launcher._logged_in({}) is False
    assert launcher._logged_in({"ANTHROPIC_API_KEY": "x"}) is True
    assert launcher._logged_in({"CLAUDE_CODE_USE_BEDROCK": "1"}) is True
    (sandbox / ".claude" / ".credentials.json").write_text("{}")
    assert launcher._logged_in({}) is True


def test_malformed_hr_wait_does_not_crash():
    opts, rest = launcher._split_hr_flags(["--hr-wait", "abc", "hello"])
    assert "wait" not in opts
    assert rest == ["hello"]


def test_launcher_import_stays_light():
    """Regression: eager imports cost ~190 ms per `claude` call on Windows. Keep the module import cheap."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "import claude_hr.launcher\n"
        "heavy = {'urllib.request', 'http.client', 'subprocess', 'socket', 'shutil', 'json', 'tomllib',\n"
        "         'claude_hr.resources', 'claude_hr.headroom', 'claude_hr.config', 'claude_hr.sysinfo'}\n"
        "loaded = sorted(heavy & set(sys.modules))\n"
        "print(loaded)\n"
        "sys.exit(1 if loaded else 0)\n"
    )
    env = {**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


HELP = """Usage: claude [options] [command] [prompt]

Options:
  --add-dir <directories...>            Additional directories
  --model <model>                       Model
  -d, --debug [filter]                  Debug
  -p, --print                           Print mode
  --dangerously-skip-permissions        Bypass
  -h, --help                            Display help

Commands:
  agents [options]                      Manage background agents
  mcp                                   Configure MCP servers
  plugin|plugins [options] [command]    Manage plugins
  purge [options] <path>                Delete state
  ultrareview [target]                  Cloud review
                                        wrapped description line that must not become a command
"""


def test_parse_help_extracts_subcommands_and_value_flags():
    subs, value, variadic = launcher._parse_help(HELP)
    assert {"agents", "mcp", "plugin", "plugins", "purge", "ultrareview"} <= subs
    assert "wrapped" not in subs
    assert {"--model", "--add-dir", "--debug", "-d"} <= value
    assert "--add-dir" in variadic
    assert "--print" not in value and "-p" not in value


def test_classify_with_real_cli_shape():
    shape = launcher._parse_help(HELP)
    c = lambda *a: launcher.classify(list(a), shape)  # noqa: E731
    assert c("--model", "sonnet", "agents") == "agents"  # flag value is not the subcommand
    assert c("--model", "sonnet", "fix the tests") == "session"
    assert c("--add-dir", "agents", "other") == "session"  # `agents` is a directory name here
    assert c("--add-dir", "a", "b", "agents") == "session"  # a variadic option swallows every token up to the next flag
    assert c("--add-dir", "a", "--dangerously-skip-permissions", "agents") == "agents"
    assert c("--debug", "mcp", "list") == "session"  # optional-value [filter] takes "mcp" (commander semantics)
    assert c("purge", "/x") == "passthrough"  # subcommands newer than the static list
    assert c("ultrareview") == "passthrough"
    assert c("--model", "opus", "--version") == "passthrough"
    assert c("-p", "agents everywhere") == "headless"


def test_hidden_subcommands_are_still_passthrough():
    shape = launcher._parse_help(HELP)  # `daemon` is not listed in this help, like the real CLI
    assert launcher.classify(["daemon", "status"], shape) == "passthrough"
