"""The plan meter as cash (planmeter.py) and its use in doctor, status and the status line."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from claude_hr import config, doctor, hooks, paths, planmeter

FULL_WINDOW = {
    "five_hour": {"utilization_pct": 0.0, "resets_at": "2026-10-17T19:20:00Z"},
    "seven_day": {"utilization_pct": 100.0, "resets_at": "2026-10-21T01:00:00Z"},
    "extra_usage": {"is_enabled": True, "monthly_limit_usd": 1000.0, "used_credits_usd": 687.1},
    "polled_at": "2026-10-17T20:28:50Z",
}


def meter(**over):
    latest = json.loads(json.dumps(FULL_WINDOW))
    for key, value in over.items():
        latest[key] = value
    return planmeter.parse(latest)


def test_parse_reads_the_poll_and_ignores_what_is_not_usable():
    m = planmeter.parse(FULL_WINDOW)
    assert (m["five_hour"], m["seven_day"], m["extra_used"], m["extra_limit"]) == (
        0.0,
        100.0,
        687.1,
        1000.0,
    )
    assert m["extra_enabled"] is True and m["resets_at"] == "2026-10-21T01:00:00Z"
    for junk in (
        None,
        [],
        "x",
        5,
        {},
        {"five_hour": "high"},
        {"seven_day": {"utilization_pct": True}},
    ):
        assert planmeter.parse(junk) is None
    # a NaN or a wrong type in one field only loses that field
    partial = planmeter.parse(
        {"seven_day": {"utilization_pct": float("nan")}, "five_hour": {"utilization_pct": 7}}
    )
    assert partial["seven_day"] is None and partial["five_hour"] == 7.0
    # a zero or negative monthly limit is no limit
    assert (
        meter(extra_usage={"is_enabled": True, "monthly_limit_usd": 0, "used_credits_usd": 5})[
            "extra_limit"
        ]
        is None
    )


def test_the_line_says_what_the_window_and_the_cash_are():
    later = datetime(2026, 10, 18, 3, 0, tzinfo=UTC)
    text = planmeter.line(planmeter.parse(FULL_WINDOW), later)
    assert "5h 0%" in text and "7d 100% (resets 2026-10-21 01:00 UTC)" in text
    assert "extra usage $687.10 of $1,000 (69%)" in text
    assert (
        "[as of 2026-10-17 20:28 UTC]" in text
    )  # the poll is hours old: it is dated, not presented as now
    quiet = planmeter.line(
        meter(seven_day={"utilization_pct": 12.5}, extra_usage={"is_enabled": False}), later
    )
    assert "7d 12.5%" in quiet and "resets" not in quiet and "extra usage" not in quiet


def test_a_fresh_poll_is_not_dated():
    now = datetime(2026, 10, 17, 20, 30, tzinfo=UTC)
    assert "as of" not in planmeter.line(planmeter.parse(FULL_WINDOW), now)
    assert planmeter.age_minutes(planmeter.parse(FULL_WINDOW), now) == pytest.approx(1.17, abs=0.01)
    assert planmeter.age_minutes({"polled_at": "garbage"}) is None
    assert planmeter.age_minutes({}) is None


@pytest.mark.parametrize(
    ("changes", "level", "needle"),
    [
        ({}, "warn", "7-day window is full"),  # full window + extra usage on: every token is billed
        (
            {
                "seven_day": {"utilization_pct": 40},
                "extra_usage": {
                    "is_enabled": True,
                    "monthly_limit_usd": 1000,
                    "used_credits_usd": 850,
                },
            },
            "warn",
            "monthly limit",
        ),
        (
            {"seven_day": {"utilization_pct": 94}, "extra_usage": {"is_enabled": False}},
            "info",
            "94%",
        ),
        (
            {
                "seven_day": {"utilization_pct": 40},
                "extra_usage": {
                    "is_enabled": True,
                    "monthly_limit_usd": 1000,
                    "used_credits_usd": 12,
                },
            },
            "info",
            "extra usage has been spent",
        ),
    ],
)
def test_the_warning_levels(changes, level, needle):
    flag = planmeter.warning(meter(**changes))
    assert flag and flag[0] == level and needle in flag[1]


def test_no_warning_when_there_is_room_and_nothing_was_spent():
    assert (
        planmeter.warning(
            meter(
                seven_day={"utilization_pct": 30},
                extra_usage={"is_enabled": True, "monthly_limit_usd": 1000, "used_credits_usd": 0},
            )
        )
        is None
    )
    # extra usage that is switched off is never "cash", even with a stale number in it
    off = meter(
        seven_day={"utilization_pct": 100},
        extra_usage={"is_enabled": False, "monthly_limit_usd": 1000, "used_credits_usd": 50},
    )
    flag = planmeter.warning(off)
    assert flag is None or flag[0] == "info"


def test_read_local_is_none_without_a_poll_and_never_raises(sandbox):
    assert planmeter.read_local() is None
    folder = paths.hr_home() / "headroom"
    folder.mkdir(parents=True)
    (folder / "subscription_state.json").write_text("{not json", encoding="utf-8")
    assert planmeter.read_local() is None
    (folder / "subscription_state.json").write_text(
        json.dumps({"latest": FULL_WINDOW}), encoding="utf-8"
    )
    assert planmeter.read_local()["extra_used"] == 687.1


def test_doctor_warns_when_the_window_is_full_and_extra_usage_is_being_spent(sandbox, monkeypatch):
    monkeypatch.setattr(planmeter, "read_local", lambda: planmeter.parse(FULL_WINDOW))
    check = doctor._plan_meter_check()
    assert check.status == "warn" and "extra usage $687.10 of $1,000" in check.detail
    assert "billed at API prices" in check.detail and "defer heavy workflows" in check.fix
    monkeypatch.setattr(
        planmeter,
        "read_local",
        lambda: meter(seven_day={"utilization_pct": 20}, extra_usage={"is_enabled": False}),
    )
    assert doctor._plan_meter_check().status == "ok"
    monkeypatch.setattr(planmeter, "read_local", lambda: None)
    assert doctor._plan_meter_check() is None


def test_the_status_line_shows_the_window_and_the_cash_only_when_they_matter(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    summary = {"saved": 0, "five_hour": 3.0, "seven_day": 100.0, "extra_used": 687.1}
    monkeypatch.setattr(hooks, "_headroom_summary", lambda port, named=(): summary)
    parts: list[str] = []
    hooks._statusline_parts({}, parts)
    assert "5h 3%" in parts and "7d 100%" in parts and "extra $687" in parts
    summary.update(seven_day=40.0, extra_used=687.1)
    parts = []
    hooks._statusline_parts({}, parts)
    assert "5h 3%" in parts and not [p for p in parts if p.startswith(("7d", "extra"))]
    summary.update(seven_day=None, extra_used=None)
    parts = []
    hooks._statusline_parts({}, parts)  # an old cache file without the new fields still renders
    assert parts == ["5h 3%"]


def test_the_status_line_cache_carries_the_new_fields(sandbox, monkeypatch):
    body = {"subscription_window": {"latest": FULL_WINDOW}, "savings": {"total_tokens": 5}}
    monkeypatch.setattr(hooks.procutil, "http_json", lambda url, timeout=0.4: (200, body))
    got = hooks._headroom_summary(8787)
    assert got["seven_day"] == 100.0 and got["extra_used"] == 687.1 and got["five_hour"] == 0.0
    off = {
        "subscription_window": {
            "latest": {**FULL_WINDOW, "extra_usage": {"is_enabled": False, "used_credits_usd": 9}}
        }
    }
    monkeypatch.setattr(hooks.procutil, "http_json", lambda url, timeout=0.4: (200, off))
    paths.state_dir().joinpath("statusline-headroom.json").unlink(missing_ok=True)
    assert hooks._headroom_summary(8787)["extra_used"] is None


def test_status_prints_the_cash_line_and_the_warning(sandbox, monkeypatch, capsys):
    from claude_hr import ctl

    monkeypatch.setattr(ctl.planmeter, "read_local", lambda: planmeter.parse(FULL_WINDOW))
    monkeypatch.setattr(
        ctl.Headroom,
        "health_patient",
        lambda self: type("H", (), {"ok": False, "status": "down", "version": ""})(),
    )
    monkeypatch.setattr(ctl.usage, "collect", lambda *a, **k: ctl.usage.Ledger(1))
    ctl.cmd_status(None, config.load())
    out = capsys.readouterr().out
    assert "Extra usage: $687.10 of $1,000 (69%) this month" in out
    assert "WARNING" in out and "7-day window is full" in out


def test_extra_usage_that_is_switched_off_is_never_shown_as_cash():
    off = meter(
        extra_usage={"is_enabled": False, "monthly_limit_usd": 1000, "used_credits_usd": 50}
    )
    assert planmeter.extra_text(off) == ""
    assert "extra usage" not in planmeter.line(off)


def test_doctor_reports_a_nearly_full_window_as_info_not_warn(sandbox, monkeypatch):
    nearly = meter(seven_day={"utilization_pct": 94}, extra_usage={"is_enabled": False})
    monkeypatch.setattr(planmeter, "read_local", lambda: nearly)
    check = doctor._plan_meter_check()
    assert check.status == "info" and "94%" in check.detail


def test_status_stays_calm_for_a_nearly_full_window(sandbox, monkeypatch, capsys):
    from claude_hr import ctl

    nearly = meter(seven_day={"utilization_pct": 94}, extra_usage={"is_enabled": False})
    monkeypatch.setattr(ctl.planmeter, "read_local", lambda: nearly)
    monkeypatch.setattr(
        ctl.Headroom,
        "health_patient",
        lambda self: type("H", (), {"ok": False, "status": "down", "version": ""})(),
    )
    monkeypatch.setattr(ctl.usage, "collect", lambda *a, **k: ctl.usage.Ledger(1))
    ctl.cmd_status(None, config.load())
    out = capsys.readouterr().out
    assert "WARNING" not in out and "Extra usage" not in out


@pytest.mark.parametrize(
    ("seven", "extra", "shows_window", "shows_cash"),
    [
        (79, 500.0, False, False),
        (80, 500.0, True, False),
        (99, 500.0, True, False),  # extra usage is only cash once the window is full
        (100, 500.0, True, True),
        (100, None, True, False),
        (100, 0, True, False),
    ],
)
def test_the_status_line_thresholds(monkeypatch, seven, extra, shows_window, shows_cash):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787")
    summary = {"saved": 0, "five_hour": 3.0, "seven_day": float(seven), "extra_used": extra}
    monkeypatch.setattr(hooks, "_headroom_summary", lambda port, named=(): summary)
    parts: list[str] = []
    hooks._statusline_parts({}, parts)
    assert any(p.startswith("7d") for p in parts) is shows_window
    assert any(p.startswith("extra") for p in parts) is shows_cash


def test_the_reset_time_is_shown_only_when_the_window_is_nearly_full():
    reset = {"resets_at": "2026-10-21T01:00:00Z"}
    off = {"is_enabled": False}
    low = planmeter.line(meter(seven_day={**reset, "utilization_pct": 89.9}, extra_usage=off))
    high = planmeter.line(meter(seven_day={**reset, "utilization_pct": 90}, extra_usage=off))
    assert "resets" not in low and "(resets 2026-10-21 01:00 UTC)" in high
