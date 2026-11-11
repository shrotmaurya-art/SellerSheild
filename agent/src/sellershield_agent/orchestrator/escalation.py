"""Orchestrator escalation triggers, policy checks, and human routing."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, and_, select

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.cases.api import open_case
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.domain.state import cases
from sellershield_agent.policy.models import Policy

DEFAULT_ESCALATION_KEYWORDS = [
    "lawyer",
    "legal",
    "court",
    "consumer forum",
    "fraud",
    "sue",
    "police",
]

HUMAN_REQUEST_PATTERN = re.compile(
    r"\b(human|representative|agent|support person|speak with someone|"
    r"talk to someone|talk to human)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EscalationResult:
    case_created: bool
    case_id: str | None
    offer_human_chip: bool
    reason: str
    priority: str = "normal"


def _check_legal_keywords(text: str, keywords: list[str]) -> str | None:
    """Check text for legal or threat keywords using regex word boundaries."""
    for kw in keywords:
        pattern = rf"\b{re.escape(kw)}\b"
        if re.search(pattern, text, re.IGNORECASE):
            return kw
    return None


def _existing_case_id(
    tx: Connection,
    *,
    customer_id: str,
    category: str,
    order_id: str | None,
    cutoff: str,
) -> str | None:
    order_cond = (
        cases.c.order_id == order_id if order_id is not None else cases.c.order_id.is_(None)
    )
    row = tx.execute(
        select(cases.c.case_id).where(
            and_(
                cases.c.customer_id == customer_id,
                cases.c.category == category,
                order_cond,
                cases.c.created_at >= cutoff,
                cases.c.status != "closed",
            )
        )
    ).fetchone()
    return str(row[0]) if row is not None else None


def evaluate_escalation_triggers(
    tx: Connection,
    *,
    customer_id: str,
    message_text: str | None = None,
    category: str = "general",
    order_id: str | None = None,
    session_id: str | None = None,
    policy_decision: str | None = None,
    sentiment: str | None = None,
    previous_sentiment: str | None = None,
    clock: Clock | None = None,
    policy: Policy | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    order_facts: dict[str, Any] | None = None,
) -> EscalationResult:
    """Evaluate escalation triggers and handle case creation or chip offering.

    Triggers:
    1. Customer angry twice in a row -> Offer 'Talk to Human' chip, no case.
    2. Legal or threat words from policy escalation_keywords -> High priority case (2h SLA).
    3. Policy decision ESCALATE -> Normal priority case.
    4. Customer asks for human -> Normal priority case.
    Idempotent: Reuses existing case within 30 minutes.
    Writes an audit event for each evaluated trigger.
    """
    active_clock: Clock = clock if clock is not None else SystemClock()
    now = active_clock.now()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = message_text or ""

    # 1. Legal / threat keywords -> High priority case (2h SLA)
    keywords = (
        policy.escalation_keywords
        if policy and policy.escalation_keywords
        else DEFAULT_ESCALATION_KEYWORDS
    )
    matched_keyword = _check_legal_keywords(text, keywords)
    if matched_keyword is not None:
        case_cat = "legal_threat" if category == "general" else category
        existing_id = _existing_case_id(
            tx, customer_id=customer_id, category=case_cat, order_id=order_id, cutoff=cutoff
        )

        case_id = open_case(
            tx,
            customer_id=customer_id,
            order_id=order_id,
            category=case_cat,
            priority="high",
            source="chat",
            session_id=session_id,
            now_iso=now_str,
            clock=active_clock,
        )

        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=f"req-esc-{secrets.token_hex(4)}",
                session_id=session_id,
                actor_type=ActorType.AGENT,
                actor_id=customer_id,
                event_type=EventType.POLICY_DECISION,
                resource_type="cases",
                resource_id=case_id,
                action="escalation_trigger",
                outcome=Outcome.SUCCESS,
                policy_version=getattr(policy, "version", "v3"),
                details={
                    "trigger": "legal_keywords",
                    "keyword": matched_keyword,
                    "priority": "high",
                },
            ),
        )

        return EscalationResult(
            case_created=(existing_id is None),
            case_id=case_id,
            offer_human_chip=False,
            reason="legal_keywords",
            priority="high",
        )

    # 2. Policy decision ESCALATE -> Case
    if (policy_decision or "").upper() == "ESCALATE":
        existing_id = _existing_case_id(
            tx, customer_id=customer_id, category=category, order_id=order_id, cutoff=cutoff
        )

        case_id = open_case(
            tx,
            customer_id=customer_id,
            order_id=order_id,
            category=category,
            priority="normal",
            source="policy",
            session_id=session_id,
            now_iso=now_str,
            clock=active_clock,
        )

        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=f"req-esc-{secrets.token_hex(4)}",
                session_id=session_id,
                actor_type=ActorType.AGENT,
                actor_id=customer_id,
                event_type=EventType.POLICY_DECISION,
                resource_type="cases",
                resource_id=case_id,
                action="escalation_trigger",
                outcome=Outcome.SUCCESS,
                policy_version=getattr(policy, "version", "v3"),
                details={"trigger": "policy_decision_escalate"},
            ),
        )

        return EscalationResult(
            case_created=(existing_id is None),
            case_id=case_id,
            offer_human_chip=False,
            reason="policy_decision_escalate",
            priority="normal",
        )

    # 3. Customer asks for human -> Case
    is_human_request = category == "human_request" or bool(HUMAN_REQUEST_PATTERN.search(text))
    if is_human_request:
        case_cat = "human_request"
        existing_id = _existing_case_id(
            tx, customer_id=customer_id, category=case_cat, order_id=order_id, cutoff=cutoff
        )

        case_id = open_case(
            tx,
            customer_id=customer_id,
            order_id=order_id,
            category=case_cat,
            priority="normal",
            source="chat",
            session_id=session_id,
            now_iso=now_str,
            clock=active_clock,
        )

        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=f"req-esc-{secrets.token_hex(4)}",
                session_id=session_id,
                actor_type=ActorType.AGENT,
                actor_id=customer_id,
                event_type=EventType.POLICY_DECISION,
                resource_type="cases",
                resource_id=case_id,
                action="escalation_trigger",
                outcome=Outcome.SUCCESS,
                policy_version=getattr(policy, "version", "v3"),
                details={"trigger": "human_request"},
            ),
        )

        return EscalationResult(
            case_created=(existing_id is None),
            case_id=case_id,
            offer_human_chip=False,
            reason="human_request",
            priority="normal",
        )

    # 4. Angry sentiment twice in a row: offer chip, no case
    if (sentiment or "").lower() == "angry" and (previous_sentiment or "").lower() == "angry":
        AuditService.append(
            tx,
            AuditEvent(
                event_id=f"AUD-{secrets.token_hex(8)}",
                ts_utc=now_str,
                request_id=f"req-esc-{secrets.token_hex(4)}",
                session_id=session_id,
                actor_type=ActorType.AGENT,
                actor_id=customer_id,
                event_type=EventType.POLICY_DECISION,
                resource_type="cases",
                resource_id=customer_id,
                action="escalation_trigger",
                outcome=Outcome.SUCCESS,
                policy_version=getattr(policy, "version", "v3"),
                details={"trigger": "angry_twice", "offer_human_chip": True},
            ),
        )
        return EscalationResult(
            case_created=False,
            case_id=None,
            offer_human_chip=True,
            reason="angry_twice",
        )

    return EscalationResult(
        case_created=False,
        case_id=None,
        offer_human_chip=False,
        reason="none",
    )
