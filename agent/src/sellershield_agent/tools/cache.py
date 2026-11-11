"""Per-session order digest cache with a 60-second TTL using the simulated Clock.

Spec §6.4:
Independent reads and order digests cached for 60 seconds.
Invalidation hook clears cache for modified or transitioned orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sellershield_agent.clock import Clock


@dataclass(frozen=True)
class _CacheEntry:
    data: Any
    expires_at: datetime
    order_ids: set[str]


class OrderDigestCache:
    """In-memory per-session cache with TTL driven by the injected Clock."""

    def __init__(self, clock: Clock, ttl_seconds: float = 60.0) -> None:
        self.clock = clock
        self.ttl_seconds = ttl_seconds
        # Mapping: (session_id, cache_key) -> _CacheEntry
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    def get(self, session_id: str, key: str) -> Any | None:
        """Fetch cached item if not expired according to the Clock."""
        cache_id = (session_id, key)
        entry = self._cache.get(cache_id)
        if entry is None:
            return None

        if self.clock.now() >= entry.expires_at:
            del self._cache[cache_id]
            return None

        return entry.data

    def set(
        self,
        session_id: str,
        key: str,
        data: Any,
        order_ids: set[str] | None = None,
    ) -> None:
        """Store item with expiration timestamp calculated from Clock."""
        from datetime import timedelta

        now = self.clock.now()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        self._cache[(session_id, key)] = _CacheEntry(
            data=data,
            expires_at=expires_at,
            order_ids=order_ids or set(),
        )

    def invalidate(self, order_id: str | None = None) -> None:
        """Invalidate entries matching an order ID across all sessions."""
        if order_id is None:
            self._cache.clear()
            return

        to_remove = [
            k for k, entry in self._cache.items() if order_id in entry.order_ids or order_id in k[1]
        ]
        for k in to_remove:
            self._cache.pop(k, None)

    def invalidate_session(self, session_id: str) -> None:
        """Invalidate all cached entries belonging to a given session."""
        to_remove = [k for k in self._cache if k[0] == session_id]
        for k in to_remove:
            self._cache.pop(k, None)
