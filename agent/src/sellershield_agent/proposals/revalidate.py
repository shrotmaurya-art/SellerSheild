"""Live store and policy revalidation for customer proposal confirmation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import (
    Order,
    PaymentMethod,
    RefundMethod,
    ReturnItem,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.domain.state import sanitize_policy_facts
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.orchestrator.cards import sanitize_text
from sellershield_agent.policy.decide import Decision, decide
from sellershield_agent.policy.facts import DecisionAction, DecisionFacts
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.policy.rules import ApproverRole, DecisionOutcome
from sellershield_agent.proposals.integrity import PreparedProposal
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreClientError,
    StoreConflict,
    StoreForbidden,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)

TEMPORAL_FACT_KEYS = frozenset(
    {
        "now",
        "placed_at",
        "shipped_at",
        "delivered_at",
        "cancelled_at",
        "eta_at",
        "occurred_at",
        "created_at",
        "updated_at",
        "authorized_at",
        "reason_text",
    }
)


@dataclass(frozen=True)
class RevalidationOutcome:
    """Result of live store and active policy revalidation before confirmation."""

    is_stale: bool
    reason: str | None
    active_policy: PolicyDocument
    order: Order | None
    live_facts: DecisionFacts | None
    live_decision: Decision | None
    effective_outcome: DecisionOutcome
    requested_items: list[ReturnItem]
    method_str: str
    resolution_str: str
    live_amounts: dict[str, int] | None
    rule_text: str


def _strip_temporal_keys(val: Any) -> Any:
    if isinstance(val, Mapping):
        return {
            str(k): _strip_temporal_keys(v)
            for k, v in val.items()
            if str(k) not in TEMPORAL_FACT_KEYS
        }
    if isinstance(val, (list, tuple)):
        return [_strip_temporal_keys(item) for item in val]
    return val


def normalize_non_temporal_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Sanitize and strip temporal fields so benign clock advances match."""
    sanitized = sanitize_policy_facts(raw)
    stripped = _strip_temporal_keys(sanitized)
    return dict(stripped) if isinstance(stripped, dict) else {}


def _extract_customer_rule_text(decision: Decision) -> str:
    matching = [
        r.customer_text
        for r in decision.rule_results
        if not r.passed and r.blocking and r.outcome_if_failed == decision.decision
    ]
    if matching:
        return " ".join(matching)
    blocking = [r.customer_text for r in decision.rule_results if not r.passed and r.blocking]
    if blocking:
        return " ".join(blocking)
    return "Your request has been evaluated against store policy."


def build_non_proposal_preview(
    decision: Decision | None,
    effective_outcome: DecisionOutcome,
    *,
    rule_text: str | None = None,
) -> dict[str, Any]:
    """Build sanitized non-proposal fresh_preview payload for stale confirmations."""
    clean_rule_text = sanitize_text(
        rule_text
        or (
            _extract_customer_rule_text(decision)
            if decision is not None
            else "This request is no longer eligible under the current order state."
        ),
        200,
    )
    preview: dict[str, Any] = {
        "proposal": None,
        "decision": effective_outcome.value.lower(),
        "rule_text": clean_rule_text,
    }
    if effective_outcome == DecisionOutcome.needs_info and decision is not None:
        preview["missing_slots"] = [sanitize_text(s, 32) for s in decision.missing_slots]
    elif effective_outcome == DecisionOutcome.escalate:
        preview["handoff_hint"] = (
            "This request requires assistance from seller support. "
            "Would you like me to create a support case?"
        )
    return preview


def _conflict(message: str) -> AppHTTPException:
    return AppHTTPException(status_code=409, code="invalid_state", message=message)


