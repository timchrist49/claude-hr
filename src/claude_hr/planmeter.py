"""The plan meter as cash: how full the subscription windows are and how much extra usage has been spent.

Headroom polls Anthropic's usage endpoint every few minutes and keeps the answer in <claude-hr home>/headroom/
subscription_state.json (the same object is `subscription_window.latest` in its /stats). Once the 7-day window is full, extra
usage (a monthly credit limit the account holder sets) is billed at API list prices, so every further token is real money.
This module turns that poll into one line and one warning for `status`, `doctor` and the status line. Pure functions over
the poll plus a local file read: no network, and nothing here raises."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from . import paths
from .jsonio import read_json

STALE_MINUTES = 30  # a poll older than this is shown with its time instead of as "now"
FULL = 100.0  # a window at or above this percentage accepts no more plan usage
NEAR = 90.0
LIMIT_SHARE = 0.8  # extra usage at this share of its monthly limit is a warning on its own


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def parse(latest: Any) -> dict | None:
    """The meter from the poll object (`latest`), or None when it carries nothing usable. A wrongly typed field is absent."""
    five = _num(_dig(latest, "five_hour", "utilization_pct"))
    seven = _num(_dig(latest, "seven_day", "utilization_pct"))
    extra = _dig(latest, "extra_usage")
    enabled = isinstance(extra, dict) and extra.get("is_enabled") is True
    used = _num(_dig(extra, "used_credits_usd"))
    limit = _num(_dig(extra, "monthly_limit_usd"))
    if five is None and seven is None and used is None:
        return None
    resets = _dig(latest, "seven_day", "resets_at")
    polled = _dig(latest, "polled_at")
    return {
        "five_hour": five,
        "seven_day": seven,
        "resets_at": resets if isinstance(resets, str) else "",
        "extra_enabled": enabled,
        "extra_used": used,
        "extra_limit": limit if limit and limit > 0 else None,
        "polled_at": polled if isinstance(polled, str) else "",
    }


def read_local() -> dict | None:
    """The meter from the file Headroom keeps, or None (no proxy yet, unreadable file, nothing usable)."""
    try:
        state = read_json(paths.hr_home() / "headroom" / "subscription_state.json", {})
        return parse(state.get("latest")) if isinstance(state, dict) else None
    except Exception:  # noqa: BLE001 - an optional readout must never break status or doctor
        return None


def _when(iso: str) -> str:
    """2026-10-21T01:00:00Z -> '2026-10-21 01:00 UTC' (anything else is returned as it is, cut short)."""
    if len(iso) >= 16 and iso[10] == "T":
        return f"{iso[:10]} {iso[11:16]} UTC"
    return iso[:32]


def age_minutes(meter: dict, now: datetime | None = None) -> float | None:
    """Minutes since the poll, or None when it carries no readable time."""
    text = meter.get("polled_at") or ""
    try:
        polled = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if polled.tzinfo is None:
            polled = polled.replace(tzinfo=UTC)
        return max(0.0, ((now or datetime.now(UTC)) - polled).total_seconds() / 60)
    except (ValueError, OverflowError):
        return None


def extra_text(meter: dict) -> str:
    """'$687.10 of $1,000 (69%)', or '' when extra usage is off or unreadable."""
    used = meter.get("extra_used")
    if not meter.get("extra_enabled") or used is None:
        return ""
    limit = meter.get("extra_limit")
    if limit:
        return f"${used:,.2f} of ${limit:,.0f} ({used / limit * 100:.0f}%)"
    return f"${used:,.2f}"


def line(meter: dict, now: datetime | None = None) -> str:
    """One line: '5h 0% | 7d 100% (resets 2026-10-21 01:00 UTC) | extra usage $687.10 of $1,000 (69%)'; a poll older than
    STALE_MINUTES gets '[as of <time>]' appended."""
    parts = []
    if meter.get("five_hour") is not None:
        parts.append(f"5h {meter['five_hour']:g}%")
    seven = meter.get("seven_day")
    if seven is not None:
        reset = (
            f" (resets {_when(meter['resets_at'])})"
            if seven >= NEAR and meter.get("resets_at")
            else ""
        )
        parts.append(f"7d {seven:g}%{reset}")
    extra = extra_text(meter)
    if extra:
        parts.append(f"extra usage {extra}")
    age = age_minutes(meter, now)
    stale = (
        f"  [as of {_when(meter['polled_at'])}]" if age is not None and age > STALE_MINUTES else ""
    )
    return " | ".join(parts) + stale


def warning(meter: dict) -> tuple[str, str] | None:
    """('warn' | 'info', text) when the meter deserves attention, else None.

    warn: the 7-day window is full and extra usage is on (every token is billed at API prices until the reset), or extra
    usage has reached 80% of its monthly limit. info: the window is nearly full, or extra usage has been spent."""
    seven = meter.get("seven_day")
    used = meter.get("extra_used") or 0.0
    limit = meter.get("extra_limit")
    enabled = bool(meter.get("extra_enabled"))
    reset = f" (resets {_when(meter['resets_at'])})" if meter.get("resets_at") else ""
    extra = extra_text(meter)
    advice = "check the meter before a workflow or a fan-out of many agents (`claude-hr-ctl status`), and prefer Sonnet and fewer agents"
    if enabled and seven is not None and seven >= FULL:
        return (
            "warn",
            f"the 7-day window is full{reset} and extra usage is on"
            + (f" ({extra} used this month)" if extra else "")
            + f": until the reset every token is billed at API prices; {advice}",
        )
    if enabled and limit and used >= LIMIT_SHARE * limit:
        return ("warn", f"extra usage is at {extra} of its monthly limit; {advice}")
    if seven is not None and seven >= NEAR:
        return ("info", f"the 7-day window is at {seven:g}%{reset}; {advice}")
    if enabled and used > 0:
        return ("info", f"extra usage has been spent this month: {extra}")
    return None
