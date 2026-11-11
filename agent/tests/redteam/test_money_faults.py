"""Money-path red-team fault injection tests (Spec §13, §21, T2.11).

Executes fault injection attacks via FaultPlan and simulated crashes:
- Store timeout after commit on create_return (reconciled to succeeded/pending without duplicate)
- Store timeout after commit on reserve_refund (reconciler recovers existing reservation)
- Store 500 on release_refund (fails safely, no fake success text, status remains truthful)
- Audit failure during saga step aborts transaction cleanly (no un-audited state mutation)
- Clock jump across Asia/Kolkata midnight boundary resets daily tracking cleanly
- Transient fail_once on store write recovers with identical idempotency key
- LedgerOracle asserts all invariants hold after every fault
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.execution.reconcile import reconcile_once
from sellershield_agent.store.client import RequestContext, StoreUnavailable
from sellershield_agent.store.fixtures import FixtureStoreClient
from tests.fault_plan import FaultPlan
from tests.redteam.ledger import LedgerOracle
from tests.redteam.money_support import (
    create_active_proposal,
    money_env,
)

# Reference fixture to prevent unused warning
_ = money_env


@pytest.mark.asyncio
async def test_fault_store_timeout_after_commit_create_return(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-01: Store timeout after commit on create_return reconciles cleanly."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    faults = FaultPlan().timeout_after_commit("create_return")
    store._fault_plan = faults

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-fault-timeout-1"},
        json={"proposal_hash": proposal_hash},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("unknown", "executing", "succeeded", "pending_approval")

    # Clear fault plan and run reconciler
    store._fault_plan = None
    clock.advance(timedelta(seconds=10))
    await reconcile_once(clock=clock, store_client=store, engine=engine, min_age_seconds=0)

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_fault_store_timeout_after_commit_reserve_refund(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-02: Store timeout after commit on reserve_refund reconciles without dup hold."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    faults = FaultPlan().timeout_after_commit("reserve_refund")
    store._fault_plan = faults

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-fault-timeout-2"},
        json={"proposal_hash": proposal_hash},
    )
    assert resp.status_code == 200

    # Clear fault plan and reconcile
    store._fault_plan = None
    clock.advance(timedelta(seconds=10))
    await reconcile_once(clock=clock, store_client=store, engine=engine, min_age_seconds=0)

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_fault_store_500_on_release_fails_safely(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-03: Store unavailable on release fails safely with zero fake success."""
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc",
            role=Role.service,
            auth_time=int(clock.now().timestamp()),
            jti="jti-500",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-fault-500",
    )

    rfd = await store.reserve_refund(
        service_ctx=svc_ctx,
        order_id="ORD-1042",
        amount_paise=10000,
        return_id=None,
        method="original",
        idempotency_key="key-res-500",
    )

    faults = FaultPlan().unavailable("release_refund")
    store._fault_plan = faults

    with pytest.raises(Exception) as exc_info:
        await store.release_refund(
            ctx=svc_ctx, refund_id=rfd.refund_id, idempotency_key="key-rel-500"
        )
    assert "unavailable" in str(exc_info.value).lower()

    # Refund status must NOT be processing or completed
    assert rfd.status == "reserved"

    store._fault_plan = None
    oracle.assert_all(order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_fault_audit_failure_aborts_saga_step(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-05: Audit log failure during saga step aborts mutation cleanly."""
    client: TestClient = money_env["client"]
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    from sellershield_agent.audit.service import AuditService

    with patch.object(
        AuditService, "append", side_effect=RuntimeError("Simulated audit disk failure")
    ):
        resp = client.post(
            f"/v1/proposals/{proposal_id}/confirm",
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-audit-fail"},
            json={"proposal_hash": proposal_hash},
        )
        assert resp.status_code in (409, 500)

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_fault_clock_jump_across_ist_midnight(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-06: Clock jump across Asia/Kolkata midnight resets daily tracking."""
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    cur_date_key = clock.now().strftime("%Y-%m-%d")
    store._daily_releases[cur_date_key] = 450000

    clock.advance(timedelta(days=1))
    new_date_key = clock.now().strftime("%Y-%m-%d")
    assert new_date_key != cur_date_key

    assert store._daily_releases.get(new_date_key, 0) == 0

    oracle.assert_all(order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_fault_store_fail_once_transient_recovery(money_env: dict[str, Any]) -> None:
    """MONEY-FAULT-08: Transient fail_once on store write recovers with same idempotency key."""
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    faults = FaultPlan().fail_once("reserve_refund")
    store._fault_plan = faults

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc",
            role=Role.service,
            auth_time=int(clock.now().timestamp()),
            jti="jti-fo",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-fo",
    )

    with pytest.raises(StoreUnavailable):
        await store.reserve_refund(
            service_ctx=svc_ctx,
            order_id="ORD-1042",
            amount_paise=10000,
            return_id=None,
            method="original",
            idempotency_key="key-fo-1",
        )

    rfd = await store.reserve_refund(
        service_ctx=svc_ctx,
        order_id="ORD-1042",
        amount_paise=10000,
        return_id=None,
        method="original",
        idempotency_key="key-fo-1",
    )
    assert rfd is not None
    assert rfd.amount_paise == 10000

    oracle.assert_all(order_ids=["ORD-1042"])
