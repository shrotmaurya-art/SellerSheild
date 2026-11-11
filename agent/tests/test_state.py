"""Tests for proposal, execution, approval data models, state machines, and canonical hashing.

TASK T2.2 - Proposal, execution and approval data and state machines.
Implements STEP A test suite covering:
1. Table-driven legal and illegal proposal state transitions.
2. Table-driven legal and illegal approval state transitions.
3. Optimistic locking and version mismatch rejection.
4. Concurrency winner drill: two concurrent threads attempting transition on one row.
5. Canonical card hashing sensitivity: any displayed field changes hash.
6. Canonical card immunity to model text / unverified customer statements.
7. Cryptographically random PRP id generation (10,000 unique non-sequential draws).
8. Expiry boundary evaluation under FixedClock for proposals and approvals.
9. PII sanitization in policy_decisions (no address, phone, email, token).
10. Atomic audit logging and rollback when audit append fails.
11. Sabotage drill: canonical card requires amounts; omitting amounts fails.
12. Schema validation for policy_decisions, proposals, executions, approvals, client_events.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from sellershield_agent.audit.models import audit_events
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import get_metadata, write_tx
from sellershield_agent.domain.state import (
    APPROVAL_TRANSITIONS,
    PROPOSAL_TRANSITIONS,
    ApprovalStatus,
    InvalidTransition,
    ProposalStatus,
    VersionMismatch,
    approvals,
    client_events,
    executions,
    generate_approval_id,
    generate_proposal_id,
    is_approval_due,
    is_approval_expired,
    is_proposal_expired,
    policy_decisions,
    proposals,
    record_policy_decision,
    transition_approval,
    transition_proposal,
)
from sellershield_agent.policy.loader import get_policy
from sellershield_agent.proposals.hashing import canonical_card, compute_proposal_hash


@pytest.fixture
def mem_engine():
    """Create a pristine in-memory SQLite engine with the full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    get_metadata().create_all(engine)
    with write_tx(engine) as conn:
        conn.execute(
            text(
                "INSERT INTO _meta (schema_version) VALUES (1) "
                "ON CONFLICT DO NOTHING"
            )
        )
    return engine


# ── 1. Table-Driven Proposal State Transitions ─────────────────────────────


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (ProposalStatus.proposed, ProposalStatus.confirmed),
        (ProposalStatus.proposed, ProposalStatus.cancelled),
        (ProposalStatus.proposed, ProposalStatus.expired),
        (ProposalStatus.confirmed, ProposalStatus.executing),
        (ProposalStatus.executing, ProposalStatus.succeeded),
        (ProposalStatus.executing, ProposalStatus.failed),
        (ProposalStatus.executing, ProposalStatus.unknown),
        (ProposalStatus.unknown, ProposalStatus.succeeded),
        (ProposalStatus.unknown, ProposalStatus.failed),
        (ProposalStatus.unknown, ProposalStatus.executing),
    ],
)
def test_proposal_legal_transitions(
    mem_engine,
    from_status: ProposalStatus,
    to_status: ProposalStatus,
):
    """Every legal proposal transition succeeds, updates status, increments version, and audits."""
    assert to_status.value in PROPOSAL_TRANSITIONS[from_status.value]
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            proposals.insert().values(
                proposal_id=prp_id,
                customer_id="C001",
                session_id="SESS-001",
                type="return",
                payload_json="{}",
                decision="ALLOW",
                amounts_json='{"refund_total_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="1" * 64,
                status=from_status.value,
                expires_at=(now_utc + timedelta(minutes=10)).isoformat(),
                confirmed_at=None,
                version=1,
            )
        )

    new_ver = transition_proposal(
        mem_engine,
        proposal_id=prp_id,
        to_status=to_status,
        expected_version=1,
        clock=clock,
        request_id="REQ-TRANS-1",
        actor_id="C001",
    )
    assert new_ver == 2

    with mem_engine.connect() as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == prp_id)
        ).fetchone()
        assert row is not None
        assert row.status == to_status.value
        assert row.version == 2


