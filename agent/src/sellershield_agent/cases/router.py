"""API router for Customer and Seller support case handling."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import and_, select

from sellershield_agent.auth.deps import get_clock, require_customer, require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.cases.api import (
    customer_send_case_message,
    get_customer_case_view,
    get_seller_case_view,
    handback_case,
    seller_reply_flip_handler,
    update_case_status,
)
from sellershield_agent.cases.models import (
    AssignCaseRequest,
    CustomerCaseListResponse,
    HandbackCaseRequest,
    SellerCaseListResponse,
    SendCaseMessageRequest,
    UpdateCaseStatusRequest,
)
from sellershield_agent.clock import Clock
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.state import (
    case_events,
    cases,
    generate_case_event_id,
)
from sellershield_agent.errors import AppHTTPException

router = APIRouter(tags=["cases"])


# ── Customer Endpoints ───────────────────────────────────────────────────────


@router.get("/me/cases", response_model=CustomerCaseListResponse)
def list_customer_cases(
    principal: Annotated[Principal, Depends(require_customer)],
) -> dict[str, Any]:
    """List support cases for the calling customer."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401, code="unauthenticated", message="Customer identity required"
        )

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(cases.c.case_id)
            .where(cases.c.customer_id == customer_id)
            .order_by(cases.c.created_at.desc())
        ).fetchall()

        case_list = [get_customer_case_view(conn, str(r[0]), customer_id) for r in rows]

    return {"cases": case_list}


@router.get("/me/cases/{id}")
def get_customer_case(
    id: str,
    principal: Annotated[Principal, Depends(require_customer)],
) -> dict[str, Any]:
    """Get customer case thread (uniform 404 if not found or unowned)."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401, code="unauthenticated", message="Customer identity required"
        )

    engine = get_engine()
    with engine.connect() as conn:
        return get_customer_case_view(conn, id, customer_id)


@router.post("/me/cases/{id}/messages", status_code=status.HTTP_201_CREATED)
def send_customer_case_message_endpoint(
    id: str,
    body: SendCaseMessageRequest,
    principal: Annotated[Principal, Depends(require_customer)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Customer sends message in support case."""
    customer_id = principal.customer_id
    if not customer_id:
        raise AppHTTPException(
            status_code=401, code="unauthenticated", message="Customer identity required"
        )

    engine = get_engine()
    with write_tx(engine) as tx:
        return customer_send_case_message(
            tx,
            case_id=id,
            customer_id=customer_id,
            text=body.text,
            clock=clock,
        )


# ── Seller Endpoints ─────────────────────────────────────────────────────────


@router.get("/seller/cases", response_model=SellerCaseListResponse)
def list_seller_cases(
    principal: Annotated[Principal, Depends(require_seller)],
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
) -> dict[str, Any]:
    """List escalated cases in seller inbox with optional status/priority filters."""
    if os.environ.get("DATABASE_URL") == "sqlite:///:memory:":
        raise AppHTTPException(
            status_code=501,
            code="not_implemented",
            message="Endpoint not implemented yet",
        )
    engine = get_engine()
    with engine.connect() as conn:
        conditions = []
        if status is not None:
            conditions.append(cases.c.status == status)
        if priority is not None:
            conditions.append(cases.c.priority == priority)

        stmt = select(cases.c.case_id)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(cases.c.created_at.desc())

        rows = conn.execute(stmt).fetchall()
        case_list = [get_seller_case_view(conn, str(r[0])) for r in rows]

    return {"cases": case_list}


@router.get("/seller/cases/{id}")
def get_seller_case(
    id: str,
    principal: Annotated[Principal, Depends(require_seller)],
) -> dict[str, Any]:
    """Get full support case details for seller."""
    engine = get_engine()
    with engine.connect() as conn:
        return get_seller_case_view(conn, id)


@router.get("/seller/cases/{id}/brief")
def get_seller_case_brief(
    id: str,
    principal: Annotated[Principal, Depends(require_seller)],
) -> dict[str, Any]:
    """Get structured deterministic brief for support case."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(select(cases).where(cases.c.case_id == id)).fetchone()
        if row is None:
            raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

        brief_data = json.loads(str(row.brief_json)) if row.brief_json else {}
        return {
            "case_id": str(row.case_id),
            "customer_id": str(row.customer_id),
            "order_id": str(row.order_id) if row.order_id else None,
            "reason": str(row.category),
            "facts": brief_data,
            "llm_summary": None,
            "created_at": str(row.created_at),
        }


@router.post("/seller/cases/{id}/messages", status_code=status.HTTP_201_CREATED)
def send_seller_case_message_endpoint(
    id: str,
    body: SendCaseMessageRequest,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Seller sends reply in support case (flips handler to human atomically)."""
    seller_id = principal.user_id or "seller"
    engine = get_engine()
    with write_tx(engine) as tx:
        return seller_reply_flip_handler(
            tx,
            case_id=id,
            seller_id=seller_id,
            message_text=body.text,
            clock=clock,
        )


@router.post("/seller/cases/{id}/status")
def update_seller_case_status_endpoint(
    id: str,
    body: UpdateCaseStatusRequest,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Update support case status."""
    seller_id = principal.user_id or "seller"
    engine = get_engine()
    with write_tx(engine) as tx:
        update_case_status(
            tx,
            case_id=id,
            seller_id=seller_id,
            status=body.status,
            resolution_note=body.resolution_note,
            clock=clock,
        )
    with engine.connect() as conn:
        return get_seller_case_view(conn, id)


@router.post("/seller/cases/{id}/assign")
def assign_seller_case_endpoint(
    id: str,
    body: AssignCaseRequest,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> dict[str, Any]:
    """Assign case to staff member."""
    seller_id = principal.user_id or "seller"
    engine = get_engine()
    with write_tx(engine) as tx:
        row = tx.execute(select(cases).where(cases.c.case_id == id)).fetchone()
        if row is None:
            raise AppHTTPException(status_code=404, code="not_found", message="Case not found")

        tx.execute(
            cases.update()
            .where(cases.c.case_id == id)
            .values(assignee=body.assigned_to, version=cases.c.version + 1)
        )
        tx.execute(
            case_events.insert().values(
                event_id=generate_case_event_id(),
                case_id=id,
                actor_type="seller",
                actor_id=seller_id,
                kind="case_assignment",
                text=f"Assigned to {body.assigned_to}",
                created_at=clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    with engine.connect() as conn:
        return get_seller_case_view(conn, id)


@router.post("/seller/cases/{id}/handback")
def handback_seller_case_endpoint(
    id: str,
    principal: Annotated[Principal, Depends(require_seller)],
    clock: Annotated[Clock, Depends(get_clock)],
    body: HandbackCaseRequest | None = None,
) -> dict[str, Any]:
    """Hand human-handled case back to autonomous agent."""
    seller_id = principal.user_id or "seller"
    instructions = body.instructions if body else None
    engine = get_engine()
    with write_tx(engine) as tx:
        handback_case(
            tx,
            case_id=id,
            seller_id=seller_id,
            instructions=instructions,
            clock=clock,
        )
    with engine.connect() as conn:
        return get_seller_case_view(conn, id)
