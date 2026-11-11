"""IST time utilities for policy-window calculations.

All policy windows use **Asia/Kolkata** (IST, UTC+05:30).  India does
not observe DST, so the offset is constant.

Spec rule (§7.2): *"the window ends at 23:59:59 IST on delivery date
plus N days"* — we use 23:59:59.999999 to include the entire last second.

Naive datetimes are always rejected.  Domain code uses the Clock
abstraction (KP-06) and passes its output here.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

# IST is always UTC+05:30 (no DST in India).  Requires the ``tzdata``
# package on Windows (KP-03).
IST = ZoneInfo("Asia/Kolkata")

# End-of-day with microsecond precision
_END_OF_DAY = time(23, 59, 59, 999999)


def to_ist(dt: datetime) -> datetime:
    """Convert an aware datetime to IST (Asia/Kolkata).

    Raises :class:`ValueError` if *dt* is naive.
    """
    _require_aware(dt)
    return dt.astimezone(IST)


def ist_day_end(dt: datetime, days: int) -> datetime:
    """Return the last instant of the IST day that is *days* after *dt*'s IST date.

    The result is 23:59:59.999999 IST on ``(IST date of dt) + days``,
    returned as an **aware UTC** datetime.

    Raises :class:`ValueError` if *dt* is naive.
    """
    _require_aware(dt)

    # Convert to IST to get the calendar date
    ist_dt = dt.astimezone(IST)
    target_date = ist_dt.date() + timedelta(days=days)

    # Build 23:59:59.999999 on that date in IST, then convert to UTC
    end_ist = datetime.combine(target_date, _END_OF_DAY, tzinfo=IST)
    return end_ist.astimezone(UTC)


def is_within_window(
    delivered_at: datetime,
    days: int,
    now: datetime,
) -> bool:
    """Check whether *now* is still within *days* of delivery.

    Both arguments must be timezone-aware.  The window boundary is
    23:59:59.999999 IST on ``(IST date of delivered_at) + days``.
    """
    _require_aware(delivered_at)
    _require_aware(now)
    deadline = ist_day_end(delivered_at, days)
    return now <= deadline


# ── internal helper ───────────────────────────────────────────────


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware, got naive: {dt!r}")
