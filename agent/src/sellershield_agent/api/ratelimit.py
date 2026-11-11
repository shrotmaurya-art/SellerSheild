"""In-memory sliding window rate limiters for chat and authentication."""

from __future__ import annotations

import time
from collections import defaultdict


class SlidingWindowRateLimiter:
    """Thread-safe sliding window in-memory rate limiter."""

    def __init__(self) -> None:
        self._history: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        """Check if request for key is within limit during the rolling window."""
        now = time.monotonic()
        cutoff = now - window_seconds

        timestamps = self._history[key]
        # Prune expired timestamps
        self._history[key] = [t for t in timestamps if t > cutoff]

        if len(self._history[key]) >= limit:
            return False

        self._history[key].append(now)
        return True

    def record_attempt(self, key: str) -> None:
        """Record an attempt without checking limit."""
        self._history[key].append(time.monotonic())

    def is_exceeded(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        """Check if attempts for key exceed limit during the rolling window."""
        now = time.monotonic()
        cutoff = now - window_seconds
        timestamps = self._history[key]
        self._history[key] = [t for t in timestamps if t > cutoff]
        return len(self._history[key]) >= limit

    def reset(self) -> None:
        """Clear all rate limiting history (for tests)."""
        self._history.clear()


user_message_limiter = SlidingWindowRateLimiter()
ip_auth_failure_limiter = SlidingWindowRateLimiter()
