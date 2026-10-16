from pathlib import Path

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
        [
            "--hr-verbose",
            "agents",
            "--hr-wait",
            "5",
            "--dangerously-skip-permissions",
            "--hr-dry-run",
        ]
    )
    assert opts == {"verbose": True, "wait": 5.0, "dry-run": True}
    assert rest == ["agents", "--dangerously-skip-permissions"]


def test_double_dash_passes_everything_after_to_claude():
    opts, rest = launcher._split_hr_flags(["--hr-direct", "--", "--hr-weird", "x"])
    assert opts == {"direct": True}
    assert rest == ["--", "--hr-weird", "x"]


def test_dry_run_plans_without_launching(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(
        launcher.procutil,
        "find_claude",
        lambda *_a, **_k: __import__("pathlib").Path("/bin/claude"),
    )
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    rc = launcher.main(["--hr-dry-run", "agents", "--dangerously-skip-permissions"])
    err = capsys.readouterr().err
    assert rc == 0
    assert (
        "kind=agents" in err
        and "agents --dangerously-skip-permissions" in err
        and "route=direct" in err
    )


def test_missing_claude_returns_127(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: None)
    assert launcher.main(["hello"]) == 127


def test_not_logged_in_goes_direct(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(
        launcher.procutil,
        "find_claude",
        lambda *_a, **_k: __import__("pathlib").Path("/bin/claude"),
    )
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
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
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
    assert (
        c("--add-dir", "a", "b", "agents") == "session"
    )  # a variadic option swallows every token up to the next flag
    assert c("--add-dir", "a", "--dangerously-skip-permissions", "agents") == "agents"
    assert (
        c("--debug", "mcp", "list") == "session"
    )  # optional-value [filter] takes "mcp" (commander semantics)
    assert c("purge", "/x") == "passthrough"  # subcommands newer than the static list
    assert c("ultrareview") == "passthrough"
    assert c("--model", "opus", "--version") == "passthrough"
    assert c("-p", "agents everywhere") == "headless"


def test_hidden_subcommands_are_still_passthrough():
    shape = launcher._parse_help(HELP)  # `daemon` is not listed in this help, like the real CLI
    assert launcher.classify(["daemon", "status"], shape) == "passthrough"


# ---- programs the launcher spawns are never searched in the current directory ---------------------------------------
def _exe(directory, name):
    """An executable file `name` (with .exe on Windows) in `directory`; it is never actually run."""
    import os
    import stat

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + (".exe" if os.name == "nt" else ""))
    path.write_bytes(b"")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def hostile_cwd(tmp_path, monkeypatch):
    """cwd is a freshly cloned repository; PATH has a relative entry, an empty one and one good absolute directory."""
    import os

    repo = tmp_path / "repo"
    repo.mkdir()
    good = tmp_path / "bin"
    good.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(good)]))
    # a normal terminal: Windows then searches the current directory first, which is exactly the hole
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    return repo, good


def _fake_claude(monkeypatch, tmp_path):
    claude = _exe(tmp_path / "claude-home", "claude")
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: claude)
    monkeypatch.setattr(
        launcher, "_cli_shape", lambda _real: None
    )  # never run the fake binary for `--help`
    return claude


def test_op_run_launch_never_picks_op_from_the_current_directory(
    hostile_cwd, tmp_path, monkeypatch, capsys
):
    """Regression: credentials.op_env_file made the launcher run a bare `op` (shutil.which + CreateProcess both look in
    the cwd first on Windows), so an `op.exe` committed to a cloned repo ran before Claude's trust prompt."""
    repo, good = hostile_cwd
    planted = _exe(repo, "op")
    claude = _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    monkeypatch.setenv("CLAUDE_HR_CREDENTIALS__OP_ENV_FILE", "x.env")

    def planned_command():
        assert launcher.main(["--hr-dry-run", "hello"]) == 0
        lines = capsys.readouterr().err.splitlines()
        return next(x.split("command: ", 1)[1] for x in lines if "command: " in x)

    line = planned_command()
    assert (
        " run --no-masking" not in line and str(planted) not in line
    )  # no op anywhere: claude starts as before (the full path: a bare 'op' is also inside pytest's temp path on POSIX)
    assert line == f"{claude} hello"

    real_op = _exe(
        good, "op"
    )  # a real 1Password CLI on an absolute PATH entry is used, by its absolute path
    assert planned_command() == f"{real_op} run --no-masking --env-file x.env -- {claude} hello"


