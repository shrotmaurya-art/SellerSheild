"""Customer proposal confirmation and cancellation orchestration service."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select

from sellershield_agent.audit.models import Outcome
from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, write_tx
from sellershield_agent.domain.state import (
    EntityNotFound,
    InvalidTransition,
    ProposalStatus,
    VersionMismatch,
    proposals,
    transition_proposal,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.execution.saga import Executor as SagaExecutor
from sellershield_agent.proposals.confirm_audit import (
    ActionSpec,
    append_action_audit,
    now_utc_iso,
    record_rejection_in_tx,
    save_idempotency_row,
)
from sellershield_agent.proposals.confirm_store import (
    claim_confirm_sync,
    finalize_confirm_sync,
    prepare_action_sync,
    record_stale_and_preview_sync,
)
from sellershield_agent.proposals.executor import ExecutionResult, Executor
from sellershield_agent.proposals.integrity import (
    ProposalValidationError,
    check_idempotency_row,
    compute_request_hash,
    validate_stored_proposal,
)
from sellershield_agent.proposals.revalidate import revalidate_against_store
from sellershield_agent.store import get_store_client
from sellershield_agent.store.client import RequestContext, StoreClient


class ConfirmProposalResponse(BaseModel):
    """Response schema for POST /proposals/{id}/confirm."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str
    return_id: str | None = None
    approval_id: str | None = None
    refund_id: str | None = None
    message_key: str


