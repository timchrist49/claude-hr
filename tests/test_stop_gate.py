"""The end-of-turn dispatcher (`claude-hr-hook stop`), tested against stand-ins for fmt.py / verify.py."""

import io
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import claude_hr
from claude_hr import auditlog, hooks, paths, stop_gate


class Fakes:
    """Counts calls and returns scripted results; `tree` plays the working tree (changing it changes the fingerprint)."""

    def __init__(self):
        self.tree = "A"
        self.trust = "trusted"
        self.fmt_calls = []
        self.gate_calls = []
        self.fingerprint_calls = []
        self.formatted = []
        self.gate_result = NS(
            action="pass", reason="", results=[NS(name="ruff"), NS(name="pytest")]
        )
        self.fmt_raises = None
        self.cannot_tell = (
            False  # the fingerprint is "" (git failed, the status listing was cut off, no time)
        )

    def fingerprint(self, root, **kw):
        self.fingerprint_calls.append(kw)
        return "" if self.cannot_tell else f"fp-{self.tree}"

    def trust_state(self, root, state_dir):
        return self.trust

    def format_changed(self, root, **kw):
        self.fmt_calls.append(kw)
        if self.fmt_raises:
            raise self.fmt_raises
        if self.formatted:
            self.tree += "F"  # formatting rewrites files: the working tree changes
        return NS(
            formatted=list(self.formatted),
            skipped=[],
            errors=[],
            formatters=["ruff"],
            elapsed_s=0.01,
        )

    def gate(self, root, state_dir, **kw):
        self.gate_calls.append(kw)
        return self.gate_result


@pytest.fixture
def fakes(sandbox, monkeypatch):
    f = Fakes()
    verify = types.ModuleType("claude_hr.verify")
    verify.fingerprint, verify.gate = f.fingerprint, f.gate
    verify.trust_state = f.trust_state
    fmt = types.ModuleType("claude_hr.fmt")
    fmt.format_changed = f.format_changed
    for name, mod in (("verify", verify), ("fmt", fmt)):
        monkeypatch.setitem(sys.modules, f"claude_hr.{name}", mod)
        monkeypatch.setattr(claude_hr, name, mod, raising=False)
    monkeypatch.delenv("CLAUDE_HR_GATES", raising=False)
    return f


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "my repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    return r


def payload(repo, **extra):
    return {"cwd": str(repo), "hook_event_name": "Stop", **extra}


def test_a_passing_turn_prints_nothing_and_leaves_an_audit_record(fakes, repo):
    fakes.formatted = ["a.py"]
    assert stop_gate.handle(payload(repo)) is None
    assert len(fakes.fmt_calls) == 1 and len(fakes.gate_calls) == 1
    rec = auditlog.tail(1)[0]
    assert rec["event"] == "gate" and rec["formatted"] == 1 and rec["verify"] == "pass"
    assert rec["checks"] == ["ruff", "pytest"] and rec["formatters"] == ["ruff"]
    assert "my repo" in rec["repo"] and str(repo) not in json.dumps(
        rec
    )  # a hash-based id, never the path


def test_an_unchanged_tree_is_not_checked_twice(fakes, repo):
    assert stop_gate.handle(payload(repo)) is None
    assert stop_gate.handle(payload(repo)) is None
    assert (
        len(fakes.fmt_calls) == 1 and len(fakes.gate_calls) == 1
    )  # the idle second Stop did no work
    fakes.tree = "B"  # Claude changed something
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2


def test_formatting_is_remembered_so_the_next_idle_stop_is_a_no_op(fakes, repo):
    fakes.formatted = ["a.py"]
    stop_gate.handle(payload(repo))
    fakes.formatted = []
    stop_gate.handle(payload(repo))  # same (post-format) tree
    assert len(fakes.fmt_calls) == 1


def test_a_failing_trusted_check_blocks_with_its_reason(fakes, repo):
    fakes.gate_result = NS(
        action="block",
        reason="claude-hr verify: pytest failed (exit 1)\nE assert 0",
        results=[NS(name="pytest")],
    )
    out = stop_gate.handle(payload(repo))
    assert out == {
        "decision": "block",
        "reason": "claude-hr verify: pytest failed (exit 1)\nE assert 0",
    }
    assert auditlog.tail(1)[0]["verify"] == "block"


