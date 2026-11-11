"""Tests for IST time utilities (sellershield_agent.timeutil)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sellershield_agent.timeutil import is_within_window, ist_day_end, to_ist

IST = ZoneInfo("Asia/Kolkata")


# ── to_ist ────────────────────────────────────────────────────────


def test_to_ist() -> None:
    utc_dt = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    result = to_ist(utc_dt)
    assert result.hour == 5
    assert result.minute == 30
    # Verify it carries IST offset (+05:30)
    assert result.utcoffset() == IST.utcoffset(result)


def test_to_ist_rejects_naive() -> None:
    with pytest.raises(ValueError):
        to_ist(datetime(2026, 11, 1, 0, 0))


# ── ist_day_end ───────────────────────────────────────────────────


def test_ist_day_end() -> None:
    dt = datetime(2026, 11, 1, 18, 20, tzinfo=IST)
    result = ist_day_end(dt, 7)

    # End of 2026-11-08 IST = 2026-11-08 23:59:59.999999 IST
    result_utc = result.astimezone(UTC)
    expected_utc = datetime(2026, 11, 8, 18, 29, 59, 999999, tzinfo=UTC)
    assert result_utc == expected_utc


def test_ist_day_end_rejects_naive() -> None:
    with pytest.raises(ValueError):
        ist_day_end(datetime(2026, 11, 1, 18, 20), 7)


# ── is_within_window ─────────────────────────────────────────────


def test_window_boundary_within() -> None:
    delivered = datetime(2026, 11, 1, 23, 50, tzinfo=IST)
    now = datetime(2026, 11, 8, 23, 59, 59, tzinfo=IST)
    assert is_within_window(delivered, 7, now) is True


def test_window_boundary_outside() -> None:
    delivered = datetime(2026, 11, 1, 23, 50, tzinfo=IST)
    now = datetime(2026, 11, 9, 0, 0, 0, tzinfo=IST)
    assert is_within_window(delivered, 7, now) is False


def test_is_within_window_rejects_naive() -> None:
    naive = datetime(2026, 11, 1, 0, 0)
    aware = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    # Both naive
    with pytest.raises(ValueError):
        is_within_window(naive, 7, naive)
    # First naive
    with pytest.raises(ValueError):
        is_within_window(naive, 7, aware)
    # Second naive
    with pytest.raises(ValueError):
        is_within_window(aware, 7, naive)


# ── edge cases ────────────────────────────────────────────────────


def test_zoneinfo_loads_kolkata() -> None:
    """Verify tzdata is installed and Asia/Kolkata loads (KP-03)."""
    tz = ZoneInfo("Asia/Kolkata")
    assert tz is not None


def test_midnight_edge() -> None:
    delivered = datetime(2026, 11, 1, 0, 0, 0, tzinfo=IST)
    # Window ends 2026-11-08 23:59:59.999999 IST

    just_before = datetime(2026, 11, 8, 23, 59, 59, tzinfo=IST)
    assert is_within_window(delivered, 7, just_before) is True

    just_after = datetime(2026, 11, 9, 0, 0, 0, tzinfo=IST)
    assert is_within_window(delivered, 7, just_after) is False


def test_leap_day() -> None:
    delivered = datetime(2028, 2, 28, 12, 0, tzinfo=IST)
    result = ist_day_end(delivered, 1)

    # End of 2028-02-29 IST (leap year)
    result_ist = result.astimezone(IST)
    assert result_ist.month == 2
    assert result_ist.day == 29
    assert result_ist.hour == 23
    assert result_ist.minute == 59
    assert result_ist.second == 59
    assert result_ist.microsecond == 999999


def test_zero_day_window() -> None:
    delivered = datetime(2026, 11, 1, 10, 0, tzinfo=IST)
    # 0 days → window ends same day 2026-11-01 23:59:59.999999 IST

    within = datetime(2026, 11, 1, 23, 59, 59, tzinfo=IST)
    assert is_within_window(delivered, 0, within) is True

    outside = datetime(2026, 11, 2, 0, 0, 0, tzinfo=IST)
    assert is_within_window(delivered, 0, outside) is False