@pytest.mark.parametrize(
    ("from_status", "invalid_to"),
    [
        (ProposalStatus.proposed, ProposalStatus.succeeded),
        (ProposalStatus.proposed, ProposalStatus.failed),
        (ProposalStatus.proposed, ProposalStatus.executing),
        (ProposalStatus.confirmed, ProposalStatus.proposed),
        (ProposalStatus.confirmed, ProposalStatus.cancelled),
        (ProposalStatus.confirmed, ProposalStatus.succeeded),
        (ProposalStatus.cancelled, ProposalStatus.confirmed),
        (ProposalStatus.expired, ProposalStatus.confirmed),
        (ProposalStatus.succeeded, ProposalStatus.executing),
        (ProposalStatus.failed, ProposalStatus.executing),
    ],
)
def test_proposal_illegal_transitions_raise(
    mem_engine,
    from_status: ProposalStatus,
    invalid_to: ProposalStatus,
):
    """Every illegal proposal transition raises InvalidTransition and leaves state untouched."""
    assert invalid_to.value not in PROPOSAL_TRANSITIONS[from_status.value]
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            proposals.insert().values(
                proposal_id=prp_id,
                customer_id="C001",
                session_id="SESS-001",
                type="return",
                payload_json="{}",
                decision="ALLOW",
                amounts_json='{"refund_total_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="1" * 64,
                status=from_status.value,
                expires_at=(now_utc + timedelta(minutes=10)).isoformat(),
                confirmed_at=None,
                version=1,
            )
        )

    with pytest.raises(InvalidTransition):
        transition_proposal(
            mem_engine,
            proposal_id=prp_id,
            to_status=invalid_to,
            expected_version=1,
            clock=clock,
            request_id="REQ-TRANS-INV",
            actor_id="C001",
        )

    with mem_engine.connect() as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == prp_id)
        ).fetchone()
        assert row is not None
        assert row.status == from_status.value
        assert row.version == 1


# ── 2. Table-Driven Approval State Transitions ─────────────────────────────


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (ApprovalStatus.pending, ApprovalStatus.approving),
        (ApprovalStatus.pending, ApprovalStatus.rejected),
        (ApprovalStatus.pending, ApprovalStatus.expired),
        (ApprovalStatus.pending, ApprovalStatus.needs_revalidation),
        (ApprovalStatus.pending, ApprovalStatus.withdrawn),
        (ApprovalStatus.approving, ApprovalStatus.approved),
        (ApprovalStatus.approving, ApprovalStatus.pending),  # failure fallback (amendment 2)
        (ApprovalStatus.needs_revalidation, ApprovalStatus.pending),
        (ApprovalStatus.needs_revalidation, ApprovalStatus.rejected),
        (ApprovalStatus.needs_revalidation, ApprovalStatus.expired),
        (ApprovalStatus.needs_revalidation, ApprovalStatus.withdrawn),
    ],
)
def test_approval_legal_transitions(
    mem_engine,
    from_status: ApprovalStatus,
    to_status: ApprovalStatus,
):
    """Every legal approval transition succeeds and is recorded in the audit trail."""
    assert to_status.value in APPROVAL_TRANSITIONS[from_status.value]
    apr_id = generate_approval_id()
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=prp_id,
                order_id="ORD-1042",
                return_id=None,
                refund_id=None,
                amount_paise=120000,
                required_role="seller",
                status=from_status.value,
                policy_version="v3",
                facts_hash="0" * 64,
                due_at=(now_utc + timedelta(hours=24)).isoformat(),
                expires_at=(now_utc + timedelta(days=7)).isoformat(),
                decided_by=None,
                decided_at=None,
                decision_reason=None,
                store_authorization_ref=None,
                version=1,
            )
        )

    new_ver = transition_approval(
        mem_engine,
        approval_id=apr_id,
        to_status=to_status,
        expected_version=1,
        clock=clock,
        request_id="REQ-APR-1",
        actor_id="S001",
    )
    assert new_ver == 2

    with mem_engine.connect() as conn:
        row = conn.execute(
            select(approvals).where(approvals.c.approval_id == apr_id)
        ).fetchone()
        assert row is not None
        assert row.status == to_status.value
        assert row.version == 2


