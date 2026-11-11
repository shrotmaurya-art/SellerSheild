"""Synchronous database transaction units for proposal confirmation and cancellation."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, select, update

from sellershield_agent.audit.models import Outcome
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.state import (
    EntityNotFound,
    InvalidTransition,
    ProposalStatus,
    VersionMismatch,
    http_idempotency_keys,
    proposals,
    transition_proposal,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.policy.rules import DecisionOutcome
from sellershield_agent.proposals.confirm_audit import (
    FINAL_MESSAGE_KEYS,
    ActionSpec,
    append_action_audit,
    now_utc_iso,
    record_not_found_audit,
    record_rejection_in_tx,
    save_idempotency_row,
)
from sellershield_agent.proposals.executor import ExecutionResult
from sellershield_agent.proposals.integrity import (
    PreparedProposal,
    ProposalValidationError,
    build_prepared_proposal,
    check_idempotency_row,
    check_overlap_guard,
    validate_stored_proposal,
)
from sellershield_agent.proposals.revalidate import (
    RevalidationOutcome,
    build_non_proposal_preview,
)
from sellershield_agent.proposals.service import persist_proposal_and_build_card


def _select_owned_proposal(conn: Any, spec: ActionSpec) -> Any:
    return conn.execute(
        select(proposals).where(
            proposals.c.proposal_id == spec.proposal_id,
            proposals.c.customer_id == spec.customer_id,
        )
    ).fetchone()


def prepare_action_sync(
    engine: Engine, spec: ActionSpec
) -> tuple[dict[str, Any] | None, PreparedProposal | None]:
    """Validate ownership, idempotency replay, hash binding, status, and overlap."""
    missing = False
    rejected_err: ProposalValidationError | None = None
    with read_tx(engine) as conn:
        # Ownership-first query BEFORE idempotency check prevents cross-tenant key oracle.
        row = _select_owned_proposal(conn, spec)
        if row is None:
            with engine.connect() as retry_conn:
                row = _select_owned_proposal(retry_conn, spec)
        if row is None:
            missing = True
        else:
            replay = check_idempotency_row(
                conn,
                customer_id=spec.customer_id,
                idempotency_key=spec.idempotency_key,
                request_hash=spec.request_hash,
            )
            if replay is not None:
                return replay, None

            try:
                payload, order_id, norm_items, canonical_amounts, method = validate_stored_proposal(
                    row, clock=spec.clock, expected_hash=spec.expected_hash
                )
                if spec.operation == "confirm":
                    check_overlap_guard(
                        conn,
                        proposal_id=spec.proposal_id,
                        customer_id=spec.customer_id,
                        order_id=order_id,
                        items=norm_items,
                    )
                    prepared = build_prepared_proposal(
                        conn,
                        row,
                        customer_id=spec.customer_id,
                        order_id=order_id,
                        norm_items=norm_items,
                        canonical_amounts=canonical_amounts,
                        method=method,
                        payload=payload,
                    )
                    return None, prepared
                return None, None
            except ProposalValidationError as err:
                rejected_err = err

    if missing:
        record_not_found_audit(engine, spec)
        raise AppHTTPException(status_code=404, code="not_found", message="Proposal not found")

    if rejected_err is not None:
        with write_tx(engine) as tx:
            row = _select_owned_proposal(tx, spec)
            record_rejection_in_tx(tx, spec, row, rejected_err)
        raise rejected_err

    return None, None


def record_stale_and_preview_sync(
    engine: Engine,
    spec: ActionSpec,
    prepared: PreparedProposal,
    reval: RevalidationOutcome,
) -> tuple[dict[str, Any] | None, dict[str, Any], AppHTTPException | None]:
    """Persist failed confirm audit and replacement preview inside a single write_tx."""
    with write_tx(engine) as tx:
        replay = check_idempotency_row(
            tx,
            customer_id=spec.customer_id,
            idempotency_key=spec.idempotency_key,
            request_hash=spec.request_hash,
        )
        if replay is not None:
            return replay, {}, None

        row = _select_owned_proposal(tx, spec)
        if row is None:
            return (
                None,
                {},
                AppHTTPException(status_code=404, code="not_found", message="Proposal not found"),
            )

        try:
            validate_stored_proposal(row, clock=spec.clock, expected_hash=spec.expected_hash)
        except ProposalValidationError as err:
            record_rejection_in_tx(tx, spec, row, err)
            return None, {}, err

        fresh_preview: dict[str, Any]
        fresh_pid: str | None = None
        if (
            reval.effective_outcome in (DecisionOutcome.allow, DecisionOutcome.needs_approval)
            and reval.order is not None
            and reval.live_facts is not None
            and reval.live_decision is not None
        ):
            extra_payload: dict[str, Any] = (
                {"cause": prepared.cause or "lost"}
                if prepared.proposal_type == "refund"
                else {
                    "reason_code": prepared.reason_code,
                    "evidence_ids": list(prepared.evidence_ids),
                }
            )
            card_payload, new_pid, rate_limited = persist_proposal_and_build_card(
                engine,
                clock=spec.clock,
                policy=reval.active_policy,
                request_id=spec.request_id,
                session_id=prepared.session_id,
                customer_id=spec.customer_id,
                order=reval.order,
                proposal_type=prepared.proposal_type,
                requested_items=reval.requested_items,
                facts=reval.live_facts,
                decision=reval.live_decision,
                effective_outcome=reval.effective_outcome,
                method_str=reval.method_str,
                resolution_str=reval.resolution_str,
                sanitized_reason_text=prepared.sanitized_reason_text,
                extra_payload=extra_payload,
                tx=tx,
                exclude_proposal_ids={prepared.proposal_id},
                expected_policy_version=reval.active_policy.version,
                expected_decision=reval.effective_outcome.value.lower(),
                expected_amounts=reval.live_amounts,
            )
            if not rate_limited and card_payload is not None:
                fresh_preview = card_payload
                fresh_pid = new_pid
            else:
                fresh_preview = build_non_proposal_preview(
                    reval.live_decision, reval.effective_outcome, rule_text=reval.rule_text
                )
        else:
            fresh_preview = build_non_proposal_preview(
                reval.live_decision, reval.effective_outcome, rule_text=reval.rule_text
            )

        audit_details: dict[str, Any] = {"reason": "stale_state"}
        if fresh_pid:
            audit_details["fresh_proposal_id"] = fresh_pid
        append_action_audit(
            tx,
            spec,
            outcome=Outcome.FAILED,
            row=row,
            session_id=prepared.session_id,
            policy_version=reval.active_policy.version,
            details=audit_details,
        )
        return None, fresh_preview, None


def claim_confirm_sync(
    engine: Engine, spec: ActionSpec
) -> tuple[bool, dict[str, Any], int, AppHTTPException | None]:
    """Atomically transition proposed -> confirmed -> executing with confirm audit."""
    with write_tx(engine) as tx:
        replay = check_idempotency_row(
            tx,
            customer_id=spec.customer_id,
            idempotency_key=spec.idempotency_key,
            request_hash=spec.request_hash,
        )
        if replay is not None:
            return True, replay, 0, None

        row = _select_owned_proposal(tx, spec)
        if row is None:
            return (
                False,
                {},
                0,
                AppHTTPException(status_code=404, code="not_found", message="Proposal not found"),
            )

        try:
            _, order_id, norm_items, _, _ = validate_stored_proposal(
                row, clock=spec.clock, expected_hash=spec.expected_hash
            )
            check_overlap_guard(
                tx,
                proposal_id=spec.proposal_id,
                customer_id=spec.customer_id,
                order_id=order_id,
                items=norm_items,
            )
        except ProposalValidationError as err:
            record_rejection_in_tx(tx, spec, row, err)
            return False, {}, 0, err

        now_iso = now_utc_iso(spec.clock)
        try:
            v_confirmed = transition_proposal(
                tx,
                proposal_id=spec.proposal_id,
                to_status=ProposalStatus.confirmed,
                expected_version=int(row.version),
                clock=spec.clock,
                request_id=spec.request_id,
                actor_id=spec.customer_id,
                extra_values={"confirmed_at": now_iso},
            )
            v_executing = transition_proposal(
                tx,
                proposal_id=spec.proposal_id,
                to_status=ProposalStatus.executing,
                expected_version=v_confirmed,
                clock=spec.clock,
                request_id=spec.request_id,
                actor_id=spec.customer_id,
            )
        except (InvalidTransition, VersionMismatch, EntityNotFound):
            conflict_err = ProposalValidationError(
                status_code=409,
                code="invalid_state",
                message="Proposal is already confirmed",
                details={"reason": "already_confirmed"},
                reason="already_confirmed",
            )
            append_action_audit(
                tx,
                spec,
                outcome=Outcome.DENIED,
                row=row,
                details={"reason": "already_confirmed"},
            )
            return False, {}, 0, conflict_err

        append_action_audit(
            tx,
            spec,
            outcome=Outcome.SUCCESS,
            row=row,
            details={
                "decision": str(row.decision),
                "proposal_hash": str(row.proposal_hash),
                "to_status": ProposalStatus.executing.value,
            },
        )
        initial_resp: dict[str, Any] = {
            "proposal_id": spec.proposal_id,
            "status": ProposalStatus.executing.value,
            "return_id": None,
            "approval_id": None,
            "refund_id": None,
            "message_key": FINAL_MESSAGE_KEYS["executing"],
        }
        save_idempotency_row(tx, spec, initial_resp, now_iso)
        return False, initial_resp, v_executing, None


def finalize_confirm_sync(
    engine: Engine,
    spec: ActionSpec,
    exec_result: ExecutionResult,
    executing_version: int,
) -> dict[str, Any]:
    """Finalize proposal status (when terminal/unknown) and update idempotency response."""
    final_status = exec_result.status
    msg_key = exec_result.message_key or FINAL_MESSAGE_KEYS[final_status]
    final_resp: dict[str, Any] = {
        "proposal_id": spec.proposal_id,
        "status": final_status,
        "return_id": exec_result.return_id,
        "approval_id": exec_result.approval_id,
        "refund_id": exec_result.refund_id,
        "message_key": msg_key,
    }
    with write_tx(engine) as tx:
        if final_status in (
            ProposalStatus.succeeded.value,
            ProposalStatus.failed.value,
            ProposalStatus.unknown.value,
        ):
            try:
                transition_proposal(
                    tx,
                    proposal_id=spec.proposal_id,
                    to_status=final_status,
                    expected_version=executing_version,
                    clock=spec.clock,
                    request_id=spec.request_id,
                    actor_id=spec.customer_id,
                )
            except (InvalidTransition, VersionMismatch, EntityNotFound):
                raise AppHTTPException(
                    status_code=409,
                    code="invalid_state",
                    message="Proposal state transition conflict",
                    details={"reason": "invalid_state"},
                ) from None
        else:
            append_action_audit(
                tx,
                spec,
                outcome=Outcome.SUCCESS,
                details={"final_status": final_status},
            )

        tx.execute(
            update(http_idempotency_keys)
            .where(
                http_idempotency_keys.c.customer_id == spec.customer_id,
                http_idempotency_keys.c.idempotency_key == spec.idempotency_key,
            )
            .values(response_json=json.dumps(final_resp, sort_keys=True, separators=(",", ":")))
        )
    return final_resp
