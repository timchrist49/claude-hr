import json
import subprocess
from pathlib import Path

import pytest

from claude_hr import claude_setup, config, paths
from claude_hr.headroom import Headroom


class FakeClaude:
    """Stands in for the `claude` CLI: records calls and keeps a tiny plugin/MCP registry."""

    def __init__(self, installed=None, marketplaces=None):
        self.calls = []
        self.installed = installed or []
        self.marketplaces = marketplaces or []

    def __call__(self, setup, *args, timeout=120):
        self.calls.append(args)
        out = ""
        if args[:3] == ("plugin", "list", "--json"):
            out = json.dumps(self.installed)
        elif args[:4] == ("plugin", "marketplace", "list", "--json"):
            out = json.dumps([{"name": n} for n in self.marketplaces])
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def setup(sandbox, monkeypatch):
    cfg = config.load()
    fake = FakeClaude()
    s = claude_setup.ClaudeSetup(cfg, Headroom(cfg), Path("/fake/claude"))
    monkeypatch.setattr(
        claude_setup.ClaudeSetup,
        "_claude",
        lambda self, *a, timeout=120: fake(self, *a, timeout=timeout),
    )
    s.fake = fake
    return s


def test_claude_md_block_is_created_updated_and_removed(setup, sandbox):
    md = paths.claude_user_md()
    md.write_text("# My own notes\n- keep this\n", encoding="utf-8")
    st = setup.sync_claude_md()
    assert st.changed
    text = md.read_text(encoding="utf-8")
    assert "- keep this" in text
    assert "@claude-hr/baseline.md" in text
    assert (
        (paths.claude_home() / "claude-hr" / "baseline.md")
        .read_text(encoding="utf-8")
        .startswith("# claude-hr baseline")
    )
    assert not setup.sync_claude_md().changed  # idempotent
    # user edits around the block survive a re-sync
    md.write_text(md.read_text(encoding="utf-8") + "\n- added later\n", encoding="utf-8")
    setup.sync_claude_md()
    assert "- added later" in md.read_text(encoding="utf-8")
    setup.remove_claude_md()
    after = md.read_text(encoding="utf-8")
    assert "CLAUDE-HR:BEGIN" not in after
    assert "- keep this" in after
    assert not (paths.claude_home() / "claude-hr").exists()


