"""doctor checks added for the dev workflow: foreign proxy / Headroom Desktop, hook binary, security-guidance health."""

import json
import subprocess
from pathlib import Path

import pytest

from claude_hr import config, doctor, paths
from claude_hr.headroom import Headroom


@pytest.fixture
def fake_claude(monkeypatch):
    """doctor needs a Claude Code binary to run its settings checks: fake one so the tests do not depend on the machine."""
    monkeypatch.setattr(doctor.procutil, "find_claude", lambda *a, **k: Path("/fake/claude"))
    real_run = doctor.procutil.run
    monkeypatch.setattr(
        doctor.procutil,
        "run",
        lambda cmd, **k: (
            subprocess.CompletedProcess(cmd, 0, "2.1.300 (Claude Code)", "")
            if str(cmd[0]).endswith("claude")
            else real_run(cmd, **k)
        ),
    )
    # ... and no Claude Code session of the developer's own may turn up in what doctor says about running processes
    from claude_hr import sysinfo

    monkeypatch.setattr(sysinfo, "process_table", lambda: {})


def checks(cwd: Path | None = None):
    return {c.name: c for c in doctor.run(config.load(), cwd or Path.cwd())}


def write_settings(env: dict):
    paths.claude_settings().write_text(json.dumps({"env": env}), encoding="utf-8")


def test_a_foreign_global_proxy_is_flagged_not_called_ok(sandbox, fake_claude, monkeypatch):
    """Headroom Desktop writes env.ANTHROPIC_BASE_URL (127.0.0.1:6767) into settings.json; that value replaces the one
    claude-hr sets per launch, so sessions silently bypass claude-hr's routing. doctor used to call this 'OK'."""
    monkeypatch.setattr(doctor.procutil, "http_json", lambda *a, **k: (200, {}))
    write_settings({"ANTHROPIC_BASE_URL": "http://127.0.0.1:6767"})
    got = checks()
    assert got["foreign proxy"].status == "warn"
    assert "6767" in got["foreign proxy"].detail and "overrides" in got["foreign proxy"].detail
    assert "global ANTHROPIC_BASE_URL" not in got


