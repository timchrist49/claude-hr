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
    monkeypatch.setattr(doctor, "_security_guidance", lambda log: {"skipped": 7, "reviews": 0})
    got = checks()["security-guidance"]
    assert got.status == "warn" and "skipped 7 time" in got.detail and "/security-review" in got.fix
    monkeypatch.setattr(doctor, "_security_guidance", lambda log: {"skipped": 7, "reviews": 3})
    assert "security-guidance" not in checks()  # reviews do run for this user
    monkeypatch.setattr(doctor, "_security_guidance", lambda log: None)
    assert "security-guidance" not in checks()  # plugin not installed / no log


def test_doctor_reads_the_security_guidance_log_from_the_claude_home(sandbox, fake_claude):
    """The wiring to footprint.security_guidance_health (skipped silently while that module is absent)."""
    pytest.importorskip("claude_hr.footprint")
    log = paths.claude_home() / "security" / "log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("LLM review disabled or no API credentials\n" * 3, encoding="utf-8")
    assert checks()["security-guidance"].status == "warn"