def test_settings_apply_is_idempotent_and_reversible(setup, sandbox):
    target = paths.claude_settings()
    target.write_text(
        json.dumps({"model": "opusplan", "env": {"MINE": "1"}, "autoUpdatesChannel": "latest"}),
        encoding="utf-8",
    )
    first = setup.apply_settings()
    assert first.changed
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["model"] == "opusplan"  # never touched
    assert (
        doc["autoUpdatesChannel"] == "latest"
    )  # user's explicit choice wins over the baseline's "stable"
    assert doc["env"]["MINE"] == "1"
    assert doc["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert "ANTHROPIC_BASE_URL" not in doc["env"]  # routing is per launch, never global
    assert not setup.apply_settings().changed
    setup.revert_settings()
    back = json.loads(target.read_text(encoding="utf-8"))
    assert back == {"model": "opusplan", "env": {"MINE": "1"}, "autoUpdatesChannel": "latest"}
    assert list((paths.backups_dir() / "settings").glob("settings.json.*.bak"))


def test_corrupt_settings_are_never_overwritten(setup, sandbox):
    paths.claude_settings().write_text("{ not json", encoding="utf-8")
    st = setup.apply_settings()
    assert not st.ok
    assert paths.claude_settings().read_text(encoding="utf-8") == "{ not json"


def test_baseline_has_no_quality_or_routing_overrides(sandbox):
    from claude_hr import baseline

    doc, _ = baseline.build(config.load(), "claude-hr-ctl statusline")
    flat = json.dumps(doc)
    for forbidden in (
        "ANTHROPIC_BASE_URL",
        "MAX_THINKING_TOKENS",
        '"model"',
        "effortLevel",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    ):
        assert forbidden not in flat
    assert (
        "disableBypassPermissionsMode" not in flat
    )  # --dangerously-skip-permissions stays usable by default


def test_strict_profile_adds_hardening(sandbox, monkeypatch):
    from claude_hr import baseline

    monkeypatch.setenv("CLAUDE_HR_SECURITY__PROFILE", "strict")
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__PRIVACY", "strict")
    doc, _ = baseline.build(config.load())
    assert doc["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert doc["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert doc["env"]["DISABLE_TELEMETRY"] == "1"


def test_plugin_profiles_resolve_and_reference_known_marketplaces(setup):
    plan = setup.plugin_plan()
    minimal = setup.profile_plugins(plan, "minimal")
    standard = setup.profile_plugins(plan, "standard")
    full = setup.profile_plugins(plan, "full")
    assert minimal == ["hr-core@claude-hr"]
    assert set(minimal) < set(standard) < set(full)
    for spec in full:
        assert spec.split("@")[1] in plan["marketplaces"], spec
    with pytest.raises(KeyError):
        setup.profile_plugins(plan, "nope")


def test_plugin_sync_skips_same_name_from_any_marketplace(setup):
    setup.fake.installed = [
        {"id": "superpowers@anthropic-plugin-directory", "scope": "user", "enabled": True}
    ]
    setup.fake.marketplaces = ["claude-hr", "claude-plugins-official", "headroom-marketplace"]
    setup.sync_plugins("standard")
    installs = [c for c in setup.fake.calls if c[:2] == ("plugin", "install")]
    assert all("superpowers" not in c[2] for c in installs)  # no duplicate skills
    assert any("claude-md-management" in c[2] for c in installs)


def test_plugin_sync_never_prunes_without_flag(setup):
    setup.fake.installed = [{"id": "vercel@x", "scope": "user", "enabled": True}]
    setup.fake.marketplaces = ["claude-hr", "claude-plugins-official", "headroom-marketplace"]
    setup.sync_plugins("minimal")
    assert not [c for c in setup.fake.calls if c[:2] == ("plugin", "uninstall")]
    setup.sync_plugins("minimal", prune=True)
    assert [c for c in setup.fake.calls if c[:2] == ("plugin", "uninstall")]


def test_headroom_marketplace_is_gated_by_config(setup, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__INSTALL_PLUGINS", "false")
    setup.cfg = config.load()
    setup.fake.marketplaces = ["claude-hr", "claude-plugins-official"]
    setup.sync_plugins("standard")
    adds = [c for c in setup.fake.calls if c[:3] == ("plugin", "marketplace", "add")]
    assert not any("headroomlabs-ai" in c[-1] for c in adds)
    assert not any(
        "headroom-snip" in c[2] for c in setup.fake.calls if c[:2] == ("plugin", "install")
    )


def test_dry_run_changes_nothing(setup, sandbox):
    setup.dry = True
    setup.apply_settings()
    setup.sync_claude_md()
    assert not paths.claude_settings().exists()
    assert not paths.claude_user_md().exists()


def test_baseline_lean_defaults_for_the_dev_workflow(sandbox):
    from claude_hr import baseline

    doc, _ = baseline.build(config.load())
    # the one hidden recurring spend of the standard profile is off; the plugin's other layers are untouched
    assert doc["env"]["ENABLE_STOP_REVIEW"] == "0"
    assert "CLAUDE_HR_GIT_GUARD" not in doc["env"]
    ask = doc["permissions"]["ask"]
    assert "Edit(**/.claude/hr-verify.toml)" in ask and "Write(**/.claude/settings.json)" in ask
    assert (
        "Bash(claude-hr-ctl verify trust*)" in ask
    )  # only the user decides which commands run unattended
    deny = doc["permissions"]["deny"]  # the trust record and gate counters are not Claude's to edit
    assert "Edit(~/.claude-hr/state/**)" in deny and "Write(~/.claude-hr/state/**)" in deny
    assert "Read(~/.ssh/**)" in deny  # next to, not instead of, the secret-read rules
    allow = doc["permissions"]["allow"]
    assert "Bash(gh pr view *)" in allow and not [
        a for a in allow if "create" in a or "merge" in a or "edit" in a
    ]
    assert "Bash(git push *)" not in allow  # nothing that writes is pre-approved


def test_baseline_stop_review_can_be_restored_and_gates_protection_dropped(sandbox, monkeypatch):
    from claude_hr import baseline

    monkeypatch.setenv("CLAUDE_HR_SECURITY__STOP_REVIEW", "plugin-default")
    monkeypatch.setenv("CLAUDE_HR_GATES__PROTECT_CONFIG", "false")
    monkeypatch.setenv("CLAUDE_HR_GITHUB__READONLY_ALLOW", "false")
    doc, _ = baseline.build(config.load())
    assert "ENABLE_STOP_REVIEW" not in doc["env"]
    assert "ask" not in doc["permissions"] and "allow" not in doc["permissions"]
    assert not [r for r in doc["permissions"]["deny"] if ".claude-hr" in r]


def test_strict_profile_refuses_risky_git_instead_of_asking(sandbox, monkeypatch):
    from claude_hr import baseline

    monkeypatch.setenv("CLAUDE_HR_SECURITY__PROFILE", "strict")
    doc, _ = baseline.build(config.load())
    assert doc["env"]["CLAUDE_HR_GIT_GUARD"] == "deny"


def test_profiles_are_lean_and_resolve(setup):
    plan = setup.plugin_plan()
    standard = setup.profile_plugins(plan, "standard")
    # the always-on cost of standard stays small: no process-heavy or overlapping plugins by default
    for heavy in ("superpowers", "feature-dev", "code-simplifier", "claude-security"):
        assert not [s for s in standard if s.startswith(heavy + "@")], heavy
    assert "hr-gates@claude-hr" in standard and "hr-core@claude-hr" in standard
    process = setup.profile_plugins(plan, "process")
    assert {"superpowers@claude-plugins-official", "feature-dev@claude-plugins-official"} <= set(
        process
    )
    assert "claude-security@claude-plugins-official" in setup.profile_plugins(plan, "audit")
    full = set(setup.profile_plugins(plan, "full"))
    assert set(process) < full and "skill-creator@claude-plugins-official" in full
    assert "ponytail@claude-hr" not in full  # third-party behaviour layer stays a deliberate choice
    assert "code-simplifier@claude-plugins-official" in plan["redundant"]


# ---- wiring review: permission rules follow the claude-hr home, setup records what it applied -------------------------
def _deny_ask():
    from claude_hr import baseline

    doc, _ = baseline.build(config.load())
    return doc["permissions"]["deny"], doc["permissions"]["ask"]


def test_state_deny_follows_a_claude_hr_home_outside_the_user_home(sandbox, tmp_path, monkeypatch):
    """The deny rule was the literal ~/.claude-hr/state/**: with CLAUDE_HR_HOME elsewhere the trust record was unprotected."""
    custom = tmp_path / "data" / "hrhome"
    monkeypatch.setenv("CLAUDE_HR_HOME", str(custom))
    deny, ask = _deny_ask()
    state = [r for r in deny if "hrhome" in r]
    assert sorted(r.split("(")[0] for r in state) == ["Edit", "Write"]
    for rule in state:
        inner = rule[rule.index("(") + 1 : -1]
        assert inner.startswith("//") and inner.endswith("/hrhome/state/**"), rule
        assert (
            "\\" not in rule and "~" not in rule
        )  # an absolute, forward-slash rule, not home-relative
    # the default home stays protected too: an install whose runtime falls back to it is still covered
    assert "Edit(~/.claude-hr/state/**)" in deny and "Write(~/.claude-hr/state/**)" in deny
    # config.toml (formatter trust, launch flags, alert command ...) is asked about, not denied
    cfg_rules = [r for r in ask if r.endswith("/hrhome/config.toml)")]
    assert sorted(r.split("(")[0] for r in cfg_rules) == ["Edit", "Write"]
    assert "Edit(~/.claude-hr/config.toml)" in ask and "Write(~/.claude-hr/config.toml)" in ask
    assert not [r for r in deny if "config.toml" in r]


def test_state_deny_uses_a_home_relative_rule_below_the_user_home(sandbox, monkeypatch):
    monkeypatch.setenv("CLAUDE_HR_HOME", str(sandbox / "tools" / "hr"))
    deny, ask = _deny_ask()
    assert "Edit(~/tools/hr/state/**)" in deny and "Write(~/tools/hr/state/**)" in deny
    assert "Edit(~/tools/hr/config.toml)" in ask and "Write(~/tools/hr/config.toml)" in ask


def test_default_home_adds_each_rule_exactly_once(sandbox):
    deny, ask = _deny_ask()
    assert deny.count("Edit(~/.claude-hr/state/**)") == 1
    assert ask.count("Write(~/.claude-hr/config.toml)") == 1
    assert len(deny) == len(set(deny)) and len(ask) == len(set(ask))


@pytest.mark.skipif(paths.IS_WINDOWS, reason="POSIX spelling")
def test_rule_path_spellings_posix(sandbox):
    from claude_hr import baseline

    assert baseline.rule_path("/data/hr/state") == "//data/hr/state"
    assert baseline.rule_path(sandbox / ".claude-hr") == "~/.claude-hr"


@pytest.mark.skipif(not paths.IS_WINDOWS, reason="Windows spelling")
def test_rule_path_spellings_windows(sandbox):
    from claude_hr import baseline

    assert baseline.rule_path("D:\\hr\\state") == "//d/hr/state"
    assert baseline.rule_path(sandbox / ".claude-hr") == "~/.claude-hr"


def test_home_rules_merge_once_move_with_the_home_and_uninstall_removes_exactly_them(
    setup, sandbox, tmp_path, monkeypatch
):
    target = paths.claude_settings()
    mine = {"permissions": {"deny": ["Read(mine)"], "ask": ["Bash(mine)"]}, "model": "opusplan"}
    target.write_text(json.dumps(mine), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HR_HOME", str(tmp_path / "data" / "hrhome"))
    setup.cfg = config.load()
    setup.apply_settings()
    setup.apply_settings()  # a second setup must not duplicate anything
    doc = json.loads(target.read_text(encoding="utf-8"))
    for key in ("deny", "ask"):
        assert len(doc["permissions"][key]) == len(set(doc["permissions"][key]))
    assert any(r.endswith("/hrhome/state/**)") for r in doc["permissions"]["deny"])
    assert any(r.endswith("/hrhome/config.toml)") for r in doc["permissions"]["ask"])
    # the home is moved (its state/ goes with it): the old home's rules are retired, the new one's added, no duplicates
    record = (paths.state_dir() / "settings-merge.json").read_text(encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HR_HOME", str(tmp_path / "data" / "other"))
    paths.state_dir().mkdir(parents=True)
    (paths.state_dir() / "settings-merge.json").write_text(record, encoding="utf-8")
    setup2 = claude_setup.ClaudeSetup(config.load(), setup.hr, setup.claude)
    setup2.apply_settings()
    deny = json.loads(target.read_text(encoding="utf-8"))["permissions"]["deny"]
    assert not [r for r in deny if "/hrhome/" in r] and any("/other/state/**" in r for r in deny)
    assert len(deny) == len(set(deny))
    # uninstall removes exactly what claude-hr added: the user's own rules and keys are all that is left
    setup2.revert_settings()
    assert json.loads(target.read_text(encoding="utf-8")) == mine


@pytest.mark.parametrize(
    "value, mode",
    [
        ("off", "off"),
        ("Off", "off"),
        ("OFF", "off"),
        ("false", "off"),  # stored as a TOML boolean by `config set`
        ("0", "off"),  # stored as an integer
        ("no", "off"),
        ("disabled", "off"),
        ("plugin-default", "plugin-default"),
        ("true", "plugin-default"),
        ("of", "unknown"),  # a typo is neither: the review stays on and doctor says so
    ],
)
def test_stop_review_accepts_the_usual_spellings_of_off(sandbox, monkeypatch, value, mode):
    from claude_hr import baseline

    monkeypatch.setenv("CLAUDE_HR_SECURITY__STOP_REVIEW", value)
    cfg = config.load()
    assert baseline.stop_review_mode(cfg) == mode
    doc, _ = baseline.build(cfg)
    assert (doc["env"].get("ENABLE_STOP_REVIEW") == "0") is (mode == "off")


def test_config_set_false_turns_the_stop_review_off(sandbox):
    """The reported case: `config set security.stop_review false` stored a boolean and the review silently stayed on."""
    from claude_hr import baseline, ctl

    assert ctl._run(["config", "set", "security.stop_review", "false"]) == 0
    doc, _ = baseline.build(config.load())
    assert doc["env"]["ENABLE_STOP_REVIEW"] == "0"


def test_setup_records_the_version_of_each_baseline_step_it_applied(sandbox, monkeypatch):
    from claude_hr import __version__, ctl

    assert claude_setup.applied_version() is None
    assert ctl._run(["setup", "--only", "claude-md", "--dry-run"]) == 0
    assert claude_setup.applied_version("claude-md") is None  # a dry run applies nothing
    assert ctl._run(["setup", "--only", "claude-md"]) == 0
    assert claude_setup.applied_version("claude-md") == __version__
    assert (
        claude_setup.applied_version("settings") is None
    )  # the settings baseline was not part of that run
    assert claude_setup.baseline_is_stale() == (True, None)
    assert ctl._run(["setup", "--only", "settings", "--dry-run"]) == 0
    assert claude_setup.applied_version("settings") is None
    assert ctl._run(["setup", "--only", "settings"]) == 0
    assert claude_setup.applied_version("settings") == __version__
    assert (
        claude_setup.applied_version("claude-md") == __version__
    )  # kept: the other step's entry survives
    assert claude_setup.applied_config_dir() == str(paths.claude_settings())
    assert claude_setup.baseline_is_stale() == (False, __version__)
    monkeypatch.setattr("claude_hr.__version__", "9.9.9")  # a newer release is installed unattended
    assert claude_setup.baseline_is_stale() == (True, __version__)


def test_a_partial_setup_does_not_mark_the_settings_baseline_as_applied(
    setup, sandbox, monkeypatch
):
    """`setup --only plugins` must leave the settings and CLAUDE.md steps unrecorded (`--only mcp` / `--only headroom` run
    no baseline step at all): doctor keeps warning and the update job keeps its hands off what the user skipped."""
    from claude_hr import __version__, ctl

    monkeypatch.setattr(claude_setup.procutil, "find_claude", lambda *a, **k: Path("/fake/claude"))
    assert ctl._run(["setup", "--only", "plugins"]) == 0
    assert claude_setup.applied_version("plugins") == __version__
    assert claude_setup.applied_version("settings") is None
    assert claude_setup.applied_version("claude-md") is None
    assert claude_setup.baseline_is_stale() == (
        True,
        None,
    )  # doctor still warns for a machine like this
    assert claude_setup.unattended_reapply() == ([], "")  # and the job has nothing it may apply


def test_setup_with_every_baseline_step_skipped_leaves_nothing_for_the_update_job(
    setup, sandbox, monkeypatch
):
    from claude_hr import config, ctl, updater

    skipped = ["headroom", "settings", "claude-md", "mcp", "plugins", "schedule"]
    assert ctl._run(["setup", "--skip", *skipped]) == 0
    assert (paths.state_dir() / "install.json").exists()  # `setup` ran: ...
    assert claude_setup.was_set_up() and not (paths.state_dir() / "applied-version.json").exists()
    monkeypatch.setattr("claude_hr.__version__", "9.9.9")
    assert updater.Updater(config.load()).reapply_baseline() is None
    assert (
        not paths.claude_settings().exists() and not paths.claude_user_md().exists()
    )  # ... the opt-out holds


def test_the_applied_record_describes_one_claude_config_dir(sandbox, monkeypatch, tmp_path):
    """Applying to another profile starts the record afresh: its entries say nothing about the new one."""
    claude_setup.record_applied_version("settings", "claude-md")
    first = claude_setup.applied_config_dir()
    other = tmp_path / "other-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert claude_setup.applied_elsewhere() == first
    claude_setup.record_applied_version("plugins")
    assert claude_setup.applied_config_dir() == str(other / "settings.json")
    assert claude_setup.applied_elsewhere() is None
    assert claude_setup.applied_version("plugins") == claude_setup.running_version()
    assert (
        claude_setup.applied_version("settings") is None
        and claude_setup.applied_version("claude-md") is None
    )
    # a damaged record is our own file: it reads as "nothing recorded" and the next setup rewrites it
    (paths.state_dir() / "applied-version.json").write_text("{ nope", encoding="utf-8")
    assert claude_setup.applied_version() is None and claude_setup.applied_config_dir() is None
    claude_setup.record_applied_version("settings")
    assert claude_setup.applied_version("settings") == claude_setup.running_version()


def test_a_failed_baseline_step_is_not_recorded_as_applied(sandbox):
    from claude_hr import ctl

    paths.claude_settings().write_text("{ not json", encoding="utf-8")
    assert ctl._run(["setup", "--only", "settings", "claude-md"]) == 1
    assert claude_setup.applied_version("settings") is None  # the step that failed stays visible
    assert claude_setup.baseline_is_stale() == (True, None)


def test_baseline_is_not_stale_on_a_machine_setup_never_ran_on(sandbox):
    assert claude_setup.baseline_is_stale() == (False, None)


def test_revert_survives_an_unparsable_settings_file_and_keeps_the_record(setup, sandbox):
    setup.apply_settings()
    state = paths.state_dir() / "settings-merge.json"
    paths.claude_settings().write_text('{"theme": "dark",}', encoding="utf-8")
    step = setup.revert_settings()
    assert not step.ok and "settings.json" in step.lines[0]
    assert "NOT reverted" in " ".join(step.lines)
    assert paths.claude_settings().read_text(encoding="utf-8") == '{"theme": "dark",}'
    assert state.exists()  # a later uninstall still knows which keys are ours
    state.write_text("{ broken", encoding="utf-8")
    assert (
        not setup.revert_settings().ok
    )  # a damaged ownership record is reported too, never a traceback
    assert not setup.apply_settings().ok


def test_unreadable_claude_json_fails_the_mcp_steps_without_a_traceback(setup, sandbox):
    paths.claude_global_json().write_text('{"mcpServers": {"a":', encoding="utf-8")
    assert not setup.register_mcp().ok
    assert not setup.unregister_mcp().ok
    assert paths.claude_global_json().read_text(encoding="utf-8") == '{"mcpServers": {"a":'


def _quiet_uninstall(monkeypatch):
    """Nothing in an uninstall test may reach the real machine: no scheduler, no claude, no proxy."""
    from claude_hr import ctl

    monkeypatch.setattr(ctl.schedule, "remove", lambda: [])
    monkeypatch.setattr(ctl.procutil, "find_claude", lambda *a, **k: None)
    monkeypatch.setattr(ctl.Headroom, "stop", lambda self: False)
    monkeypatch.setattr(
        ctl.procutil, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    return ctl


def test_uninstall_skips_an_unreadable_settings_file_runs_the_rest_and_exits_nonzero(
    setup, sandbox, monkeypatch, capsys
):
    setup.apply_settings()
    setup.sync_claude_md()
    broken = '{"theme": "dark",}'
    paths.claude_settings().write_text(broken, encoding="utf-8")
    ctl = _quiet_uninstall(monkeypatch)
    assert ctl._run(["uninstall"]) == 1
    text = capsys.readouterr().out
    assert "[ERR] settings-revert" in text and "INCOMPLETE" in text
    assert paths.claude_settings().read_text(encoding="utf-8") == broken  # untouched
    assert (paths.state_dir() / "settings-merge.json").exists()
    md = paths.claude_user_md()
    assert "CLAUDE-HR:BEGIN" not in (md.read_text(encoding="utf-8") if md.exists() else "")
    # ... the steps after the failed one still ran


def test_uninstall_purge_keeps_the_ownership_record_when_settings_could_not_be_reverted(
    setup, sandbox, monkeypatch, capsys
):
    setup.apply_settings()
    paths.claude_settings().write_text("{ nope", encoding="utf-8")
    ctl = _quiet_uninstall(monkeypatch)
    assert ctl._run(["uninstall", "--purge"]) == 1
    assert (
        paths.state_dir() / "settings-merge.json"
    ).exists()  # purge must not destroy what a retry needs
    assert "was kept" in capsys.readouterr().out


def test_uninstall_removes_the_setup_markers_so_no_update_run_resurrects_the_baseline(
    setup, sandbox, monkeypatch
):
    """Reproduced: setup, uninstall (install.json was left behind), then the next full `update` (or the 03:17 job, when the
    schedule could not be removed) re-created CLAUDE.md, re-merged settings.json and re-installed the plugins."""
    from claude_hr import config, ctl, updater

    with monkeypatch.context() as old:
        old.setattr("claude_hr.__version__", "0.1.9")
        assert ctl._run(["setup", "--only", "settings", "claude-md"]) == 0
    state = paths.state_dir()
    assert (state / "install.json").exists() and (state / "applied-version.json").exists()
    ctl = _quiet_uninstall(monkeypatch)
    assert ctl._run(["uninstall", "--dry-run"]) == 0
    assert (state / "install.json").exists() and (state / "applied-version.json").exists()  # dry
    assert ctl._run(["uninstall"]) == 0
    assert not (state / "install.json").exists() and not (state / "applied-version.json").exists()
    assert not (state / "settings-merge.json").exists() and not claude_setup.was_set_up()
    assert json.loads(paths.claude_settings().read_text(encoding="utf-8")) == {}
    assert not paths.claude_user_md().exists()
    assert claude_setup.baseline_is_stale() == (False, None)
    # a newer release arrives unattended, then the update job runs: it has nothing to re-apply
    monkeypatch.setattr("claude_hr.__version__", "9.9.9")
    setup.fake.calls.clear()
    assert updater.Updater(config.load()).reapply_baseline() is None
    assert json.loads(paths.claude_settings().read_text(encoding="utf-8")) == {}
    assert not paths.claude_user_md().exists() and setup.fake.calls == []


def test_uninstall_clears_the_markers_even_when_settings_could_not_be_reverted(
    setup, sandbox, monkeypatch
):
    """The record that lets a later uninstall revert exactly our keys stays; the 'set up' markers go regardless."""
    ctl = _quiet_uninstall(monkeypatch)
    assert ctl._run(["setup", "--only", "settings", "claude-md"]) == 0
    paths.claude_settings().write_text("{ nope", encoding="utf-8")
    assert ctl._run(["uninstall"]) == 1
    state = paths.state_dir()
    assert (state / "settings-merge.json").exists()
    assert not (state / "install.json").exists() and not (state / "applied-version.json").exists()


def test_effective_plugins_drop_a_marketplace_that_is_switched_off(setup, monkeypatch):
    plan = setup.plugin_plan()
    assert "headroom-snip@headroom-marketplace" in setup.effective_plugins(plan, "standard")
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__INSTALL_PLUGINS", "false")
    setup.cfg = config.load()
    eff = setup.effective_plugins(plan, "standard")
    assert "headroom-snip@headroom-marketplace" not in eff and "hr-gates@claude-hr" in eff
    assert "headroom-snip@headroom-marketplace" in setup.profile_plugins(plan, "standard")


def test_plugin_listing_tolerates_wrapped_or_odd_output(setup):
    setup.fake.installed = {"plugins": [{"id": "a@b", "scope": "user"}, "junk", {"nope": 1}]}
    assert setup._installed_plugins() == [{"id": "a@b", "scope": "user"}]
    setup.fake.installed = "text"
    assert setup._installed_plugins() == []
