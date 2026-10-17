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


# ---- est.cost: n/a for a model without a price, and what the number is -------------------------------------------


def test_a_model_without_a_price_shows_n_a_never_a_zero_dollar_cost(projects):
    write(
        projects / "p" / "s.jsonl",
        [
            msg("m1", "r1", output_tokens=1_000_000),
            msg("m2", "r2", model="mystery-1", output_tokens=1_000_000),
        ],
    )
    led = usage.collect(7)
    text = usage.format_ledger(led, "model")
    rows = {ln.split()[0]: ln for ln in text.splitlines() if ln.split()[:1]}
    assert rows["mystery-1"].rstrip().endswith("n/a") and "$0.00" not in rows["mystery-1"]
    assert rows["claude-sonnet-5-5"].rstrip().endswith("$10.00")
    assert rows["TOTAL"].rstrip().endswith("$10.00*")  # the star: some of it has no price
    day_row = next(ln for ln in usage.format_ledger(led, "day").splitlines() if ln[:2] == "20")
    assert day_row.rstrip().endswith("$10.00*")
    assert "n/a = no price known for the model; * = the figure leaves out 1 message(s)" in text
    assert "(mystery-1)" in text


def test_a_ledger_with_nothing_priced_says_n_a_in_the_total(projects):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", model="mystery-1", output_tokens=5)])
    text = usage.format_ledger(usage.collect(7))
    lines = text.splitlines()
    total = next(ln for ln in lines if ln.startswith("TOTAL"))
    day_row = next(ln for ln in lines if ln[:2] == "20")
    main_row = next(ln for ln in lines if ln.startswith("main sessions"))
    assert all(row.rstrip().endswith("n/a") for row in (total, day_row, main_row))


def test_the_cost_footnote_says_it_is_an_estimate_and_what_it_ignores(projects):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=5)])
    text = usage.format_ledger(usage.collect(7))
    foot = " ".join(ln for ln in text.splitlines() if ln.startswith("est.cost"))
    assert "not a bill" in foot and "API list prices" in foot
    for word in ("tiered", "long-context", "batch"):
        assert word in foot
    assert "a subscription plan is not billed per token" in foot


# ---- the daily snapshot ------------------------------------------------------------------------------------------

SINCE = datetime(
    2026, 10, 9, 10, 0, tzinfo=UTC
).timestamp()  # a scan of 8 days, made on 2026-10-17 at 10:00 UTC
UNTIL = datetime(2026, 10, 17, 10, 0, tzinfo=UTC).timestamp()
WINDOW_DAYS = [f"2026-10-{d:02d}" for d in range(9, 18)]
WHOLE_DAYS = WINDOW_DAYS[
    1:-1
]  # the first day began before the scan did, the last one is still running


def ledger(days=WINDOW_DAYS, subagents=True, partial=False, messages=3) -> usage.Ledger:
    led = usage.Ledger(8, since=SINCE, until=UNTIL, subagents=subagents, partial=partial)
    for d in days:
        for _ in range(messages):
            led.by_day[d].add({"input_tokens": 5, "output_tokens": 10}, 0.0)
    return led


def stored(res: usage.SnapshotResult) -> dict:
    return json.loads(res.file.read_text(encoding="utf-8"))


def test_the_snapshot_stores_only_whole_days_each_with_its_scope(tmp_path):
    res = usage.write_snapshot(ledger(), tmp_path)
    got = stored(res)
    assert (
        sorted(got) == WHOLE_DAYS
    )  # seven days, not the first (partly in the window) or the last (running)
    assert all(v["scope"] == "main+subagents" and v["messages"] == 3 for v in got.values())
    assert got["2026-10-10"] == {
        "input": 15,
        "output": 30,
        "cache_write": 0,
        "cache_read": 0,
        "messages": 3,
        "scope": "main+subagents",
    }
    assert res.not_whole == ["2026-10-09", "2026-10-17"] and not res.skipped


