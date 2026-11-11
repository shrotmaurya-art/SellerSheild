"""Background expiry sweep for stale seller approvals (never auto-approves)."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, select

from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.config import get_settings
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.models import RefundStatus, ReturnStatus
from sellershield_agent.domain.state import (
    PROPOSAL_TRANSITIONS,
    ApprovalStatus,
    InvalidTransition,
    ProposalStatus,
    VersionMismatch,
    approvals,
    client_events,
    is_approval_expired,
    proposals,
    transition_approval,
    transition_proposal,
)
from sellershield_agent.events.emit import emit_client_event
from sellershield_agent.execution.common import build_service_ctx, format_utc_iso
from sellershield_agent.execution.db_ops import append_audit_in_tx
from sellershield_agent.store import StoreClient, get_store_client
from sellershield_agent.store.client import (
    StoreClientError,
    StoreInvalidState,
    StoreNotFound,
)

_logger = structlog.get_logger("sellershield_agent.approvals.expire")


def _scan_expired_approvals_sync(engine: Engine, clock: Clock) -> list[dict[str, Any]]:
    """Read-only scan for pending/needs_revalidation approvals past expires_at."""
    with read_tx(engine) as conn:
        rows = conn.execute(
            select(approvals).where(
                approvals.c.status.in_(
                    (ApprovalStatus.pending.value, ApprovalStatus.needs_revalidation.value)
                )
            )
        ).fetchall()
        expired_items: list[dict[str, Any]] = []
        for row in rows:
            if not is_approval_expired(str(row.expires_at), clock):
                continue
            prop = conn.execute(
                select(proposals.c.customer_id).where(proposals.c.proposal_id == row.proposal_id)
            ).fetchone()
            customer_id = str(prop.customer_id) if prop is not None else ""
            item = dict(row._mapping)
            item["customer_id"] = customer_id
            expired_items.append(item)
        return expired_items


def _emit_expired_client_event_in_tx(
    tx: Connection,
    *,
    customer_id: str,
    approval_id: str,
    proposal_id: str,
    order_id: str,
    return_id: str | None,
    now_iso: str,
) -> None:
    if not customer_id:
        return
    existing = tx.execute(
        select(client_events.c.payload_json).where(
            client_events.c.customer_id == customer_id,
            client_events.c.type == "proposal.expired",
        )
    ).fetchall()
    for row in existing:
        try:
            parsed = json.loads(str(row.payload_json))
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("proposal_id") == proposal_id:
            return

    payload: dict[str, Any] = {
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "order_id": order_id,
        "return_id": return_id,
        "status": "expired",
        "message_key": "proposal.expired",
        "message": f"Your request for order {order_id} expired and was not completed.",
    }
    emit_client_event(tx, customer_id, "proposal.expired", payload, now_iso)


def _finalize_expired_approval_sync(
    engine: Engine,
    *,
    approval_id: str,
    expected_version: int,
    customer_id: str,
    clock: Clock,
    request_id: str,
    actor_id: str,
) -> bool:
    """Atomically mark expired approval, fail parent proposal, emit client_event, and audit."""
    now_iso = format_utc_iso(clock.now())
    with write_tx(engine) as tx:
        apr = tx.execute(
            select(approvals).where(approvals.c.approval_id == approval_id)
        ).fetchone()
        if apr is None or int(apr.version) != expected_version:
            return False
        if str(apr.status) not in (
            ApprovalStatus.pending.value,
            ApprovalStatus.needs_revalidation.value,
        ):
            return False

        try:
            transition_approval(
                tx,
                approval_id=approval_id,
                to_status=ApprovalStatus.expired,
                expected_version=expected_version,
                clock=clock,
                request_id=request_id,
                actor_id=actor_id,
                actor_type=ActorType.SYSTEM.value,
                extra_values={"last_error": None},
            )
        except (InvalidTransition, VersionMismatch):
            return False

        prop_row = tx.execute(
            select(proposals).where(proposals.c.proposal_id == apr.proposal_id)
        ).fetchone()
        if prop_row is not None:
            curr_p = str(prop_row.status)
            if ProposalStatus.failed.value in PROPOSAL_TRANSITIONS.get(curr_p, set()):
                transition_proposal(
                    tx,
                    proposal_id=str(apr.proposal_id),
                    to_status=ProposalStatus.failed,
                    expected_version=int(prop_row.version),
                    clock=clock,
                    request_id=request_id,
                    actor_id=actor_id,
                    actor_type=ActorType.SYSTEM.value,
                )

        resolved_cust = customer_id or (str(prop_row.customer_id) if prop_row else "")
        _emit_expired_client_event_in_tx(
            tx,
            customer_id=resolved_cust,
            approval_id=approval_id,
            proposal_id=str(apr.proposal_id),
            order_id=str(apr.order_id),
            return_id=str(apr.return_id) if apr.return_id else None,
            now_iso=now_iso,
        )

        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=str(prop_row.session_id) if prop_row else None,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            event_type=EventType.RECONCILE,
            resource_type="approvals",
            resource_id=approval_id,
            action="approval_expired",
            outcome=Outcome.SUCCESS,
            policy_version=str(apr.policy_version),
            details={
                "proposal_id": str(apr.proposal_id),
                "order_id": str(apr.order_id),
                "return_id": str(apr.return_id) if apr.return_id else None,
                "refund_id": str(apr.refund_id) if apr.refund_id else None,
            },
        )
        return True


async def expire_single_approval(
    *,
    engine: Engine,
    store_client: StoreClient,
    apr_item: dict[str, Any],
    clock: Clock,
    request_id: str,
    actor_id: str = "reconciler",
) -> bool:
    """Release store holds via ServiceContext and mark a single expired approval.

    Never auto-approves or calls authorize_refund.
    """
    approval_id = str(apr_item["approval_id"])
    refund_id = str(apr_item["refund_id"]) if apr_item.get("refund_id") else None
    return_id = str(apr_item["return_id"]) if apr_item.get("return_id") else None
    svc_ctx = build_service_ctx(clock, request_id)

    if refund_id is not None:
        try:
            rfd = await store_client.get_refund(svc_ctx, refund_id)
            if rfd.status == RefundStatus.reserved:
                await store_client.cancel_reservation(
                    svc_ctx,
                    refund_id,
                    f"{approval_id}:expire_cancel_reservation",
                )
        except (StoreInvalidState, StoreNotFound):
            pass

    if return_id is not None:
        try:
            ret = await store_client.get_return(svc_ctx, return_id)
            if ret.status == ReturnStatus.requested:
                await store_client.transition_return(
                    svc_ctx,
                    return_id,
                    "cancelled",
                    "Approval request expired",
                    f"{approval_id}:expire_cancel_return",
                )
        except (StoreInvalidState, StoreNotFound):
            pass

    return await asyncio.to_thread(
        _finalize_expired_approval_sync,
        engine,
        approval_id=approval_id,
        expected_version=int(apr_item["version"]),
        customer_id=str(apr_item.get("customer_id", "")),
        clock=clock,
        request_id=request_id,
        actor_id=actor_id,
    )


async def expire_stale_approvals(
    *,
    clock: Clock,
    store_client: StoreClient | None = None,
    engine: Engine,
) -> dict[str, int]:
    """Scan and expire all overdue pending/needs_revalidation approvals via ServiceContext."""
    items = await asyncio.to_thread(_scan_expired_approvals_sync, engine, clock)
    if not items:
        return {"scanned": 0, "resolved": 0, "stuck": 0}

    resolved_store = store_client or get_store_client(clock=clock, settings=get_settings())
    resolved = 0
    stuck = 0
    for item in items:
        req_id = f"req-exp-{secrets.token_hex(6)}"
        try:
            done = await expire_single_approval(
                engine=engine,
                store_client=resolved_store,
                apr_item=item,
                clock=clock,
                request_id=req_id,
                actor_id="reconciler",
            )
            if done:
                resolved += 1
        except (StoreClientError, TimeoutError) as exc:
            _logger.warning(
                "approval_expiry_store_error",
                approval_id=str(item.get("approval_id")),
                error=type(exc).__name__,
            )
            stuck += 1

    return {"scanned": len(items), "resolved": resolved, "stuck": stuck}
