"""Clock abstraction so domain code never calls ``datetime.now()`` (KP-06).

Domain modules receive a :class:`Clock` and call ``clock.now()`` to get
the current time as an **aware UTC** datetime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocol for a clock that returns the current aware-UTC datetime."""

    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock backed by the OS clock."""

    def now(self) -> datetime:
        """Return ``datetime.now(UTC)``."""
        return datetime.now(UTC)


class FixedClock:
    """Test clock with a settable time.

    Both ``__init__`` and ``set`` reject naive datetimes.
    """

    def __init__(self, initial: datetime) -> None:
        _require_aware(initial)
        self._now = initial

    def now(self) -> datetime:
        """Return the fixed time."""
        return self._now

    def set(self, dt: datetime) -> None:
        """Advance (or rewind) the clock to *dt*."""
        _require_aware(dt)
        self._now = dt


class StoreClock:
    """Clock that reads the store's simulated time.

    Wired in Phase 1 when the store HTTP client is available.
    """

    def now(self) -> datetime:
        """Not yet wired — raises ``NotImplementedError``."""
        raise NotImplementedError("wired in Phase 1")


# ── internal helper ───────────────────────────────────────────────


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware, got naive: {dt!r}")