def test_claude_hrs_own_global_proxy_keeps_the_old_wording(sandbox, fake_claude, monkeypatch):
    port = Headroom(config.load()).port
    monkeypatch.setattr(doctor.procutil, "http_json", lambda *a, **k: (200, {}))
    write_settings({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"})
    got = checks()
    assert got["global ANTHROPIC_BASE_URL"].status == "ok" and "foreign proxy" not in got
    monkeypatch.setattr(doctor.procutil, "http_json", lambda *a, **k: (0, None))  # down
    assert checks()["global ANTHROPIC_BASE_URL"].status == "fail"


def test_a_dead_foreign_proxy_is_still_a_failure(sandbox, fake_claude, monkeypatch):
    monkeypatch.setattr(doctor.procutil, "http_json", lambda *a, **k: (0, None))
    write_settings({"ANTHROPIC_BASE_URL": "http://127.0.0.1:6767"})
    assert (
        checks()["global ANTHROPIC_BASE_URL"].status == "fail"
    )  # claude cannot reach the API at all


def test_is_own_proxy_parsing():
    assert doctor._is_own_proxy("http://127.0.0.1:8787", 8787)
    assert doctor._is_own_proxy("http://localhost:8787/", 8787)
    assert not doctor._is_own_proxy("http://127.0.0.1:6767", 8787)
    assert not doctor._is_own_proxy("https://gateway.example.com", 8787)
    assert not doctor._is_own_proxy("not a url", 8787)


def test_headroom_desktop_markers(sandbox, fake_claude, monkeypatch):
    monkeypatch.setattr(doctor.procutil, "port_listening", lambda *a, **k: False)
    assert "headroom desktop" not in checks()
    hook = paths.claude_home() / "hooks" / "headroom-rtk-rewrite.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    (paths.home() / ".zshrc").write_text(
        "export A=1\n# >>> headroom:routing >>>\nexport X=1\n", encoding="utf-8"
    )
    got = checks()["headroom desktop"]
    assert (
        got.status == "warn" and "headroom-rtk-rewrite.sh" in got.detail and ".zshrc" in got.detail
    )
    assert "not both route Claude" in got.detail or "should not both route Claude" in got.detail


def test_headroom_desktop_listener_on_6767_is_noticed_but_not_when_it_is_our_port(
    sandbox, fake_claude, monkeypatch
):
    monkeypatch.setattr(doctor.procutil, "port_listening", lambda host, port, *a, **k: port == 6767)
    assert "6767" in checks()["headroom desktop"].detail
    monkeypatch.setenv(
        "CLAUDE_HR_HEADROOM__PORT", "6767"
    )  # claude-hr itself uses 6767: not Desktop
    assert "headroom desktop" not in checks()


def test_missing_hook_binary_is_reported(sandbox, fake_claude, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name, *a, **k: None)
    got = checks()["hook binary"]
    assert got.status == "warn" and "claude-hr-hook" in got.detail and "update-shell" in got.fix
    monkeypatch.setattr(doctor.shutil, "which", lambda name, *a, **k: "/usr/bin/claude-hr-hook")
    assert "hook binary" not in checks()


def test_security_guidance_silently_skipping_reviews_is_reported(sandbox, fake_claude, monkeypatch):
    monkeypatch.setattr(
        doctor, "_security_guidance", lambda: {"skipped_reviews": 7, "completed_reviews": 0}
    )
    got = checks()["security-guidance"]
    assert got.status == "warn" and "skipped 7 time" in got.detail and "/security-review" in got.fix
    monkeypatch.setattr(
        doctor, "_security_guidance", lambda: {"skipped_reviews": 7, "completed_reviews": 3}
    )
    assert "security-guidance" not in checks()  # reviews do run for this user
    monkeypatch.setattr(doctor, "_security_guidance", lambda: None)
    assert "security-guidance" not in checks()  # plugin not installed / no log


def test_doctor_reads_the_security_guidance_log_from_the_claude_home(sandbox, fake_claude):
    """The wiring to footprint.security_guidance_health (skipped silently while that module is absent)."""
    pytest.importorskip("claude_hr.footprint")
    log = paths.claude_home() / "security" / "log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("LLM review disabled or no API credentials\n" * 3, encoding="utf-8")
    assert checks()["security-guidance"].status == "warn"


def test_verify_recipe_states_are_reported(sandbox, fake_claude, monkeypatch, tmp_path):
    from claude_hr import verify

    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    assert "verify recipe" not in checks(repo)  # no recipe: nothing to say
    recipe = repo / ".claude" / "hr-verify.toml"
    recipe.write_text('[[fast]]\nname = "t"\nargv = ["pytest", "-q"]\n', encoding="utf-8")
    got = checks(repo)["verify recipe"]
    assert got.status == "warn" and "has not been trusted" in got.detail and "terminal" in got.fix
    verify.trust(repo, paths.state_dir())
    assert checks(repo)["verify recipe"].status == "ok"
    recipe.write_text(recipe.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert "changed since you trusted it" in checks(repo)["verify recipe"].detail
    recipe.write_text('[[fast]]\nname = "t"\nargv = ["a && b"]\n', encoding="utf-8")
    assert "not valid" in checks(repo)["verify recipe"].detail
    monkeypatch.setenv("CLAUDE_HR_VERIFY__GATE", "false")
    assert "verify recipe" not in checks(repo)


def test_verify_recipe_check_uses_the_repository_root_not_the_cwd(sandbox, fake_claude, tmp_path):
    """Run from a subdirectory, doctor looked for <cwd>/.claude/hr-verify.toml, found none and said nothing, although the
    end-of-turn gate (git top level) does nothing for that untrusted recipe."""
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    (repo / "sub" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".claude" / "hr-verify.toml").write_text(
        '[[fast]]\nname = "t"\nargv = ["pytest", "-q"]\n', encoding="utf-8"
    )
    for cwd in (repo, repo / "sub", repo / "sub" / "deep"):
        got = checks(cwd)["verify recipe"]
        assert got.status == "warn" and "has not been trusted" in got.detail, cwd


def git_project(tmp_path, name="proj"):
    repo = tmp_path / name
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".claude" / "hr-verify.toml").write_text(
        '[[fast]]\nname = "t"\nargv = ["pytest", "-q"]\n', encoding="utf-8"
    )
    return repo


def test_a_state_folder_inside_the_repository_is_reported_not_called_trusted(
    sandbox, fake_claude, monkeypatch, tmp_path
):
    """The end-of-turn hook ignores a trust record that lies inside the repository (the repository could have written it):
    doctor said "trusted: the end-of-turn test gate runs it" while the hook did nothing, and nothing said why."""
    from claude_hr import verify

    repo = git_project(tmp_path)
    monkeypatch.setenv("CLAUDE_HR_HOME", str(repo / ".hr"))
    verify.trust(repo, paths.state_dir())
    assert verify.trust_state(repo, paths.state_dir()) == "trusted"
    for cwd in (repo, repo / ".claude"):
        got = checks(cwd)
        warn = got["verify state folder"]
        assert warn.status == "warn"
        assert warn.detail == (
            f"the state folder {paths.state_dir()} is inside this repository, so the end-of-turn hook "
            "ignores its trust record (set CLAUDE_HR_HOME outside the repository)"
        )
        assert "outside the repository" in warn.fix
        assert "verify recipe" not in got, (
            "no 'the gate runs it' for a recipe the hook will not run"
        )
    verify.untrust(repo, paths.state_dir())  # the recipe's own state is still reported next to it
    got = checks(repo)
    assert got["verify state folder"].status == "warn"
    assert "has not been trusted" in got["verify recipe"].detail


def test_a_state_folder_outside_the_repository_is_not_reported(
    sandbox, fake_claude, monkeypatch, tmp_path
):
    from claude_hr import verify

    repo = git_project(tmp_path)
    verify.trust(repo, paths.state_dir())
    got = checks(repo)
    assert "verify state folder" not in got and got["verify recipe"].status == "ok"
    # a twin whose name merely starts with the repository's is not inside it
    monkeypatch.setenv("CLAUDE_HR_HOME", str(repo) + "-home")
    verify.trust(repo, paths.state_dir())
    assert "verify state folder" not in checks(repo)


def test_a_state_folder_in_a_folder_without_git_or_with_the_gate_off_is_not_reported(
    sandbox, fake_claude, monkeypatch, tmp_path
):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("CLAUDE_HR_HOME", str(plain / ".hr"))
    assert "verify state folder" not in checks(plain), "the hook only runs in a git work tree"
    repo = git_project(tmp_path)
    monkeypatch.setenv("CLAUDE_HR_HOME", str(repo / ".hr"))
    assert "verify state folder" in checks(repo)
    monkeypatch.setenv("CLAUDE_HR_VERIFY__GATE", "false")
    assert "verify state folder" not in checks(repo)


def test_the_state_folder_check_never_breaks_doctor(sandbox, fake_claude, monkeypatch, tmp_path):
    repo = git_project(tmp_path)

    def boom(cwd):
        raise OSError("no git here")

    monkeypatch.setattr("claude_hr.worktree.repo_root", boom)
    assert doctor.state_inside_repo(repo) is False
    assert "platform" in checks(repo)


# ---- unparsable files: reported as failed checks, never a traceback -------------------------------------------------
def test_an_unparsable_claude_json_is_a_failed_check_not_a_crash(sandbox, fake_claude):
    paths.claude_global_json().write_text(
        '{"mcpServers": {"headroom": {"command": "x"', encoding="utf-8"
    )
    got = checks()
    assert (
        got[".claude.json"].status == "fail" and "refusing to modify" in got[".claude.json"].detail
    )
    assert (
        "credentials" in got
    )  # the credential scan reads the same file: reported as skipped, not raised


def test_an_unparsable_settings_merge_record_is_a_failed_check_not_a_crash(sandbox, fake_claude):
    f = paths.state_dir() / "settings-merge.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"leaves": {"a": 1', encoding="utf-8")
    got = checks()["managed settings"]
    assert got.status == "fail" and "settings-merge.json" in got.detail and "setup" in got.fix


def test_ctl_doctor_exits_one_instead_of_crashing_on_unparsable_files(sandbox, fake_claude, capsys):
    from claude_hr import ctl

    paths.claude_global_json().write_text("{ nope", encoding="utf-8")
    paths.claude_settings().write_text("{ nope", encoding="utf-8")
    state = paths.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    (state / "settings-merge.json").write_text("{ nope", encoding="utf-8")
    assert ctl._run(["doctor"]) == 1
    text = capsys.readouterr().out
    assert ".claude.json" in text and "settings.json" in text and "managed settings" in text


# ---- the managed baseline: stale after an unattended self-update, or changed since setup ----------------------------
def _apply_baseline(cfg=None):
    from claude_hr import claude_setup
    from claude_hr.headroom import Headroom

    cfg = cfg or config.load()
    step = claude_setup.ClaudeSetup(cfg, Headroom(cfg), None).apply_settings()
    assert step.ok, step.lines
    # every release's `setup` also leaves this marker; it is what makes the machine "set up" (uninstall removes it)
    (paths.state_dir() / "install.json").write_text(json.dumps({"version": "0.1.3"}), "utf-8")


def managed():
    """Just the 'managed settings' check (a whole doctor run takes seconds)."""
    cfg = config.load()
    return doctor._managed_settings_check(cfg, Headroom(cfg), None)


def test_a_baseline_applied_by_another_release_is_flagged_with_the_fix(sandbox, fake_claude):
    """0.1.x machine that self-updated to 0.2.0: the new settings were never applied, and doctor said 'OK managed settings'."""
    from claude_hr import claude_setup

    paths.claude_settings().write_text(
        json.dumps({"autoUpdatesChannel": "latest"}), encoding="utf-8"
    )
    _apply_baseline()  # what a release before 0.2.0 left behind: ownership recorded, no applied-version file
    got = checks()["managed settings"]
    assert (
        got.status == "warn" and "before 0.2.0" in got.detail and "claude-hr-ctl setup" in got.fix
    )
    assert "plugins" in got.fix  # the plugin set (hr-gates) is part of what the new release adds
    claude_setup.record_applied_version()
    assert (
        checks()["managed settings"].status == "ok"
    )  # a value the user chose (kept) is never counted as pending
    (paths.state_dir() / "applied-version.json").write_text(
        json.dumps({"settings": "0.1.3", "config_dir": str(paths.claude_settings())}),
        encoding="utf-8",
    )
    got = checks()["managed settings"]
    assert got.status == "warn" and "0.1.3" in got.detail


def test_a_baseline_that_setup_would_still_change_is_flagged(sandbox, fake_claude, monkeypatch):
    from claude_hr import claude_setup

    monkeypatch.setenv("CLAUDE_HR_GATES__PROTECT_CONFIG", "false")
    _apply_baseline(config.load())
    claude_setup.record_applied_version()
    assert checks()["managed settings"].status == "ok"
    monkeypatch.delenv(
        "CLAUDE_HR_GATES__PROTECT_CONFIG"
    )  # config changed since (or the release added the rules)
    got = checks()["managed settings"]
    assert got.status == "warn" and "not applied yet" in got.detail
    assert got.fix == "claude-hr-ctl setup --only settings"


def test_the_check_reads_the_settings_entry_of_the_per_step_record(sandbox, fake_claude):
    """`setup --only plugins` records only the plugins step: the settings baseline is still unapplied for this release."""
    from claude_hr import claude_setup

    _apply_baseline()
    claude_setup.record_applied_version("plugins", "claude-md")
    got = managed()
    assert got.status == "warn" and "before 0.2.0" in got.detail and "plugins" in got.fix
    claude_setup.record_applied_version("settings")
    assert managed().status == "ok"


@pytest.mark.parametrize(
    "var, then, retired",
    [
        # a change that REMOVES a managed key: nothing is "set", yet `setup --only settings` would change the file
        ("CLAUDE_HR_SECURITY__STOP_REVIEW", "plugin-default", "env.ENABLE_STOP_REVIEW"),
        ("CLAUDE_HR_GATES__PROTECT_CONFIG", "false", "permissions.deny"),  # + the ask rules
        ("CLAUDE_HR_CREDENTIALS__BLOCK_SECRET_READS", "false", "permissions.deny"),
    ],
)
def test_a_config_change_that_retires_a_managed_key_is_flagged(
    sandbox, fake_claude, monkeypatch, var, then, retired
):
    from claude_hr import claude_setup
    from claude_hr.headroom import Headroom

    _apply_baseline()
    claude_setup.record_applied_version("settings")
    assert managed().status == "ok"
    monkeypatch.setenv(var, then)
    got = managed()
    assert got.status == "warn" and "to retire" in got.detail and retired in got.detail, got
    assert got.fix == "claude-hr-ctl setup --only settings"
    cfg = config.load()  # ... and the fix doctor names does clear it
    step = claude_setup.ClaudeSetup(cfg, Headroom(cfg), None).apply_settings()
    assert step.ok
    assert managed().status == "ok"


def test_a_baseline_applied_to_another_claude_config_dir_is_flagged(
    sandbox, fake_claude, monkeypatch, tmp_path
):
    """`setup` ran in a shell with CLAUDE_CONFIG_DIR=<custom>; doctor runs in one without it. The scheduled update job is in
    the second situation: it leaves that profile alone, and doctor says to run setup from the right shell."""
    from claude_hr import claude_setup

    custom = tmp_path / "custom"
    custom.mkdir()
    default = paths.claude_home()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    _apply_baseline()
    claude_setup.record_applied_version("settings", "claude-md")
    assert managed().status == "ok"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(default))
    got = managed()
    assert got.status == "warn" and str(custom) in got.detail and "daily update job" in got.detail
    assert "claude-hr-ctl setup" in got.fix and "plugins" in got.fix


