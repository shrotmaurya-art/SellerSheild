"""Delay Monitor APIRouter implementing seller run triggers and history inspection."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from sellershield_agent.auth import deps as auth_deps
from sellershield_agent.auth.deps import require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.config import Settings, get_settings
from sellershield_agent.db import get_engine, read_tx
from sellershield_agent.domain.state import monitor_runs
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.monitor.service import run_monitor
from sellershield_agent.store.client import StoreClient

router = APIRouter(prefix="/seller/monitor", tags=["monitor"])

_last_manual_run_at: datetime | None = None


def reset_monitor_cooldown() -> None:
    """Reset the manual run cooldown timer."""
    global _last_manual_run_at
    _last_manual_run_at = None


def get_monitor_store_client() -> StoreClient:
    """Provider for store client injected into monitor runs."""
    from sellershield_agent.store import get_store_client

    return get_store_client()


class MonitorRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MonitorRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    checked_orders_count: int
    anomalies_detected: int
    actions_taken: list[str] = Field(default_factory=list)
    created_at: str


class MonitorRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[MonitorRunResponse]


@router.post("/run", response_model=MonitorRunResponse)
async def trigger_monitor_run(
    request: Request,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(auth_deps.get_clock)],
    store_client: Annotated[StoreClient, Depends(get_monitor_store_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> MonitorRunResponse:
    """Trigger proactive delay monitor with seller RBAC and rate-limiting."""
    global _last_manual_run_at

    # Reject unexpected body parameters with 422
    body_bytes = await request.body()
    if body_bytes and body_bytes.strip():
        try:
            parsed = json.loads(body_bytes.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("Body must be an object")
            MonitorRunRequest.model_validate(parsed)
        except Exception as exc:
            raise AppHTTPException(
                status_code=422,
                code="validation_error",
                message=f"Invalid request body: {exc}",
            ) from exc

    # Enforce cooldown: 10s (6/min) in DEV_MODE, 60s (1/min) in production
    cooldown_seconds = 10.0 if getattr(settings, "DEV_MODE", False) else 60.0
    now_dt = clock.now()
    if _last_manual_run_at is not None:
        elapsed = (now_dt - _last_manual_run_at).total_seconds()
        if elapsed < cooldown_seconds:
            raise AppHTTPException(
                status_code=429,
                code="rate_limited",
                message=f"Monitor runs are limited to once per {int(cooldown_seconds)} seconds",
                details={"retry_after_seconds": int(cooldown_seconds - elapsed) + 1},
            )

    _last_manual_run_at = now_dt

    # Always use fresh Role.service context without forwarding caller's JWT
    req_id = getattr(request.state, "request_id", None) or f"req-mon-{int(now_dt.timestamp())}"
    service_ctx = build_service_ctx(clock, req_id)

    engine = get_engine()
    res = await run_monitor(service_ctx, clock, store_client=store_client, engine=engine)

    return MonitorRunResponse(
        run_id=res.run_id,
        checked_orders_count=res.scanned,
        anomalies_detected=res.flagged,
        actions_taken=res.actions_taken,
        created_at=res.created_at,
    )


@router.get("/runs", response_model=MonitorRunListResponse)
def list_monitor_runs(
    principal: Annotated[Principal, Depends(require_seller)],
) -> MonitorRunListResponse:
    """List historical monitor runs."""
    engine = get_engine()
    with read_tx(engine) as conn:
        rows = conn.execute(
            select(
                monitor_runs.c.run_id,
                monitor_runs.c.scanned,
                monitor_runs.c.flagged,
                monitor_runs.c.started_at,
                monitor_runs.c.summary_json,
            ).order_by(monitor_runs.c.started_at.desc())
        ).fetchall()

        items = []
        for r in rows:
            actions_taken = []
            if r[4]:
                try:
                    s_data = json.loads(str(r[4]))
                    if isinstance(s_data, dict):
                        actions_taken = s_data.get("actions_taken", [])
                except Exception:
                    pass
            items.append(
                MonitorRunResponse(
                    run_id=str(r[0]),
                    checked_orders_count=int(r[1] or 0),
                    anomalies_detected=int(r[2] or 0),
                    actions_taken=actions_taken,
                    created_at=str(r[3]),
                )
            )

        return MonitorRunListResponse(runs=items)


@router.get("/runs/{id}", response_model=MonitorRunResponse)
def get_monitor_run_by_id(
    id: str,
    principal: Annotated[Principal, Depends(require_seller)],
) -> MonitorRunResponse:
    """Fetch details of a single monitor run."""
    engine = get_engine()
    with read_tx(engine) as conn:
        r = conn.execute(
            select(
                monitor_runs.c.run_id,
                monitor_runs.c.scanned,
                monitor_runs.c.flagged,
                monitor_runs.c.started_at,
                monitor_runs.c.summary_json,
            ).where(monitor_runs.c.run_id == id)
        ).fetchone()

        if r is None:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message=f"Monitor run '{id}' not found",
            )

        actions_taken = []
        if r[4]:
            try:
                s_data = json.loads(str(r[4]))
                if isinstance(s_data, dict):
                    actions_taken = s_data.get("actions_taken", [])
            except Exception:
                pass

        return MonitorRunResponse(
            run_id=str(r[0]),
            checked_orders_count=int(r[1] or 0),
            anomalies_detected=int(r[2] or 0),
            actions_taken=actions_taken,
            created_at=str(r[3]),
        )