def test_a_blocked_tree_is_looked_at_again_even_if_nothing_changed(fakes, repo):
    """If Claude stops again without touching the files, the failing checks must not be waved through."""
    fakes.gate_result = NS(action="block", reason="failed", results=[])
    assert stop_gate.handle(payload(repo)) is not None
    assert stop_gate.handle(payload(repo)) is not None
    assert len(fakes.gate_calls) == 2
    fakes.gate_result = NS(
        action="skip",
        reason="block cap reached (2/2) for this change; last failure: claude-hr verify: pytest failed (exit 1)",
        results=[],
    )  # verify.gate's own cap: Claude may stop, but the USER is told the failure is still there
    out = stop_gate.handle(payload(repo))
    assert "decision" not in out and "block cap reached (2/2)" in out["systemMessage"]
    assert "pytest failed (exit 1)" in out["systemMessage"]
    assert "block cap reached" in auditlog.tail(1)[0]["reason"]


def test_skip_and_error_outcomes_leave_a_one_line_reason_in_the_trail(fakes, repo):
    fakes.gate_result = NS(
        action="skip", reason="no .claude/hr-verify.toml\nsecond line is dropped", results=[]
    )
    assert stop_gate.handle(payload(repo)) is None  # nothing to tell Claude or the user: no recipe
    assert auditlog.tail(1)[0]["reason"] == "no .claude/hr-verify.toml"
    fakes.tree = "B"
    fakes.gate_result = NS(
        action="error", reason="claude-hr verify: cannot run pytest: " + "x" * 500, results=[]
    )
    assert stop_gate.handle(payload(repo)) is None
    row = auditlog.tail(1)[0]
    assert row["verify"] == "error" and row["reason"].startswith(
        "claude-hr verify: cannot run pytest"
    )
    assert len(row["reason"]) <= 110
    fakes.tree = "C"
    fakes.gate_result = NS(action="pass", reason="1 check(s) passed", results=[NS(name="pytest")])
    stop_gate.handle(payload(repo))
    assert "reason" not in auditlog.tail(1)[0]  # a pass needs no explanation


def test_trusting_the_recipe_reruns_the_gate_even_if_no_file_changed(fakes, repo):
    fakes.trust = "untrusted"
    fakes.gate_result = NS(action="skip", reason="not trusted", results=[])
    assert stop_gate.handle(payload(repo)) is None
    assert stop_gate.handle(payload(repo)) is None  # still untrusted, same tree: idle
    assert len(fakes.gate_calls) == 1
    fakes.trust = "trusted"  # the user ran `verify trust`; nothing else changed
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2


def test_an_error_is_not_remembered_so_it_is_retried(fakes, repo):
    fakes.gate_result = NS(action="error", reason="tool missing", results=[])
    stop_gate.handle(payload(repo))
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2


def test_the_formatter_step_gets_the_repo_trust_decision(fakes, repo, monkeypatch):
    """Repo-local formatters and prettier configs that run code are used only for a trusted repository (fmt.py)."""
    fakes.trust = "untrusted"
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[-1]["trusted"] is False
    fakes.tree = "B"
    fakes.trust = "trusted"
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[-1]["trusted"] is True
    fakes.tree = "C"
    fakes.trust = "modified"  # edited since it was trusted: not trusted
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[-1]["trusted"] is False
    fakes.tree = "D"
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_FORMAT__REPO_TOOLS", "always")
        stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[-1]["trusted"] is True


def test_stop_hook_active_still_evaluates_because_the_gate_has_its_own_cap(fakes, repo):
    stop_gate.handle(payload(repo, stop_hook_active=True))
    assert len(fakes.gate_calls) == 1


@pytest.mark.parametrize(
    "extra",
    [{"agent_id": "sub-1"}, {"permission_mode": "plan"}],
)
def test_subagents_and_plan_mode_are_skipped(fakes, repo, extra):
    assert stop_gate.handle(payload(repo, **extra)) is None
    assert fakes.fmt_calls == [] and fakes.gate_calls == [] and auditlog.tail(1) == []