def _launch_and_record(monkeypatch, args, headroom=False):
    """Run launcher.main up to the spawn, record the argv that would have been started, and fail the spawn."""
    spawned: list[list[str]] = []

    def refuse_posix(path, argv, env=None):
        spawned.append(list(argv))
        raise OSError("recorded, not started")

    class RefusePopen:
        def __init__(self, argv, *a, **k):
            spawned.append(list(argv))
            raise OSError("recorded, not started")

    import subprocess

    monkeypatch.setattr(launcher.os, "execve", refuse_posix)
    monkeypatch.setattr(subprocess, "Popen", RefusePopen)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "true" if headroom else "false")
    rc = launcher.main(["--hr-no-limits", *args])
    return rc, spawned


def test_the_spawn_uses_the_resolved_op_not_a_planted_one(hostile_cwd, tmp_path, monkeypatch):
    repo, good = hostile_cwd
    planted = _exe(repo, "op")
    claude = _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_HR_CREDENTIALS__OP_ENV_FILE", "x.env")

    rc, spawned = _launch_and_record(monkeypatch, ["hello"])  # no real op anywhere on PATH
    assert rc == 126 and spawned == [
        [str(claude), "hello"]
    ]  # fail open: no wrapper, and never the planted file

    real_op = _exe(good, "op")
    rc, spawned = _launch_and_record(monkeypatch, ["hello"])
    assert spawned[0][0] == str(real_op) and spawned[0][0] != str(planted)


def test_a_bare_configured_claude_name_is_not_taken_from_the_current_directory(
    hostile_cwd, tmp_path, monkeypatch, capsys
):
    """claude.binary = "claude" next to a committed ./claude file: pathlib drops the "./", so the old code exec'd it."""
    repo, good = hostile_cwd
    _exe(repo, "claude")
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: Path("claude"))
    monkeypatch.setattr(launcher, "_cli_shape", lambda _real: None)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    assert (
        launcher.main(["--hr-no-limits", "hello"]) == 127
    )  # not on any absolute PATH entry: not found
    assert "not found" in capsys.readouterr().err
    real = _exe(good, "claude")
    rc, spawned = _launch_and_record(monkeypatch, ["hello"])
    assert spawned == [[str(real), "hello"]]

    # the two fast paths (a nested launch from inside a session, and a passthrough command) resolve the same way
    import subprocess

    seen: list[list[str]] = []

    def fake_call(argv, *a, **k):
        seen.append(list(argv))
        return 7

    def fake_execv(path, argv):
        seen.append(list(argv))
        raise SystemExit(7)

    monkeypatch.setenv(
        "CLAUDE_HR_NEST", "0"
    )  # main() writes it for a nested launch: let monkeypatch undo that
    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setattr(launcher.os, "execv", fake_execv)
    for env_session, argv in (("1", ["hello"]), ("", ["mcp", "list"])):
        if env_session:
            monkeypatch.setenv("CLAUDE_HR_SESSION", env_session)
        else:
            monkeypatch.delenv("CLAUDE_HR_SESSION", raising=False)
        seen.clear()
        try:
            rc = launcher.main(argv)
        except SystemExit as exc:
            rc = exc.code
        assert rc == 7 and seen == [[str(real), *argv]], argv


@pytest.mark.skipif(__import__("os").name == "nt", reason="the limiter prefix is POSIX only")
def test_the_limiter_prefix_is_resolved_and_dropped_when_missing(
    hostile_cwd, tmp_path, monkeypatch
):
    repo, good = hostile_cwd
    planted = _exe(repo, "systemd-run")
    claude = _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setattr(
        launcher.paths, "IS_WINDOWS", False
    )  # already the case here; explicit for the reader
    from claude_hr import resources

    monkeypatch.setattr(
        resources,
        "wrap_command_posix",
        lambda _cfg, cmd: ["systemd-run", "--user", "--scope", *cmd],
    )
    spawned: list[list[str]] = []

    def record(path, argv, env=None):
        spawned.append(list(argv))
        raise OSError("recorded, not started")

    monkeypatch.setattr(launcher.os, "execve", record)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")
    assert launcher.main(["--hr-force", "hello"]) == 126
    assert spawned == [
        [str(claude), "hello"]
    ]  # the planted systemd-run is ignored; claude runs unlimited

    real = _exe(good, "systemd-run")
    spawned.clear()
    assert launcher.main(["--hr-force", "hello"]) == 126
    assert spawned == [[str(real), "--user", "--scope", str(claude), "hello"]]
    assert str(planted) not in spawned[0]


