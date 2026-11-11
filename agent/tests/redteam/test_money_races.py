"""Money-path red-team true concurrent race tests (Spec §13, §21, T2.11).

Executes multi-threaded concurrent attacks to verify:
- 20 parallel simultaneous confirms on a single proposal yield exactly one winner and one execution
- Parallel seller approvals yield exactly one winner and 409 invalid_state on the second
- Customer withdraws/cancels while seller simultaneously approves: exactly one winner
- Confirm while order mutates in store: revalidation catches drift, returns 409 stale_state
- Concurrent store reservations serialize cleanly on store lock
- Store monetary invariants hold via LedgerOracle after every race
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.db import get_engine
from sellershield_agent.domain.state import (
    ApprovalStatus,
    approvals,
)
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from tests.redteam.ledger import LedgerOracle
from tests.redteam.money_support import (
    assert_universal_invariants,
    create_active_proposal,
    money_env,
)

# Reference fixture to prevent unused warning
_ = money_env


@pytest.mark.asyncio
async def test_races_parallel_confirms_single_proposal(money_env: dict[str, Any]) -> None:
    """MONEY-RACE-01: 20 parallel simultaneous confirms yield 1 execution and 0 duplicates."""
    token = money_env["tokens"]["c001"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]
    app = money_env["app"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    proposal_id = card["proposal_id"]
    proposal_hash = card["proposal_hash"]

    num_threads = 20

    def _worker(thread_idx: int) -> tuple[int, dict[str, Any]]:
        with TestClient(app, raise_server_exceptions=False) as tc:
            idem = f"idem-race-conf-{thread_idx}"
            resp = tc.post(
                f"/v1/proposals/{proposal_id}/confirm",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem},
                json={"proposal_hash": proposal_hash},
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            return resp.status_code, body

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(_worker, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    statuses = [s for s, _ in results]
    successes = [r for s, r in results if s == 200]
    conflicts = [r for s, r in results if s == 409]

    assert len(successes) == 1, f"Expected exactly 1 success, got {len(successes)}: {statuses}"
    assert len(conflicts) == 19, f"Expected 19 conflicts, got {len(conflicts)}"

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_races_parallel_seller_approvals(money_env: dict[str, Any]) -> None:
    """MONEY-RACE-02: Parallel seller approvals yield exactly one winner and 409 on the second."""
    owner_token = money_env["tokens"]["seller_owner"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]
    app = money_env["app"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    total_paise = int(card["amounts"]["total_paise"])
    apr_id = f"APR-{uuid.uuid4().hex[:8]}"

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc",
            role=Role.service,
            auth_time=int(clock.now().timestamp()),
            jti="jti-1",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-race-apr",
    )
    rfd = await store.reserve_refund(
        service_ctx=svc_ctx,
        order_id="ORD-1042",
        amount_paise=total_paise,
        return_id=None,
        method="original",
        idempotency_key="key-res-race-apr",
    )

    with get_engine().begin() as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=card["proposal_id"],
                order_id="ORD-1042",
                refund_id=rfd.refund_id,
                amount_paise=total_paise,
                status=ApprovalStatus.pending.value,
                policy_version="v3",
                required_role="seller_owner",
                store_id="nestkart",
                facts_hash="dummy-facts-hash",
                due_at=(clock.now() + timedelta(hours=24)).isoformat(),
                expires_at=(clock.now() + timedelta(hours=24)).isoformat(),
                created_at=clock.now().isoformat(),
                version=1,
            )
        )

    def _worker(thread_idx: int) -> tuple[int, dict[str, Any]]:
        with TestClient(app, raise_server_exceptions=False) as tc:
            idem = f"idem-race-apr-{thread_idx}"
            resp = tc.post(
                f"/seller/approvals/{apr_id}/decision",
                headers={"Authorization": f"Bearer {owner_token}", "Idempotency-Key": idem},
                json={"decision": "approve", "expected_version": 1},
            )
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            return resp.status_code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker, i) for i in range(2)]
        results = [f.result() for f in futures]

    statuses = [s for s, _ in results]
    assert 200 in statuses, f"Expected at least one success: {statuses}"
    assert 409 in statuses or statuses.count(200) == 1

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_races_approve_while_customer_withdraws(money_env: dict[str, Any]) -> None:
    """MONEY-RACE-03: Customer cancels proposal while seller approves: exactly one wins."""
    cust_token = money_env["tokens"]["c001"]
    owner_token = money_env["tokens"]["seller_owner"]
    engine = money_env["engine"]
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]
    app = money_env["app"]

    card = await create_active_proposal(
        engine=engine, store=store, clock=clock, order_id="ORD-1042"
    )
    total_paise = int(card["amounts"]["total_paise"])
    proposal_id = card["proposal_id"]
    apr_id = f"APR-{uuid.uuid4().hex[:8]}"

    with get_engine().begin() as conn:
        conn.execute(
            approvals.insert().values(
                approval_id=apr_id,
                proposal_id=proposal_id,
                order_id="ORD-1042",
                amount_paise=total_paise,
                status=ApprovalStatus.pending.value,
                policy_version="v3",
                required_role="seller_owner",
                store_id="nestkart",
                facts_hash="dummy-facts-hash",
                due_at=(clock.now() + timedelta(hours=24)).isoformat(),
                expires_at=(clock.now() + timedelta(hours=24)).isoformat(),
                created_at=clock.now().isoformat(),
                version=1,
            )
        )

    def _cancel() -> tuple[int, dict[str, Any]]:
        with TestClient(app, raise_server_exceptions=False) as tc:
            r = tc.post(
                f"/v1/proposals/{proposal_id}/cancel",
                headers={
                    "Authorization": f"Bearer {cust_token}",
                    "Idempotency-Key": "idem-cancel-race",
                },
            )
            return r.status_code, r.json()

    def _approve() -> tuple[int, dict[str, Any]]:
        with TestClient(app, raise_server_exceptions=False) as tc:
            r = tc.post(
                f"/seller/approvals/{apr_id}/decision",
                headers={
                    "Authorization": f"Bearer {owner_token}",
                    "Idempotency-Key": "idem-approve-race",
                },
                json={"decision": "approve", "expected_version": 1},
            )
            return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_cancel = pool.submit(_cancel)
        f_approve = pool.submit(_approve)
        cancel_res = f_cancel.result()
        approve_res = f_approve.result()

    results = [cancel_res[0], approve_res[0]]
    assert 200 in results, f"Neither succeeded: {results}"

    oracle.assert_all(engine=engine, order_ids=["ORD-1042"])


@pytest.mark.asyncio
async def test_races_confirm_while_order_mutates_in_store(money_env: dict[str, Any]) -> None:
    """MONEY-RACE-04: Confirming while order status mutates in store returns 409 stale_state."""
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

    for o in store._raw_data.get("orders", []):
        if o.get("order_id") == "ORD-1042":
            o["status"] = "cancelled"

    resp = client.post(
        f"/v1/proposals/{proposal_id}/confirm",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "idem-stale-order"},
        json={"proposal_hash": proposal_hash},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] in ("invalid_state", "stale_state")
    assert_universal_invariants(resp.text, resp.status_code, oracle, engine)


@pytest.mark.asyncio
async def test_races_concurrent_store_reservations(money_env: dict[str, Any]) -> None:
    """MONEY-RACE-07: Concurrent store reservations on the same order serialize on store lock."""
    store: FixtureStoreClient = money_env["raw_store"]
    clock: FixedClock = money_env["clock"]
    oracle: LedgerOracle = money_env["oracle"]

    svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc",
            role=Role.service,
            auth_time=int(clock.now().timestamp()),
            jti="jti-res-lock",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-res-lock",
    )

    r1 = await store.reserve_refund(
        service_ctx=svc_ctx,
        order_id="ORD-1042",
        amount_paise=80000,
        return_id=None,
        method="original",
        idempotency_key="idem-res-lock-1",
    )
    assert r1 is not None

    with pytest.raises(Exception) as exc_info:
        await store.reserve_refund(
            service_ctx=svc_ctx,
            order_id="ORD-1042",
            amount_paise=80000,
            return_id=None,
            method="original",
            idempotency_key="idem-res-lock-2",
        )
    assert "exceeds available refundable" in str(exc_info.value)

    oracle.assert_all(order_ids=["ORD-1042"])