class CancelProposalResponse(BaseModel):
    """Response schema for POST /proposals/{id}/cancel."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: str


def _commit_cancel_sync(
    engine: Engine, spec: ActionSpec
) -> tuple[dict[str, Any], AppHTTPException | None]:
    with write_tx(engine) as tx:
        replay = check_idempotency_row(
            tx,
            customer_id=spec.customer_id,
            idempotency_key=spec.idempotency_key,
            request_hash=spec.request_hash,
        )
        if replay is not None:
            return replay, None

        row = tx.execute(
            select(proposals).where(
                proposals.c.proposal_id == spec.proposal_id,
                proposals.c.customer_id == spec.customer_id,
            )
        ).fetchone()
        if row is None:
            return {}, AppHTTPException(
                status_code=404, code="not_found", message="Proposal not found"
            )

        try:
            validate_stored_proposal(row, clock=spec.clock, expected_hash=None)
        except ProposalValidationError as err:
            record_rejection_in_tx(tx, spec, row, err)
            return {}, err

        now_iso = now_utc_iso(spec.clock)
        try:
            transition_proposal(
                tx,
                proposal_id=spec.proposal_id,
                to_status=ProposalStatus.cancelled,
                expected_version=int(row.version),
                clock=spec.clock,
                request_id=spec.request_id,
                actor_id=spec.customer_id,
            )
        except (InvalidTransition, VersionMismatch, EntityNotFound):
            conflict_err = ProposalValidationError(
                status_code=409,
                code="invalid_state",
                message="Proposal cannot be modified in its current state",
                details={"reason": "invalid_state"},
                reason="invalid_state",
            )
            append_action_audit(
                tx,
                spec,
                outcome=Outcome.DENIED,
                row=row,
                details={"reason": "invalid_state"},
            )
            return {}, conflict_err

        append_action_audit(
            tx,
            spec,
            outcome=Outcome.SUCCESS,
            row=row,
            details={"to_status": ProposalStatus.cancelled.value},
        )
        resp_dict: dict[str, Any] = {
            "proposal_id": spec.proposal_id,
            "status": ProposalStatus.cancelled.value,
        }
        save_idempotency_row(tx, spec, resp_dict, now_iso)
        return resp_dict, None


async def confirm_proposal(
    ctx: RequestContext,
    proposal_id: str,
    proposal_hash: str,
    *,
    engine: Engine | None = None,
    store_client: StoreClient | None = None,
    executor: Executor | None = None,
    clock: Clock | None = None,
    idempotency_key: str | None = None,
    request_path: str | None = None,
) -> ConfirmProposalResponse:
    """Validate hash binding, revalidate against store/policy, claim, and execute once."""
    if ctx.principal.role != Role.customer or not ctx.principal.customer_id:
        raise AppHTTPException(status_code=403, code="forbidden", message="Access denied")

    resolved_engine = engine or get_engine()
    resolved_clock = clock or SystemClock()
    resolved_store = store_client or get_store_client(clock=resolved_clock, settings=get_settings())
    resolved_executor = executor or SagaExecutor(
        store_client=resolved_store,
        engine=resolved_engine,
        clock=resolved_clock,
    )
    resolved_idem = idempotency_key or f"direct:{proposal_id}:{secrets.token_hex(8)}"
    resolved_path = request_path or f"/v1/proposals/{proposal_id}/confirm"

    spec = ActionSpec(
        operation="confirm",
        proposal_id=proposal_id,
        customer_id=ctx.principal.customer_id,
        idempotency_key=resolved_idem,
        request_hash=compute_request_hash(
            operation="confirm",
            proposal_id=proposal_id,
            canonical_body={"proposal_hash": proposal_hash},
        ),
        expected_hash=proposal_hash,
        clock=resolved_clock,
        request_id=ctx.request_id,
        request_path=resolved_path,
    )

    replay, prepared = await asyncio.to_thread(prepare_action_sync, resolved_engine, spec)
    if replay is not None:
        return ConfirmProposalResponse.model_validate(replay)
    assert prepared is not None

    reval = await revalidate_against_store(
        prepared,
        store_client=resolved_store,
        customer_ctx=ctx,
        clock=resolved_clock,
    )
    if reval.is_stale:
        replay_stale, fresh_preview, stale_err = await asyncio.to_thread(
            record_stale_and_preview_sync, resolved_engine, spec, prepared, reval
        )
        if replay_stale is not None:
            return ConfirmProposalResponse.model_validate(replay_stale)
        if stale_err is not None:
            raise stale_err
        raise AppHTTPException(
            status_code=409,
            code="invalid_state",
            message="Proposal state changed before confirmation",
            details={"reason": "stale_state", "fresh_preview": fresh_preview},
        )

    is_replay, claim_resp, executing_version, claim_err = await asyncio.to_thread(
        claim_confirm_sync, resolved_engine, spec
    )
    if claim_err is not None:
        raise claim_err
    if is_replay:
        return ConfirmProposalResponse.model_validate(claim_resp)

    try:
        exec_result = await resolved_executor.execute(
            ctx,
            prepared,
            clock=resolved_clock,
            idempotency_key=resolved_idem,
        )
    except Exception:
        exec_result = ExecutionResult(status="unknown", message_key="proposal.unknown")

    final_resp = await asyncio.to_thread(
        finalize_confirm_sync, resolved_engine, spec, exec_result, executing_version
    )
    return ConfirmProposalResponse.model_validate(final_resp)


async def confirm_proposal_action(
    *,
    engine: Engine,
    store_client: StoreClient,
    customer_ctx: RequestContext,
    proposal_id: str,
    proposal_hash: str,
    idempotency_key: str,
    clock: Clock,
    request_path: str,
    executor: Executor | None = None,
) -> ConfirmProposalResponse:
    """Delegate HTTP/action confirmation parameters to confirm_proposal."""
    return await confirm_proposal(
        customer_ctx,
        proposal_id,
        proposal_hash,
        engine=engine,
        store_client=store_client,
        executor=executor,
        clock=clock,
        idempotency_key=idempotency_key,
        request_path=request_path,
    )


async def cancel_proposal_action(
    *,
    engine: Engine,
    customer_id: str,
    proposal_id: str,
    idempotency_key: str,
    clock: Clock,
    request_id: str,
    request_path: str,
) -> CancelProposalResponse:
    """Validate ownership/state and cancel a proposed proposal atomically."""
    spec = ActionSpec(
        operation="cancel",
        proposal_id=proposal_id,
        customer_id=customer_id,
        idempotency_key=idempotency_key,
        request_hash=compute_request_hash(
            operation="cancel",
            proposal_id=proposal_id,
            canonical_body={},
        ),
        expected_hash=None,
        clock=clock,
        request_id=request_id,
        request_path=request_path,
    )

    replay, _ = await asyncio.to_thread(prepare_action_sync, engine, spec)
    if replay is not None:
        return CancelProposalResponse.model_validate(replay)

    committed, commit_err = await asyncio.to_thread(_commit_cancel_sync, engine, spec)
    if commit_err is not None:
        raise commit_err
    return CancelProposalResponse.model_validate(committed)