def test_headroom_wrap_is_never_a_headroom_from_the_current_directory(
    hostile_cwd, tmp_path, monkeypatch, capsys
):
    """`--hr-wrap` starts `headroom wrap claude`: the binary found on a relative PATH entry comes back as a bare name."""
    repo, good = hostile_cwd
    planted = _exe(repo, "headroom")
    _fake_claude(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_HR_HEADROOM_BIN", raising=False)
    rc, spawned = _launch_and_record(monkeypatch, ["--hr-wrap", "hello"], headroom=True)
    assert rc == 126 and spawned == [], "fail open to an error message, never to the planted file"
    assert "was not found on PATH" in capsys.readouterr().err
    real = _exe(good, "headroom")
    monkeypatch.setenv(
        "CLAUDE_HR_HEADROOM_BIN", str(real)
    )  # an explicit, absolute choice is honoured
    rc, spawned = _launch_and_record(monkeypatch, ["--hr-wrap", "hello"], headroom=True)
    assert spawned and spawned[0][:2] == [str(real), "wrap"] and str(planted) not in spawned[0]


def test_resolved_command_is_the_single_choke_point(hostile_cwd):
    repo, good = hostile_cwd
    _exe(repo, "tool")
    assert launcher._resolved_command(["tool", "--x"]) is None
    real = _exe(good, "tool")
    assert launcher._resolved_command(["tool", "--x"]) == [str(real), "--x"]
    absolute = str(real)
    assert launcher._resolved_command([absolute, "y"]) == [
        absolute,
        "y",
    ]  # an explicit path is the caller's choice


# ---- the one-time offer of the test gate ----------------------------------------------------------------------------
class _OfferWorld:
    def __init__(self, repo, launched, prompts, answers):
        self.repo = repo
        self.launched = launched
        self.prompts = prompts
        self.answers = answers


@pytest.fixture
def offer_world(tmp_path, monkeypatch, request):
    """A real git repository as the current directory, a person at a terminal (scripted), a fake claude whose start is
    only recorded, and a discovery that proposes two real checks (`python -c pass`)."""
    import json
    import os
    import subprocess
    import sys

    from claude_hr import gate_offer, human, verify

    repo = tmp_path / "work" / "proj"
    repo.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is needed for a real repository")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    # the folder is one the person has already told Claude Code to trust (the offer waits for that)
    from claude_hr import paths as _paths

    _paths.claude_global_json().write_text(
        json.dumps({"projects": {str(repo).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8",
    )
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_HR_GATES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__ENABLED", "false")

    claude = _exe(tmp_path / "claude-home", "claude")
    monkeypatch.setattr(launcher.procutil, "find_claude", lambda *_a, **_k: claude)
    monkeypatch.setattr(launcher, "_cli_shape", lambda _real: None)

    launched: list[list[str]] = []

    def fake_execve(path, argv, env=None):
        launched.append(list(argv))
        raise OSError("recorded, not started")

    def fake_run_windows(cfg, cmd, env, cwd, kind, routed_url, limits, pids):
        launched.append(list(cmd))
        return 0

    monkeypatch.setattr(launcher.os, "execve", fake_execve)
    monkeypatch.setattr(launcher, "_run_windows", fake_run_windows)

    prompts: list[str] = []
    answers: list[str] = []
    unscripted: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not answers:
            unscripted.append(prompt)
            raise AssertionError(f"nobody was supposed to be asked: {prompt!r}")
        return answers.pop(0)

    def nobody_unexpected_was_asked():
        # the launcher swallows every error from the offer (Claude must start): the question that raised is kept here
        assert unscripted == [], f"a question nobody scripted was asked: {unscripted!r}"

    request.addfinalizer(nobody_unexpected_was_asked)
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(human, "not_a_person", lambda: None)
    monkeypatch.setattr(
        human, "_live_console", lambda: False
    )  # input() is scripted: no console to flush or wait on
    monkeypatch.setattr(gate_offer, "_can_see_the_question", lambda: True)
    py = sys.executable
    prop = verify.Recipe(
        [
            verify.Check("lint", [py, "-c", "pass"], 30),
            verify.Check("tests", [py, "-c", "pass"], 30),
        ],
        [
            verify.Check("lint", [py, "-c", "pass"], 30),
            verify.Check("tests", [py, "-c", "pass"], 30),
        ],
        "discovered from test",
        [],
    )
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    return _OfferWorld(repo, launched, prompts, answers)


def test_an_interactive_session_is_offered_the_gate_once(offer_world, capsys):
    from claude_hr import paths, verify

    offer_world.answers.append("")  # Enter
    launcher.main(["--hr-no-limits", "fix the bug"])
    err = capsys.readouterr().err
    assert "claude-hr can double-check Claude's work in this folder" in err
    assert "Turned on" in err
    assert len(offer_world.launched) == 1 and offer_world.launched[0][-1] == "fix the bug"
    assert verify.trust_state(offer_world.repo, paths.state_dir()) == "trusted"
    # the second launch is silent and still starts Claude
    launcher.main(["--hr-no-limits", "fix the bug"])
    err = capsys.readouterr().err
    assert "double-check" not in err and len(offer_world.prompts) == 1
    assert len(offer_world.launched) == 2


def test_no_to_the_offer_is_final_and_claude_still_starts(offer_world, capsys):
    from claude_hr import gate_offer, paths, verify

    offer_world.answers.append("n")
    launcher.main(["--hr-no-limits"])
    launcher.main(["--hr-no-limits"])
    assert len(offer_world.prompts) == 1 and len(offer_world.launched) == 2
    assert verify.trust_state(offer_world.repo, paths.state_dir()) == "no-config"
    assert gate_offer.decision_for(offer_world.repo)["decision"] == "declined"


def test_ctrl_c_at_the_offer_launches_claude_and_asks_again_next_time(offer_world, monkeypatch):
    def interrupted(prompt=""):
        offer_world.prompts.append(prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    launcher.main(["--hr-no-limits"])
    launcher.main(["--hr-no-limits"])
    assert len(offer_world.prompts) == 2 and len(offer_world.launched) == 2


def test_nobody_at_the_terminal_does_not_hold_claude_back(offer_world, monkeypatch, capsys):
    from claude_hr import gate_offer, human

    waits: list[float] = []
    monkeypatch.setattr(human, "_live_console", lambda: True)
    monkeypatch.setattr(human, "_discard_pending_input", lambda: None)
    # the wait ran out
    monkeypatch.setattr(human, "_wait_for_answer", lambda limit_s: waits.append(limit_s) or False)
    launcher.main(["--hr-no-limits"])
    err = capsys.readouterr().err
    assert human.LAUNCHER_TIMEOUT_NOTE in err and "Claude starts without asking" in err
    assert offer_world.prompts == [] and len(waits) == 1
    assert len(offer_world.launched) == 1  # claude started
    assert gate_offer.decision_for(offer_world.repo) is None  # and nothing was decided ...
    entry = gate_offer._entry_for(offer_world.repo)  # ... only "ask again tomorrow"
    assert entry["decision"] == "later" and entry["until_days"] == 1
    # the next launch in the folder neither asks nor waits: a terminal nobody sits at costs one wait a day
    launcher.main(["--hr-no-limits"])
    assert len(waits) == 1 and len(offer_world.launched) == 2
    assert "double-check" not in capsys.readouterr().err


def test_dry_run_never_asks(offer_world, capsys):
    assert launcher.main(["--hr-dry-run", "hello"]) == 0
    assert offer_world.prompts == [] and offer_world.launched == []
    assert "double-check" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [["-p", "hi"], ["--print", "hi", "--output-format", "json"], ["--bg", "x"], ["agents"]]
)
def test_headless_background_and_agents_launches_never_ask(offer_world, argv):
    launcher.main(["--hr-no-limits", *argv])
    assert offer_world.prompts == [] and len(offer_world.launched) == 1


@pytest.mark.parametrize("argv", [["mcp", "list"], ["--version"], ["plugin", "list"], ["doctor"]])
def test_passthrough_commands_never_ask(offer_world, monkeypatch, argv):
    import subprocess

    seen = []

    def fake_call(cmd, *a, **k):
        seen.append(list(cmd))
        return 0

    def fake_execv(path, cmd):
        seen.append(list(cmd))
        raise SystemExit(0)

    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setattr(launcher.os, "execv", fake_execv)
    try:
        launcher.main(argv)
    except SystemExit:
        pass
    assert offer_world.prompts == [] and len(seen) == 1


def test_a_nested_launch_from_inside_a_session_never_asks(offer_world, monkeypatch):
    import subprocess

    seen = []

    def fake_call(cmd, *a, **k):
        seen.append(list(cmd))
        return 0

    def fake_execv(path, cmd):
        seen.append(list(cmd))
        raise SystemExit(0)

    monkeypatch.setattr(subprocess, "call", fake_call)
    monkeypatch.setattr(launcher.os, "execv", fake_execv)
    monkeypatch.setenv("CLAUDE_HR_SESSION", "1")
    monkeypatch.setenv("CLAUDE_HR_NEST", "0")
    try:
        launcher.main(["hello"])
    except SystemExit:
        pass
    assert offer_world.prompts == [] and len(seen) == 1


def test_without_a_terminal_the_session_is_not_asked(offer_world, monkeypatch):
    from claude_hr import human

    with monkeypatch.context() as m:
        m.setattr(human, "not_a_person", lambda: "there is no interactive terminal")
        launcher.main(["--hr-no-limits"])
    assert offer_world.prompts == [] and len(offer_world.launched) == 1


def test_the_offer_can_be_switched_off_in_the_config(offer_world, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_VERIFY__OFFER", "off")
    launcher.main(["--hr-no-limits"])
    assert offer_world.prompts == [] and len(offer_world.launched) == 1


def test_a_config_file_that_cannot_be_read_does_not_bring_the_offer_back(offer_world, capsys):
    """The person switched the offer off in config.toml and then broke the file: the defaults must not stand in for it."""
    from claude_hr import paths

    cfg_file = paths.user_config_file()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text('[verify]\noffer = "off"\n[broken', encoding="utf-8")
    launcher.main(["--hr-no-limits"])
    assert "ignoring unreadable config" in capsys.readouterr().err
    assert offer_world.prompts == [] and len(offer_world.launched) == 1  # claude still starts
    cfg_file.write_text(
        '[verify]\noffer = "ask"\n', encoding="utf-8"
    )  # the same launch, config fixed
    offer_world.answers.append("n")
    launcher.main(["--hr-no-limits"])
    assert len(offer_world.prompts) == 1


def test_a_crash_inside_the_offer_never_stops_claude(offer_world, monkeypatch):
    from claude_hr import gate_offer

    def boom(args, cfg):
        raise RuntimeError("the offer exploded")

    monkeypatch.setattr(gate_offer, "maybe_offer", boom)
    launcher.main(["--hr-no-limits", "hello"])
    assert len(offer_world.launched) == 1


def test_a_missing_offer_module_never_stops_claude(offer_world, monkeypatch):
    import sys

    import claude_hr

    monkeypatch.delattr(claude_hr, "gate_offer", raising=False)
    monkeypatch.setitem(sys.modules, "claude_hr.gate_offer", None)
    launcher.main(["--hr-no-limits", "hello"])
    assert len(offer_world.launched) == 1


def _run_launcher_in_a_fresh_process(tmp_path, argv):
    """Start launcher.main in a new interpreter with the spawn replaced; returns the modules it had loaded."""
    import os
    import subprocess
    import sys

    claude = _exe(tmp_path / "fresh-claude", "claude")
    code = (
        "import sys\n"
        "from claude_hr import launcher\n"
        "launcher._cli_shape = lambda real: None\n"
        "def no_exec(*a, **k):\n"
        "    raise OSError('recorded')\n"
        "launcher.os.execve = no_exec\n"
        "launcher._run_windows = lambda *a, **k: 0\n"
        "launcher.main(['--hr-no-limits', *sys.argv[1:]])\n"
        "print('LOADED', *sorted(m for m in sys.modules if m.startswith('claude_hr.')))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "CLAUDE_HR_CLAUDE_BIN": str(claude),
        "CLAUDE_HR_HEADROOM__ENABLED": "false",
    }
    env.pop("CLAUDE_HR_SESSION", None)
    r = subprocess.run(
        [sys.executable, "-c", code, *argv],
        env=env,
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    line = next((x for x in r.stdout.splitlines() if x.startswith("LOADED ")), None)
    assert line is not None, r.stdout + r.stderr
    return set(line.split()[1:])


@pytest.mark.parametrize("argv", [["-p", "hello"], ["--print", "x"], ["agents"], ["--bg", "x"]])
def test_a_non_interactive_launch_does_not_even_import_the_offer(tmp_path, argv):
    loaded = _run_launcher_in_a_fresh_process(tmp_path, argv)
    assert not {"claude_hr.gate_offer", "claude_hr.verify", "claude_hr.audit"} & loaded


def test_a_session_without_a_terminal_imports_neither_verify_nor_audit(tmp_path):
    # stdin is /dev/null here: the cheap checks stop it before any heavy module is touched
    loaded = _run_launcher_in_a_fresh_process(tmp_path, ["hello"])
    assert not {"claude_hr.verify", "claude_hr.audit"} & loaded