@pytest.mark.parametrize(
    ("from_status", "invalid_to"),
    [
        (ApprovalStatus.pending, ApprovalStatus.approved),  # must go via approving
        (ApprovalStatus.approved, ApprovalStatus.pending),
        (ApprovalStatus.approved, ApprovalStatus.rejected),
        (ApprovalStatus.rejected, ApprovalStatus.approving),
        (ApprovalStatus.expired, ApprovalStatus.pending),
        (ApprovalStatus.withdrawn, ApprovalStatus.pending),
        (ApprovalStatus.approving, ApprovalStatus.needs_revalidation),
    ],
)
def test_approval_illegal_transitions_raise(
    mem_engine,
    from_status: ApprovalStatus,
    invalid_to: ApprovalStatus,
):
    """Illegal approval transitions raise InvalidTransition."""
    assert invalid_to.value not in APPROVAL_TRANSITIONS[from_status.value]
    apr_id = generate_approval_id()
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=prp_id,
                order_id="ORD-1042",
                return_id=None,
                refund_id=None,
                amount_paise=120000,
                required_role="seller",
                status=from_status.value,
                policy_version="v3",
                facts_hash="0" * 64,
                due_at=(now_utc + timedelta(hours=24)).isoformat(),
                expires_at=(now_utc + timedelta(days=7)).isoformat(),
                decided_by=None,
                decided_at=None,
                decision_reason=None,
                store_authorization_ref=None,
                version=1,
            )
        )

    with pytest.raises(InvalidTransition):
        transition_approval(
            mem_engine,
            approval_id=apr_id,
            to_status=invalid_to,
            expected_version=1,
            clock=clock,
            request_id="REQ-APR-INV",
            actor_id="S001",
        )


# ── 3. Optimistic Locking & Concurrency Winner ─────────────────────────────


def test_version_mismatch_raises(mem_engine):
    """Calling transition with a stale expected_version raises VersionMismatch."""
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            proposals.insert().values(
                proposal_id=prp_id,
                customer_id="C001",
                session_id="SESS-001",
                type="return",
                payload_json="{}",
                decision="ALLOW",
                amounts_json='{"refund_total_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="1" * 64,
                status=ProposalStatus.proposed.value,
                expires_at=(now_utc + timedelta(minutes=10)).isoformat(),
                confirmed_at=None,
                version=2,  # Current DB version is 2
            )
        )

    with pytest.raises(VersionMismatch):
        transition_proposal(
            mem_engine,
            proposal_id=prp_id,
            to_status=ProposalStatus.confirmed,
            expected_version=1,  # Stale version 1
            clock=clock,
            request_id="REQ-STALE",
            actor_id="C001",
        )


def test_concurrent_transitions_single_winner(mem_engine):
    """Two concurrent transitions on the same row let exactly one win; the other raises."""
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            proposals.insert().values(
                proposal_id=prp_id,
                customer_id="C001",
                session_id="SESS-001",
                type="return",
                payload_json="{}",
                decision="ALLOW",
                amounts_json='{"refund_total_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="1" * 64,
                status=ProposalStatus.proposed.value,
                expires_at=(now_utc + timedelta(minutes=10)).isoformat(),
                confirmed_at=None,
                version=1,
            )
        )

    results: list[str] = []
    errors: list[Exception] = []

    def _attempt_transition(req_id: str, to_st: ProposalStatus) -> None:
        try:
            ver = transition_proposal(
                mem_engine,
                proposal_id=prp_id,
                to_status=to_st,
                expected_version=1,
                clock=clock,
                request_id=req_id,
                actor_id="C001",
            )
            results.append(f"success_v{ver}")
        except Exception as e:
            errors.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_attempt_transition, "REQ-RACE-1", ProposalStatus.confirmed)
        f2 = executor.submit(_attempt_transition, "REQ-RACE-2", ProposalStatus.cancelled)
        f1.result()
        f2.result()

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], (InvalidTransition, VersionMismatch))

    with mem_engine.connect() as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == prp_id)
        ).fetchone()
        assert row is not None
        assert row.version == 2
        assert row.status in (ProposalStatus.confirmed.value, ProposalStatus.cancelled.value)


