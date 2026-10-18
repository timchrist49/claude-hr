"""The ~/.claude size alert says when the size is a lower bound and when cleanup frees little."""

from __future__ import annotations

from claude_hr import config, monitor


# ---- the ~/.claude size alert ---------------------------------------------------------------------------------------------
def snap(**over):
    base = {
        "disk_free_gb": 100,
        "ram_total_gb": 64,
        "ram_free_gb": 30,
        "sessions": 0,
        "claude_dir_gb": 21.11,
        "proxy": {"enabled": False, "ok": True, "status": "off"},
    }
    return {**base, **over}


def test_the_size_alert_says_when_the_size_is_a_lower_bound_and_when_cleanup_frees_little(sandbox):
    cfg = config.load()
    complete = [
        a for a in monitor.evaluate(cfg, snap(claude_dir_complete=True)) if a.key == "claude-dir"
    ][0]
    assert "21.11 GB." in complete.body and "at least" not in complete.body
    cut_short = [
        a for a in monitor.evaluate(cfg, snap(claude_dir_complete=False)) if a.key == "claude-dir"
    ][0]
    assert "at least 21.11 GB" in cut_short.body and "ran out of time" in cut_short.body
    for alert in (complete, cut_short):
        assert (
            "14 days" in alert.body
            and "jobs_retention_days" in alert.body
            and "may free little" in alert.body
        )
    # a snapshot written before this field existed still evaluates as a complete scan
    assert [a for a in monitor.evaluate(cfg, snap()) if a.key == "claude-dir"]


def test_the_size_info_remembers_whether_the_scan_finished(sandbox, monkeypatch):
    monkeypatch.setattr(
        monitor.sysinfo, "dir_size_info", lambda root, budget_s=20.0: (21.11, False)
    )
    assert monitor._claude_dir_info() == (21.11, False)
    monkeypatch.setattr(
        monitor.sysinfo, "dir_size_info", lambda root, budget_s=20.0: (1.0, True)
    )  # cached for 6 hours
    assert monitor._claude_dir_info() == (21.11, False) and monitor._claude_dir_gb() == 21.11


def test_a_snapshot_without_the_scan_flag_reads_as_a_complete_scan(sandbox):
    alert = [a for a in monitor.evaluate(config.load(), snap()) if a.key == "claude-dir"][0]
    assert "at least" not in alert.body and "21.11 GB." in alert.body