def test_off_switches(fakes, repo, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_GATES", "off")
        assert stop_gate.handle(payload(repo)) is None
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_GATES__ENABLED", "false")
        assert stop_gate.handle(payload(repo)) is None
    assert fakes.fmt_calls == [] and fakes.gate_calls == []


def test_not_a_git_repository_is_skipped(fakes, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert stop_gate.handle(payload(plain)) is None
    assert fakes.fmt_calls == [] and fakes.gate_calls == []


def test_individual_steps_can_be_turned_off(fakes, repo, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_FORMAT__ENABLED", "false")
        stop_gate.handle(payload(repo))
    assert fakes.fmt_calls == [] and len(fakes.gate_calls) == 1
    fakes.tree = "B"
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_VERIFY__GATE", "false")
        m.setenv("CLAUDE_HR_GATES__TRAIL", "false")
        stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 1 and len(fakes.fmt_calls) == 1
    assert [
        r for r in auditlog.tail(5) if r.get("verify") == "off"
    ] == []  # trail off: nothing recorded for this turn


def test_config_values_reach_the_modules(fakes, repo, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_FORMAT__MAX_FILES", "7")
        m.setenv("CLAUDE_HR_FORMAT__CLEAN_GATE", "false")
        m.setenv("CLAUDE_HR_VERIFY__GATE_MAX_BLOCKS", "5")
        m.setenv("CLAUDE_HR_VERIFY__TIER", "full")
        stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[0]["max_files"] == 7 and fakes.fmt_calls[0]["clean_gate"] is False
    assert fakes.gate_calls[0]["max_blocks"] == 5 and fakes.gate_calls[0]["tier"] == "full"
    assert fakes.gate_calls[0]["test_guard"] is True


def test_run_prints_json_only_when_blocking(fakes, repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(repo))))
    assert stop_gate.run() == 0
    assert capsys.readouterr().out == ""
    fakes.tree = "B"
    fakes.gate_result = NS(action="block", reason="nope", results=[])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(repo))))
    assert stop_gate.run() == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "block", "reason": "nope"}


def test_any_failure_fails_open_and_leaves_a_trace(fakes, repo, monkeypatch, capsys):
    fakes.fmt_raises = RuntimeError("formatter exploded")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(repo))))
    assert stop_gate.run() == 0
    assert capsys.readouterr().out == ""
    assert "formatter exploded" in (paths.logs_dir() / "crash.log").read_text(encoding="utf-8")
    for garbage in ("not json", "[]", ""):
        monkeypatch.setattr("sys.stdin", io.StringIO(garbage))
        assert stop_gate.run() == 0