def test_a_partial_scan_never_overwrites_a_stored_day(tmp_path):
    first = usage.write_snapshot(ledger(), tmp_path)
    before = first.file.read_text(encoding="utf-8")
    cut = ledger(messages=1, partial=True)
    res = usage.write_snapshot(cut, tmp_path)
    assert res.skipped and "cut short" in res.skipped and not res.written
    assert first.file.read_text(encoding="utf-8") == before
    fresh = usage.write_snapshot(ledger(partial=True), tmp_path / "other")
    assert fresh.skipped and not (tmp_path / "other").exists()  # not even the folder


def test_a_day_stored_without_a_scope_is_main_only_and_is_kept_as_it_is(tmp_path):
    old = {"input": 1, "output": 2, "cache_write": 0, "cache_read": 0, "messages": 9}
    assert usage.snapshot_scope(old) == "main"
    assert usage.snapshot_scope({**old, "scope": "main+subagents"}) == "main+subagents"
    assert usage.snapshot_scope("garbage") == "main"
    month = f"{time.strftime('%Y-%m')}.json"
    (tmp_path / month).write_text(
        json.dumps(
            {
                "2026-10-10": old,  # from before the field existed
                "2026-10-11": {
                    **old,
                    "messages": 100,
                    "scope": "main+subagents",
                },  # more than this scan sees
                "2026-10-12": {**old, "messages": 1, "scope": "main+subagents"},  # less
            }
        ),
        encoding="utf-8",
    )
    res = usage.write_snapshot(ledger(), tmp_path)
    got = stored(res)
    assert got["2026-10-10"] == old  # untouched: no scope added, no number replaced
    assert (
        got["2026-10-11"]["messages"] == 100
    )  # the larger figure stays: Claude Code prunes transcripts
    assert (
        got["2026-10-12"]["messages"] == 3
    )  # replaced by a figure of the same scope that is not smaller
    assert res.kept_other_scope == ["2026-10-10"] and res.kept_larger == ["2026-10-11"]
    assert "2026-10-12" in res.written and len(res.written) == 5
    lines = "\n".join(res.lines())
    assert "kept as stored (counted with another scope): 2026-10-10" in lines


def test_a_main_only_snapshot_does_not_overwrite_days_counted_with_subagents(tmp_path):
    usage.write_snapshot(ledger(), tmp_path)
    res = usage.write_snapshot(ledger(subagents=False, messages=1), tmp_path)
    assert res.scope == "main" and not res.written
    assert res.kept_other_scope == WHOLE_DAYS
    assert all(v["scope"] == "main+subagents" for v in stored(res).values())
    fresh = usage.write_snapshot(ledger(subagents=False), tmp_path / "main-only")
    assert all(v["scope"] == "main" for v in stored(fresh).values())


def test_an_unparsable_snapshot_file_is_left_alone(tmp_path):
    f = tmp_path / f"{time.strftime('%Y-%m')}.json"
    f.write_text('{"2026-10-10": {', encoding="utf-8")
    res = usage.write_snapshot(ledger(), tmp_path)
    assert res.skipped and "not valid JSON" in res.skipped and not res.written
    assert f.read_text(encoding="utf-8") == '{"2026-10-10": {'


def test_usage_snapshot_command_reports_what_it_stored_and_what_it_did_not(projects, capsys):
    fleet(projects)  # one message each, all of today: a day that is still running is not stored
    assert ctl._run(["usage", "--snapshot", "--days", "7"]) == 0
    text = capsys.readouterr().out
    assert "snapshot unchanged:" in text and "scope main+subagents" in text
    assert "not stored (the scan saw only part of the day)" in text
    assert ctl._run(["usage", "--snapshot", "--days", "7", "--no-subagents"]) == 0
    assert "scope main)" in capsys.readouterr().out


