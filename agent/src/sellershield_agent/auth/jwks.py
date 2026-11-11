"""JWKS client for fetching and caching store RSA public keys safely."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import httpx
from jwt import PyJWK, PyJWKSet

from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import get_settings
from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.auth.jwks")


class JWKSClient:
    """Client for fetching and caching store JWKS public keys.

    Invariants:
    - Lazy fetch on first use (does not block or crash startup).
    - Cache valid keys for 1 hour.
    - Rate limit refetches on unknown kids to at most once per 30 seconds.
    - 3 tries with backoff on network failures.
    - Preserves last known good keys if a refetch fails.
    - follow_redirects=False; redirects treated as failures.
    - Plain HTTP rejected unless DEV_MODE=True.
    """

    def __init__(
        self,
        url: str,
        http: httpx.Client | None = None,
        clock: Clock | None = None,
        dev_mode: bool | None = None,
    ) -> None:
        self.url = url
        self.clock = clock or SystemClock()

        if dev_mode is None:
            try:
                self.dev_mode = get_settings().DEV_MODE
            except Exception:
                self.dev_mode = False
        else:
            self.dev_mode = dev_mode

        if not self.dev_mode and self.url.lower().startswith("http://"):
            raise ValueError(
                f"Insecure plain HTTP JWKS URL ({self.url}) is prohibited unless DEV_MODE=True."
            )

        self._own_client = http is None
        self.http = http or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=3.0),
        )

        self._keys: dict[str, PyJWK] = {}
        self._last_fetched_at: datetime | None = None
        self._last_refresh_attempt_at: datetime | None = None

    def close(self) -> None:
        """Close internal HTTP client if owned."""
        if self._own_client:
            self.http.close()

    def _fetch_with_retries(self) -> bool:
        """Attempt to fetch JWKS with up to 3 tries and exponential backoff."""
        self._last_refresh_attempt_at = self.clock.now()
        backoff_delays = [0.05, 0.1, 0.2]
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = self.http.get(self.url)
                if response.status_code == 200:
                    data = response.json()
                    jwk_set = PyJWKSet.from_dict(data)
                    new_keys: dict[str, PyJWK] = {}
                    for key in jwk_set.keys:
                        if key.key_id:
                            new_keys[key.key_id] = key
                    self._keys = new_keys
                    self._last_fetched_at = self.clock.now()
                    logger.info("jwks_fetched", key_count=len(self._keys))
                    return True
                else:
                    logger.warning(
                        "jwks_fetch_bad_status",
                        status_code=response.status_code,
                        attempt=attempt + 1,
                    )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "jwks_fetch_error",
                    error=str(exc),
                    attempt=attempt + 1,
                )

            if attempt < 2:
                time.sleep(backoff_delays[attempt])

        logger.error(
            "jwks_fetch_exhausted_retries",
            last_error=str(last_error) if last_error else "bad_status",
            has_cached_keys=bool(self._keys),
        )
<<<<<<< HEAD

        # In DEV_MODE, fall back to dev_auth server if port 8000 is down
        if (
            not self._keys
            and self.dev_mode
            and self.url != "http://localhost:8099/.well-known/jwks.json"
        ):
            try:
                dev_resp = self.http.get("http://localhost:8099/.well-known/jwks.json")
                if dev_resp.status_code == 200:
                    dev_data = dev_resp.json()
                    dev_set = PyJWKSet.from_dict(dev_data)
                    for key in dev_set.keys:
                        if key.key_id:
                            self._keys[key.key_id] = key
                    if self._keys:
                        self._last_fetched_at = self.clock.now()
                        logger.info("jwks_dev_fallback_fetched", key_count=len(self._keys))
                        return True
            except Exception:
                pass

=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
        return False

    def get_signing_key(self, kid: str) -> PyJWK:
        """Retrieve PyJWK for given kid, fetching lazily or refreshing as needed."""
        if not isinstance(kid, str) or not kid.strip() or len(kid) > 128:
            raise KeyError(f"Invalid kid format: {kid!r}")

        now = self.clock.now()

        # Lazy fetch on first use
        if self._last_fetched_at is None:
            self._fetch_with_retries()

        # Cache expired (1 hour)
        elif now - self._last_fetched_at > timedelta(hours=1):
            self._fetch_with_retries()

        if kid in self._keys:
            return self._keys[kid]

        # Unknown kid: rate limit refetch to at most once per 30 seconds
        if self._last_refresh_attempt_at is None or (
            now - self._last_refresh_attempt_at
        ) >= timedelta(seconds=30):
            logger.info("jwks_unknown_kid_attempt_refresh", kid=kid)
            self._fetch_with_retries()
            if kid in self._keys:
                return self._keys[kid]

        raise KeyError(f"Key ID {kid!r} not found in JWKS.")

    def is_healthy(self) -> bool:
        """Check if JWKS keys are currently available or reachable without throwing."""
        if self._keys:
            return True
        return self._fetch_with_retries()