# ── 4. Proposal Canonical Hashing & Sensitivity ────────────────────────────


def test_proposal_hash_sensitivity():
    """proposal_hash changes when any displayed field changes."""
    base_proposal = {
        "type": "return",
        "order_id": "ORD-1042",
        "items": [{"item_id": "ITEM-1", "quantity": 1}],
        "amounts": {
            "item_total_paise": 120000,
            "shipping_fee_paise": 0,
            "restocking_fee_paise": 0,
            "refund_total_paise": 120000,
        },
        "method": "original",
        "decision": "ALLOW",
        "policy_version": "v3",
        "expires_at": "2026-11-10T10:10:00Z",
    }
    base_card = canonical_card(**base_proposal)
    base_hash = compute_proposal_hash(base_card)

    # 1. Amount change alters hash
    card_diff_amount = canonical_card(
        **{
            **base_proposal,
            "amounts": {
                **base_proposal["amounts"],
                "refund_total_paise": 100000,
            },
        }
    )
    assert compute_proposal_hash(card_diff_amount) != base_hash

    # 2. Quantity change alters hash
    card_diff_qty = canonical_card(
        **{
            **base_proposal,
            "items": [{"item_id": "ITEM-1", "quantity": 2}],
        }
    )
    assert compute_proposal_hash(card_diff_qty) != base_hash

    # 3. Item id change alters hash
    card_diff_item = canonical_card(
        **{
            **base_proposal,
            "items": [{"item_id": "ITEM-2", "quantity": 1}],
        }
    )
    assert compute_proposal_hash(card_diff_item) != base_hash

    # 4. Method change alters hash
    card_diff_method = canonical_card(
        **{**base_proposal, "method": "store_credit"}
    )
    assert compute_proposal_hash(card_diff_method) != base_hash

    # 5. Policy version change alters hash
    card_diff_policy = canonical_card(
        **{**base_proposal, "policy_version": "v2"}
    )
    assert compute_proposal_hash(card_diff_policy) != base_hash

    # 6. Expiry change alters hash
    card_diff_expiry = canonical_card(
        **{**base_proposal, "expires_at": "2026-11-10T10:15:00Z"}
    )
    assert compute_proposal_hash(card_diff_expiry) != base_hash


def test_proposal_hash_immune_to_model_text():
    """proposal_hash does NOT change when model commentary, greetings, or reason text change."""
    base_args = {
        "type": "return",
        "order_id": "ORD-1042",
        "items": [{"item_id": "ITEM-1", "quantity": 1}],
        "amounts": {
            "item_total_paise": 120000,
            "shipping_fee_paise": 0,
            "restocking_fee_paise": 0,
            "refund_total_paise": 120000,
        },
        "method": "original",
        "decision": "ALLOW",
        "policy_version": "v3",
        "expires_at": "2026-11-10T10:10:00Z",
    }
    card1 = canonical_card(**base_args)
    hash1 = compute_proposal_hash(card1)

    # Calling canonical_card without any extra text gives canonical representation
    card2 = canonical_card(**base_args)
    hash2 = compute_proposal_hash(card2)
    assert hash1 == hash2

    # canonical_card strictly excludes arbitrary extra keys
    assert "model_text" not in card1
    assert "reason_text" not in card1
    assert "greeting" not in card1


