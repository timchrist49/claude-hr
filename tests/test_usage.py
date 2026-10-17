"""The token ledger and the tool-use counts read every transcript: sessions, subagents and workflow agents."""

import builtins
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_hr import config, ctl, paths, plugins_audit, usage


def ts(ago_s: float = 0.0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=ago_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def msg(mid, req, model="claude-sonnet-5-5", when=None, **usage_fields):
    """One logged line of a model message (every line of a streamed message has a uuid of its own)."""
    return {
        "timestamp": when or ts(),
        "uuid": f"u-{mid}-{usage_fields.get('output_tokens')}-{time.monotonic_ns()}",
        "requestId": req,
        "type": "assistant",
        "message": {"id": mid, "model": model, "usage": usage_fields},
    }


def tool_use(bid, name, inp):
    return {
        "timestamp": ts(),
        "type": "assistant",
        "message": {
            "id": f"m-{bid}",
            "model": "claude-sonnet-5-5",
            "content": [{"type": "tool_use", "id": bid, "name": name, "input": inp}],
            "usage": {"output_tokens": 1},
        },
    }


def write(path: Path, records, raw=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, separators=(",", ":")) for r in records] + list(raw)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def projects(sandbox):
    return paths.claude_home() / "projects"


def fleet(projects: Path) -> None:
    """Every place Claude Code puts a transcript, with one message each (output 100, 200, 300, 400) and a journal."""
    write(projects / "proj-a" / "s1.jsonl", [msg("m1", "r1", input_tokens=10, output_tokens=100)])
    sub = projects / "proj-a" / "s1" / "subagents"
    write(sub / "agent-a1.jsonl", [msg("m2", "r2", input_tokens=20, output_tokens=200)])
    wf = sub / "workflows" / "wf_1"
    write(wf / "agent-b1.jsonl", [msg("m3", "r3", input_tokens=30, output_tokens=300)])
    write(
        wf / "journal.jsonl", [msg("m9", "r9", output_tokens=999)]
    )  # orchestration, not a transcript
    write(
        projects / "proj-b" / "subagents" / "agent-c1.jsonl", [msg("m4", "r4", output_tokens=400)]
    )
    (projects / "proj-a" / "memory").mkdir()
    (projects / "proj-a" / "memory" / "notes.md").write_text('"usage"', encoding="utf-8")
    (projects / "proj-a" / "s1" / "tool-results").mkdir()
    (projects / "proj-a" / "s1" / "tool-results" / "r.jsonl").write_text(
        json.dumps(msg("m8", "r8", output_tokens=888)), encoding="utf-8"
    )  # a tool result that happens to be named like a transcript is not one


# ---- which files are read ---------------------------------------------------------------------------------------


def test_the_ledger_reads_sessions_subagents_and_workflow_agents(projects):
    fleet(projects)
    led = usage.collect(7)
    assert (led.files_main, led.files_sub, led.files) == (1, 3, 4)
    assert led.total.messages == 4 and led.total.output == 1000
    assert (led.main.messages, led.main.output) == (1, 100)
    assert (led.sub.messages, led.sub.output) == (3, 900)
    assert led.by_project["proj-a"].output == 600  # session, subagent and workflow agent
    assert led.by_project["proj-b"].output == 400  # subagents/ directly under the project
    assert sum(b.output for b in led.by_day.values()) == 1000


def test_no_subagents_gives_the_main_sessions_numbers(projects):
    fleet(projects)
    led = usage.collect(7, subagents=False)
    assert (led.files_main, led.files_sub) == (1, 0)
    assert (led.total.messages, led.total.output) == (1, 100)
    text = usage.format_ledger(led)
    assert "subagents and workflows left out" in text
    assert "where it ran" not in text and "note:" not in text


def test_transcripts_listing_skips_what_is_not_a_transcript(projects):
    fleet(projects)
    got = usage.list_transcripts(projects, 0.0)
    assert sorted((t.project, t.kind, t.path.name) for t in got) == [
        ("proj-a", "main", "s1.jsonl"),
        ("proj-a", "subagent", "agent-a1.jsonl"),
        ("proj-a", "subagent", "agent-b1.jsonl"),
        ("proj-b", "subagent", "agent-c1.jsonl"),
    ]
    assert [t.path.name for t in usage.list_transcripts(projects, 0.0, subagents=False)] == [
        "s1.jsonl"
    ]


def test_has_transcripts_counts_a_lone_subagent_file_of_any_age(projects):
    assert not usage.has_transcripts()
    f = write(
        projects / "p" / "s" / "subagents" / "agent-x.jsonl", [msg("m", "r", output_tokens=1)]
    )
    age(f, 400 * 86400)
    assert usage.has_transcripts()


