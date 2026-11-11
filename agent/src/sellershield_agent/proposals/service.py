"""Proposal persistence, deduplication, rate limits, and confirmation card builder.

Implements:
- Open proposal deduplication (same customer, order_id, canonical sorted items).
- Rate limits: at most 3 open proposals per customer and 10 per simulated IST day.
- Atomic persistence across policy_decisions, proposals, and audit_events.
- Customer-safe confirmation card payload construction with canonical hashing.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import read_tx, write_tx
from sellershield_agent.domain.models import Order, ReturnItem
from sellershield_agent.domain.state import (
    ProposalStatus,
    generate_proposal_id,
    is_proposal_expired,
    proposals,
    record_policy_decision,
)
from sellershield_agent.money import format_inr
from sellershield_agent.orchestrator.cards import sanitize_text
from sellershield_agent.policy.decide import Decision
from sellershield_agent.policy.facts import DecisionFacts
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.policy.rules import DecisionOutcome
from sellershield_agent.proposals.hashing import canonical_card, compute_proposal_hash

IST = ZoneInfo("Asia/Kolkata")
MAX_OPEN_PROPOSALS = 3
MAX_DAILY_PROPOSALS = 10
PROPOSAL_EXPIRY_MINUTES = 10


def canonical_item_signature(
    items: list[ReturnItem] | list[dict[str, Any]],
) -> list[tuple[str, int]]:
    """Return deterministically sorted (item_id, qty) pairs for deduplication."""
    pairs: list[tuple[str, int]] = []
    for it in items:
        if isinstance(it, dict):
            item_id = str(it["item_id"])
            raw_qty = it.get("quantity", it.get("qty"))
            qty = int(raw_qty) if raw_qty is not None else 0
        else:
            item_id = it.item_id
            qty = it.qty
        pairs.append((item_id, qty))
    return sorted(pairs, key=lambda pair: pair[0])


def _parse_created_at_ist_date(created_at_str: str | None) -> str | None:
    if not created_at_str:
        return None
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(IST).strftime("%Y-%m-%d")
    except ValueError:
        return None


def find_open_or_check_limits(
    conn: Connection,
    *,
    customer_id: str,
    order_id: str,
    item_sig: list[tuple[str, int]],
    clock: Clock,
    exclude_proposal_ids: set[str] | None = None,
    expected_policy_version: str | None = None,
    expected_decision: str | None = None,
    expected_amounts: dict[str, int] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Check existing open proposal for dedup and enforce open/daily limits.

    Returns (existing_card, existing_proposal_id, is_rate_limited).
    """
    rows = conn.execute(
        select(proposals)
        .where(proposals.c.customer_id == customer_id)
        .order_by(proposals.c.created_at.asc())
    ).fetchall()

    excluded = exclude_proposal_ids or set()
    today_ist = clock.now().astimezone(IST).strftime("%Y-%m-%d")
    open_count = 0
    daily_count = 0

    for row in reversed(rows):
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {}

        created_str = getattr(row, "created_at", None) or payload.get("created_at")
        if _parse_created_at_ist_date(created_str) == today_ist:
            daily_count += 1

        row_pid = str(row.proposal_id)
        if row_pid in excluded:
            continue

        if row.status == ProposalStatus.proposed.value and not is_proposal_expired(
            row.expires_at, clock
        ):
            open_count += 1
            row_order_id = payload.get("order_id")
            row_items = payload.get("items", [])
            if row_order_id == order_id and canonical_item_signature(row_items) == item_sig:
                if (
                    expected_policy_version is not None
                    and str(row.policy_version) != expected_policy_version
                ):
                    continue
                if (
                    expected_decision is not None
                    and str(row.decision).lower() != expected_decision.lower()
                ):
                    continue
                if expected_amounts is not None:
                    try:
                        row_amounts = json.loads(str(row.amounts_json))
                    except Exception:
                        row_amounts = None
                    if row_amounts != expected_amounts:
                        continue
                card = payload.get("card")
                if isinstance(card, dict):
                    return card, row_pid, False

    if open_count >= MAX_OPEN_PROPOSALS or daily_count >= MAX_DAILY_PROPOSALS:
        return None, None, True

    return None, None, False


