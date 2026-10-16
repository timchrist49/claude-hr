"""The end-of-turn dispatcher (`claude-hr-hook stop`), tested against stand-ins for fmt.py / verify.py."""

import io
import json
import subprocess
import sys
import types
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
        self.formatted = []
        self.gate_result = NS(
            action="pass", reason="", results=[NS(name="ruff"), NS(name="pytest")]
        )
        self.fmt_raises = None

    def fingerprint(self, root):
        return f"fp-{self.tree}"

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
        action="skip", reason="block cap reached", results=[]
    )  # verify.gate's own cap
    assert stop_gate.handle(payload(repo)) is None


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
