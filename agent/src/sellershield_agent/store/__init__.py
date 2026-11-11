"""Store client interfaces, execution context and in-memory fixture client."""

from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreClientError,
    StoreConflict,
    StoreForbidden,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.store.http_client import HttpStoreClient


def get_store_client(
    clock: Clock | None = None,
    settings: Settings | None = None,
) -> StoreClient:
    """Factory creating StoreClient according to active STORE_MODE (fixtures or http)."""
    from pathlib import Path

    active_clock = clock if clock is not None else SystemClock()
    active_settings = settings if settings is not None else get_settings()

    if active_settings.STORE_MODE == "http":
        return HttpStoreClient(
            base_url=active_settings.STORE_BASE_URL,
            monitor_client_id=active_settings.STORE_MONITOR_CLIENT_ID,
            monitor_client_secret=active_settings.STORE_MONITOR_CLIENT_SECRET,
            executor_client_id=active_settings.STORE_EXECUTOR_CLIENT_ID,
            executor_client_secret=active_settings.STORE_EXECUTOR_CLIENT_SECRET,
            clock=active_clock,
        )

    candidate_paths = [
        Path("scenarios/fixtures/store.json"),
        Path(__file__).resolve().parents[4] / "scenarios" / "fixtures" / "store.json",
        Path(__file__).resolve().parents[3] / "scenarios" / "fixtures" / "store.json",
    ]
    fixtures_path = next((p for p in candidate_paths if p.is_file()), candidate_paths[0])
    return FixtureStoreClient(fixtures_path=fixtures_path, clock=active_clock)


__all__ = [
    "FixtureStoreClient",
    "HttpStoreClient",
    "RequestContext",
    "StoreClient",
    "StoreClientError",
    "StoreConflict",
    "StoreForbidden",
    "StoreNotFound",
    "StoreTimeout",
    "StoreUnavailable",
    "StoreValidationError",
    "get_store_client",
]
