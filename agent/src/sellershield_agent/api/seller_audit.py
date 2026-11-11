"""Seller API endpoints for audit log inspection, chain verification, and decision traces."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from sellershield_agent.audit.queries import (
    query_audit_events_sync,
    verify_audit_chain_report_sync,
)
from sellershield_agent.audit.trace import build_decision_trace_sync
from sellershield_agent.auth.deps import get_clock, require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine

router = APIRouter(prefix="/seller", tags=["seller_audit"])


@router.get("/audit")
def list_seller_audit_events(
    principal: Annotated[Principal, Depends(require_seller)],
    limit: int = 50,
    cursor: str | None = None,
    request_id: str | None = None,
    resource_id: str | None = None,
    event_type: str | None = None,
    from_ts: str | None = Query(None, alias="from"),
    to_ts: str | None = Query(None, alias="to"),
) -> dict[str, Any]:
    """Query immutable audit events with filters and keyset cursor pagination."""
    engine = get_engine()
    return query_audit_events_sync(
        engine,
        limit=limit,
        cursor=cursor,
        request_id=request_id,
        resource_id=resource_id,
        event_type=event_type,
        from_ts=from_ts,
        to_ts=to_ts,
    )


@router.get("/audit/verify")
def verify_seller_audit_chain(
    principal: Annotated[Principal, Depends(require_seller)],
) -> dict[str, Any]:
    """Verify cryptographic SHA-256 hash chain integrity of the audit log."""
    engine = get_engine()
    return verify_audit_chain_report_sync(engine)


@router.get("/traces/{request_id}")
def get_seller_decision_trace(
    request_id: str,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Get complete Decision Trace joining telemetry, policy facts, and audit trail."""
    engine = get_engine()
    return build_decision_trace_sync(engine, request_id=request_id, clock=clock)