def test_a_symlinked_folder_is_not_followed(projects, tmp_path):
    outside = tmp_path / "elsewhere"
    write(outside / "agent-z.jsonl", [msg("mz", "rz", output_tokens=5)])
    (projects / "p").mkdir(parents=True)
    try:
        os.symlink(outside, projects / "p" / "subagents", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symlinks")
    assert usage.list_transcripts(projects, 0.0) == []


# ---- what is counted --------------------------------------------------------------------------------------------


def test_a_streamed_message_counts_once_with_its_final_usage(projects):
    """Claude Code logs a message once per content block; the first lines carry a partial output count."""
    write(
        projects / "p" / "s.jsonl",
        [
            msg("m1", "r1", input_tokens=7, output_tokens=5, cache_read_input_tokens=900),
            msg("m1", "r1", input_tokens=7, output_tokens=177, cache_read_input_tokens=900),
        ],
    )
    sub = projects / "p" / "s" / "subagents" / "agent-1.jsonl"
    write(
        sub,
        [
            msg("a1", "ra1", output_tokens=8),
            msg("a1", "ra1", output_tokens=8),
            msg("a1", "ra1", output_tokens=128),
            msg("b1", "rb1", output_tokens=40),
            msg(
                "a1", "ra1", output_tokens=130
            ),  # the same message again, further down (a resumed log)
        ],
    )
    led = usage.collect(7)
    assert led.total.messages == 3
    assert led.main.output == 177
    assert led.sub.output == 130 + 40
    assert led.total.cache_read == 900


def test_a_message_copied_into_another_file_counts_once_and_stays_with_the_session(projects):
    """A forked subagent starts with a copy of its parent's history."""
    main = write(projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=100)])
    age(main, 3600)  # the agent file is the newer one, but the session is read first
    write(
        projects / "p" / "s" / "subagents" / "agent-f.jsonl",
        [msg("m1", "r1", output_tokens=100), msg("m2", "r2", output_tokens=50)],
    )
    led = usage.collect(7)
    assert led.total.messages == 2 and led.total.output == 150
    assert (led.main.messages, led.sub.messages) == (1, 1)


def test_the_same_message_in_two_projects_counts_once(projects):
    """The old key was (message id, project), so a worktree copy of a session counted twice."""
    write(projects / "p1" / "s.jsonl", [msg("m1", "r1", output_tokens=10)])
    write(projects / "p2" / "s.jsonl", [msg("m1", "r1", output_tokens=10)])
    assert usage.collect(7).total.messages == 1


def test_records_outside_the_window_and_malformed_lines_are_skipped(projects):
    f = write(
        projects / "p" / "s.jsonl",
        [
            msg(
                "old", "r0", when="2020-01-01T00:00:00.000Z", output_tokens=7
            ),  # the file is new, this line is not
            msg("ok", "r1", output_tokens=11),
            msg("syn", "r2", model="<synthetic>", output_tokens=13),
            {
                "timestamp": "not a time",
                "message": {"id": "bad-ts", "usage": {"output_tokens": 17}},
            },
            {"timestamp": 12345, "message": {"id": "num-ts", "usage": {"output_tokens": 19}}},
            {"timestamp": ts(), "message": {"id": "null-usage", "usage": None}},
            {"timestamp": ts(), "message": "a string, not an object", "x": "usage"},
            ["a", "list", "usage"],
        ],
        raw=['{"truncated": "a line with \\"usage\\" that never ends', 'not json at all "usage"'],
    )
    f.write_bytes(f.read_bytes() + b'\xff\xfe a line that is not UTF-8 "usage"\n')
    led = usage.collect(7)
    assert led.total.messages == 1 and led.total.output == 11


