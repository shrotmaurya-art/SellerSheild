"""Synchronous database queries for building and listing seller approval cards."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, select

from sellershield_agent.db import read_tx
from sellershield_agent.domain.models import CustomerRiskFacts
from sellershield_agent.domain.state import ApprovalStatus, approvals, proposals
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.proposals.integrity import load_proposal_decision_snapshot


def load_approval_db_context_sync(
    engine: Engine,
    approval_id: str,
    store_id: str | None,
) -> tuple[Any, Any, CustomerRiskFacts, int, dict[str, Any] | None, dict[str, int]]:
    """Read approval, parent proposal, decision snapshot, and past approval counts in read_tx."""
    with read_tx(engine) as conn:
        apr_row = conn.execute(
            select(approvals).where(approvals.c.approval_id == approval_id)
        ).fetchone()
        if apr_row is None:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message="Approval not found",
                details={},
            )

        row_store_id = str(getattr(apr_row, "store_id", None) or "nestkart")
        if store_id is not None and row_store_id != store_id:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message="Approval not found",
                details={},
            )

        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == apr_row.proposal_id)
        ).fetchone()
        if prop_row is None:
            raise AppHTTPException(
                status_code=404,
                code="not_found",
                message="Approval not found",
                details={},
            )

        customer_id = str(prop_row.customer_id)
        fallback_risk, prev_refunds, snapshot = load_proposal_decision_snapshot(
            conn,
            customer_id=customer_id,
            order_id=str(apr_row.order_id),
            proposal_id=str(apr_row.proposal_id),
        )

        # Compute similar past decision counts from the store's approvals table
        all_rows = conn.execute(
            select(
                approvals.c.approval_id,
                approvals.c.order_id,
                approvals.c.status,
                approvals.c.store_id,
            )
        ).fetchall()
        counts: dict[str, int] = {
            "approved_count": 0,
            "rejected_count": 0,
            "expired_count": 0,
            "withdrawn_count": 0,
            "pending_count": 0,
            "same_order_count": 0,
        }
        target_order = str(apr_row.order_id)
        for r in all_rows:
            r_store = str(getattr(r, "store_id", None) or "nestkart")
            if r_store != row_store_id:
                continue
            st = str(r.status)
            if st == ApprovalStatus.approved.value:
                counts["approved_count"] += 1
            elif st == ApprovalStatus.rejected.value:
                counts["rejected_count"] += 1
            elif st == ApprovalStatus.expired.value:
                counts["expired_count"] += 1
            elif st == ApprovalStatus.withdrawn.value:
                counts["withdrawn_count"] += 1
            elif st in (
                ApprovalStatus.pending.value,
                ApprovalStatus.approving.value,
                ApprovalStatus.needs_revalidation.value,
            ):
                counts["pending_count"] += 1

            if str(r.order_id) == target_order and str(r.approval_id) != str(apr_row.approval_id):
                counts["same_order_count"] += 1

        return apr_row, prop_row, fallback_risk, prev_refunds, snapshot, counts


def list_store_approval_ids_sync(
    engine: Engine,
    *,
    store_id: str,
    status_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[str], int]:
    """Query approval IDs for a store ordered deterministically with total count."""
    with read_tx(engine) as conn:
        stmt = select(approvals.c.approval_id, approvals.c.store_id, approvals.c.status).order_by(
            approvals.c.due_at.asc(),
            approvals.c.approval_id.asc(),
        )
        rows = conn.execute(stmt).fetchall()
        matching_ids: list[str] = []
        for r in rows:
            r_store = str(getattr(r, "store_id", None) or "nestkart")
            if r_store != store_id:
                continue
            if status_filter is not None and str(r.status) != status_filter:
                continue
            matching_ids.append(str(r.approval_id))

        total = len(matching_ids)
        paged = matching_ids[offset : offset + limit]
        return paged, total