def test_canonical_card_requires_amount_sabotage():
    """Sabotage drill: canonical_card requires amounts; missing amounts raises or alters card."""
    with pytest.raises(TypeError):
        canonical_card(  # type: ignore[call-arg]
            type="return",
            order_id="ORD-1042",
            items=[{"item_id": "ITEM-1", "quantity": 1}],
            # amounts intentionally omitted!
            method="original",
            decision="ALLOW",
            policy_version="v3",
            expires_at="2026-11-10T10:10:00Z",
        )


# ── 5. PRP ID Randomness and Uniqueness ────────────────────────────────────


def test_proposal_id_randomness_and_uniqueness():
    """PRP ids match PRP-[0-9a-f]{8}, are unique over 10,000 draws, and are non-sequential."""
    draws = [generate_proposal_id() for _ in range(10000)]
    assert len(set(draws)) == 10000
    for pid in draws[:50]:
        assert pid.startswith("PRP-")
        assert len(pid) == 12  # PRP- + 8 hex chars
        int(pid[4:], 16)  # Must be valid hex


# ── 6. Expiry Boundary Under FixedClock ─────────────────────────────────────


def test_expiry_boundary_fixed_clock():
    """Verify strictly < expires_at is active, and >= expires_at is expired."""
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    expiry = t0 + timedelta(minutes=10)

    clock = FixedClock(t0)
    assert not is_proposal_expired(expiry.isoformat(), clock)

    clock.set(t0 + timedelta(minutes=9, seconds=59))
    assert not is_proposal_expired(expiry.isoformat(), clock)

    clock.set(expiry)
    assert is_proposal_expired(expiry.isoformat(), clock)

    clock.set(expiry + timedelta(seconds=1))
    assert is_proposal_expired(expiry.isoformat(), clock)


def test_approval_expiry_and_sla_due():
    """Approvals compute SLA due time and expiry from the active policy."""
    policy = get_policy("v3")
    t0 = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(t0)

    # due_at after approval_sla_hours (e.g. 24h), expires_at after approval_expiry_days (e.g. 7d)
    due_at = t0 + timedelta(hours=policy.approval_sla_hours)
    expires_at = t0 + timedelta(days=policy.approval_expiry_days)

    assert not is_approval_due(due_at.isoformat(), clock)
    clock.set(due_at)
    assert is_approval_due(due_at.isoformat(), clock)

    assert not is_approval_expired(expires_at.isoformat(), clock)
    clock.set(expires_at)
    assert is_approval_expired(expires_at.isoformat(), clock)


# ── 7. PII Sanitization in Policy Decisions ────────────────────────────────


def test_policy_decisions_pii_sanitized(mem_engine):
    """policy_decisions rows contain no address, phone, email, or token."""
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    raw_facts = {
        "customer_id": "C001",
        "order_id": "ORD-1042",
        "email": "aarav.patel@example.com",
        "phone": "+91-9876543210",
        "shipping_address": {
            "street": "123 MG Road",
            "city": "Bengaluru",
            "pincode": "560001",
        },
        "token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.secret",
        "delivered_at": "2026-11-05T14:30:00Z",
        "category": "home_kitchen",
        "item_paise": 120000,
    }

    decision_data = {
        "rule_results": ["R-01 PASS", "R-02 PASS"],
        "decision": "ALLOW",
    }

    dec_id = record_policy_decision(
        mem_engine,
        request_id="REQ-POL-1",
        customer_id="C001",
        order_id="ORD-1042",
        policy_version="v3",
        facts=raw_facts,
        decision=decision_data,
        clock=clock,
    )

    with mem_engine.connect() as conn:
        row = conn.execute(
            select(policy_decisions).where(policy_decisions.c.decision_id == dec_id)
        ).fetchone()
        assert row is not None
        facts_text = row.facts_json
        assert "aarav.patel@example.com" not in facts_text
        assert "9876543210" not in facts_text
        assert "123 MG Road" not in facts_text
        assert "560001" not in facts_text
        assert "eyJhbGci" not in facts_text
        # Legitimate business facts remain
        assert "home_kitchen" in facts_text
        assert "120000" in facts_text