def test_doctor_does_not_touch_the_settings_it_inspects(sandbox, fake_claude):
    _apply_baseline()
    before = paths.claude_settings().read_bytes()
    state_before = (paths.state_dir() / "settings-merge.json").read_bytes()
    checks()
    assert paths.claude_settings().read_bytes() == before
    assert (paths.state_dir() / "settings-merge.json").read_bytes() == state_before


# ---- plugins and the footprint budget --------------------------------------------------------------------------------
STANDARD_WITHOUT_HEADROOM = (
    "hr-core",
    "hr-gates",
    "claude-md-management",
    "security-guidance",
    "context7",
)


def _listing(names):
    return [{"id": f"{n}@m", "scope": "user", "enabled": True} for n in names]


def test_plugins_check_does_not_ask_for_a_plugin_that_sync_will_not_install(
    sandbox, fake_claude, monkeypatch
):
    """headroom.install_plugins = false: `plugins sync` skips headroom-snip, so 'missing: headroom-snip' could never clear."""
    from claude_hr.claude_setup import ClaudeSetup

    monkeypatch.setattr(
        ClaudeSetup, "_installed_plugins", lambda self: _listing(STANDARD_WITHOUT_HEADROOM)
    )
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__INSTALL_PLUGINS", "false")
    got = checks()["plugins"]
    assert got.status == "ok" and "5/5" in got.detail and not got.fix
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__INSTALL_PLUGINS", "true")
    got = checks()["plugins"]
    assert got.status == "warn" and "headroom-snip" in got.fix and "5/6" in got.detail
    # the unattended update never installs plugins: a missing one is doctor's to report, `plugins sync` is the fix
    assert got.fix.startswith("claude-hr-ctl plugins sync")


