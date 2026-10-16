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
