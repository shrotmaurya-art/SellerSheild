import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
from app.admin import router as admin_router
from app.auth import router as auth_router
from app.carrier import router as carrier_router
from app.clock import Clock
from app.config import settings
from app.demo import router as demo_router
from app.errors import (
    AppException,
    UpstreamUnavailableException,
    app_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.evidence import router as evidence_router
from app.mailbox import router as mailbox_router
from app.middleware import (
    BodySizeLimitMiddleware,
    RequestIdLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from app.orders import router as orders_router
from app.outbox import run_outbox_worker_loop
from app.refunds import router as refunds_router
from app.returns import router as returns_router
from app.tick import run_store_tick_loop


class ClockResponse(BaseModel):
    now_utc: str
    mode: str


class HealthzResponse(BaseModel):
    status: str


class ReadyzResponse(BaseModel):
    status: str
    database: str
    clock: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: start background store tick task (10s) and outbox worker (1s)
    tick_task = asyncio.create_task(run_store_tick_loop(interval_seconds=10))
    outbox_task = asyncio.create_task(run_outbox_worker_loop(interval_seconds=1.0))
    yield
    # Shutdown: gracefully cancel background tasks
    tick_task.cancel()
    outbox_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    try:
        await outbox_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="NestKart Store API",
    version="0.1.0",
    description="NestKart Playground Store API",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS — allowlist from config; explicit methods and headers (no wildcards)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-CSRF-Token",
        "Idempotency-Key",
        "If-Match",
    ],
    expose_headers=["X-Request-ID"],
)

# Security headers (CSP, nosniff, frame-ancestors none)
app.add_middleware(SecurityHeadersMiddleware)

# Body size limit (1 MB)
app.add_middleware(BodySizeLimitMiddleware)

# Request ID and logging middleware
app.add_middleware(RequestIdLoggingMiddleware)

# Auth router
app.include_router(auth_router)

# Orders, Returns, Refunds, Evidence, Admin, Carrier, and Mailbox routers
app.include_router(orders_router)
app.include_router(returns_router)
app.include_router(refunds_router)
app.include_router(evidence_router)
app.include_router(admin_router)
app.include_router(carrier_router)
app.include_router(mailbox_router)
app.include_router(demo_router)

# Custom Error Handlers
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)


@app.get("/healthz", summary="Liveness check", tags=["System"], response_model=HealthzResponse)
@app.get(
    "/api/v1/healthz",
    summary="Liveness check alias",
    tags=["System"],
    response_model=HealthzResponse,
)
def healthz() -> HealthzResponse:
    return HealthzResponse(status="ok")


@app.get("/readyz", summary="Readiness check", tags=["System"], response_model=ReadyzResponse)
@app.get(
    "/api/v1/readyz",
    summary="Readiness check alias",
    tags=["System"],
    response_model=ReadyzResponse,
)
def readyz() -> ReadyzResponse:
    # Check database
    try:
        with db.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db_status = "ok"
    except Exception as e:
        raise UpstreamUnavailableException(message=f"Database check failed: {e}")

    # Check clock
    try:
        _ = Clock.now()
        clock_status = "ok"
    except Exception as e:
        raise UpstreamUnavailableException(message=f"Clock check failed: {e}")

    return ReadyzResponse(
        status="ready",
        database=db_status,
        clock=clock_status,
    )


@app.get(
    "/api/v1/clock",
    summary="Current store time",
    tags=["Clock"],
    response_model=ClockResponse,
)
def get_clock() -> ClockResponse:
    info = Clock.get_info()
    return ClockResponse(now_utc=info["now_utc"], mode=info["mode"])