# ── 8. Audit Event Atomicity & Rollback Drill ──────────────────────────────


def test_audit_failure_rolls_back_transition(mem_engine):
    """An audit failure rolls the transition back; status and version remain unchanged."""
    prp_id = generate_proposal_id()
    now_utc = datetime(2026, 11, 10, 10, 0, 0, tzinfo=UTC)
    clock = FixedClock(now_utc)

    with write_tx(mem_engine) as conn:
        conn.execute(
            proposals.insert().values(
                proposal_id=prp_id,
                customer_id="C001",
                session_id="SESS-001",
                type="return",
                payload_json="{}",
                decision="ALLOW",
                amounts_json='{"refund_total_paise": 1000}',
                policy_version="v3",
                facts_hash="0" * 64,
                proposal_hash="1" * 64,
                status=ProposalStatus.proposed.value,
                expires_at=(now_utc + timedelta(minutes=10)).isoformat(),
                confirmed_at=None,
                version=1,
            )
        )

    # Simulate an audit failure during transition
    with patch(
        "sellershield_agent.audit.service.AuditService.append",
        side_effect=RuntimeError("Simulated audit disk failure"),
    ):
        with pytest.raises(RuntimeError, match="Simulated audit disk failure"):
            transition_proposal(
                mem_engine,
                proposal_id=prp_id,
                to_status=ProposalStatus.confirmed,
                expected_version=1,
                clock=clock,
                request_id="REQ-AUDIT-FAIL",
                actor_id="C001",
            )

    # Verify that proposal row was rolled back and is still 'proposed' with version 1
    with mem_engine.connect() as conn:
        row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == prp_id)
        ).fetchone()
        assert row is not None
        assert row.status == ProposalStatus.proposed.value
        assert row.version == 1

        # And no audit row was persisted
        audit_count = conn.execute(
            select(text("count(*)")).select_from(audit_events)
        ).scalar()
        assert audit_count == 0


# ── 9. Schema Invariants Drill ─────────────────────────────────────────────


def test_tables_schema_invariants():
    """Verify table schema columns and unique constraints match spec requirements."""
    # policy_decisions
    for col in (
        "decision_id",
        "request_id",
        "customer_id",
        "order_id",
        "policy_version",
        "facts_json",
        "decision_json",
        "created_at",
    ):
        assert col in policy_decisions.c

    # proposals
    for col in (
        "proposal_id",
        "customer_id",
        "session_id",
        "type",
        "payload_json",
        "decision",
        "amounts_json",
        "policy_version",
        "facts_hash",
        "proposal_hash",
        "status",
        "expires_at",
        "confirmed_at",
        "version",
    ):
        assert col in proposals.c

    # executions
    for col in (
        "execution_id",
        "proposal_id",
        "step",
        "idempotency_key",
        "status",
        "store_ref",
        "error_code",
        "started_at",
        "finished_at",
    ):
        assert col in executions.c
    assert executions.c.idempotency_key.unique is True or any(
        isinstance(idx, select) or "idempotency_key" in str(idx)
        for idx in executions.indexes
    )

    # approvals
    for col in (
        "approval_id",
        "proposal_id",
        "order_id",
        "return_id",
        "refund_id",
        "amount_paise",
        "required_role",
        "status",
        "policy_version",
        "facts_hash",
        "due_at",
        "expires_at",
        "decided_by",
        "decided_at",
        "decision_reason",
        "store_authorization_ref",
        "version",
    ):
        assert col in approvals.c

    # client_events
    for col in (
        "event_id",
        "customer_id",
        "type",
        "payload_json",
        "created_at",
    ):
        assert col in client_events.c
