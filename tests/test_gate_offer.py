"""The one-time offer of the end-of-turn test gate (src/claude_hr/gate_offer.py, human.py).

Real tiny git repositories in tmp_path, real checks (`sys.executable -c ...` argv, so no external tool is needed), a
scripted person (human.not_a_person and builtins.input are replaced; nothing here needs a terminal). The invariant
under test throughout: nothing is approved without a person at a terminal, and what is approved is exactly what was
shown."""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from claude_hr import config, gate_offer, human, paths, stop_gate, verify

PY = sys.executable
PASS = "pass"
FAIL = "raise SystemExit(3)"


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, timeout=60)


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is needed for a real repo")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "work" / "proj"
    r.mkdir(parents=True)
    _git(r, "init", "-q")
    (r / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
    monkeypatch.chdir(r)
    monkeypatch.delenv("CLAUDE_HR_GATES", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    return r


def chk(name, code, timeout_s=30, cwd="."):
    return verify.Check(name, [PY, "-c", code], timeout_s, cwd)


def proposal_of(*checks):
    return verify.Recipe(list(checks), list(checks), "discovered from test", [])


@pytest.fixture
def cfg():
    return config.load()


@pytest.fixture
def person(monkeypatch):
    """A person at a normal terminal (the terminal itself is not needed: input() is scripted)."""
    monkeypatch.setattr(human, "not_a_person", lambda: None)
    monkeypatch.setattr(gate_offer, "_can_see_the_question", lambda: True)


class Keys:
    def __init__(self):
        self.answers: list = []
        self.prompts: list[str] = []

    def type(self, *answers):
        self.answers.extend(answers)
        return self


@pytest.fixture
def keys(monkeypatch):
    """What the person types. A question nobody scripted fails the test: that is how "never asked" is proved."""
    k = Keys()

    def fake_input(prompt=""):
        k.prompts.append(prompt)
        if not k.answers:
            raise AssertionError(f"nobody was supposed to be asked: {prompt!r}")
        answer = k.answers.pop(0)
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return k


@pytest.fixture
def offered(monkeypatch):
    """verify.discover proposes two real checks; both pass today."""
    prop = proposal_of(chk("lint", PASS), chk("tests", PASS))
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    return prop


def state_dir():
    return paths.state_dir()


def trust_state(repo):
    return verify.trust_state(repo, state_dir())


def offer_record(repo):
    return gate_offer.decision_for(repo)


def tree(root):
    """Every file under root (relative, forward slashes), .git excluded."""
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            out.add(Path(dirpath, f).relative_to(root).as_posix())
    return out


def git_tree(root):
    out = set()
    for dirpath, _dirs, filenames in os.walk(Path(root) / ".git"):
        for f in filenames:
            out.add(Path(dirpath, f).relative_to(Path(root) / ".git").as_posix())
    return out


def run_offer(cfg, args=()):
    """What the launcher does for a plain session."""
    gate_offer.maybe_offer(list(args), cfg)


# ---- human.py ---------------------------------------------------------------------------------------------------
class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_not_a_person_needs_a_terminal_on_both_ends(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    for stdin, stdout in ((False, True), (True, False), (False, False)):
        monkeypatch.setattr(sys, "stdin", _Stream(stdin))
        monkeypatch.setattr(sys, "stdout", _Stream(stdout))
        assert "no interactive terminal" in human.not_a_person()
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    monkeypatch.setattr(sys, "stdout", _Stream(True))
    assert human.not_a_person() is None


@pytest.mark.parametrize("var", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"])
def test_a_claude_code_shell_is_not_a_person_even_with_a_terminal(monkeypatch, var):
    monkeypatch.setattr(sys, "stdin", _Stream(True))
    monkeypatch.setattr(sys, "stdout", _Stream(True))
    monkeypatch.setenv(var, "1")
    assert "Claude Code session" in human.not_a_person()


def test_a_closed_stream_is_not_a_terminal(monkeypatch):
    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(sys, "stdin", Closed())
    assert human.not_a_person() == "there is no interactive terminal"


def test_ctl_keeps_a_replaceable_wrapper(monkeypatch):
    from claude_hr import ctl

    monkeypatch.setattr(human, "not_a_person", lambda: "because")
    assert ctl._not_a_person() == "because"


@pytest.mark.parametrize(
    "typed,default,expected",
    [
        ("", True, True),
        ("", False, False),
        ("y", False, True),
        ("YES", False, True),
        (" yes ", False, True),
        ("n", True, False),
        ("No", True, False),
    ],
)
def test_ask_yes_no_understands_the_answers(keys, typed, default, expected):
    keys.type(typed)
    assert human.ask_yes_no("Turn it on?", default) is expected
    assert keys.prompts == [f"Turn it on? {'[Y/n]' if default else '[y/N]'} "]


def test_an_answer_nobody_understands_is_asked_again_once_then_counts_as_no(keys):
    said = []
    keys.type("maybe", "perhaps")
    assert human.ask_yes_no("Q?", True, tell=said.append) is False
    assert said == ["Please answer y or n."] and len(keys.prompts) == 2
    keys.type("what", "y")
    assert human.ask_yes_no("Q?", False, tell=said.append) is True
    keys.type("what", "")
    assert (
        human.ask_yes_no("Q?", True, tell=said.append) is True
    )  # Enter on the second try is the default


@pytest.mark.parametrize("what", [EOFError, KeyboardInterrupt])
def test_eof_and_ctrl_c_are_not_now(keys, what):
    keys.type(what)
    assert human.ask_yes_no("Q?", True) is None


# ---- when is anything asked at all -------------------------------------------------------------------------------
def nothing_asked(cfg, repo, args=()):
    """maybe_offer must neither ask nor write anything: `keys` fails on a question, and the state stays empty."""
    before = tree(repo)
    run_offer(cfg, args)
    assert tree(repo) == before
    assert not gate_offer.offer_path().exists()
    assert trust_state(repo) in ("no-config", "invalid", "untrusted", "modified", "trusted")


@pytest.mark.parametrize("value", ["off", "false", "OFF", "never", ""])
def test_the_config_key_switches_the_offer_off(repo, person, keys, offered, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_HR_VERIFY__OFFER", json.dumps(value) if value else '""')
    nothing_asked(config.load(), repo)


@pytest.mark.parametrize(
    "var,value",
    [
        ("CLAUDE_HR_GATES__ENABLED", "false"),
        ("CLAUDE_HR_VERIFY__GATE", "false"),
        ("CLAUDE_HR_GATES", "off"),
        ("CLAUDE_HR_PLUGINS__PROFILE", '"minimal"'),
    ],
)
def test_no_gate_no_offer(repo, person, keys, offered, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    nothing_asked(config.load(), repo)


@pytest.mark.parametrize(
    "args",
    [
        ["-p", "hello"],
        ["--print", "hi", "--output-format", "json"],
        ["agents"],
        ["mcp", "list"],
        ["plugin", "install", "x"],
        ["--bg", "do it"],
        ["--version"],
        ["-h"],
    ],
)
def test_only_a_plain_interactive_session_is_asked(repo, person, keys, offered, cfg, args):
    nothing_asked(cfg, repo, args)


def test_without_a_person_nothing_is_asked(repo, keys, offered, cfg, monkeypatch):
    # the real human.not_a_person: pytest has no terminal on stdin
    nothing_asked(cfg, repo)
    monkeypatch.setattr(
        human, "not_a_person", lambda: "this shell belongs to a Claude Code session"
    )
    nothing_asked(cfg, repo)


def test_a_redirected_stderr_means_no_question(repo, keys, offered, cfg, monkeypatch):
    # the question is written to stderr: with that sent to a file the person could not see it while we waited for a key
    monkeypatch.setattr(human, "not_a_person", lambda: None)  # stdin and stdout are fine
    nothing_asked(cfg, repo)  # pytest's captured stderr is not a terminal


def test_can_see_the_question_needs_a_terminal_on_stderr(monkeypatch):
    class Odd:
        def isatty(self):
            raise ValueError("closed")

    for stream, expected in (
        (_Stream(True), True),
        (_Stream(False), False),
        (None, False),
        (Odd(), False),
    ):
        monkeypatch.setattr(sys, "stderr", stream)
        assert gate_offer._can_see_the_question() is expected


def test_outside_a_git_work_tree_nothing_is_asked(
    tmp_path, person, keys, offered, cfg, monkeypatch
):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "pyproject.toml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(plain)
    run_offer(cfg)
    assert not gate_offer.offer_path().exists()


def test_a_bare_repository_and_the_inside_of_dot_git_are_skipped(
    tmp_path, person, keys, offered, cfg, monkeypatch
):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    (bare / "pyproject.toml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(bare)
    run_offer(cfg)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    (work / "pyproject.toml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(work / ".git" / "hooks")
    run_offer(cfg)
    assert gate_offer.work_tree(work / ".git") is None and gate_offer.work_tree(bare) is None
    assert not gate_offer.offer_path().exists()


def test_a_home_directory_that_is_a_repository_is_skipped(
    sandbox, person, keys, offered, cfg, monkeypatch
):
    _git(sandbox, "init", "-q")  # a dotfiles repository
    (sandbox / "pyproject.toml").write_text("x", encoding="utf-8")
    monkeypatch.chdir(sandbox)
    run_offer(cfg)
    sub = sandbox / "notes"
    sub.mkdir()
    monkeypatch.chdir(sub)
    run_offer(cfg)  # a folder inside it belongs to the same repository
    assert gate_offer.work_tree(sub) is None
    assert not gate_offer.offer_path().exists()


def test_a_subfolder_of_a_repository_belongs_to_its_top(repo):
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    assert gate_offer.work_tree(sub) == Path(os.path.realpath(repo))


def test_a_trusted_repository_is_never_asked_and_needs_no_subprocess(
    repo, person, keys, offered, cfg, monkeypatch
):
    p = verify.config_path(repo)
    p.parent.mkdir(parents=True)
    p.write_text(verify.render_toml(proposal_of(chk("t", PASS))), encoding="utf-8", newline="\n")
    verify.trust(repo, state_dir())
    import subprocess as sp

    from claude_hr import procutil

    def boom(*a, **k):
        raise AssertionError("a trusted folder must not start a process")

    monkeypatch.setattr(sp, "Popen", boom)
    monkeypatch.setattr(procutil, "run", boom)
    run_offer(cfg)
    assert not gate_offer.offer_path().exists()


def test_an_invalid_recipe_is_not_nagged_about(repo, person, keys, offered, cfg):
    p = verify.config_path(repo)
    p.parent.mkdir(parents=True)
    p.write_text('[[fast]]\nname = "x"\nargv = ["a && b"]\n', encoding="utf-8")
    nothing_asked(cfg, repo)


def test_nothing_to_propose_asks_and_records_nothing(repo, person, keys, cfg, monkeypatch):
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of())
    run_offer(cfg)  # package markers exist, discover finds no fast check
    assert not gate_offer.offer_path().exists()
    assert not verify.config_path(repo).exists()


def test_a_folder_without_project_files_does_not_even_import_discovery(
    repo, person, keys, cfg, monkeypatch
):
    (repo / "pyproject.toml").unlink()
    (repo / "notes.txt").write_text("hi", encoding="utf-8")

    def boom(root):
        raise AssertionError("discover must not run for a folder with no project files")

    monkeypatch.setattr(verify, "discover", boom)
    run_offer(cfg)
    assert gate_offer._quick_verdict(Path(os.path.realpath(repo))) == "quiet"


# ---- the question and what it does -------------------------------------------------------------------------------
def test_the_offer_is_plain_language_and_enter_means_yes(repo, person, keys, offered, cfg, capsys):
    keys.type("")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert (
        "claude-hr can double-check Claude's work in this folder (optional; asked once per folder)."
        in err
    )
    assert "After each turn it would run this project's own checks" in err
    assert (
        f"1. {verify.command_line(offered.fast[0])}" in err
        and f"2. {verify.command_line(offered.fast[1])}" in err
    )
    assert "Nothing runs unless you say yes now" in err and "say no if you do not trust it" in err
    assert keys.prompts == ["Turn it on? [Y/n] "]
    assert trust_state(repo) == "trusted"


def test_a_yes_enables_the_folder_and_the_second_launch_is_silent(
    repo, person, keys, offered, cfg, capsys
):
    before_tree, before_git = tree(repo), git_tree(repo)
    keys.type("y")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert "[ok]   lint" in err and "[ok]   tests" in err and "Turned on" in err
    assert "claude-hr-ctl verify disable" in err and "claude-hr-ctl verify status" in err
    # exactly one new file in the work tree, and in .git only the local exclude changed
    assert tree(repo) - before_tree == {".claude/hr-verify.toml"}
    assert git_tree(repo) - before_git <= {"info/exclude"}
    loaded = verify.load_checked(repo)
    assert loaded.recipe.fast == offered.fast and loaded.recipe.full == offered.fast
    assert trust_state(repo) == "trusted"
    rec = offer_record(repo)
    assert rec["decision"] == "enabled" and rec["recipe_sha"] == loaded.sha256
    # git does not see the generated file
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".claude" not in status
    # second launch: silent (keys would raise)
    run_offer(cfg)
    assert capsys.readouterr().err == "" and keys.prompts == ["Turn it on? [Y/n] "]


def test_the_trusted_bytes_are_the_bytes_that_were_written(repo, person, keys, offered, cfg):
    keys.type("yes")
    run_offer(cfg)
    data = verify.config_path(repo).read_bytes()
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    trusted = json.loads((state_dir() / "verify-trust.json").read_text(encoding="utf-8"))
    assert trusted[verify._repo_key(repo)] == digest
    assert offer_record(repo)["recipe_sha"] == digest
    text = data.decode("utf-8")
    assert "Generated by claude-hr" in text and "claude-hr-ctl verify disable" in text
    import re

    assert len(re.findall(r"(?m)^\[\[fast\]\]$", text)) == 2
    assert len(re.findall(r"(?m)^\[\[full\]\]$", text)) == 2
    assert b"\r" not in data  # LF on every platform


def test_no_is_remembered_and_nothing_is_written(repo, person, keys, offered, cfg, capsys):
    keys.type("n")
    run_offer(cfg)
    assert "not turning it on" in capsys.readouterr().err
    assert offer_record(repo)["decision"] == "declined"
    assert not verify.config_path(repo).exists() and trust_state(repo) == "no-config"
    run_offer(cfg)  # never asked again
    assert len(keys.prompts) == 1


@pytest.mark.parametrize("what", [EOFError, KeyboardInterrupt])
def test_eof_and_ctrl_c_record_nothing_and_ask_again_next_time(
    repo, person, keys, offered, cfg, what
):
    keys.type(what)
    run_offer(cfg)
    assert not gate_offer.offer_path().exists() and not verify.config_path(repo).exists()
    keys.type("n")
    run_offer(cfg)
    assert offer_record(repo)["decision"] == "declined"


def test_an_unclear_answer_is_asked_again_once_then_is_a_no(repo, person, keys, offered, cfg):
    keys.type("maybe", "dunno")
    run_offer(cfg)
    assert len(keys.prompts) == 2 and offer_record(repo)["decision"] == "declined"
    assert not verify.config_path(repo).exists()


def test_risky_agent_settings_make_no_the_default(repo, person, keys, offered, cfg, capsys):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8"
    )
    keys.type("")  # Enter
    run_offer(cfg)
    err = capsys.readouterr().err
    assert "mcp-auto-approve" in err and "HIGH" in err and "default answer is No" in err
    assert keys.prompts == ["Turn it on? [y/N] "]
    assert offer_record(repo)["decision"] == "declined" and not verify.config_path(repo).exists()


def test_a_person_can_still_say_yes_to_a_risky_folder(repo, person, keys, offered, cfg):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8"
    )
    keys.type("y")
    run_offer(cfg)
    assert trust_state(repo) == "trusted"


def test_a_scan_that_cannot_run_is_not_a_clean_scan(
    repo, person, keys, offered, cfg, monkeypatch, capsys
):
    from claude_hr import audit

    def broken(root):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(audit, "preflight", broken)
    keys.type("")
    run_offer(cfg)
    assert keys.prompts == ["Turn it on? [y/N] "] and "could not scan" in capsys.readouterr().err
    assert offer_record(repo)["decision"] == "declined"


def test_real_discovery_shows_the_commands_it_found(repo, person, keys, cfg, capsys):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    keys.type("n")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert " -m pytest -q" in err or "pytest -q" in err
    assert not verify.config_path(repo).exists()


# ---- failing today is not enabled ---------------------------------------------------------------------------------
def test_only_checks_that_pass_today_are_kept(repo, person, keys, cfg, monkeypatch, capsys):
    prop = proposal_of(
        chk("lint", PASS),
        chk("tests", FAIL),
        verify.Check("ghost", ["definitely-not-a-real-tool-xyz", "--x"], 30, "."),
    )
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    keys.type("y")
    run_offer(cfg)
    out = capsys.readouterr().err
    assert "[ok]   lint" in out
    assert "[FAIL] tests: failing today (exit 3)" in out
    assert "[FAIL] ghost: could not start" in out
    assert (
        "Failing today, not turned on" in out
        and "add them to .claude/hr-verify.toml yourself" in out
    )
    loaded = verify.load_checked(repo)
    assert [c.name for c in loaded.recipe.fast] == ["lint"]
    text = verify.config_path(repo).read_text(encoding="utf-8")
    assert (
        "SystemExit(3)" in text and "not turned on, failing today (exit 3)" in text
    )  # a comment, never run
    assert loaded.recipe.full == loaded.recipe.fast and trust_state(repo) == "trusted"


def test_when_nothing_passes_nothing_is_enabled_and_it_says_so(
    repo, person, keys, cfg, monkeypatch, capsys
):
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of(chk("tests", FAIL)))
    before_git = git_tree(repo)
    keys.type("y")
    run_offer(cfg)
    out = capsys.readouterr().err
    assert "None of these checks pass today" in out and "claude-hr-ctl verify enable" in out
    assert not verify.config_path(repo).exists() and trust_state(repo) == "no-config"
    assert offer_record(repo) == {
        **offer_record(repo),
        "decision": "declined",
        "reason": "none of the checks pass today",
    }
    assert git_tree(repo) == before_git  # not even the exclude line
    run_offer(cfg)  # not asked again
    assert len(keys.prompts) == 1


def test_a_check_that_takes_too_long_is_left_out_and_killed(
    repo, person, keys, cfg, monkeypatch, capsys
):
    monkeypatch.setattr(gate_offer, "CHECK_CAP_S", 1)
    marker = repo / "slow-finished.txt"
    slow = chk(
        "slow", f"import time; time.sleep(30); open({str(marker)!r}, 'w').close()", timeout_s=120
    )
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of(chk("fast", PASS), slow))
    keys.type("y")
    run_offer(cfg)
    out = capsys.readouterr().err
    assert "[FAIL] slow: did not finish within 1s" in out
    assert [c.name for c in verify.load_checked(repo).recipe.fast] == ["fast"]
    assert not marker.exists()


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):  # strftime, time, ...: the real module
        import time

        return getattr(time, name)


def test_the_whole_step_has_a_time_cap(repo, person, keys, cfg, monkeypatch, capsys):
    clock = _Clock()
    monkeypatch.setattr(gate_offer, "time", clock)
    real_run = verify.run
    seen = []

    def run(root, checks, *, total_timeout_s, env_allow):
        seen.append((checks[0].name, round(total_timeout_s)))
        clock.now += 100  # the first check "takes" 100 s
        return real_run(root, checks, total_timeout_s=total_timeout_s, env_allow=env_allow)

    monkeypatch.setattr(verify, "run", run)
    monkeypatch.setattr(
        verify, "discover", lambda root: proposal_of(chk("a", PASS), chk("b", PASS), chk("c", PASS))
    )
    keys.type("y")
    run_offer(cfg)
    out = capsys.readouterr().err
    assert seen[0] == ("a", 120) and seen[1] == ("b", 20)  # what was left of the 120 s
    assert [c.name for c in verify.load_checked(repo).recipe.fast] == ["a", "b"]
    assert "[skip] c: not run (time limit for this step)" in out
    assert "not turned on, not run: the time limit" in verify.config_path(repo).read_text(
        encoding="utf-8"
    )


def test_a_check_runs_with_the_scrubbed_environment_and_closed_stdin(
    repo, person, keys, cfg, monkeypatch, capsys
):
    monkeypatch.setenv("MY_SECRET_TOKEN", "hunter2")
    code = (
        "import os, sys; sys.exit(0 if 'MY_SECRET_TOKEN' not in os.environ and "
        "os.environ.get('CI') == '1' and sys.stdin.read() == '' else 4)"
    )
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of(chk("env", code)))
    keys.type("y")
    run_offer(cfg)
    assert trust_state(repo) == "trusted", capsys.readouterr().err


# ---- the file and the exclude line --------------------------------------------------------------------------------
def test_exclude_is_added_once_and_keeps_what_was_there(repo):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# mine\n*.log", encoding="utf-8", newline="\n")  # no final newline
    assert gate_offer.exclude_locally(repo) == "added"
    assert gate_offer.exclude_locally(repo) == "already"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines == ["# mine", "*.log", ".claude/hr-verify.toml"]


def test_exclude_is_created_when_missing_and_anchored_form_counts_as_present(repo):
    shutil.rmtree(repo / ".git" / "info")
    assert gate_offer.exclude_locally(repo) == "added"
    assert (repo / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ) == ".claude/hr-verify.toml\n"
    (repo / ".git" / "info" / "exclude").write_text("/.claude/hr-verify.toml\n", encoding="utf-8")
    assert gate_offer.exclude_locally(repo) == "already"


def test_exclude_is_skipped_for_a_worktree_or_submodule(repo, tmp_path):
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
    assert gate_offer.exclude_locally(wt) == "skipped"
    assert tree(wt) == {".git"}  # nothing created next to it


def test_exclude_never_writes_through_a_link(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        shutil.rmtree(repo / ".git" / "info")
        os.symlink(outside, repo / ".git" / "info", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert gate_offer.exclude_locally(repo) == "skipped"
    assert list(outside.iterdir()) == []


def test_exclude_file_that_is_a_directory_is_left_alone(repo):
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()
    exclude.mkdir()
    assert gate_offer.exclude_locally(repo) == "skipped"


def test_a_linked_worktree_still_gets_the_recipe_but_no_exclude_line(
    repo, person, keys, offered, cfg, tmp_path, monkeypatch
):
    _git(
        repo,
        "-c",
        "user.email=t@e",
        "-c",
        "user.name=t",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )
    linked = tmp_path / "linked-wt"
    _git(repo, "worktree", "add", "-q", str(linked))
    (linked / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(linked)
    keys.type("y")
    run_offer(cfg)
    assert trust_state(linked) == "trusted"
    assert ".claude/hr-verify.toml" not in (repo / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    )


# ---- hostile repositories -----------------------------------------------------------------------------------------
def test_a_symlinked_dot_claude_is_refused_before_anything_runs(
    repo, person, keys, cfg, monkeypatch, tmp_path, capsys
):
    outside = tmp_path / "victim"
    outside.mkdir()
    try:
        os.symlink(outside, repo / ".claude", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    marker = repo / "ran.txt"
    prop = proposal_of(chk("m", f"open({str(marker)!r}, 'w').close()"))
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    keys.type("y")
    run_offer(cfg)
    assert list(outside.iterdir()) == [] and not marker.exists()
    assert "symbolic link" in capsys.readouterr().err
    assert trust_state(repo) == "no-config"
    run_offer(cfg)  # a hostile layout does not make every launch ask again
    assert len(keys.prompts) == 1


def test_a_dangling_symlinked_recipe_is_not_written_through(
    repo, person, keys, cfg, monkeypatch, tmp_path
):
    victim = tmp_path / "victim.toml"
    (repo / ".claude").mkdir()
    try:
        os.symlink(victim, repo / ".claude" / "hr-verify.toml")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    prop = proposal_of(chk("x", PASS))
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    result = gate_offer.enable(repo, prop, cfg, lambda s: None)
    assert result.status == "failed" and not victim.exists()


def test_a_dot_claude_that_is_a_file_is_reported_not_crashed_on(
    repo, person, keys, cfg, monkeypatch, capsys
):
    (repo / ".claude").write_text("i am a file", encoding="utf-8")
    prop = proposal_of(chk("x", PASS))
    monkeypatch.setattr(verify, "discover", lambda root: prop)
    keys.type("y")
    run_offer(cfg)
    assert "not a directory" in capsys.readouterr().err
    assert (repo / ".claude").read_text(encoding="utf-8") == "i am a file"
    assert offer_record(repo)["decision"] == "declined"


def test_enable_never_overwrites_an_existing_recipe(repo, person, cfg):
    p = verify.config_path(repo)
    p.parent.mkdir()
    p.write_text("# mine\n", encoding="utf-8")
    result = gate_offer.enable(repo, proposal_of(chk("x", PASS)), cfg, lambda s: None)
    assert result.status == "failed" and "already exists" in result.detail
    assert p.read_text(encoding="utf-8") == "# mine\n"


def test_a_recipe_that_is_a_directory_is_invalid_and_quiet(repo, person, keys, offered, cfg):
    (repo / ".claude" / "hr-verify.toml").mkdir(parents=True)
    nothing_asked(cfg, repo)
    assert trust_state(repo) == "invalid"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_manifest_cannot_hang_the_launcher(repo, person, keys, cfg):
    (repo / "pyproject.toml").unlink()
    os.mkfifo(repo / "package.json")
    done = []

    def go():
        run_offer(cfg)
        done.append(1)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(20)
    assert done, "the launcher hung on a FIFO named package.json"
    assert not gate_offer.offer_path().exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_recipe_cannot_hang_the_launcher(repo, person, keys, offered, cfg):
    (repo / ".claude").mkdir()
    os.mkfifo(repo / ".claude" / "hr-verify.toml")
    done = []
    t = threading.Thread(target=lambda: (run_offer(cfg), done.append(1)), daemon=True)
    t.start()
    t.join(20)
    assert done and not gate_offer.offer_path().exists()


def test_an_enormous_manifest_is_not_read_into_memory_and_proposes_nothing(repo, person, keys, cfg):
    (repo / "pyproject.toml").unlink()
    with open(repo / "package.json", "wb") as fh:
        fh.write(b'{"scripts": {"test": "x"}, "pad": "')
        fh.write(b" " * (3 * 1024 * 1024))
        fh.write(b'"}')
    run_offer(cfg)  # real discover: too large to inspect
    assert not gate_offer.offer_path().exists() and not verify.config_path(repo).exists()


def test_an_enormous_recipe_is_quietly_invalid(repo, person, keys, offered, cfg):
    p = verify.config_path(repo)
    p.parent.mkdir()
    p.write_bytes(b"# " + b"x" * (200 * 1024) + b"\n")
    nothing_asked(cfg, repo)


def test_only_the_recipe_and_the_exclude_line_are_written(repo, person, keys, offered, cfg):
    before_tree, before_git = tree(repo), git_tree(repo)
    keys.type("y")
    run_offer(cfg)
    assert tree(repo) - before_tree == {".claude/hr-verify.toml"}
    assert before_tree <= tree(repo)
    assert git_tree(repo) ^ before_git <= {"info/exclude"}


# ---- no approval without a person ---------------------------------------------------------------------------------
def test_enable_refuses_without_a_person_and_does_nothing(repo, cfg, monkeypatch):
    marker = repo / "ran.txt"
    prop = proposal_of(chk("m", f"open({str(marker)!r}, 'w').close()"))
    monkeypatch.setattr(
        human, "not_a_person", lambda: "this shell belongs to a Claude Code session"
    )
    result = gate_offer.enable(repo, prop, cfg, lambda s: None)
    assert result.status == "refused" and "Claude Code session" in result.detail
    assert (
        not marker.exists()
        and not verify.config_path(repo).exists()
        and trust_state(repo) == "no-config"
    )


def test_the_untrusted_flow_does_not_approve_when_the_person_vanished(repo, keys, cfg, monkeypatch):
    p = verify.config_path(repo)
    p.parent.mkdir()
    p.write_text(verify.render_toml(proposal_of(chk("t", PASS))), encoding="utf-8", newline="\n")
    # the answer was typed, but by the time it would be acted on this is no longer a person's terminal
    monkeypatch.setattr(
        human, "not_a_person", lambda: "this shell belongs to a Claude Code session"
    )
    keys.type("y")
    assert gate_offer.offer_now(repo, cfg, lambda s: None) == "refused"
    assert trust_state(repo) == "untrusted"


def test_there_is_no_switch_that_answers_for_the_person(
    repo, person, keys, offered, cfg, monkeypatch
):
    for var in (
        "CLAUDE_HR_YES",
        "CLAUDE_HR_ASSUME_YES",
        "CLAUDE_HR_GATES__YES",
        "CLAUDE_HR_VERIFY__YES",
    ):
        monkeypatch.setenv(var, "yes")
    keys.type(EOFError)  # the only way to an answer is the person; EOF is "not now"
    run_offer(config.load())
    assert len(keys.prompts) == 1 and trust_state(repo) == "no-config"
    assert not gate_offer.offer_path().exists()


# ---- a recipe that ships with the repository ----------------------------------------------------------------------
def write_recipe(repo, *, fast, full_only=()):
    full = list(fast) + list(full_only)
    rec = verify.Recipe(list(fast), full, "test", [])
    p = verify.config_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(verify.render_toml(rec), encoding="utf-8", newline="\n")
    return p


def test_a_shipped_recipe_shows_every_check_and_defaults_to_no(repo, person, keys, cfg, capsys):
    write_recipe(repo, fast=[chk("lint", PASS)], full_only=[chk("slow-build", PASS)])
    keys.type("")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert "have not approved" in err and "[fast 1] lint" in err and "[full 2] slow-build" in err
    assert "file sha256: " + verify.load_checked(repo).sha256 in err
    assert keys.prompts == ["Run these checks after every turn? [y/N] "]
    assert trust_state(repo) == "untrusted"
    rec = offer_record(repo)
    assert rec["decision"] == "declined" and rec["recipe_sha"] == verify.load_checked(repo).sha256


def test_a_shipped_recipe_that_was_declined_is_not_asked_again_until_it_changes(
    repo, person, keys, cfg
):
    write_recipe(repo, fast=[chk("lint", PASS)])
    keys.type("n")
    run_offer(cfg)
    run_offer(cfg)
    assert len(keys.prompts) == 1
    write_recipe(repo, fast=[chk("lint", PASS), chk("more", PASS)])  # the repository changed it
    keys.type("n")
    run_offer(cfg)
    assert len(keys.prompts) == 2


def test_yes_trusts_exactly_the_bytes_that_were_shown(repo, person, keys, cfg):
    write_recipe(repo, fast=[chk("lint", PASS)])
    shown = verify.load_checked(repo).sha256
    keys.type("y")
    run_offer(cfg)
    assert trust_state(repo) == "trusted"
    trusted = json.loads((state_dir() / "verify-trust.json").read_text(encoding="utf-8"))
    assert trusted[verify._repo_key(repo)] == shown and offer_record(repo)["decision"] == "enabled"


def test_a_recipe_changed_while_the_person_reads_it_is_not_trusted(
    repo, person, cfg, monkeypatch, capsys
):
    p = write_recipe(repo, fast=[chk("lint", PASS)])
    evil = verify.render_toml(proposal_of(chk("evil", "import os")))

    def answer(prompt=""):
        p.write_text(evil, encoding="utf-8", newline="\n")  # swapped after it was displayed
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    run_offer(cfg)
    assert "changed after it was shown" in capsys.readouterr().err
    assert trust_state(repo) == "modified" or trust_state(repo) == "untrusted"
    assert trust_state(repo) != "trusted"


def test_an_edited_generated_recipe_is_asked_about_again_with_default_no(
    repo, person, keys, offered, cfg, capsys
):
    keys.type("y")
    run_offer(cfg)
    p = verify.config_path(repo)
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8", newline="\n")
    assert trust_state(repo) == "modified"
    keys.type("")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert "has changed since you approved it" in err
    assert keys.prompts[-1] == "Run these checks after every turn? [y/N] "
    assert trust_state(repo) == "modified"  # Enter was No


def test_terminal_escapes_in_a_shipped_recipe_are_shown_not_obeyed(repo, person, keys, cfg, capsys):
    esc, rlo = chr(27), chr(0x202E)
    write_recipe(repo, fast=[verify.Check("lint", [PY, "-c", f"pass{esc}[2J{rlo}"], 30, ".")])
    keys.type("n")
    run_offer(cfg)
    err = capsys.readouterr().err
    assert esc not in err and rlo not in err
    assert "\\x1b" in err and "\\u202e" in err


def test_a_shipped_recipe_with_an_audit_finding_shows_it(repo, person, keys, cfg, capsys):
    write_recipe(repo, fast=[chk("lint", PASS)])
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"enableAllProjectMcpServers": True}), encoding="utf-8"
    )
    keys.type("n")
    run_offer(cfg)
    assert "mcp-auto-approve" in capsys.readouterr().err


# ---- disable, and the state file ----------------------------------------------------------------------------------
def test_disable_untrusts_remembers_no_and_keeps_the_file(repo, person, keys, offered, cfg):
    keys.type("y")
    run_offer(cfg)
    assert gate_offer.disable(repo) is True
    assert trust_state(repo) == "untrusted" and verify.config_path(repo).exists()
    rec = offer_record(repo)
    assert rec["decision"] == "declined" and rec["recipe_sha"] == verify.load_checked(repo).sha256
    run_offer(cfg)  # same bytes, declined: silent
    assert len(keys.prompts) == 1
    assert gate_offer.disable(repo) is False  # nothing left to undo


def test_a_second_terminal_that_got_there_first_is_not_recorded_as_a_no(
    repo, person, keys, offered, cfg, monkeypatch
):
    real_enable = gate_offer.enable

    def raced(root, proposal, cfg, say):
        monkeypatch.setattr(gate_offer, "enable", real_enable)
        assert (
            real_enable(root, proposal, cfg, lambda s: None).status == "enabled"
        )  # the other terminal
        return real_enable(root, proposal, cfg, lambda s: None)  # this one now finds the file

    monkeypatch.setattr(gate_offer, "enable", raced)
    keys.type("y")
    run_offer(cfg)
    assert trust_state(repo) == "trusted" and offer_record(repo)["decision"] == "enabled"


def test_offer_now_explicit_ignores_an_earlier_no(repo, person, keys, offered, cfg):
    keys.type("n")
    run_offer(cfg)
    keys.type("y")
    out = []
    assert (
        gate_offer.offer_now(Path(os.path.realpath(repo)), cfg, out.append, explicit=True)
        == "enabled"
    )
    assert offer_record(repo)["decision"] == "enabled" and trust_state(repo) == "trusted"
    assert gate_offer.offer_now(repo, cfg, out.append, explicit=True) == "already-on"


def test_offer_now_explains_when_there_is_nothing_to_enable(repo, person, keys, cfg, monkeypatch):
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of())
    out = []
    assert gate_offer.offer_now(repo, cfg, out.append, explicit=True) == "nothing-to-offer"
    assert "nothing to turn on" in " ".join(out)


def test_a_corrupt_state_file_counts_as_empty(repo):
    f = gate_offer.offer_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{not json", encoding="utf-8")
    assert gate_offer.decision_for(repo) is None
    assert gate_offer.record(repo, "declined") is True
    assert gate_offer.decision_for(repo)["decision"] == "declined"
    f.write_text('["a list"]', encoding="utf-8")
    assert gate_offer.decision_for(repo) is None


def test_the_state_file_keeps_the_last_200_repositories(repo):
    for i in range(210):
        gate_offer.record(repo / f"r{i}", "declined")
    data = json.loads(gate_offer.offer_path().read_text(encoding="utf-8"))
    assert len(data) == 200
    assert (
        gate_offer.decision_for(repo / "r209") is not None
        and gate_offer.decision_for(repo / "r0") is None
    )


def test_an_entry_with_an_unknown_decision_is_ignored(repo):
    gate_offer.offer_path().parent.mkdir(parents=True, exist_ok=True)
    gate_offer.offer_path().write_text(
        json.dumps({gate_offer._repo_key(repo): {"decision": "approved"}}), encoding="utf-8"
    )
    assert gate_offer.decision_for(repo) is None
    with pytest.raises(ValueError):
        gate_offer.record(repo, "approved")


def test_the_state_file_is_replaced_not_written_through_a_link(repo, tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("precious", encoding="utf-8")
    f = gate_offer.offer_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(victim, f)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    assert gate_offer.record(repo, "declined")
    assert victim.read_text(encoding="utf-8") == "precious" and not f.is_symlink()


def test_status_hints_say_declined_or_never_offered(repo, cfg, monkeypatch):
    assert "never offered here" in gate_offer.status_hint(repo, "no-config", cfg)
    monkeypatch.setenv("CLAUDE_HR_VERIFY__OFFER", "off")
    assert "verify.offer is off" in gate_offer.status_hint(repo, "no-config", config.load())
    gate_offer.record(repo, "declined", reason="none of the checks pass today")
    hint = gate_offer.status_hint(repo, "no-config", cfg)
    assert (
        "declined on " in hint
        and "claude-hr-ctl verify enable" in hint
        and "none of the checks pass" in hint
    )
    assert gate_offer.status_hint(repo, "untrusted", cfg) is not None  # the no is on record
    assert gate_offer.status_hint(repo, "trusted", cfg) is None  # a folder that is on is just on
    gate_offer.record(repo, "enabled")
    assert gate_offer.status_hint(repo, "untrusted", cfg) is None


# ---- the fast path must agree with verify -------------------------------------------------------------------------
def test_mirrored_constants_have_not_drifted(tmp_path):
    assert gate_offer._CONFIG_NAME == verify.CONFIG_NAME
    assert gate_offer._MAX_CONFIG_BYTES == verify.MAX_CONFIG_BYTES
    assert gate_offer._TRUST_FILE == verify._trust_file(tmp_path).name
    assert gate_offer.MAX_ENTRIES == verify.MAX_TRUSTED_REPOS
    for p in (tmp_path, tmp_path / "Sub Dir" / "..", Path("relative/x")):
        assert gate_offer._repo_key(p) == verify._repo_key(p)


def test_the_builtin_sha256_gives_hashlibs_digest_and_falls_back(monkeypatch):
    import hashlib

    for data in (b"", b"abc", os.urandom(5000)):
        assert gate_offer._sha256_hex(data) == hashlib.sha256(data).hexdigest()
    monkeypatch.setitem(
        sys.modules, "_sha2", None
    )  # as on an interpreter without the built-in module
    monkeypatch.setitem(sys.modules, "_sha256", None)
    assert gate_offer._sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_quick_verdict_agrees_with_the_real_trust_state(repo):
    root = Path(os.path.realpath(repo))
    # no recipe, markers present -> look; declined -> quiet
    assert gate_offer._quick_verdict(root) == "look"
    gate_offer.record(root, "declined")
    assert gate_offer._quick_verdict(root) == "quiet"
    gate_offer.forget(root)
    # untrusted -> look; trusted -> quiet; edited -> look; invalid / directory / oversize -> quiet
    p = write_recipe(root, fast=[chk("t", PASS)])
    assert (
        verify.trust_state(root, state_dir()) == "untrusted"
        and gate_offer._quick_verdict(root) == "look"
    )
    verify.trust(root, state_dir())
    assert gate_offer._quick_verdict(root) == "quiet"
    p.write_text(p.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8", newline="\n")
    assert (
        verify.trust_state(root, state_dir()) == "modified"
        and gate_offer._quick_verdict(root) == "look"
    )
    p.write_text("not [valid", encoding="utf-8")
    assert verify.trust_state(root, state_dir()) == "invalid"
    # (an invalid recipe parses as nothing: the quick path cannot tell and lets offer_now decide, which stays quiet)
    out = []
    assert gate_offer.offer_now(root, config.load(), out.append) == "unavailable" and out == []
    p.unlink()
    p.mkdir()
    assert gate_offer._quick_verdict(root) == "quiet"
    p.rmdir()
    p.write_bytes(b"#" * (verify.MAX_CONFIG_BYTES + 5))
    assert (
        verify.trust_state(root, state_dir()) == "invalid"
        and gate_offer._quick_verdict(root) == "quiet"
    )


def test_recipe_bytes_matches_verify_read_config(repo):
    root = Path(os.path.realpath(repo))
    assert gate_offer._recipe_bytes(root) == ("missing", None)
    p = verify.config_path(root)
    p.parent.mkdir()
    p.write_bytes(b"abc")
    assert gate_offer._recipe_bytes(root) == ("ok", b"abc") and verify._read_config(root) == b"abc"
    p.unlink()
    p.mkdir()
    assert gate_offer._recipe_bytes(root)[0] == "odd"
    with pytest.raises(verify.VerifyConfigError):
        verify._read_config(root)


@pytest.mark.parametrize(
    "files",
    [
        {"package.json": '{"scripts": {"test": "jest", "lint": "eslint ."}}'},
        {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"},
        {"pytest.ini": "[pytest]\n"},
        {"setup.cfg": "[tool:pytest]\n"},
        {"tests/test_a.py": "def test_a(): pass\n", "requirements.txt": "pytest\n"},
        {"Cargo.toml": "[package]\nname='x'\n"},
        {"go.mod": "module x\n"},
        {"Makefile": "test:\n\techo hi\n"},
        {"makefile": "lint:\n\techo hi\n"},
        {"GNUmakefile": "check:\n\techo hi\n"},
        {"justfile": "test:\n  echo hi\n"},
        {"Justfile": "lint:\n  echo hi\n"},
        {".justfile": "check:\n  echo hi\n"},
    ],
)
def test_every_ecosystem_discovery_understands_passes_the_cheap_filter(tmp_path, files):
    root = tmp_path / "eco"
    root.mkdir()
    for name, body in files.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    assert verify.discover(root).fast, (
        "the fixture should be something discovery proposes checks for"
    )
    assert gate_offer._might_have_checks(root)


def test_a_folder_with_a_trusted_recipe_never_imports_verify_or_audit(repo, tmp_path):
    p = write_recipe(repo, fast=[chk("t", PASS)])
    verify.trust(repo, state_dir())
    root = Path(os.path.realpath(repo))
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from claude_hr import gate_offer\n"
        f"v = gate_offer._quick_verdict(Path({str(root)!r}))\n"
        "print(v, 'claude_hr.verify' in sys.modules, 'claude_hr.audit' in sys.modules)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    r = subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.stdout.split() == ["quiet", "False", "False"], r.stdout + r.stderr
    assert p.exists()


# ---- the real stop gate agrees with the launcher about which repository this is -----------------------------------
def test_the_stop_gate_sees_the_repository_the_offer_trusted(repo, person, keys, cfg, monkeypatch):
    ok = repo / "ok.txt"
    ok.write_text("x", encoding="utf-8")
    code = "import os, sys; sys.exit(0 if os.path.exists('ok.txt') else 1)"
    monkeypatch.setattr(verify, "discover", lambda root: proposal_of(chk("needs-ok", code)))
    sub = repo / "pkg"
    sub.mkdir()
    monkeypatch.chdir(sub)  # started in a subfolder
    keys.type("y")
    run_offer(cfg)
    top = stop_gate._git_root(str(sub))
    assert verify._repo_key(top) == gate_offer._repo_key(repo) and trust_state(top) == "trusted"
    monkeypatch.delenv(verify.ACTIVE_ENV, raising=False)
    assert stop_gate.handle({"cwd": str(sub), "hook_event_name": "Stop"}) is None  # passes
    ok.unlink()
    blocked = stop_gate.handle({"cwd": str(sub), "hook_event_name": "Stop"})
    assert blocked and blocked["decision"] == "block" and "needs-ok" in blocked["reason"]


# ---- never raises -----------------------------------------------------------------------------------------------
def test_maybe_offer_never_raises(repo, person, keys, offered, cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(gate_offer, "offer_now", boom)
    run_offer(cfg)  # does not raise
    monkeypatch.setattr(
        gate_offer, "work_tree", lambda cwd: (_ for _ in ()).throw(OSError("cwd is gone"))
    )
    run_offer(cfg)


def test_maybe_offer_treats_ctrl_c_anywhere_as_not_now(
    repo, person, keys, offered, cfg, monkeypatch
):
    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(gate_offer, "offer_now", interrupted)
    run_offer(cfg)
    assert not gate_offer.offer_path().exists()


def test_ctrl_c_while_the_checks_run_enables_nothing(repo, person, keys, cfg, monkeypatch):
    prop = proposal_of(chk("a", PASS))
    monkeypatch.setattr(verify, "discover", lambda root: prop)

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(verify, "run", interrupted)
    keys.type("y")
    run_offer(cfg)
    assert not verify.config_path(repo).exists() and trust_state(repo) == "no-config"
    assert not gate_offer.offer_path().exists()


def test_output_survives_a_console_that_cannot_encode(monkeypatch):
    class Ascii:
        def __init__(self):
            self.text = ""

        def write(self, s):
            s.encode("ascii")
            self.text += s

        def flush(self):
            pass

    stream = Ascii()
    monkeypatch.setattr(sys, "stderr", stream)
    gate_offer.stderr_say("café ✓")
    assert "caf" in stream.text