def test_the_hook_entry_point_dispatches_stop(fakes, repo, monkeypatch, capsys):
    fakes.gate_result = NS(action="block", reason="x", results=[])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(repo))))
    assert hooks.main(["stop"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_state_remembers_at_most_fifty_repositories(sandbox):
    state = stop_gate._State({})
    for i in range(60):
        state.remember(f"repo-{i}", f"fp-{i}")
    assert len(state.data) == stop_gate.STATE_KEEP
    assert state.seen("repo-0") is None and state.seen("repo-59") == "fp-59"
    state.remember("repo-10", "again")  # re-seen: moves to the newest end
    assert list(state.data)[-1] == "repo-10"


def test_the_hook_budget_is_below_the_plugins_hook_timeout():
    hooks_json = Path(__file__).resolve().parents[1] / "plugins/hr-gates/hooks/hooks.json"
    timeout = json.loads(hooks_json.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0][
        "timeout"
    ]
    assert stop_gate.HOOK_BUDGET_S == 150
    assert stop_gate.HOOK_BUDGET_S + 10 <= timeout, "a killed hook silently drops its block"


def test_every_step_is_handed_what_is_left_of_one_budget(fakes, repo, monkeypatch):
    monkeypatch.setattr(stop_gate, "HOOK_BUDGET_S", 40)
    t0 = time.monotonic()
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[0]["timeout_s"] <= 20.0, "the formatter may use half of what is left"
    deadline = fakes.gate_calls[0]["deadline"]
    assert t0 + 39 < deadline < t0 + 41
    assert len(fakes.fingerprint_calls) == 2
    assert all(call.get("deadline") == deadline for call in fakes.fingerprint_calls)


def test_configured_timeouts_are_limits_inside_the_budget_not_additions(fakes, repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_FORMAT__TIMEOUT_S", "600")
    monkeypatch.setenv("CLAUDE_HR_VERIFY__GATE_TIMEOUT_S", "900")
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls[0]["timeout_s"] <= stop_gate.HOOK_BUDGET_S / 2
    # verify.gate clamps its own limit against the deadline it is given (tested with the real module)
    assert fakes.gate_calls[0]["timeout_s"] == 900 and fakes.gate_calls[0]["deadline"]


def test_the_formatter_is_skipped_when_almost_nothing_is_left(fakes, repo, monkeypatch):
    monkeypatch.setattr(stop_gate, "HOOK_BUDGET_S", 1.5)
    stop_gate.handle(payload(repo))
    assert fakes.fmt_calls == [] and len(fakes.gate_calls) == 1
    monkeypatch.setattr(stop_gate, "HOOK_BUDGET_S", 0)  # already spent: not even git is asked
    fakes.tree = "B"
    assert stop_gate.handle(payload(repo)) is None and len(fakes.gate_calls) == 1


def test_a_budget_skip_that_started_no_check_is_retried_a_cut_short_run_is_remembered(fakes, repo):
    fakes.gate_result = NS(
        action="skip", reason="time budget: 3s left", results=[], note="time budget"
    )
    stop_gate.handle(payload(repo))
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2, "the tree was not looked at: try again next time"
    row = auditlog.tail(1)[0]
    assert row["verify"] == "skip" and row["note"] == "time budget"

    fakes.tree = "B"
    fakes.gate_result = NS(
        action="skip", reason="inconclusive", results=[NS(name="slow")], note="time budget"
    )
    stop_gate.handle(payload(repo))
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 3, (
        "a run that started and was cut short is not started again and again"
    )
    assert auditlog.tail(1)[0]["note"] == "time budget"


def test_an_ordinary_turn_leaves_no_note_in_the_trail(fakes, repo):
    stop_gate.handle(payload(repo))
    assert "note" not in auditlog.tail(1)[0]


def test_a_run_cut_short_by_the_time_budget_tells_the_user_once(fakes, repo):
    """Not a block, and nothing for the model: a one-line systemMessage so the user knows the gate has no verdict."""
    fakes.gate_result = NS(
        action="skip",
        reason="time budget (3s) ran out inside slow; inconclusive, not blocking",
        results=[NS(name="fast"), NS(name="slow")],
        note="time budget",
        check="slow",
    )
    first = stop_gate.handle(payload(repo))
    assert first == {
        "systemMessage": "claude-hr: slow did not finish inside the time budget; not blocking"
    }
    assert stop_gate.handle(payload(repo)) is None, "remembered: an idle Stop says nothing"
    assert len(fakes.gate_calls) == 1
    fakes.tree = "B"  # a changed tree gets a fresh run and a fresh message
    assert "slow did not finish" in stop_gate.handle(payload(repo))["systemMessage"]
    # without the check's name the last result is it; the name is one clean line
    fakes.tree = "C"
    fakes.gate_result = NS(
        action="skip",
        reason="x",
        results=[NS(name="a"), NS(name="lint\x1b[2K\nsecond line " + "x" * 200)],
        note="time budget",
    )
    message = stop_gate.handle(payload(repo))["systemMessage"]
    assert "\x1b" not in message and "\n" not in message and len(message) < 200
    assert message.startswith("claude-hr: lint") and message.endswith("time budget; not blocking")


def test_no_message_when_the_budget_left_no_time_to_start_anything(fakes, repo):
    fakes.gate_result = NS(
        action="skip", reason="time budget: 3s left", results=[], note="time budget"
    )
    assert stop_gate.handle(payload(repo)) is None


def test_a_check_that_cannot_start_is_remembered_so_the_first_checks_do_not_rerun(fakes, repo):
    """`tests` passed, `types` has no tool on PATH: every idle Stop used to run `tests` again, silently."""
    fakes.gate_result = NS(
        action="error",
        reason="claude-hr verify: cannot run types: not found: nope (searched PATH)",
        results=[NS(name="tests"), NS(name="types")],
        note="cannot start",
    )
    assert stop_gate.handle(payload(repo)) is None
    assert stop_gate.handle(payload(repo)) is None
    assert len(fakes.gate_calls) == 1
    row = auditlog.tail(1)[0]
    assert row["verify"] == "error" and row["note"] == "cannot start"
    assert row["checks"] == ["tests", "types"] and row["reason"].startswith(
        "claude-hr verify: cannot run types"
    )
    fakes.tree = "B"  # any change to the tree tries again (the tool may have been installed)
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2


def test_a_second_gate_run_in_progress_is_retried_not_remembered(fakes, repo):
    fakes.gate_result = NS(
        action="skip",
        reason="another gate run is in progress for this repository",
        results=[],
        note="another gate run in progress",
    )
    assert stop_gate.handle(payload(repo)) is None
    assert stop_gate.handle(payload(repo)) is None
    assert len(fakes.gate_calls) == 2, "the tree was not looked at"
    assert auditlog.tail(1)[0]["note"] == "another gate run in progress"


def test_a_tree_that_cannot_be_fingerprinted_is_never_taken_for_an_idle_one(fakes, repo):
    """git failed, the status listing was cut off at its cap, or time ran out: 'cannot tell' was remembered as a
    signature, so after the first Stop the formatter and the gate silently never ran again."""
    fakes.cannot_tell = True
    for _ in range(3):
        assert stop_gate.handle(payload(repo)) is None
    assert len(fakes.fmt_calls) == 3 and len(fakes.gate_calls) == 3
    assert stop_gate._State.load().data == {}, "nothing was remembered"
    fakes.cannot_tell = False  # git answers again: the tree is looked at, remembered, and then idle
    stop_gate.handle(payload(repo))
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 4
    assert stop_gate._signature(sys.modules["claude_hr.verify"], repo) != ""
    fakes.cannot_tell = True
    assert stop_gate._signature(sys.modules["claude_hr.verify"], repo) == ""


def test_the_env_allow_setting_reaches_the_gate(fakes, repo, monkeypatch):
    stop_gate.handle(payload(repo))
    assert fakes.gate_calls[0]["env_allow"] == []
    fakes.tree = "B"
    with monkeypatch.context() as m:
        m.setenv("CLAUDE_HR_VERIFY__ENV_ALLOW", '["DATABASE_URL", "AWS_PROFILE"]')
        stop_gate.handle(payload(repo))
    assert fakes.gate_calls[1]["env_allow"] == ["DATABASE_URL", "AWS_PROFILE"]


def test_two_sessions_saving_together_do_not_overwrite_each_other(sandbox):
    a, b = stop_gate._State.load(), stop_gate._State.load()  # both read the same (empty) file
    a.remember("repo-a", "fp-a")
    b.remember("repo-b", "fp-b")
    a.save()
    b.save()  # b's copy knows nothing of repo-a: its save must merge, not replace
    merged = stop_gate._State.load()
    assert merged.seen("repo-a") == "fp-a" and merged.seen("repo-b") == "fp-b"
    assert not (paths.state_dir() / "gates.lock").exists(), "the lock is released"
    idle = stop_gate._State.load()
    idle.save()  # nothing remembered: nothing written
    assert stop_gate._State.load().data == merged.data


def test_saving_state_keeps_at_most_fifty_repositories_on_disk(sandbox):
    for batch in range(3):
        state = stop_gate._State.load()
        for i in range(30):
            state.remember(f"repo-{batch}-{i}", "fp")
        state.save()
    on_disk = stop_gate._State.load().data
    assert (
        len(on_disk) == stop_gate.STATE_KEEP
        and "repo-2-29" in on_disk
        and "repo-0-0" not in on_disk
    )


def test_the_stop_path_does_not_import_the_control_cli_or_heavy_modules(sandbox):
    """The hook must stay fast: no ctl, no updater, no headroom."""
    from pathlib import Path

    code = (
        "import sys, io, json\n"
        "sys.stdin = io.StringIO('{}')\n"
        "from claude_hr import hooks\n"
        "hooks.main(['stop'])\n"
        "bad = [m for m in ('claude_hr.ctl', 'claude_hr.updater', 'claude_hr.headroom', 'claude_hr.claude_setup') if m in sys.modules]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = {
        **__import__("os").environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0


# ---- the trust record must come from a folder the repository cannot have written ----------------------------------


@pytest.mark.parametrize("rel", ["", ".hr", "sub/.claude-hr", "a/b/c/hr"])
def test_a_state_directory_inside_the_repository_means_the_gate_does_nothing(
    fakes, repo, monkeypatch, rel
):
    """CLAUDE_HR_HOME can come from the repository's own .claude/settings.json (Claude Code hands the hook the environment it
    built from it): the repository could ship a trust record for its own recipe and have it run without being asked."""
    monkeypatch.setenv("CLAUDE_HR_HOME", str(repo / rel) if rel else str(repo))
    assert stop_gate.handle(payload(repo)) is None
    assert fakes.fingerprint_calls == [] and fakes.fmt_calls == [] and fakes.gate_calls == []
    assert not (paths.state_dir() / "gates.json").exists(), "it did not even remember the tree"
    assert not (paths.hr_home() / "audit").exists(), (
        "and it wrote nothing into the folder the repository chose"
    )


def test_the_working_folder_counts_as_well_as_the_repository_root(
    fakes, repo, tmp_path, monkeypatch
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.setattr(stop_gate, "_git_root", lambda cwd, timeout=0: elsewhere)
    monkeypatch.setenv("CLAUDE_HR_HOME", str(sub / ".hr"))
    assert stop_gate.handle(payload(sub)) is None and fakes.gate_calls == []


def test_a_state_directory_outside_the_repository_is_still_honoured(
    fakes, repo, tmp_path, monkeypatch
):
    assert not paths.state_dir().is_relative_to(repo)  # the sandbox keeps it beside the repository
    assert stop_gate.handle(payload(repo)) is None
    assert len(fakes.gate_calls) == 1
    # a folder whose name merely STARTS with the repository's is not inside it
    twin = tmp_path / (repo.name + "-home")
    monkeypatch.setenv("CLAUDE_HR_HOME", str(twin / ".claude-hr"))
    fakes.tree = "B"
    stop_gate.handle(payload(repo))
    assert len(fakes.gate_calls) == 2


def test_when_it_cannot_tell_where_the_state_directory_is_it_does_nothing(fakes, repo, monkeypatch):
    def boom(path):
        raise OSError("cannot resolve")

    monkeypatch.setattr(stop_gate, "_fold", boom)
    assert stop_gate.handle(payload(repo)) is None and fakes.gate_calls == []


def test_a_link_into_the_repository_is_followed(fakes, repo, tmp_path, monkeypatch):
    target = repo / ".hr"
    target.mkdir()
    link = tmp_path / "hr-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # no symlink privilege on Windows: a directory junction needs none, and resolves the same way
        made = paths.IS_WINDOWS and (
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
            ).returncode
            == 0
        )
        if not made:
            pytest.skip("neither symlinks nor junctions are available")
    monkeypatch.setenv("CLAUDE_HR_HOME", str(link))
    assert stop_gate.handle(payload(repo)) is None and fakes.gate_calls == []


@pytest.mark.skipif(not paths.IS_WINDOWS, reason="Windows paths are not case sensitive")
def test_the_comparison_ignores_case_on_windows(fakes, repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HOME", str(repo).upper() + "/.hr")
    assert stop_gate.handle(payload(repo)) is None and fakes.gate_calls == []


RECIPE = """
[[fast]]
name = "marker"
argv = [{py}, "-c", "open('ran.txt', 'w').write('x')"]
timeout_s = 30
"""


@pytest.fixture
def real_repo(tmp_path, monkeypatch):
    """A real repository with a recipe whose one check leaves ran.txt behind, and the real verify / stop_gate code."""
    from claude_hr import verify

    r = tmp_path / "work" / "real repo"
    r.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True, timeout=60)

    git("init", "-q")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    verify.config_path(r).parent.mkdir(parents=True, exist_ok=True)
    verify.config_path(r).write_text(
        RECIPE.format(py=json.dumps(sys.executable)), encoding="utf-8", newline="\n"
    )
    git("add", "-A")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "init",
    )
    monkeypatch.delenv(verify.ACTIVE_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_HR_GATES", raising=False)
    monkeypatch.setenv("CLAUDE_HR_FORMAT__ENABLED", "false")
    return r


def test_a_trust_record_the_repository_shipped_does_not_run_its_recipe(real_repo, monkeypatch):
    """The reproduction of the review finding: .claude/settings.json {"env": {"CLAUDE_HR_HOME": "<repo>/.hr"}} plus a
    .hr/state/verify-trust.json for the recipe next to it ran the recipe at the end of the first turn, with no question."""
    from claude_hr import verify

    monkeypatch.setenv("CLAUDE_HR_HOME", str(real_repo / ".hr"))
    verify.trust(
        real_repo, paths.state_dir()
    )  # what the repository would ship, written by the same code
    assert (real_repo / ".hr" / "state" / "verify-trust.json").is_file()
    assert verify.trust_state(real_repo, paths.state_dir()) == "trusted", (
        "a valid record, so only the location rejects it"
    )
    assert stop_gate.handle(payload(real_repo)) is None
    assert not (real_repo / "ran.txt").exists(), "the repository's recipe must not have run"


def test_a_trust_record_in_the_users_own_state_directory_runs_the_recipe(real_repo):
    from claude_hr import verify

    assert not paths.state_dir().is_relative_to(real_repo)
    verify.trust(real_repo, paths.state_dir())
    assert stop_gate.handle(payload(real_repo)) is None  # a pass prints nothing
    assert (real_repo / "ran.txt").is_file(), "the trusted recipe ran"
    assert auditlog.tail(1)[0]["verify"] == "pass"