def test_a_file_older_than_the_window_is_never_opened(projects, monkeypatch):
    old = write(projects / "p" / "old.jsonl", [msg("o", "ro", output_tokens=1)])
    old_sub = write(
        projects / "p" / "old" / "subagents" / "agent-o.jsonl", [msg("o2", "ro2", output_tokens=1)]
    )
    write(projects / "p" / "new.jsonl", [msg("n", "rn", output_tokens=2)])
    for f in (old, old_sub):
        age(f, 40 * 86400)
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            opened.append(Path(file).name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    led = usage.collect(7)
    tools = usage.collect_tool_use(7)
    monkeypatch.setattr(builtins, "open", real_open)
    assert "new.jsonl" in opened
    assert "old.jsonl" not in opened and "agent-o.jsonl" not in opened
    assert led.total.messages == 1 and led.files == 1
    assert not tools


def test_the_old_files_are_not_even_listed(projects):
    f = write(
        projects / "p" / "s" / "subagents" / "agent-x.jsonl", [msg("m", "r", output_tokens=1)]
    )
    age(f, 40 * 86400)
    assert usage.list_transcripts(projects, time.time() - 7 * 86400) == []


def test_reading_stops_when_the_time_budget_is_spent_and_says_so(projects):
    fleet(projects)
    led = usage.collect(7, max_seconds=-1)
    assert led.partial and led.files == 0 and led.total.messages == 0
    assert "PARTIAL" in usage.format_ledger(led)
    assert not usage.collect(7).partial


# ---- cost and the split report ----------------------------------------------------------------------------------


def test_the_cost_estimate_uses_api_list_prices():
    u = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    assert usage.estimate_cost("claude-sonnet-5-5", u) == pytest.approx(2.0 + 10.0 + 2.5 + 0.2)
    assert usage.estimate_cost("claude-opus-5-5", u) == pytest.approx(4.0 + 20.0 + 5.0 + 0.2)
    assert usage.estimate_cost("claude-haiku-5-5", u) == pytest.approx(0.1 + 0.5 + 0.125 + 0.01)
    one_hour = {
        "cache_creation_input_tokens": 1_000_000,
        "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
    }
    assert usage.estimate_cost("claude-sonnet-5-5", one_hour) == pytest.approx(
        4.0
    )  # twice the input price
    assert usage.estimate_cost("a-model-nobody-priced", u) is None
    assert usage.estimate_cost("claude-sonnet-5-5", {"output_tokens": "garbage"}) == 0.0


def test_a_model_without_a_price_is_counted_but_left_out_of_the_cost(projects):
    write(
        projects / "p" / "s.jsonl",
        [
            msg("m1", "r1", output_tokens=1_000_000),
            msg("m2", "r2", model="mystery-1", output_tokens=1_000_000),
        ],
    )
    led = usage.collect(7)
    assert led.total.messages == 2 and led.unpriced == 1
    assert led.total.cost == pytest.approx(10.0)
    assert "leaves out 1 message(s)" in usage.format_ledger(led)


def test_the_report_splits_main_sessions_from_subagents_and_says_when_subagents_dominate(projects):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=100_000)])
    sub = projects / "p" / "s" / "subagents"
    write(sub / "agent-1.jsonl", [msg("a1", "ra1", output_tokens=600_000)])
    write(sub / "workflows" / "wf_1" / "agent-2.jsonl", [msg("a2", "ra2", output_tokens=400_000)])
    led = usage.collect(7)
    assert led.subagent_share() == pytest.approx(10.0 / 11.0)
    text = usage.format_ledger(led, "model")
    assert "1 main + 2 subagent/workflow transcript files" in text
    assert "where it ran" in text and "main sessions" in text and "subagents and workflows" in text
    assert (
        "note: most of your token use is subagent/workflow work: look at workflow design, not plugins"
        in text
    )
    assert "91% of the estimated cost" in text


def test_no_subagent_note_when_the_main_sessions_use_most(projects):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=900_000)])
    write(
        projects / "p" / "s" / "subagents" / "agent-1.jsonl",
        [msg("a1", "ra1", output_tokens=100_000)],
    )
    led = usage.collect(7)
    text = usage.format_ledger(led)
    assert "where it ran" in text
    assert "note: most of your token use" not in text


def test_the_share_falls_back_to_output_tokens_when_nothing_is_priced(projects):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", model="mystery", output_tokens=10)])
    write(
        projects / "p" / "s" / "subagents" / "agent-1.jsonl",
        [msg("a1", "ra1", model="mystery", output_tokens=30)],
    )
    assert usage.collect(7).subagent_share() == pytest.approx(0.75)


def test_usage_command_prints_the_split_and_accepts_no_subagents(projects, capsys):
    fleet(projects)
    assert ctl._run(["usage", "--days", "7"]) == 0
    text = capsys.readouterr().out
    assert "1 main + 3 subagent/workflow transcript files" in text and "where it ran" in text
    assert ctl._run(["usage", "--days", "7", "--no-subagents"]) == 0
    text = capsys.readouterr().out
    assert "subagents and workflows left out" in text and "where it ran" not in text


# ---- skills, subagents and MCP servers used (feeds `plugins audit`) ---------------------------------------------


