"""Short-transaction database helpers for refund release context and audits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select

from sellershield_agent.audit.models import ActorType, EventType, Outcome
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.models import CustomerRiskFacts
from sellershield_agent.domain.state import approvals, executions, proposals
from sellershield_agent.execution.common import SagaPlan, load_saga_plan_from_row
from sellershield_agent.execution.db_ops import append_audit_in_tx, update_approval_metadata_in_tx
from sellershield_agent.proposals.integrity import load_proposal_decision_snapshot


@dataclass(frozen=True)
class ReleaseContext:
    """Loaded proposal, approval, execution, and snapshot context for release checks."""

    plan: SagaPlan
    return_id: str | None
    refund_id: str | None
    evidence_ids: list[str]
    fallback_risk: CustomerRiskFacts
    previous_refunds_30d_paise: int
    approval: dict[str, Any] | None
    release_step: dict[str, Any] | None
    by_step: dict[str, dict[str, Any]]


def _extract_evidence_ids(payload_json: Any) -> list[str]:
    try:
        parsed = json.loads(str(payload_json))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    raw_ev = parsed.get("evidence_ids")
    return [str(e) for e in raw_ev if isinstance(e, str)] if isinstance(raw_ev, list) else []


def load_release_context_sync(
    engine: Engine,
    *,
    return_id: str | None = None,
    proposal_id: str | None = None,
    request_id: str | None = None,
) -> ReleaseContext | None:
    """Load proposal, approval, executions, and stored risk snapshot in one read_tx."""
    with read_tx(engine) as conn:
        resolved_pid = proposal_id
        if resolved_pid is None and return_id is not None:
            cr_row = conn.execute(
                select(executions.c.proposal_id)
                .where(
                    executions.c.step == "create_return",
                    executions.c.store_ref == return_id,
                )
                .order_by(executions.c.started_at.desc())
                .limit(1)
            ).fetchone()
            if cr_row is not None:
                resolved_pid = str(cr_row.proposal_id)

        if resolved_pid is None and return_id is not None:
            apr_by_ret = conn.execute(
                select(approvals.c.proposal_id)
                .where(approvals.c.return_id == return_id)
                .limit(1)
            ).fetchone()
            if apr_by_ret is not None:
                resolved_pid = str(apr_by_ret.proposal_id)

        if resolved_pid is None:
            return None

        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == resolved_pid)
        ).fetchone()
        if prop_row is None:
            return None

        plan = load_saga_plan_from_row(prop_row, request_id=request_id)
        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == resolved_pid)
        ).fetchall()
        apr_row = conn.execute(
            select(approvals).where(approvals.c.proposal_id == resolved_pid)
        ).fetchone()

        by_step = {str(r.step): dict(r._mapping) for r in exec_rows}
        resolved_ret_id = return_id or (
            str(by_step["create_return"]["store_ref"])
            if "create_return" in by_step and by_step["create_return"].get("store_ref")
            else (str(apr_row.return_id) if apr_row is not None and apr_row.return_id else None)
        )
        resolved_rfd_id = (
            str(by_step["reserve_refund"]["store_ref"])
            if "reserve_refund" in by_step and by_step["reserve_refund"].get("store_ref")
            else (str(apr_row.refund_id) if apr_row is not None and apr_row.refund_id else None)
        )
        fallback_risk, prev_refunds, _ = load_proposal_decision_snapshot(
            conn,
            customer_id=plan.customer_id,
            order_id=plan.order_id,
            proposal_id=plan.proposal_id,
        )
        return ReleaseContext(
            plan=plan,
            return_id=resolved_ret_id,
            refund_id=resolved_rfd_id,
            evidence_ids=_extract_evidence_ids(prop_row.payload_json),
            fallback_risk=fallback_risk,
            previous_refunds_30d_paise=prev_refunds,
            approval=dict(apr_row._mapping) if apr_row is not None else None,
            release_step=by_step.get("release_refund"),
            by_step=by_step,
        )


def record_release_denied_sync(
    engine: Engine,
    *,
    resource_type: str,
    resource_id: str,
    session_id: str | None,
    policy_version: str | None,
    reason: str,
    details: dict[str, Any],
    clock: Clock,
    request_id: str,
    actor_type: ActorType | str = ActorType.SYSTEM,
    actor_id: str = "webhook_release",
) -> None:
    """Audit a blocked refund release attempt cleanly without mutating store state."""
    with write_tx(engine) as tx:
        append_audit_in_tx(
            tx,
            clock=clock,
            request_id=request_id,
            session_id=session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_type=EventType.EXECUTION_STEP,
            resource_type=resource_type,
            resource_id=resource_id[:64],
            action="release_refund_blocked",
            outcome=Outcome.DENIED,
            policy_version=policy_version,
            details={"reason": reason, **details},
        )


def sync_approval_refund_status_sync(
    engine: Engine,
    *,
    proposal_id: str,
    refund_status: str,
    last_error: str | None = None,
) -> None:
    """Synchronize approvals.refund_status and optional last_error inside a short write_tx."""
    with write_tx(engine) as tx:
        values: dict[str, Any] = {"refund_status": refund_status}
        if last_error is not None:
            values["last_error"] = last_error
        update_approval_metadata_in_tx(tx, proposal_id=proposal_id, values=values)
        append_audit_in_tx(
            tx,
            request_id=None,
            session_id=None,
            actor_type=ActorType.SYSTEM,
            actor_id="saga_reconciler",
            event_type=EventType.EXECUTION_STEP,
            resource_type="approvals",
            resource_id=proposal_id,
            action="sync_approval_refund_status",
            outcome=Outcome.SUCCESS,
            policy_version="v3",
            details=values,
        )
