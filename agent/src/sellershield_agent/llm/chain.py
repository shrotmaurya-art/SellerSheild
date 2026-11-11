"""LLM Provider Chain with Circuit Breaker, Fallback, and Caching."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import ConfigError
from sellershield_agent.llm.cache import LLMCache, make_cache_key
from sellershield_agent.llm.client import LLMClient, LLMResult
from sellershield_agent.llm.config import ProviderConfig, load_providers_from_settings
from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.llm.chain")


class LLMError(Exception):
    """Base exception for LLM operations."""
    pass


class LLMUnavailable(LLMError):
    """Raised when all configured LLM providers are unavailable or fail."""
    pass


class CircuitBreaker:
    """In-memory circuit breaker per provider using the Clock abstraction.

    Rules:
    - 3 consecutive failures within 60 seconds opens the breaker (unhealthy).
    - Provider is skipped for 60 seconds while open.
    - After 60 seconds, provider half-opens (permitting a trial attempt).
    - Success closes the breaker and resets failure history.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._failures: dict[str, list[datetime]] = {}
        self._opened_at: dict[str, datetime | None] = {}

    def is_available(self, provider_name: str) -> bool:
        """Check whether provider is healthy or half-open for a trial attempt."""
        opened_at = self._opened_at.get(provider_name)
        if opened_at is None:
            return True

        now = self.clock.now()
        elapsed = (now - opened_at).total_seconds()
        if elapsed < 60.0:
            return False  # Breaker still open (unhealthy)

        # 60s cooldown elapsed -> half-open
        return True

    def record_failure(self, provider_name: str) -> None:
        """Record a failure event and trip breaker if threshold reached."""
        now = self.clock.now()
        failures = self._failures.setdefault(provider_name, [])
        # Retain only failures within the last 60s
        recent_failures = [t for t in failures if (now - t).total_seconds() <= 60.0]
        recent_failures.append(now)
        self._failures[provider_name] = recent_failures

        if len(recent_failures) >= 3:
            self._opened_at[provider_name] = now
            logger.warning(
                "circuit_breaker_opened",
                provider=provider_name,
                failures=len(recent_failures),
            )

    def record_success(self, provider_name: str) -> None:
        """Record a successful call, resetting failures and closing breaker."""
        self._failures[provider_name] = []
        self._opened_at[provider_name] = None


class LLMChain:
    """Manages multi-provider failover, circuit breaking, and caching."""

    def __init__(
        self,
        providers: list[ProviderConfig] | None = None,
        clock: Clock | None = None,
        cache: LLMCache | None = None,
    ) -> None:
        self.providers = providers if providers is not None else load_providers_from_settings()
        self.clock = clock or SystemClock()
        self.cache = cache or LLMCache()
        self.circuit_breaker = CircuitBreaker(self.clock)
        self._clients: dict[str, LLMClient] = {}

    def _get_client(self, provider: ProviderConfig) -> LLMClient:
        if provider.name not in self._clients:
            self._clients[provider.name] = LLMClient(provider)
        return self._clients[provider.name]

    async def aclose(self) -> None:
        """Close all instantiated provider clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        schema_for_cache: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Attempt LLM chat across provider chain with failover and caching."""
        if not self.providers:
            raise LLMUnavailable("No LLM providers configured")

        # In replay mode, check if first provider has a cached response
        # or if any provider was recorded
        if self.cache.mode == "replay":
            for provider in self.providers:
                cache_key = make_cache_key(
                    provider=provider.name,
                    model=provider.model,
                    messages=messages,
                    tools=tools,
                    schema=schema_for_cache,
                )
                cached = self.cache.get(cache_key)
                if cached is not None:
                    logger.info(
                        "llm_cache_replay_hit",
                        provider=cached.provider,
                        model=cached.model,
                        latency_ms=cached.latency_ms,
                        cached=True,
                    )
                    return cached
            # If nothing found across configured providers, trigger replay miss
            first = self.providers[0]
            first_key = make_cache_key(
                provider=first.name,
                model=first.model,
                messages=messages,
                tools=tools,
                schema=schema_for_cache,
            )
            # This call raises CacheMiss:
            self.cache.get(first_key)

        attempt = 0
        last_error: Exception | None = None

        for provider in self.providers:
            if not self.circuit_breaker.is_available(provider.name):
                logger.info("llm_provider_skipped_breaker_open", provider=provider.name)
                continue

            attempt += 1
            cache_key = make_cache_key(
                provider=provider.name,
                model=provider.model,
                messages=messages,
                tools=tools,
                schema=schema_for_cache,
            )

            # Check cache in record mode
            cached_result = self.cache.get(cache_key)
            if cached_result is not None:
                logger.info(
                    "llm_cache_hit",
                    provider=cached_result.provider,
                    model=cached_result.model,
                    latency_ms=cached_result.latency_ms,
                    cached=True,
                    attempt=attempt,
                )
                return cached_result

            client = self._get_client(provider)
            try:
                result = await client.chat(
                    messages=messages,
                    tools=tools,
                    response_format=response_format,
                )
                self.circuit_breaker.record_success(provider.name)
                self.cache.put(cache_key, result)

                logger.info(
                    "llm_call_success",
                    provider=result.provider,
                    model=result.model,
                    latency_ms=result.latency_ms,
                    cached=result.cached,
                    attempt=attempt,
                )
                return result

            except ConfigError:
                # 401, 403, 404: Configuration error must abort without trying next provider
                raise

            except Exception as err:
                self.circuit_breaker.record_failure(provider.name)
                last_error = err
                logger.warning(
                    "llm_provider_failed",
                    provider=provider.name,
                    model=provider.model,
                    attempt=attempt,
                    error=type(err).__name__,
                )

        err_type = type(last_error).__name__ if last_error else "None"
        raise LLMUnavailable(
            f"All configured LLM providers failed or are unavailable. Last error: {err_type}"
        ) from last_error
