from datetime import datetime, timedelta, timezone
from typing import Literal


class ClockService:
    def __init__(self) -> None:
        self._mode: Literal["realtime", "frozen"] = "realtime"
        self._offset: timedelta = timedelta(0)
        self._frozen_at: datetime | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def offset_seconds(self) -> float:
        return self._offset.total_seconds()

    def now(self) -> datetime:
        """
        Return the current business time in UTC.
        This is the single source of truth for time across the application.
        All calls to datetime.now are strictly isolated to this file.
        """
        if self._mode == "frozen":
            base = self._frozen_at if self._frozen_at is not None else datetime.now(timezone.utc)
            return base + self._offset
        return datetime.now(timezone.utc) + self._offset

    def freeze(self, at: datetime | None = None) -> None:
        if at is None:
            self._frozen_at = self.now()
        else:
            if at.tzinfo is None:
                self._frozen_at = at.replace(tzinfo=timezone.utc)
            else:
                self._frozen_at = at.astimezone(timezone.utc)
        self._mode = "frozen"
        self._offset = timedelta(0)

    def unfreeze(self) -> None:
        self._mode = "realtime"
        self._frozen_at = None

    def advance(self, *, seconds: float = 0, hours: float = 0, days: float = 0) -> None:
        self._offset += timedelta(seconds=seconds, hours=hours, days=days)

    def reset(self) -> None:
        self._mode = "realtime"
        self._offset = timedelta(0)
        self._frozen_at = None

    def get_info(self) -> dict[str, str]:
        return {
            "now_utc": self.now().isoformat(),
            "mode": self._mode,
        }


# Singleton Clock instance for the application
Clock = ClockService()