def test_footprint_budget_is_checked_from_the_plugin_list_doctor_already_fetched(
    sandbox, fake_claude, monkeypatch
):
    from claude_hr import footprint
    from claude_hr.claude_setup import ClaudeSetup

    asked = []
    monkeypatch.setattr(
        ClaudeSetup, "_installed_plugins", lambda self: asked.append(1) or _listing(["x"])
    )
    monkeypatch.setattr(footprint, "scan_installed", lambda listing: ["fp"])
    monkeypatch.setattr(footprint, "always_on_tokens", lambda fps: 2600)
    got = checks()["footprint"]
    assert (
        got.status == "warn"
        and "OVER by 1,100" in got.detail
        and "claude-hr-ctl footprint" in got.fix
    )
    assert asked == [
        1
    ]  # one `claude plugin list` call serves both the plugins check and the budget
    monkeypatch.setattr(footprint, "always_on_tokens", lambda fps: 900)
    assert checks()["footprint"].status == "ok"
    monkeypatch.setenv("CLAUDE_HR_FOOTPRINT__BUDGET_STANDARD_TOKENS", "500")
    assert checks()["footprint"].status == "warn"  # the budget is configurable
    monkeypatch.delenv("CLAUDE_HR_FOOTPRINT__BUDGET_STANDARD_TOKENS")
    monkeypatch.setenv("CLAUDE_HR_PLUGINS__PROFILE", "full")
    assert "footprint" not in checks()  # other profiles add plugins on purpose: no budget
    monkeypatch.delenv("CLAUDE_HR_PLUGINS__PROFILE")
    monkeypatch.setattr(ClaudeSetup, "_installed_plugins", lambda self: [])
    assert "footprint" not in checks()  # nothing was measured: no verdict either way


