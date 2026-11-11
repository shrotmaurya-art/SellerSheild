"""Stored proposal integrity, risk loading, idempotency, and overlap helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, TypeGuard

from sqlalchemy import Connection, select

from sellershield_agent.audit.models import audit_events
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import (
    CustomerRiskFacts,
    ReturnReasonCode,
    ReturnResolution,
)
from sellershield_agent.domain.state import (
    ProposalStatus,
    http_idempotency_keys,
    is_proposal_expired,
    policy_decisions,
    proposals,
)
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.proposals.hashing import canonical_card, compute_proposal_hash

ORDER_ID_RE = re.compile(r"^ORD-[0-9]{3,6}$")
HASH_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
AMOUNT_KEYS = frozenset({"items_paise", "shipping_refund_paise", "fee_paise", "total_paise"})
_CONFIRMED_STATUSES = frozenset(
    {
        ProposalStatus.confirmed.value,
        ProposalStatus.executing.value,
        ProposalStatus.succeeded.value,
        ProposalStatus.failed.value,
        ProposalStatus.unknown.value,
    }
)
_ACTIVE_OVERLAP_STATUSES = (
    ProposalStatus.confirmed.value,
    ProposalStatus.executing.value,
    ProposalStatus.unknown.value,
)


@dataclass(frozen=True)
class PreparedProposal:
    """Validated stored proposal state prepared for store revalidation."""

    proposal_id: str
    customer_id: str
    proposal_type: str
    order_id: str
    items: list[dict[str, Any]]
    canonical_amounts: dict[str, int]
    method: str
    resolution: str
    reason_code: str
    evidence_ids: list[str]
    decision: str
    policy_version: str
    proposal_hash: str
    expires_at: str
    customer_risk: CustomerRiskFacts
    previous_refunds_30d_paise: int
    session_id: str = "default"
    sanitized_reason_text: str = ""
    cause: str | None = None
    stored_facts_hash: str = ""
    facts_snapshot: dict[str, Any] | None = None


class ProposalValidationError(AppHTTPException):
    """HTTP 409 conflict carrying an internal machine-readable reason code."""


def _invalid_state(message: str, *, reason: str = "invalid_state") -> ProposalValidationError:
    return ProposalValidationError(
        status_code=409,
        code="invalid_state",
        message=message,
        details={"reason": reason},
        reason=reason,
    )


def is_non_bool_int(val: Any) -> TypeGuard[int]:
    """Return True strictly for non-boolean integers."""
    return isinstance(val, int) and not isinstance(val, bool)


def compute_request_hash(
    *,
    operation: str,
    proposal_id: str,
    canonical_body: dict[str, Any],
) -> str:
    """Compute canonical SHA-256 request fingerprint for idempotency checks."""
    payload = {"op": operation, "proposal_id": proposal_id, "body": canonical_body}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_idempotency_row(
    conn: Connection,
    *,
    customer_id: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    """Return stored replay dict if matching, raise 409 on payload mismatch, else None."""
    row = conn.execute(
        select(http_idempotency_keys).where(
            http_idempotency_keys.c.customer_id == customer_id,
            http_idempotency_keys.c.idempotency_key == idempotency_key,
        )
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(str(row.request_hash), request_hash):
        raise AppHTTPException(
            status_code=409,
            code="idempotency_mismatch",
            message="Idempotency-Key reused with a different request payload",
        )
    parsed = json.loads(str(row.response_json))
    if not isinstance(parsed, dict):
        raise AppHTTPException(status_code=500, code="internal", message="Internal server error")
    return dict(parsed)


def validate_stored_proposal(
    row: Any,
    *,
    clock: Clock,
    expected_hash: str | None,
) -> tuple[dict[str, Any], str, list[dict[str, Any]], dict[str, int], str]:
    """Verify stored proposal integrity, client hash match, status, and expiry."""
    try:
        payload = json.loads(str(row.payload_json))
        raw_amounts = json.loads(str(row.amounts_json))
    except Exception:
        raise _invalid_state("Proposal data is invalid", reason="invalid_data") from None

    if not isinstance(payload, dict) or not isinstance(raw_amounts, dict):
        raise _invalid_state("Proposal data is invalid", reason="invalid_data")

    order_id = payload.get("order_id")
    raw_items = payload.get("items")
    method = payload.get("refund_method")
    if (
        not isinstance(order_id, str)
        or not ORDER_ID_RE.fullmatch(order_id)
        or not isinstance(raw_items, list)
        or len(raw_items) == 0
        or not isinstance(method, str)
        or not method
    ):
        raise _invalid_state("Proposal data is invalid", reason="invalid_data")

    norm_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise _invalid_state("Proposal data is invalid", reason="invalid_data")
        item_id = item.get("item_id")
        raw_qty = item.get("quantity", item.get("qty"))
        if not isinstance(item_id, str) or not item_id or not is_non_bool_int(raw_qty):
            raise _invalid_state("Proposal data is invalid", reason="invalid_data")
        if int(raw_qty) < 1:
            raise _invalid_state("Proposal data is invalid", reason="invalid_data")
        norm_items.append({"item_id": item_id, "quantity": int(raw_qty)})

    if frozenset(raw_amounts.keys()) != AMOUNT_KEYS:
        raise _invalid_state("Proposal amounts are invalid", reason="invalid_data")
    canonical_amounts: dict[str, int] = {}
    for k, v in raw_amounts.items():
        if not is_non_bool_int(v) or int(v) < 0:
            raise _invalid_state("Proposal amounts are invalid", reason="invalid_data")
        canonical_amounts[str(k)] = int(v)

    try:
        card_check = canonical_card(
            type=str(row.type),
            order_id=order_id,
            items=norm_items,
            amounts=canonical_amounts,
            method=method,
            decision=str(row.decision),
            policy_version=str(row.policy_version),
            expires_at=str(row.expires_at),
        )
        recomputed_hash = compute_proposal_hash(card_check)
    except Exception:
        raise _invalid_state(
            "Proposal hash integrity check failed", reason="invalid_data"
        ) from None

    stored_hash = str(row.proposal_hash)
    stored_digest_ok = hmac.compare_digest(
        stored_hash.encode("utf-8"), recomputed_hash.encode("utf-8")
    )
    stored_fmt_ok = bool(HASH_HEX_RE.fullmatch(stored_hash))
    if not (stored_digest_ok & stored_fmt_ok):
        raise _invalid_state("Proposal hash integrity check failed", reason="invalid_data")

    embedded_card = payload.get("card")
    if embedded_card is not None:
        if not isinstance(embedded_card, dict) or not hmac.compare_digest(
            str(embedded_card.get("proposal_hash", "")).encode("utf-8"),
            recomputed_hash.encode("utf-8"),
        ):
            raise _invalid_state("Proposal hash integrity check failed", reason="invalid_data")
        card_amts = embedded_card.get("amounts")
        if isinstance(card_amts, dict):
            for k in AMOUNT_KEYS:
                card_val = card_amts.get(k)
                if not is_non_bool_int(card_val) or int(card_val) != canonical_amounts[k]:
                    raise _invalid_state(
                        "Proposal hash integrity check failed", reason="invalid_data"
                    )

    if expected_hash is not None:
        client_digest_ok = hmac.compare_digest(
            expected_hash.encode("utf-8"), stored_hash.encode("utf-8")
        )
        client_fmt_ok = bool(HASH_HEX_RE.fullmatch(expected_hash))
        if not (client_digest_ok & client_fmt_ok):
            raise _invalid_state("Proposal hash integrity check failed", reason="hash_mismatch")

    row_status = str(row.status)
    if row_status == ProposalStatus.expired.value:
        raise _invalid_state("Proposal has expired", reason="expired")
    if row_status == ProposalStatus.cancelled.value:
        raise _invalid_state("Proposal is already cancelled", reason="already_cancelled")
    if row_status in _CONFIRMED_STATUSES:
        raise _invalid_state("Proposal is already confirmed", reason="already_confirmed")
    if row_status != ProposalStatus.proposed.value:
        raise _invalid_state("Proposal state conflict", reason="invalid_state")
    if is_proposal_expired(str(row.expires_at), clock):
        raise _invalid_state("Proposal has expired", reason="expired")
    return payload, order_id, norm_items, canonical_amounts, method


def load_proposal_decision_snapshot(
    conn: Connection, *, customer_id: str, order_id: str, proposal_id: str
) -> tuple[CustomerRiskFacts, int, dict[str, Any] | None]:
    """Load persisted customer_risk, 30d total, and facts snapshot from policy_decisions."""
    default_risk = CustomerRiskFacts(
        customer_id=customer_id,
        returns_30d=0,
        refund_ratio_90d_bp=0,
        account_age_days=30,
        refunded_30d_paise=0,
    )
    decision_row = None
    audit_row = conn.execute(
        select(audit_events)
        .where(
            audit_events.c.resource_type == "proposals",
            audit_events.c.resource_id == proposal_id,
            audit_events.c.action == "proposal_created",
        )
        .order_by(audit_events.c.seq.desc())
        .limit(1)
    ).fetchone()
    if audit_row is not None:
        try:
            details = json.loads(str(audit_row.details_json))
            dec_id = details.get("decision_id") if isinstance(details, dict) else None
            if isinstance(dec_id, str) and dec_id:
                decision_row = conn.execute(
                    select(policy_decisions).where(
                        policy_decisions.c.decision_id == dec_id,
                        policy_decisions.c.customer_id == customer_id,
                    )
                ).fetchone()
        except Exception:
            decision_row = None

    if decision_row is None:
        decision_row = conn.execute(
            select(policy_decisions)
            .where(
                policy_decisions.c.customer_id == customer_id,
                policy_decisions.c.order_id == order_id,
            )
            .order_by(policy_decisions.c.created_at.desc())
            .limit(1)
        ).fetchone()
    if decision_row is None:
        return default_risk, 0, None

    try:
        facts_dict = json.loads(str(decision_row.facts_json))
        if not isinstance(facts_dict, dict):
            return default_risk, 0, None
        raw_risk = facts_dict.get("customer_risk")
        if not isinstance(raw_risk, dict):
            return default_risk, 0, dict(facts_dict)
        risk = CustomerRiskFacts.model_validate(raw_risk)
        raw_prev = facts_dict.get("previous_refunds_30d_paise", risk.refunded_30d_paise)
        prev_paise = (
            int(raw_prev)
            if is_non_bool_int(raw_prev) and int(raw_prev) >= 0
            else risk.refunded_30d_paise
        )
        return risk, prev_paise, dict(facts_dict)
    except Exception:
        return default_risk, 0, None


def check_overlap_guard(
    conn: Connection,
    *,
    proposal_id: str,
    customer_id: str,
    order_id: str,
    items: list[dict[str, Any]],
) -> None:
    """Reject confirming a proposal overlapping an in-flight confirmed/executing proposal."""
    target_item_ids = {str(it["item_id"]) for it in items}
    active_rows = conn.execute(
        select(proposals).where(
            proposals.c.customer_id == customer_id,
            proposals.c.proposal_id != proposal_id,
            proposals.c.status.in_(_ACTIVE_OVERLAP_STATUSES),
        )
    ).fetchall()
    for other in active_rows:
        try:
            other_payload = json.loads(str(other.payload_json))
        except Exception:
            continue
        if not isinstance(other_payload, dict) or other_payload.get("order_id") != order_id:
            continue
        other_items = other_payload.get("items")
        if not isinstance(other_items, list):
            continue
        other_ids = {
            str(it["item_id"])
            for it in other_items
            if isinstance(it, dict) and isinstance(it.get("item_id"), str)
        }
        if target_item_ids.intersection(other_ids):
            raise _invalid_state(
                "An active proposal already exists for one or more of these items",
                reason="overlap_active",
            )


def build_prepared_proposal(
    conn: Connection,
    row: Any,
    *,
    customer_id: str,
    order_id: str,
    norm_items: list[dict[str, Any]],
    canonical_amounts: dict[str, int],
    method: str,
    payload: dict[str, Any],
) -> PreparedProposal:
    """Build a PreparedProposal snapshot for asynchronous store revalidation."""
    raw_ev = payload.get("evidence_ids")
    evidence_ids = (
        [str(e) for e in raw_ev if isinstance(e, str)] if isinstance(raw_ev, list) else []
    )
    raw_cause = payload.get("cause")
    cause = str(raw_cause) if isinstance(raw_cause, str) and raw_cause else None
    risk, prev_refunds, facts_snapshot = load_proposal_decision_snapshot(
        conn, customer_id=customer_id, order_id=order_id, proposal_id=str(row.proposal_id)
    )
    return PreparedProposal(
        proposal_id=str(row.proposal_id),
        customer_id=customer_id,
        proposal_type=str(row.type),
        order_id=order_id,
        items=norm_items,
        canonical_amounts=canonical_amounts,
        method=method,
        resolution=str(payload.get("resolution", ReturnResolution.refund.value)),
        reason_code=str(payload.get("reason_code", ReturnReasonCode.changed_mind.value)),
        evidence_ids=evidence_ids,
        decision=str(row.decision),
        policy_version=str(row.policy_version),
        proposal_hash=str(row.proposal_hash),
        expires_at=str(row.expires_at),
        customer_risk=risk,
        previous_refunds_30d_paise=prev_refunds,
        session_id=str(getattr(row, "session_id", None) or "default"),
        sanitized_reason_text=str(payload.get("reason_text", "")),
        cause=cause,
        stored_facts_hash=str(getattr(row, "facts_hash", "") or ""),
        facts_snapshot=facts_snapshot,
    )