def test_the_maintain_job_stores_the_seven_whole_days_before_today(projects, monkeypatch):
    """It runs weekly: the old 3-day window stored 3 days in 7, and its first and last day were partial."""
    now = datetime.now(UTC)
    for k in range(1, 11):  # one message at noon UTC of each of the last 10 days
        noon = (now - timedelta(days=k)).replace(hour=12, minute=0, second=0, microsecond=0)
        write(
            projects / "p" / f"s{k}.jsonl",
            [msg(f"m{k}", f"r{k}", when=noon.strftime("%Y-%m-%dT%H:%M:%S.000Z"), output_tokens=k)],
        )
    monkeypatch.setattr(ctl.maintenance, "plan", lambda cfg: None)
    monkeypatch.setattr(ctl.maintenance, "apply", lambda p: (0, 0.0))
    monkeypatch.setattr(ctl.Headroom, "binary", lambda self: None)  # no real `headroom learn`
    assert ctl._run(["job", "maintain"]) == 0
    f = paths.state_dir() / "usage" / f"{time.strftime('%Y-%m')}.json"
    got = json.loads(f.read_text(encoding="utf-8"))
    expected = {(now - timedelta(days=k)).strftime("%Y-%m-%d") for k in range(1, 8)}
    assert set(got) == expected
    assert all(v["scope"] == "main+subagents" for v in got.values())
    log = (paths.logs_dir() / "job-maintain.log").read_text(encoding="utf-8")
    assert "usage snapshot: 7 day(s) stored" in log


def test_the_maintain_job_does_not_store_from_a_cut_short_scan(projects, monkeypatch):
    write(projects / "p" / "s.jsonl", [msg("m1", "r1", output_tokens=1)])
    real = usage.collect
    monkeypatch.setattr(usage, "collect", lambda days: real(days, max_seconds=-1))
    monkeypatch.setattr(ctl.maintenance, "plan", lambda cfg: None)
    monkeypatch.setattr(ctl.maintenance, "apply", lambda p: (0, 0.0))
    monkeypatch.setattr(ctl.Headroom, "binary", lambda self: None)
    assert ctl._run(["job", "maintain"]) == 0
    assert not (paths.state_dir() / "usage").exists()
    log = (paths.logs_dir() / "job-maintain.log").read_text(encoding="utf-8")
    assert "usage snapshot not written: the scan was cut short" in log


# ---- plugins audit: a partial result says so, and how the window is measured -------------------------------------


def test_plugins_audit_says_how_the_window_is_measured(projects, fake_plugins):
    tool_fleet(projects)
    rows = plugins_audit.audit_plugins(config.load(), Path("claude"))
    text = plugins_audit.format_plugins(rows)
    assert not rows.partial and rows.measured
    assert "modified in the last 30 days" in text
    assert "by file modification time, not by when each call was made" in text
    assert "PARTIAL" not in text and ">=" not in text


def test_plugins_audit_marks_a_partial_scan_as_partial(projects, fake_plugins, monkeypatch):
    tool_fleet(projects)
    counts = usage.ToolCounts(int)
    counts["skill:alpha:brainstorming"] = 2
    counts.partial = True
    monkeypatch.setattr(usage, "collect_tool_use", lambda days: counts)
    rows = plugins_audit.audit_plugins(config.load(), Path("claude"))
    text = plugins_audit.format_plugins(rows)
    assert rows.partial
    assert "PARTIAL: the transcript scan stopped early; the uses column is a lower bound" in text
    alpha = next(ln for ln in text.splitlines() if ln.startswith("alpha@market"))
    assert alpha.rstrip().endswith(">=2")  # a lower bound is written as one
    assert not any("not used" in n for r in rows for n in r.notes)


def test_plugins_audit_does_not_call_a_plugin_unused_when_transcripts_were_unreadable(
    projects, fake_plugins, monkeypatch
):
    tool_fleet(projects)
    counts = usage.ToolCounts(int)
    counts["skill:alpha:brainstorming"] = 2
    counts.unreadable = 3
    monkeypatch.setattr(usage, "collect_tool_use", lambda days: counts)
    rows = plugins_audit.audit_plugins(config.load(), Path("claude"))
    text = plugins_audit.format_plugins(rows)
    assert rows.unreadable == 3
    assert "3 transcript file(s) could not be read" in text
    assert not any("not used" in n for r in rows for n in r.notes)


