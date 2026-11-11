"""Clock abstraction so domain code never calls ``datetime.now()`` (KP-06).

Domain modules receive a :class:`Clock` and call ``clock.now()`` to get
the current time as an **aware UTC** datetime.
"""

from __future__ import annotations

<<<<<<< HEAD
from datetime import UTC, datetime, timedelta
=======
from datetime import UTC, datetime
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
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

<<<<<<< HEAD
    def advance(self, delta: timedelta) -> None:
        """Advance the fixed time by the given timedelta."""
        self.set(self._now + delta)


class StoreClock:
    """Clock that tracks the store's simulated time.

    Caches the offset relative to UTC for 60 seconds and retains the last
    offset if the store is unreachable.
    """

    def __init__(
        self,
        initial_offset_seconds: float = 0.0,
        frozen_time: datetime | None = None,
    ) -> None:
        self._offset_seconds = initial_offset_seconds
        self._frozen_time = frozen_time
        self._last_synced_at: datetime | None = None
        if frozen_time is not None:
            _require_aware(frozen_time)

    @property
    def offset_seconds(self) -> float:
        return self._offset_seconds

    def is_cache_valid(self, max_age_seconds: float = 60.0) -> bool:
        """Check if cached offset is younger than max_age_seconds."""
        if self._last_synced_at is None:
            return False
        age = (datetime.now(UTC) - self._last_synced_at).total_seconds()
        return age < max_age_seconds

    def set_sync(
        self,
        store_time: datetime,
        mode: str = "simulated",
        offset_seconds: float | None = None,
    ) -> None:
        """Update synchronized store time and cache timestamp."""
        _require_aware(store_time)
        self._last_synced_at = datetime.now(UTC)
        if mode == "frozen":
            self._frozen_time = store_time
        else:
            self._frozen_time = None
            if offset_seconds is not None:
                self._offset_seconds = float(offset_seconds)
            else:
                self._offset_seconds = (store_time - datetime.now(UTC)).total_seconds()

    def set(self, dt: datetime) -> None:
        """Set an exact time (supports test / sync compatibility)."""
        _require_aware(dt)
        self._frozen_time = dt

    def now(self) -> datetime:
        """Return current simulated or synchronized store time."""
        if self._frozen_time is not None:
            return self._frozen_time
        return datetime.now(UTC) + timedelta(seconds=self._offset_seconds)
=======

class StoreClock:
    """Clock that reads the store's simulated time.

    Wired in Phase 1 when the store HTTP client is available.
    """

    def now(self) -> datetime:
        """Not yet wired — raises ``NotImplementedError``."""
        raise NotImplementedError("wired in Phase 1")
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be


# ── internal helper ───────────────────────────────────────────────


def _require_aware(dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"datetime must be timezone-aware, got naive: {dt!r}")
<<<<<<< HEAD
=======

>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