def tool_fleet(projects: Path) -> None:
    write(
        projects / "p" / "s.jsonl",
        [tool_use("t1", "Skill", {"skill": "alpha:brainstorming"}), tool_use("t2", "Bash", {})],
    )
    sub = projects / "p" / "s" / "subagents"
    write(
        sub / "agent-1.jsonl",
        [
            tool_use("t3", "Skill", {"skill": "alpha:brainstorming"}),
            tool_use("t4", "Task", {"subagent_type": "explorer"}),
            tool_use(
                "t1", "Skill", {"skill": "alpha:brainstorming"}
            ),  # a copied block: the same id
        ],
    )
    wf = write(
        sub / "workflows" / "wf_1" / "agent-2.jsonl",
        [tool_use("t5", "mcp__plugin_bravo_bravo__list_projects", {})],
    )
    # the same shape written with spaces after the colons, as another JSON writer would
    with wf.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(tool_use("t6", "Agent", {})) + "\n")


def test_tool_use_counts_include_subagent_and_workflow_transcripts(projects):
    tool_fleet(projects)
    counts = usage.collect_tool_use(30)
    assert counts["skill:alpha:brainstorming"] == 2  # the main session and the subagent, t1 once
    assert counts["agent:explorer"] == 1
    assert counts["agent:general-purpose"] == 1
    assert counts["mcp:plugin_bravo_bravo"] == 1
    assert not counts.partial
    assert not usage.collect_tool_use(30, subagents=False).get("agent:explorer")


def test_tool_use_scan_says_when_it_was_cut_short(projects):
    tool_fleet(projects)
    counts = usage.collect_tool_use(30, max_seconds=-1)
    assert counts.partial and not counts
    assert "PARTIAL" in usage.format_tool_use(counts, 30)


def test_tool_use_survives_records_that_are_not_what_it_expects(projects):
    write(
        projects / "p" / "s.jsonl",
        [{"message": "a string", "name": "Skill", "type": "tool_use"}],
        raw=[
            '{"type":"tool_use","name":"Skill" broken',
            '{"message":{"content":"text","name":"Skill"}}',
        ],
    )
    assert not usage.collect_tool_use(30)


# ---- plugins audit ----------------------------------------------------------------------------------------------


class FakeSetup:
    def __init__(self, *args, **kwargs):
        pass

    def _installed_plugins(self):
        return [
            {"id": "alpha@market", "scope": "user", "enabled": True},
            {"id": "bravo@market", "scope": "user", "enabled": True},
            {"id": "idle@market", "scope": "user", "enabled": True},
        ]


@pytest.fixture
def fake_plugins(monkeypatch):
    monkeypatch.setattr(plugins_audit, "ClaudeSetup", FakeSetup)
    monkeypatch.setattr(
        plugins_audit,
        "plugin_cost",
        lambda claude, plugin_id: plugins_audit.PluginRow(
            plugin_id, "", True, always_on_tokens=500
        ),
    )


def test_plugins_audit_counts_uses_made_inside_subagents(projects, fake_plugins):
    tool_fleet(projects)
    rows = {r.id: r for r in plugins_audit.audit_plugins(config.load(), Path("claude"))}
    assert rows["alpha@market"].uses == 2
    assert rows["bravo@market"].uses == 1  # an MCP tool a workflow agent called
    assert rows["idle@market"].uses == 0
    assert any("not used in the last 30 days" in n for n in rows["idle@market"].notes)
    assert not rows["alpha@market"].notes


def test_a_plugin_used_only_by_subagents_is_not_called_unused(projects, fake_plugins):
    write(
        projects / "p" / "s" / "subagents" / "agent-1.jsonl",
        [tool_use("t1", "Skill", {"skill": "idle:something"})],
    )
    rows = {r.id: r for r in plugins_audit.audit_plugins(config.load(), Path("claude"))}
    assert rows["idle@market"].uses == 1
    assert not any("not used" in n for n in rows["idle@market"].notes)


def test_plugins_audit_with_only_subagent_transcripts_is_measured(projects, fake_plugins):
    write(projects / "p" / "s" / "subagents" / "agent-1.jsonl", [msg("m1", "r1", output_tokens=1)])
    rows = {r.id: r for r in plugins_audit.audit_plugins(config.load(), Path("claude"))}
    assert rows["idle@market"].uses == 0  # measured (not None): there is something to measure from


def test_plugins_audit_does_not_call_a_plugin_unused_from_a_cut_short_scan(
    projects, fake_plugins, monkeypatch
):
    write(
        projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=1)]
    )  # something was measured from
    counts = usage.ToolCounts(int)
    counts.partial = True
    monkeypatch.setattr(usage, "collect_tool_use", lambda days: counts)
    rows = {r.id: r for r in plugins_audit.audit_plugins(config.load(), Path("claude"))}
    assert rows["idle@market"].uses == 0
    assert not any("not used" in n for n in rows["idle@market"].notes)