# ---- Windows: deep transcript paths, and files that cannot be read are counted -----------------------------------

DEEP = (
    "C:\\Users\\tim\\.claude\\projects\\"
    + "p" * 190
    + "\\s\\subagents\\workflows\\wf_1\\agent-1.jsonl"
)


def test_long_paths_get_the_extended_length_form_on_windows_only():
    short = "C:\\Users\\tim\\.claude\\projects\\p\\s.jsonl"
    assert usage.long_path(short, windows=True) == short
    assert len(DEEP) > 259
    assert usage.long_path(DEEP, windows=True) == "\\\\?\\" + DEEP
    assert usage.long_path(DEEP.replace("\\", "/"), windows=True) == "\\\\?\\" + DEEP  # no '/' left
    unc = "\\\\server\\share\\" + "d\\" * 130 + "agent.jsonl"
    assert (
        usage.long_path(unc, windows=True)
        == "\\\\?\\UNC\\server\\share\\" + unc[len("\\\\server\\share\\") :]
    )
    assert usage.long_path("\\\\?\\" + DEEP, windows=True) == "\\\\?\\" + DEEP  # not twice
    assert usage.long_path(DEEP, windows=False) == DEEP
    assert usage.plain_path("\\\\?\\" + DEEP) == DEEP
    assert usage.plain_path("\\\\?\\UNC\\server\\share\\x") == "\\\\server\\share\\x"
    assert usage.plain_path(short) == short


def test_transcripts_are_listed_and_opened_through_long_path(projects, monkeypatch):
    fleet(projects)
    seen: list[str] = []
    real = usage.long_path
    monkeypatch.setattr(
        usage, "long_path", lambda p, windows=None: seen.append(str(p)) or real(p, windows)
    )
    usage.collect(7)
    usage.collect_tool_use(7)
    names = {Path(p).name for p in seen}
    assert {"s1.jsonl", "agent-a1.jsonl", "agent-b1.jsonl", "agent-c1.jsonl"} <= names  # opened
    assert str(projects / "proj-a" / "s1" / "subagents") in seen  # listed


def test_a_transcript_that_cannot_be_read_is_counted_and_said(projects, monkeypatch):
    fleet(projects)
    real_open = builtins.open

    def deny(file, *args, **kwargs):
        if str(file).endswith("agent-a1.jsonl"):
            raise PermissionError(13, "denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny)
    led = usage.collect(7)
    counts = usage.collect_tool_use(7)
    monkeypatch.setattr(builtins, "open", real_open)
    assert led.unreadable == 1
    assert (led.files, led.files_main, led.files_sub) == (
        3,
        1,
        2,
    )  # the file that was read, not the one listed
    assert led.total.messages == 3
    assert (
        "1 transcript file(s) could not be read; the figures leave them out."
        in usage.format_ledger(led)
    )
    assert counts.unreadable == 1
    assert "1 transcript file(s) could not be read" in usage.format_tool_use(counts, 30)


def test_a_folder_that_cannot_be_listed_is_counted(projects, monkeypatch):
    fleet(projects)
    sub = projects / "proj-a" / "s1" / "subagents"
    real_scandir = os.scandir

    def scandir(path="."):
        if str(path) == str(sub):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    with monkeypatch.context() as m:
        m.setattr(os, "scandir", scandir)
        problems: list[str] = []
        got = usage.list_transcripts(projects, 0.0, problems=problems)
        led = usage.collect(7)
    assert problems == [str(sub)]
    assert sorted(t.path.name for t in got) == ["agent-c1.jsonl", "s1.jsonl"]
    assert led.unreadable == 1
