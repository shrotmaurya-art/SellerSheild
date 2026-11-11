"""Exhaustive T20 coverage test across every public mutator in SellerShield AI.

Verifies that patching AuditService.append to raise causes every single public
mutator to fail loudly and roll back all DB state without partial writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

import sellershield_agent.domain.state as state_mod
from sellershield_agent.api.chat import create_chat_session
from sellershield_agent.approvals.common_db import append_audit_sync, save_idempotency_sync
from sellershield_agent.approvals.decide_db import (
    claim_approving_sync,
    finalize_approved_sync,
    finalize_rejected_sync,
    record_reject_error_sync,
    record_revalidation_needed_sync,
    rollback_approving_to_pending_sync,
)
from sellershield_agent.approvals.expire import _finalize_expired_approval_sync
from sellershield_agent.approvals.withdraw_db import finalize_withdrawn_sync
from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth.deps import record_auth_denied
from sellershield_agent.config import get_settings
from sellershield_agent.db import reset_engine
from sellershield_agent.domain.models import ReturnItem
from sellershield_agent.domain.state import (
    ApprovalStatus,
    ProposalStatus,
    record_policy_decision,
    transition_approval,
    transition_proposal,
)
from sellershield_agent.execution.db_ops import (
    finish_execution_step_sync,
    prepare_run_sync,
    start_execution_step_sync,
)
from sellershield_agent.execution.finalize_db import finalize_approval_sync, finalize_proposal_sync
from sellershield_agent.execution.refund_webhook_db import apply_refund_updated_sync
from sellershield_agent.execution.release_db import (
    record_release_denied_sync,
    sync_approval_refund_status_sync,
)
from sellershield_agent.execution.webhook_db import (
    ingest_webhook_event_sync,
    record_webhook_auth_denied_sync,
)
from sellershield_agent.policy.rules import DecisionOutcome
from sellershield_agent.proposals.confirm import _commit_cancel_sync
from sellershield_agent.proposals.confirm_store import (
    claim_confirm_sync,
    finalize_confirm_sync,
    prepare_action_sync,
    record_stale_and_preview_sync,
)
from sellershield_agent.proposals.service import persist_proposal_and_build_card
from tests.audit_test_helpers import setup_t20_coverage_env, snapshot_db


def _build_mutators(env: dict[str, Any]) -> list[tuple[str, Callable[[], Any]]]:
    engine = env["engine"]
    clock = env["clock"]
    pid_prop, pid_exec, pid_with_apr = env["pid_prop"], env["pid_exec"], env["pid_with_apr"]
    aid_pend, aid_aprv, v_aprv = env["aid_pend"], env["aid_aprv"], env["v_aprv"]
    ses_seed = env["ses_seed"]
    gw, pipeline = env["gateway"], env["pipeline"]
    cust_ctx, cust_p = env["cust_ctx"], env["cust_principal"]

    return [
        ("create_chat_session", partial(create_chat_session, principal=cust_p, clock=clock)),
        (
            "_persist_turn",
            partial(
                pipeline._persist_turn,
                session_id=ses_seed, customer_id="C001", client_msg_id="t20-m1",
                user_text="hi", agent_msg_id="msg_t20_1", agent_text="ok", cards=[],
            ),
        ),
        (
            "gateway_read",
            lambda: asyncio.run(
                gw.call(
                    cust_ctx,
                    "get_order",
                    {"order_id": "ORD-1042"},
                    allowed=["get_order"],
                    session_id=ses_seed,
                )
            ),
        ),
        (
            "gateway_propose",
            lambda: asyncio.run(
                gw.call(
                    cust_ctx,
                    "propose_return",
                    {
                        "order_id": "ORD-1042",
                        "items": [{"item_id": "ITEM-1042-1", "qty": 1}],
                        "reason_code": "defective",
                    },
                    allowed=["propose_return"],
                    session_id=ses_seed,
                )
            ),
        ),
        (
            "record_policy_decision",
            partial(
                record_policy_decision, engine, request_id="req-t20", customer_id="C001",
                order_id="ORD-1042", policy_version="v3", facts={"a": 1},
                decision={"b": 2}, clock=clock,
            ),
        ),
        (
            "transition",
            partial(
                state_mod.transition, engine, entity_type="proposals", entity_id=pid_prop,
                to_status=ProposalStatus.cancelled, expected_version=1, clock=clock,
                request_id="req-t20", actor_id="C-T20-PROP",
            ),
        ),
        (
            "transition_proposal",
            partial(
                transition_proposal, engine, proposal_id=pid_prop,
                to_status=ProposalStatus.cancelled, expected_version=1, clock=clock,
                request_id="req-t20", actor_id="C-T20-PROP",
            ),
        ),
        (
            "transition_approval",
            partial(
                transition_approval, engine, approval_id=aid_pend,
                to_status=ApprovalStatus.rejected, expected_version=1, clock=clock,
                request_id="req-t20", actor_id="seller-1",
            ),
        ),
        (
            "persist_proposal_and_build_card",
            partial(
                persist_proposal_and_build_card, engine, clock=clock, policy=env["policy"],
                request_id="req-t20", session_id=ses_seed, customer_id="C001", order=env["order"],
                proposal_type="return", requested_items=[ReturnItem(item_id="ITEM-1042-1", qty=1)],
                facts=env["facts"], decision=env["dec"],
                effective_outcome=DecisionOutcome.needs_approval, method_str="original",
                resolution_str="refund", sanitized_reason_text="x",
                extra_payload={"reason_code": "defective"},
            ),
        ),
        ("prepare_action_not_found", partial(prepare_action_sync, engine, env["spec_missing"])),
        ("prepare_action_expired", partial(prepare_action_sync, engine, env["spec_expired"])),
        ("claim_confirm_sync", partial(claim_confirm_sync, engine, env["spec_prop"])),
        (
            "record_stale_and_preview_sync",
            partial(
                record_stale_and_preview_sync,
                engine, env["spec_prop"], env["prep_prop"], env["reval_stale"],
            ),
        ),
        ("_commit_cancel_sync", partial(_commit_cancel_sync, engine, env["spec_cancel"])),
        (
            "finalize_confirm_pending",
            partial(
                finalize_confirm_sync, engine, env["spec_exec"],
                env["res_pend"], env["exec_prop_ver"],
            ),
        ),
        (
            "finalize_confirm_succeeded",
            partial(
                finalize_confirm_sync, engine, env["spec_exec"],
                env["res_succ"], env["exec_prop_ver"],
            ),
        ),
        (
            "prepare_run_sync",
            partial(prepare_run_sync, engine, env["plan_prop"], clock=clock, request_id="req-t20"),
        ),
        (
            "start_execution_step_sync",
            partial(
                start_execution_step_sync, engine, proposal_id=pid_exec, step="reserve_refund",
                idempotency_key=f"{pid_exec}:reserve_refund", actor_type=ActorType.SYSTEM,
                actor_id="svc", policy_version="v3", session_id=ses_seed,
                request_id="req-t20", clock=clock,
            ),
        ),
        (
            "finish_execution_step_sync",
            partial(
                finish_execution_step_sync, engine, proposal_id=pid_exec,
                execution_id=env["exec_id_exec"], step="create_return", new_status="succeeded",
                store_ref="RET-X", error_code=None, actor_type=ActorType.CUSTOMER,
                actor_id="C-T20-EXEC", policy_version="v3", session_id=ses_seed,
                request_id="req-t20", clock=clock,
                outbox_stub={"dedupe_key": "t20-obx", "template": "t", "payload": {"k": 1}},
            ),
        ),
        (
            "finalize_approval_new",
            partial(
                finalize_approval_sync, engine, plan=env["plan_exec"], return_id="RET-X",
                refund_id="RFD-X", clock=clock, request_id="req-t20",
                actor_type=ActorType.CUSTOMER, actor_id="C-T20-EXEC",
            ),
        ),
        (
            "finalize_approval_existing",
            partial(
                finalize_approval_sync, engine, plan=env["plan_apr1"], return_id="RET-APR-1",
                refund_id="RFD-APR-1", clock=clock, request_id="req-t20",
                actor_type=ActorType.CUSTOMER, actor_id="C-T20-APR1",
            ),
        ),
        (
            "finalize_proposal_idempotency_only",
            partial(
                finalize_proposal_sync, engine, plan=env["plan_exec"], result=env["res_pend"],
                clock=clock, request_id="req-t20", actor_id="C-T20-EXEC",
                update_proposal_status=False, update_idempotency=True,
            ),
        ),
        (
            "finalize_proposal_transition",
            partial(
                finalize_proposal_sync, engine, plan=env["plan_exec"], result=env["res_succ"],
                clock=clock, request_id="req-t20", actor_id="C-T20-EXEC",
                update_proposal_status=True, update_idempotency=True,
            ),
        ),
        (
            "sync_approval_refund_status_sync",
            partial(
                sync_approval_refund_status_sync, engine, proposal_id=pid_with_apr,
                refund_status="processing", last_error="retry",
            ),
        ),
        (
            "record_release_denied_sync",
            partial(
                record_release_denied_sync, engine, resource_type="proposals",
                resource_id=pid_with_apr, session_id=ses_seed, policy_version="v3",
                reason="not_approved", details={"k": 1}, clock=clock, request_id="req-t20",
            ),
        ),
        (
            "apply_refund_updated_sync",
            partial(
                apply_refund_updated_sync, engine, refund_id="RFD-APR-1", status_val="failed",
                order_id="ORD-1042", return_id="RET-APR-1", clock=clock, request_id="req-t20",
            ),
        ),
        (
            "ingest_webhook_event_sync",
            partial(
                ingest_webhook_event_sync, engine, event_id="evt-t20-wh",
                event_type="refund.updated", resource_type="refund", resource_id="RFD-APR-1",
                resource_version=9, occurred_at_iso=env["now_iso"],
                request_id="req-t20", clock=clock,
            ),
        ),
        (
            "record_webhook_auth_denied_sync",
            partial(
                record_webhook_auth_denied_sync, engine, request_id="req-t20", clock=clock,
                event_id_hint="evt-bad", reason="bad_sig", path="/v1/webhooks/store",
            ),
        ),
        (
            "record_revalidation_needed_sync",
            partial(
                record_revalidation_needed_sync, engine, approval_id=aid_pend, expected_version=1,
                diff=[{"field": "a"}], actor_id="seller-1", policy_version="v3",
                session_id=ses_seed, clock=clock, request_id="req-t20",
            ),
        ),
        (
            "claim_approving_sync",
            partial(
                claim_approving_sync, engine, approval_id=aid_pend, expected_version=1,
                actor_id="seller-1", clock=clock, request_id="req-t20",
            ),
        ),
        (
            "rollback_approving_to_pending_sync",
            partial(
                rollback_approving_to_pending_sync, engine, approval_id=aid_aprv,
                approving_version=v_aprv, actor_id="seller-1", error_summary="err",
                store_auth_ref="AUTH-1", policy_version="v3", session_id=ses_seed,
                clock=clock, request_id="req-t20",
            ),
        ),
        (
            "record_reject_error_sync",
            partial(
                record_reject_error_sync, engine, approval_id=aid_pend, actor_id="seller-1",
                error_summary="err", policy_version="v3", session_id=ses_seed,
                clock=clock, request_id="req-t20",
            ),
        ),
        (
            "finalize_approved_sync",
            partial(
                finalize_approved_sync, engine, approval_id=aid_aprv, approving_version=v_aprv,
                proposal_id=env["pid_aprv"], order_id="ORD-1042", customer_id="C-T20-APR2",
                return_id="RET-APR-2", refund_id="RFD-APR-2", amount_paise=120000,
                store_auth_ref="AUTH-2", reason="ok", actor_id="seller-1", policy_version="v3",
                session_id=ses_seed, clock=clock, request_id="req-t20",
            ),
        ),
        (
            "finalize_rejected_sync",
            partial(
                finalize_rejected_sync, engine, approval_id=aid_pend, expected_version=1,
                proposal_id=pid_with_apr, order_id="ORD-1042", customer_id="C-T20-APR1",
                return_id="RET-APR-1", refund_id="RFD-APR-1", reason="no", actor_id="seller-1",
                policy_version="v3", session_id=ses_seed, clock=clock, request_id="req-t20",
            ),
        ),
        (
            "finalize_withdrawn_sync",
            partial(
                finalize_withdrawn_sync, engine, approval_id=aid_pend, expected_version=1,
                proposal_id=pid_with_apr, order_id="ORD-1042", customer_id="C-T20-APR1",
                policy_version="v3", session_id=ses_seed, idempotency_key="idem-t20-wd",
                request_hash="h-wd", response_dict={"status": "withdrawn"},
                clock=clock, request_id="req-t20",
            ),
        ),
        (
            "_finalize_expired_approval_sync",
            partial(
                _finalize_expired_approval_sync, engine, approval_id=aid_pend, expected_version=1,
                customer_id="C-T20-APR1", clock=clock, request_id="req-t20", actor_id="reconciler",
            ),
        ),
        (
            "save_idempotency_sync",
            partial(
                save_idempotency_sync, engine, actor_key="seller:seller-1",
                idempotency_key="idem-t20-sv", operation="approve", target_id=aid_pend,
                request_hash="h-sv", response_dict={"ok": True}, clock=clock,
            ),
        ),
        (
            "append_audit_sync",
            partial(
                append_audit_sync, engine, clock=clock, request_id="req-t20", session_id=ses_seed,
                actor_type=ActorType.SYSTEM, actor_id="sys", event_type=EventType.RECONCILE,
                resource_type="approvals", resource_id=aid_pend, action="act",
                outcome=Outcome.SUCCESS, policy_version="v3", details={"k": 1},
            ),
        ),
        (
            "record_auth_denied",
            partial(
                record_auth_denied, principal=cust_p, request_id="req-t20",
                path="/v1/seller/audit", reason="role_forbidden", status_code=403, clock=clock,
            ),
        ),
    ]


def test_t20_every_public_mutator_rolls_back_on_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling EVERY public mutator with failing AuditService.append leaves DB 100% unchanged."""
    env = setup_t20_coverage_env(tmp_path, monkeypatch)
    engine = env["engine"]
    try:
        snap_before = snapshot_db(engine)

        def _audit_fail(_cls: Any, _tx: Any, _ev: AuditEvent) -> int:
            raise RuntimeError("injected_audit_failure")

        monkeypatch.setattr(AuditService, "append", classmethod(_audit_fail))
        for label, call in _build_mutators(env):
            with pytest.raises(RuntimeError, match="injected_audit_failure"):
                call()
            assert snapshot_db(engine) == snap_before, f"Mutator '{label}' left partial DB!"
    finally:
        engine.dispose()
        reset_engine()
        get_settings.cache_clear()
