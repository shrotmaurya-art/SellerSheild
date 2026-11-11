"""Application factory and lifecycle for SellerShield AI Agent."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import router as chat_router
from sellershield_agent.api.customer_approvals import router as customer_approvals_router
from sellershield_agent.api.health import router as health_router
from sellershield_agent.api.proposals import router as proposals_router
from sellershield_agent.api.seller_approvals import router as seller_approvals_router
from sellershield_agent.api.seller_audit import router as seller_audit_router
from sellershield_agent.api.seller_stub import router as seller_router
from sellershield_agent.api.webhooks import router as webhooks_router
from sellershield_agent.auth import deps as auth_deps
from sellershield_agent.cases.router import router as cases_router
from sellershield_agent.clock import Clock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.dev.faults import clear_faults
from sellershield_agent.dev.router import router as dev_router
from sellershield_agent.errors import register_error_handlers
from sellershield_agent.events.router import router as events_router
from sellershield_agent.execution.reconcile import reconcile_once
from sellershield_agent.logging import configure_logging
from sellershield_agent.memory.router import router as memory_router
from sellershield_agent.middleware import (
    AccessLogMiddleware,
    BodyLimitMiddleware,
    RequestIDMiddleware,
)
from sellershield_agent.monitor.router import router as monitor_router
from sellershield_agent.outbox.router import router as outbox_router
from sellershield_agent.overview.router import router as overview_router
from sellershield_agent.store import StoreClient

RECONCILE_INTERVAL_SECONDS: float = 5.0
_logger = structlog.get_logger("sellershield_agent.startup")


def _resolve_runtime_deps(app: FastAPI, settings: Settings) -> tuple[Clock, StoreClient | None]:
    clock_provider = app.dependency_overrides.get(auth_deps.get_clock)
    clock: Clock = clock_provider() if callable(clock_provider) else auth_deps.get_clock()

    store_override: StoreClient | None = None
    store_provider = app.dependency_overrides.get(proposals_api.get_proposal_store_client)
    if callable(store_provider):
        params = inspect.signature(store_provider).parameters
        required = [
            p
            for p in params.values()
            if p.default is inspect.Parameter.empty
            and p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if not required:
            store_override = store_provider()
        else:
            store_override = store_provider(clock=clock, settings=settings)
    return clock, store_override


async def _background_reconcile_loop(app: FastAPI, settings: Settings) -> None:
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
        try:
            clock, store_override = _resolve_runtime_deps(app, settings)
            await reconcile_once(clock, store_client=store_override, min_age_seconds=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("reconcile_loop_error", error=type(exc).__name__)


async def _background_outbox_loop(app: FastAPI, settings: Settings) -> None:
    from sellershield_agent.db import get_engine
    from sellershield_agent.outbox.worker import has_due_email_rows, process_outbox_batch
    from sellershield_agent.store import get_store_client

    while True:
        await asyncio.sleep(2.0)
        try:
            active_settings = get_settings()
            clock, store_override = _resolve_runtime_deps(app, active_settings)
            engine = get_engine()
            if store_override is None and not await asyncio.to_thread(
                has_due_email_rows, engine, clock
            ):
                continue
            store_client = store_override or get_store_client(clock=clock, settings=active_settings)
            await process_outbox_batch(engine, store_client, clock)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("outbox_loop_error", error=type(exc).__name__)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    try:
        clock, store_override = _resolve_runtime_deps(app, settings)
        await reconcile_once(clock, store_client=store_override, min_age_seconds=0)
    except Exception as exc:
        _logger.warning("startup_reconcile_failed", error=type(exc).__name__)

    reconcile_task = asyncio.create_task(_background_reconcile_loop(app, settings))
    outbox_task = asyncio.create_task(_background_outbox_loop(app, settings))
    try:
        yield
    finally:
        reconcile_task.cancel()
        outbox_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reconcile_task
        with contextlib.suppress(asyncio.CancelledError):
            await outbox_task


class SafeOverrides(dict[Any, Any]):
    """Protects dependency overrides from FastAPI kwarg query-parameter coercion."""

    def __setitem__(self, k: Any, v: Any) -> None:
        if callable(v):
            try:
                sig = inspect.signature(v)
                has_var_keyword = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                )
                positional = [
                    p
                    for p in sig.parameters.values()
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.POSITIONAL_ONLY,
                    )
                ]
                if has_var_keyword and len(positional) == 0:
                    orig = v
                    super().__setitem__(k, lambda: orig())
                    return
            except Exception:
                pass
        super().__setitem__(k, v)


def create_app() -> FastAPI:
    """Application factory for the SellerShield Agent service."""
    configure_logging()
    settings = get_settings()

    if not settings.DEV_MODE and os.environ.get("STORE_MODE") == "fixtures":
        raise RuntimeError("STORE_MODE=fixtures is prohibited outside DEV_MODE (KP-24)")

    _logger.info(
        "service_started",
        dev_mode=settings.DEV_MODE,
        store_mode=settings.STORE_MODE,
    )

    app = FastAPI(
        title="SellerShield AI Agent",
        version="0.1.0",
        docs_url="/docs" if settings.DEV_MODE else None,
        redoc_url="/redoc" if settings.DEV_MODE else None,
        openapi_url="/openapi.json" if settings.DEV_MODE else None,
        lifespan=_lifespan,
    )
    app.dependency_overrides = SafeOverrides()

    register_error_handlers(app)

    # In-memory faults do not survive process restart
    clear_faults()

    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(proposals_router, prefix="/v1")
    app.include_router(proposals_router)
    app.include_router(customer_approvals_router, prefix="/v1")
    app.include_router(customer_approvals_router)
    app.include_router(seller_approvals_router, prefix="/v1")
    app.include_router(seller_approvals_router)
    app.include_router(seller_audit_router, prefix="/v1")
    app.include_router(seller_audit_router)
    app.include_router(events_router, prefix="/v1")
    app.include_router(events_router)
    app.include_router(outbox_router, prefix="/v1")
    app.include_router(outbox_router)
    app.include_router(overview_router, prefix="/v1")
    app.include_router(overview_router)
    app.include_router(seller_router, prefix="/v1")
    app.include_router(seller_router)
    app.include_router(webhooks_router, prefix="/v1")
    app.include_router(webhooks_router)
    app.include_router(dev_router, prefix="/v1")
    app.include_router(dev_router)
    app.include_router(cases_router, prefix="/v1")
    app.include_router(cases_router)
    app.include_router(memory_router, prefix="/v1")
    app.include_router(memory_router)
    app.include_router(monitor_router, prefix="/v1")
    app.include_router(monitor_router)

    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RequestIDMiddleware)

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("sellershield_agent.main:create_app", factory=True, port=8001, reload=True)