def test_footprint_check_fails_open(sandbox, fake_claude, monkeypatch):
    from claude_hr import footprint
    from claude_hr.claude_setup import ClaudeSetup

    monkeypatch.setattr(ClaudeSetup, "_installed_plugins", lambda self: _listing(["x"]))
    monkeypatch.setattr(
        footprint, "scan_installed", lambda listing: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    got = checks()
    assert "footprint" not in got and "plugins" in got


@pytest.mark.parametrize("value", ["of", "disable"])
def test_a_mistyped_stop_review_value_is_reported(sandbox, fake_claude, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_HR_SECURITY__STOP_REVIEW", value)
    got = checks()["security.stop_review"]
    assert got.status == "warn" and "stays ON" in got.detail and "off" in got.fix


@pytest.mark.parametrize("value", ["off", "Off", "false", "0", "plugin-default"])
def test_recognised_stop_review_values_are_not_reported(sandbox, fake_claude, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_HR_SECURITY__STOP_REVIEW", value)
    assert "security.stop_review" not in checks()


# ---- a running proxy that was started with other settings than the configuration asks for now ----------------------

RESTART = "apply them when no Claude session is running: claude-hr-ctl headroom restart"


@pytest.fixture
def healthy_proxy(sandbox, fake_claude, monkeypatch):
    """A proxy that answers /health, whatever started it: doctor takes it from here."""
    from claude_hr.headroom import Health

    monkeypatch.setenv("CLAUDE_HR_HEADROOM__FOOTPRINT", "full")
    monkeypatch.setattr(Headroom, "binary", lambda self: Path("/fake/headroom"))
    monkeypatch.setattr(Headroom, "version", lambda self: "0.41.0")
    monkeypatch.setattr(
        Headroom, "health", lambda self, timeout=0.6: Health(True, "healthy", "0.41.0")
    )
    monkeypatch.setattr(Headroom, "_listener_pids", lambda self: set())


def record_start(**fields):
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    rec = {"pid": 1234, "port": 8787, "started": 1.0, **fields}
    (paths.state_dir() / "headroom-proxy.json").write_text(json.dumps(rec), encoding="utf-8")


def test_a_proxy_started_with_older_settings_is_a_warning_with_the_restart(healthy_proxy):
    record_start(args="0123456789ab")  # what another configuration hashed to
    got = checks()
    assert got["headroom proxy"].status == "ok"
    warn = got["headroom settings"]
    assert warn.status == "warn"  # a working proxy is never a failure
    assert warn.fix == f"Headroom is running with older settings; {RESTART}"
    assert "Headroom is running with older settings; " + RESTART in warn.line()


def test_a_proxy_started_by_an_older_claude_hr_is_a_warning_too(healthy_proxy):
    record_start()  # 0.2.0 recorded pid, port and the start time only
    warn = checks()["headroom settings"]
    assert warn.status == "warn"
    assert warn.fix == f"Headroom was started by an older claude-hr; {RESTART}"


def test_a_proxy_started_with_the_current_settings_is_not_warned_about(healthy_proxy):
    record_start(args=Headroom(config.load()).settings_hash())
    got = checks()
    assert got["headroom proxy"].status == "ok" and "headroom settings" not in got


def test_a_proxy_claude_hr_has_no_record_of_is_not_judged(healthy_proxy):
    assert (
        "headroom settings" not in checks()
    )  # somebody else's proxy on the port: nothing to compare


def test_changing_a_setting_after_the_start_is_noticed(healthy_proxy, monkeypatch):
    record_start(args=Headroom(config.load()).settings_hash())
    assert "headroom settings" not in checks()
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__OUTPUT_SHAPER", "true")
    assert checks()["headroom settings"].status == "warn"


def test_a_proxy_that_is_not_serving_is_not_compared(healthy_proxy, monkeypatch):
    from claude_hr.headroom import Health

    record_start()
    monkeypatch.setattr(Headroom, "health", lambda self, timeout=0.6: Health(False, "down"))
    got = checks()
    assert got["headroom proxy"].status == "warn" and "headroom settings" not in got


def test_the_settings_comparison_never_breaks_doctor(healthy_proxy, monkeypatch):
    def boom(self):
        raise RuntimeError("cannot compare")

    monkeypatch.setattr(Headroom, "settings_state", boom)
    got = checks()
    assert got["headroom proxy"].status == "ok" and "headroom settings" not in got


# ---- doctor: the update channel, and Claude Code processes claude-hr did not start ---------------------------------------
def write_channel_settings(value):
    paths.claude_settings().write_text(json.dumps({"autoUpdatesChannel": value}), encoding="utf-8")


def test_doctor_names_the_claude_update_channel_in_force(sandbox, fake_claude):
    got = checks()
    assert got["claude update channel"].status == "info"
    assert got["claude update channel"].detail == (
        "stable (claude.channel=auto, settings.json autoUpdatesChannel=unset)"
    )
    write_channel_settings("latest")
    got = checks()
    assert got["claude update channel"].detail == (
        "latest (claude.channel=auto, settings.json autoUpdatesChannel=latest)"
    )
    assert "claude channel conflict" not in got


def test_doctor_warns_when_an_explicit_channel_disagrees_with_the_users_own(
    sandbox, fake_claude, monkeypatch
):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__CHANNEL", "stable")
    write_channel_settings("latest")
    got = checks()
    assert got["claude update channel"].detail == (
        "stable (claude.channel=stable, settings.json autoUpdatesChannel=latest)"
    )
    warn = got["claude channel conflict"]
    assert warn.status == "warn"
    assert (
        'claude.channel is "stable" but autoUpdatesChannel in settings.json is "latest"'
        in warn.detail
    )
    assert "claude.channel auto" in warn.fix
    # the same explicit channel on both sides, or no setting of the user's: nothing to warn about
    write_channel_settings("stable")
    assert "claude channel conflict" not in checks()
    paths.claude_settings().unlink()
    assert "claude channel conflict" not in checks()


def test_the_auto_channel_never_conflicts_even_with_a_value_it_does_not_know(sandbox, fake_claude):
    write_channel_settings("beta")
    got = checks()
    assert got["claude update channel"].detail == (
        "stable (claude.channel=auto, settings.json autoUpdatesChannel=beta)"
    )
    assert "claude channel conflict" not in got


def test_doctor_says_nothing_about_a_channel_claude_hr_does_not_manage(
    sandbox, fake_claude, monkeypatch
):
    monkeypatch.setenv("CLAUDE_HR_CLAUDE__MANAGE_UPDATES", "false")
    write_channel_settings("latest")
    got = checks()
    assert "claude update channel" not in got and "claude channel conflict" not in got


def test_doctor_survives_a_settings_file_it_cannot_parse_with_the_channel_line_in_place(
    sandbox, fake_claude
):
    paths.claude_settings().write_text("{ not json", encoding="utf-8")
    got = checks()
    assert got["settings.json"].status == "fail"
    assert got["claude update channel"].detail.endswith("autoUpdatesChannel=unset)")


def test_doctor_notes_claude_code_processes_that_claude_hr_did_not_start(
    sandbox, fake_claude, monkeypatch
):
    from claude_hr import resources, sysinfo

    table = {4100: (1, "claude.exe"), 4200: (1, "claude"), 4300: (1, "node.exe")}
    monkeypatch.setattr(sysinfo, "process_table", lambda: table)
    got = checks()
    c = got["claude outside claude-hr"]
    assert c.status == "info"
    lines = c.detail.splitlines()
    assert lines[0] == (
        "2 Claude Code process(es) not started by claude-hr are running; their traffic does not go through Headroom "
        "(no compression, no session limits, not seen by the update guard)"
    )
    assert "type `claude-hr` instead of `claude`" in lines[1]
    assert "claude-hr-ctl shim install" in lines[2]
    # one that claude-hr started (it is in the session registry) is not one of them
    monkeypatch.setattr(resources, "live_sessions", lambda: [{"pid": 4100}])
    assert checks()["claude outside claude-hr"].detail.startswith("1 Claude Code process(es)")
    # nothing running, or Headroom switched off (there is no routing to miss): no line
    monkeypatch.setattr(sysinfo, "process_table", lambda: {})
    assert "claude outside claude-hr" not in checks()
    monkeypatch.setattr(sysinfo, "process_table", lambda: table)
    monkeypatch.setenv("CLAUDE_HR_HEADROOM__MODE", "off")
    assert "claude outside claude-hr" not in checks()


def test_the_process_probe_never_breaks_doctor(sandbox, fake_claude, monkeypatch):
    from claude_hr import sysinfo

    def boom():
        raise OSError("no snapshot")

    monkeypatch.setattr(sysinfo, "process_table", boom)
    got = checks()
    assert "claude outside claude-hr" not in got and "platform" in got