def check_limits_readonly(
    engine: Engine,
    *,
    customer_id: str,
    order_id: str,
    item_sig: list[tuple[str, int]],
    clock: Clock,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Read-only wrapper for find_open_or_check_limits."""
    with read_tx(engine) as conn:
        return find_open_or_check_limits(
            conn,
            customer_id=customer_id,
            order_id=order_id,
            item_sig=item_sig,
            clock=clock,
        )


def persist_proposal_and_build_card(
    engine: Engine,
    *,
    clock: Clock,
    policy: PolicyDocument,
    request_id: str,
    session_id: str | None,
    customer_id: str,
    order: Order,
    proposal_type: str,
    requested_items: list[ReturnItem],
    facts: DecisionFacts,
    decision: Decision,
    effective_outcome: DecisionOutcome,
    method_str: str,
    resolution_str: str,
    sanitized_reason_text: str,
    extra_payload: dict[str, Any],
    tx: Connection | None = None,
    exclude_proposal_ids: set[str] | None = None,
    expected_policy_version: str | None = None,
    expected_decision: str | None = None,
    expected_amounts: dict[str, int] | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Persist policy decision, proposal, and audit event atomically.

    Re-validates dedup and rate limits inside write_tx to close TOCTOU races.
    Returns (card_payload, proposal_id, is_rate_limited).
    """
    now_dt = clock.now()
    now_iso = now_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at_dt = now_dt + timedelta(minutes=PROPOSAL_EXPIRY_MINUTES)
    expires_at_str = expires_at_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    sorted_items = sorted(requested_items, key=lambda x: x.item_id)
    order_items_by_id = {it.item_id: it for it in order.items}
    canonical_items = [{"item_id": it.item_id, "quantity": it.qty} for it in sorted_items]
    display_items = [
        {
            "item_id": it.item_id,
            "quantity": it.qty,
            "name": (
                sanitize_text(order_items_by_id[it.item_id].name, 120)
                if it.item_id in order_items_by_id
                else ""
            ),
        }
        for it in sorted_items
    ]

    canonical_amounts = {
        "items_paise": decision.amounts.items_paise,
        "shipping_refund_paise": decision.amounts.shipping_refund_paise,
        "fee_paise": decision.amounts.fee_paise,
        "total_paise": decision.amounts.total_paise,
    }
    outcome_lower = effective_outcome.value.lower()
    card_for_hash = canonical_card(
        type=proposal_type,
        order_id=order.order_id,
        items=canonical_items,
        amounts=canonical_amounts,
        method=method_str,
        decision=outcome_lower,
        policy_version=policy.version,
        expires_at=expires_at_str,
    )
    proposal_hash = compute_proposal_hash(card_for_hash)
    proposal_id = generate_proposal_id()

    card_payload: dict[str, Any] = {
        "type": "confirmation",
        "proposal_id": proposal_id,
        "proposal_hash": proposal_hash,
        "proposal_type": proposal_type,
        "order_id": order.order_id,
        "items": display_items,
        "amounts": {
            "items_paise": decision.amounts.items_paise,
            "shipping_refund_paise": decision.amounts.shipping_refund_paise,
            "fee_paise": decision.amounts.fee_paise,
            "total_paise": decision.amounts.total_paise,
            "capped": decision.amounts.capped,
            "display": format_inr(decision.amounts.total_paise),
            "items_display": format_inr(decision.amounts.items_paise),
            "shipping_refund_display": format_inr(decision.amounts.shipping_refund_paise),
            "fee_display": format_inr(decision.amounts.fee_paise),
        },
        "method": method_str,
        "resolution": resolution_str,
        "decision": outcome_lower,
        "policy_version": policy.version,
        "capped": decision.flags.capped,
        "expires_at": expires_at_str,
    }
    if effective_outcome == DecisionOutcome.needs_approval:
        card_payload["review_statement"] = (
            f"The seller will review your request. A decision is expected within "
            f"{policy.approval_sla_hours} hours."
        )

    payload_dict: dict[str, Any] = {
        "order_id": order.order_id,
        "items": canonical_items,
        "reason_text": sanitized_reason_text,
        "resolution": resolution_str,
        "refund_method": method_str,
        "created_at": now_iso,
        "card": card_payload,
        **extra_payload,
    }
    item_sig = canonical_item_signature(sorted_items)

    def _write_in_tx(conn: Connection) -> tuple[dict[str, Any] | None, str | None, bool]:
        existing_card, existing_id, rate_limited = find_open_or_check_limits(
            conn,
            customer_id=customer_id,
            order_id=order.order_id,
            item_sig=item_sig,
            clock=clock,
            exclude_proposal_ids=exclude_proposal_ids,
            expected_policy_version=expected_policy_version,
            expected_decision=expected_decision,
            expected_amounts=expected_amounts,
        )
        if existing_card is not None and existing_id is not None:
            return existing_card, existing_id, False
        if rate_limited:
            return None, None, True

        dec_id = record_policy_decision(
            conn,
            request_id=request_id,
            customer_id=customer_id,
            order_id=order.order_id,
            policy_version=policy.version,
            facts=facts.model_dump(mode="json"),
            decision=decision.model_dump(mode="json"),
            clock=clock,
        )
        conn.execute(
            proposals.insert().values(
                proposal_id=proposal_id,
                customer_id=customer_id,
                session_id=session_id or "default",
                type=proposal_type,
                payload_json=json.dumps(payload_dict, sort_keys=True, separators=(",", ":")),
                decision=outcome_lower,
                amounts_json=json.dumps(canonical_amounts, sort_keys=True, separators=(",", ":")),
                policy_version=policy.version,
                facts_hash=decision.facts_hash,
                proposal_hash=proposal_hash,
                status=ProposalStatus.proposed.value,
                created_at=now_iso,
                expires_at=expires_at_str,
                confirmed_at=None,
                version=1,
            )
        )
        AuditService.append(
            conn,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_iso,
                request_id=request_id,
                session_id=session_id,
                actor_type=ActorType.CUSTOMER,
                actor_id=customer_id,
                event_type=EventType.PROPOSAL_CREATED,
                resource_type="proposals",
                resource_id=proposal_id,
                action="proposal_created",
                outcome=Outcome.SUCCESS,
                policy_version=policy.version,
                details={
                    "decision_id": dec_id,
                    "order_id": order.order_id,
                    "proposal_type": proposal_type,
                    "decision": effective_outcome.value,
                    "proposal_hash": proposal_hash,
                },
            ),
        )
        return card_payload, proposal_id, False

    if tx is not None:
        return _write_in_tx(tx)

    with write_tx(engine) as conn:
        return _write_in_tx(conn)