async def revalidate_against_store(
    prepared: PreparedProposal,
    *,
    store_client: StoreClient,
    customer_ctx: RequestContext,
    clock: Clock,
) -> RevalidationOutcome:
    """Revalidate proposal eligibility and amounts against live store state.

    Must be called outside DB write locks (KP-01). Strips temporal datetime offsets
    from facts snapshots before comparison so normal clock advances within the
    validity window do not cause false invalidation while real mutations do.
    """
    try:
        active_policy = load_policy()
    except (FileNotFoundError, ValueError):
        raise _conflict("Active policy cannot validate this proposal") from None

    requested_items = [
        ReturnItem(item_id=str(it["item_id"]), qty=int(it["quantity"])) for it in prepared.items
    ]
    req_pairs = [(it.item_id, it.qty) for it in requested_items]

    def _stale_deny(rule_text: str, order_obj: Order | None = None) -> RevalidationOutcome:
        return RevalidationOutcome(
            is_stale=True,
            reason="stale_state",
            active_policy=active_policy,
            order=order_obj,
            live_facts=None,
            live_decision=None,
            effective_outcome=DecisionOutcome.deny,
            requested_items=requested_items,
            method_str=prepared.method,
            resolution_str=prepared.resolution,
            live_amounts=None,
            rule_text=rule_text,
        )

    order: Order | None = None
    evidence_ok = True
    try:
        order = await store_client.get_order(customer_ctx, prepared.order_id)
        if order.customer_id != prepared.customer_id:
            return _stale_deny("Order is no longer available for this request.")
        refundable = await store_client.get_refundable(
            customer_ctx, prepared.order_id, items=req_pairs
        )
        active_returns = await store_client.list_returns(customer_ctx, prepared.order_id)
        try:
            shipment = await store_client.get_shipment(customer_ctx, prepared.order_id)
        except StoreNotFound:
            shipment = None

        if prepared.proposal_type == "return" and prepared.evidence_ids:
            list_ev = getattr(store_client, "list_evidence", None)
            if callable(list_ev):
                store_ev = await list_ev(customer_ctx, prepared.order_id)
                valid_ids = {getattr(ev, "evidence_id", None) for ev in store_ev}
                if not set(prepared.evidence_ids).issubset(valid_ids):
                    evidence_ok = False
    except (StoreTimeout, StoreUnavailable):
        raise AppHTTPException(
            status_code=503,
            code="upstream_unavailable",
            message="Store service temporarily unavailable",
        ) from None
    except (
        StoreNotFound,
        StoreConflict,
        StoreForbidden,
        StoreInvalidState,
        StoreLimitExceeded,
        StoreValidationError,
        StoreClientError,
    ):
        return _stale_deny("Order state changed and is no longer eligible for this request.", order)

    try:
        refund_method = RefundMethod(prepared.method)
        resolution = ReturnResolution(prepared.resolution)
        reason_code = (
            ReturnReasonCode.defective
            if prepared.proposal_type == "refund"
            else ReturnReasonCode(prepared.reason_code)
        )
    except ValueError:
        return _stale_deny("Proposal parameters are no longer eligible.", order)

    if prepared.proposal_type == "return":
        if (
            order.payment_method == PaymentMethod.cod
            and resolution == ReturnResolution.refund
            and refund_method == RefundMethod.original
        ):
            return _stale_deny("Selected refund method is not permitted for Cash on Delivery.")
        action = DecisionAction.return_
        evidence_present = bool(prepared.evidence_ids) and evidence_ok
    elif prepared.proposal_type == "refund":
        if order.payment_method == PaymentMethod.cod and refund_method == RefundMethod.original:
            return _stale_deny("Selected refund method is not permitted for Cash on Delivery.")
        action = DecisionAction.refund_request
        resolution = ReturnResolution.refund
        evidence_present = False
    elif prepared.proposal_type == "cancel":
        action = DecisionAction.cancellation
        resolution = ReturnResolution.refund
        evidence_present = False
    else:
        return _stale_deny("Unsupported proposal type.", order)

    live_facts = DecisionFacts(
        action=action,
        order=order,
        items=requested_items,
        reason_code=reason_code,
        reason_text=None,
        resolution=resolution,
        refund_method=refund_method,
        evidence_present=evidence_present,
        active_returns=active_returns,
        previous_refunds_30d_paise=prepared.previous_refunds_30d_paise,
        refundable=refundable,
        customer_risk=prepared.customer_risk,
        shipment=shipment,
        now=clock.now(),
    )
    live_decision = decide(live_facts, active_policy, live_facts.now)

    if prepared.proposal_type == "refund":
        if live_decision.decision in (
            DecisionOutcome.deny,
            DecisionOutcome.needs_info,
            DecisionOutcome.escalate,
        ):
            effective_outcome = DecisionOutcome(live_decision.decision)
        else:
            effective_outcome = DecisionOutcome.needs_approval
            if (
                live_decision.decision != DecisionOutcome.needs_approval
                or live_decision.required_approver_role is None
            ):
                role = (
                    ApproverRole.seller_owner
                    if live_decision.amounts.total_paise > active_policy.staff_approval_limit_paise
                    else ApproverRole.seller_staff
                )
                live_decision = live_decision.model_copy(
                    update={"decision": effective_outcome, "required_approver_role": role}
                )
    else:
        effective_outcome = DecisionOutcome(live_decision.decision)

    live_amounts = {
        "items_paise": live_decision.amounts.items_paise,
        "shipping_refund_paise": live_decision.amounts.shipping_refund_paise,
        "fee_paise": live_decision.amounts.fee_paise,
        "total_paise": live_decision.amounts.total_paise,
    }
    facts_drifted = False
    if prepared.facts_snapshot is not None:
        facts_drifted = normalize_non_temporal_facts(
            prepared.facts_snapshot
        ) != normalize_non_temporal_facts(live_facts.model_dump(mode="json"))

    is_stale = (
        active_policy.version != prepared.policy_version
        or effective_outcome.value.lower() != prepared.decision
        or live_amounts != prepared.canonical_amounts
        or not evidence_ok
        or facts_drifted
    )
    return RevalidationOutcome(
        is_stale=is_stale,
        reason="stale_state" if is_stale else None,
        active_policy=active_policy,
        order=order,
        live_facts=live_facts,
        live_decision=live_decision,
        effective_outcome=effective_outcome,
        requested_items=requested_items,
        method_str=refund_method.value,
        resolution_str=resolution.value,
        live_amounts=live_amounts,
        rule_text=_extract_customer_rule_text(live_decision),
    )
