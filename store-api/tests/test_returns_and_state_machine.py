"""
Stage 7 test suite: Returns, state machine, and idempotency.

Tests:
- SC-04: Aarav creates a return on ORD-1042 (cookware, defective).
- Idempotency replay (same key + same payload -> identical response).
- Idempotency mismatch (same key + different payload -> 409).
- Missing Idempotency-Key -> 422 validation_error.
- SC-09: Duplicate return on same item blocked (409 duplicate).
- SC-14: Two parallel confirmations -> exactly one return (race test).
- Full parametrized test of every illegal state transition edge.
- Executor above the autonomy cap -> rejected.
- Uniform 404 for non-owner.
- Customer cancel before approval.
- Seller transition (approve, then approved->completed invalid).
- Admin receive and inspect endpoints.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import db
from app.auth import _mint_access_token, _mint_service_token
from app.main import app
from app.models import Return, ReturnItem, User
from app.returns import RETURN_STATE_TRANSITIONS

client = TestClient(app)


SEEDED_RETURN_IDS = {"RET-3001", "RET-3002", "RET-3003", "RET-3004", "RET-9050"}


@pytest.fixture(autouse=True)
def cleanup_test_returns():
    yield
    with Session(db.engine) as session:
        extra_returns = session.scalars(
            select(Return).where(Return.return_id.notin_(SEEDED_RETURN_IDS))
        ).all()
        for r in extra_returns:
            session.delete(r)
        session.commit()


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
def _token(email: str) -> str:
    """Mint a valid access token for a seeded user."""
    with Session(db.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None, f"Seeded user not found: {email}"
        cid = None
        if user.role == "customer" and user.customer:
            cid = user.customer.customer_id
        return _mint_access_token(user, cid)


def _executor_token() -> str:
    return _mint_service_token("agent-executor", ["returns:write"])


def _idem() -> str:
    return str(uuid.uuid4())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post_return(
    order_id: str,
    token: str,
    items: list[dict],
    reason_code: str = "defective",
    reason_text: str = "Item is broken",
    resolution: str = "refund",
    idem_key: str | None = None,
) -> tuple[str, "TestClient"]:
    """Helper to create a return; returns (idem_key, response)."""
    key = idem_key or _idem()
    body = {
        "items": items,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "resolution": resolution,
    }
    resp = client.post(
        f"/api/v1/orders/{order_id}/returns",
        json=body,
        headers={**_auth(token), "Idempotency-Key": key},
    )
    return key, resp


# -----------------------------------------------------------------------
# Seed context: get item_id for ORD-1042
# -----------------------------------------------------------------------
def _ord1042_item_id() -> str:
    """Return the item_id of the first item on ORD-1042."""
    with Session(db.engine) as session:
        from app.models import OrderItem

        item = session.scalar(select(OrderItem).where(OrderItem.order_id == "ORD-1042"))
        assert item is not None
        return item.item_id


# =======================================================================
# SC-04: Aarav creates a return on delivered ORD-1042
# =======================================================================
class TestSC04CreateReturn:
    def test_create_return_on_delivered_order(self):
        """Aarav creates a defective return on ORD-1042 -> 201."""
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        key, resp = _post_return(
            "ORD-1042",
            token,
            [{"item_id": item_id, "qty": 1}],
            reason_code="defective",
            reason_text="Cookware set has scratches",
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["return_id"].startswith("RET-")
        assert data["status"] == "requested"
        assert data["order_id"] == "ORD-1042"
        assert data["customer_id"] == "C001"
        assert data["reason_code"] == "defective"
        assert data["resolution"] == "refund"
        assert len(data["items"]) == 1


# =======================================================================
# Idempotency tests
# =======================================================================
class TestIdempotency:
    def test_replay_same_key_same_payload(self):
        """Same Idempotency-Key + same payload -> same RET id, no dup."""
        # Reset DB first so ORD-1042 has no active return on that item
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()

        # Cancel any active return on this item first
        self._cleanup_active_returns_on(item_id)

        key = _idem()
        body = {
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "replay test",
            "resolution": "refund",
        }

        resp1 = client.post(
            "/api/v1/orders/ORD-1042/returns",
            json=body,
            headers={**_auth(token), "Idempotency-Key": key},
        )
        assert resp1.status_code == 201, resp1.text
        ret_id = resp1.json()["return_id"]

        # Replay
        resp2 = client.post(
            "/api/v1/orders/ORD-1042/returns",
            json=body,
            headers={**_auth(token), "Idempotency-Key": key},
        )
        assert resp2.status_code == 201
        assert resp2.json()["return_id"] == ret_id

        # GET /returns shows exactly one return with this id
        list_resp = client.get(
            "/api/v1/returns",
            headers=_auth(token),
        )
        ids = [r["return_id"] for r in list_resp.json()]
        assert ids.count(ret_id) == 1

    def test_mismatch_same_key_different_payload(self):
        """Same key + changed reason_text -> 409 idempotency_mismatch."""
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        self._cleanup_active_returns_on(item_id)

        key = _idem()
        body1 = {
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "original text",
            "resolution": "refund",
        }
        resp1 = client.post(
            "/api/v1/orders/ORD-1042/returns",
            json=body1,
            headers={**_auth(token), "Idempotency-Key": key},
        )
        assert resp1.status_code == 201

        body2 = {**body1, "reason_text": "changed text"}
        resp2 = client.post(
            "/api/v1/orders/ORD-1042/returns",
            json=body2,
            headers={**_auth(token), "Idempotency-Key": key},
        )
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "idempotency_mismatch"

    def test_missing_idempotency_key(self):
        """POST without Idempotency-Key -> 422 validation_error."""
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        resp = client.post(
            "/api/v1/orders/ORD-1042/returns",
            json={
                "items": [{"item_id": item_id, "qty": 1}],
                "reason_code": "defective",
                "reason_text": "no key",
                "resolution": "refund",
            },
            headers=_auth(token),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"

    def _cleanup_active_returns_on(self, item_id: str):
        """Cancel any active return items for the given item_id."""
        with Session(db.engine) as session:
            items = session.scalars(select(ReturnItem).where(ReturnItem.item_id == item_id)).all()
            for ri in items:
                if ri.status not in ("completed", "cancelled", "rejected"):
                    ri.status = "cancelled"
                    ret = session.get(Return, ri.return_id)
                    if ret and ret.status not in ("completed", "cancelled", "rejected"):
                        ret.status = "cancelled"
            session.commit()


# =======================================================================
# SC-09: Duplicate blocked
# =======================================================================
class TestSC09DuplicateBlocked:
    def test_duplicate_return_on_same_item(self):
        """Second return on same active item -> 409 duplicate."""
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()

        # Cleanup and create the first
        TestIdempotency()._cleanup_active_returns_on(item_id)

        _, resp1 = _post_return(
            "ORD-1042",
            token,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp1.status_code == 201

        # Attempt second with a new key
        _, resp2 = _post_return(
            "ORD-1042",
            token,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "duplicate"


# =======================================================================
# SC-14: Double concurrent confirmation -> exactly one return
# =======================================================================
class TestSC14DoubleConfirmation:
    def test_double_confirmation_race(self):
        """
        Two parallel requests for the same item produce exactly
        one return (the second gets duplicate or idempotency replay).
        Selectable with -k double.
        """
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        body = {
            "items": [{"item_id": item_id, "qty": 1}],
            "reason_code": "defective",
            "reason_text": "race test",
            "resolution": "refund",
        }

        results = []

        def _submit(key: str):
            resp = client.post(
                "/api/v1/orders/ORD-1042/returns",
                json=body,
                headers={
                    **_auth(token),
                    "Idempotency-Key": key,
                },
            )
            return resp.status_code, resp.json()

        key_a = _idem()
        key_b = _idem()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_submit, key_a),
                pool.submit(_submit, key_b),
            ]
            for f in as_completed(futures):
                results.append(f.result())

        statuses = [r[0] for r in results]
        # Exactly one 201 and one 409 (duplicate)
        assert 201 in statuses, f"No 201 in {results}"
        wins = [r for r in results if r[0] == 201]
        assert len(wins) == 1, f"Expected 1 winner: {results}"

        # DB has exactly one active return on this item
        with Session(db.engine) as session:
            active = session.scalars(
                select(ReturnItem).where(
                    ReturnItem.item_id == item_id,
                    ReturnItem.status.notin_(["completed", "cancelled", "rejected"]),
                )
            ).all()
            assert len(active) == 1


# =======================================================================
# State machine: every illegal edge rejected
# =======================================================================
def _all_illegal_edges():
    """Generate (from_state, to_state) for every illegal transition."""
    all_states = list(RETURN_STATE_TRANSITIONS.keys())
    for from_st in all_states:
        allowed = RETURN_STATE_TRANSITIONS[from_st]
        for to_st in all_states:
            if to_st not in allowed:
                yield from_st, to_st


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        list(_all_illegal_edges()),
        ids=[f"{f}->{t}" for f, t in _all_illegal_edges()],
    )
    def test_illegal_edge_rejected(self, from_state, to_state):
        """Every non-allowed transition -> 409 invalid_state."""
        seller_token = _token("priya.sharma@nestkart.test")

        # Create a return manually in the desired from_state
        with Session(db.engine) as session:
            from app.clock import Clock

            now = Clock.now()
            ret_id = f"RET-T-{from_state[:4]}-{to_state[:4]}"
            # Remove any previous test return with this id
            old = session.get(Return, ret_id)
            if old:
                session.delete(old)
                session.commit()

            ret = Return(
                return_id=ret_id,
                order_id="ORD-1042",
                customer_id="C001",
                status=from_state,
                reason_code="defective",
                reason_text="state machine test",
                resolution="refund",
                created_at=now,
                updated_at=now,
                version=1,
            )
            session.add(ret)
            session.commit()

        try:
            resp = client.post(
                f"/api/v1/returns/{ret_id}/transition",
                json={"to": to_state, "reason": "test"},
                headers={
                    **_auth(seller_token),
                    "Idempotency-Key": _idem(),
                },
            )
            assert resp.status_code == 409, (
                f"{from_state}->{to_state} should be 409 but got "
                f"{resp.status_code}: {resp.text}"
            )
            assert resp.json()["error"]["code"] == "invalid_state"
        finally:
            with Session(db.engine) as session:
                r = session.get(Return, ret_id)
                if r:
                    session.delete(r)
                    session.commit()


# =======================================================================
# Executor above autonomy cap rejected
# =======================================================================
class TestExecutorAutonomyCap:
    def test_executor_above_cap_rejected(self):
        """
        ORD-1042 value ₹1,200 (120000 paise) > autonomy_cap 50000.
        Executor approving is rejected with limit_exceeded.
        """
        token = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        # Create a return as customer
        _, resp = _post_return(
            "ORD-1042",
            token,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        # Executor tries to approve
        exec_token = _executor_token()
        resp2 = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            json={"to": "approved", "reason": "auto"},
            headers={
                "Authorization": f"Bearer {exec_token}",
                "Idempotency-Key": _idem(),
            },
        )
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "limit_exceeded"

    def test_executor_within_cap_accepted(self):
        """
        ORD-2032 value ₹499 (49900 paise) < autonomy_cap 50000.
        Executor approving is accepted.
        """
        token = _token("diya.nair@example.test")

        # Get item on ORD-2032
        with Session(db.engine) as session:
            from app.models import OrderItem

            item = session.scalar(select(OrderItem).where(OrderItem.order_id == "ORD-2032"))
            assert item is not None
            item_id = item.item_id

        TestIdempotency()._cleanup_active_returns_on(item_id)

        # Create a return
        _, resp = _post_return(
            "ORD-2032",
            token,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        exec_token = _executor_token()
        resp2 = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            json={"to": "approved", "reason": "within cap"},
            headers={
                "Authorization": f"Bearer {exec_token}",
                "Idempotency-Key": _idem(),
            },
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "approved"


# =======================================================================
# Uniform 404 for non-owner
# =======================================================================
class TestUniform404:
    def test_diya_cannot_return_aaravs_order(self):
        """Diya tries to create a return on ORD-1042 -> uniform 404."""
        token = _token("diya.nair@example.test")
        item_id = _ord1042_item_id()
        _, resp = _post_return(
            "ORD-1042",
            token,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
        assert resp.json()["error"]["message"] == "Order not found"


# =======================================================================
# Seller transition tests
# =======================================================================
class TestSellerTransition:
    def test_seller_approve_then_completed_invalid(self):
        """
        Priya moves requested->approved -> OK.
        Then approved->completed -> 409 invalid_state.
        """
        # Create a fresh return
        aarav = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        _, resp = _post_return(
            "ORD-1042",
            aarav,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        priya = _token("priya.sharma@nestkart.test")

        # Approve
        resp2 = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            json={"to": "approved", "reason": "looks good"},
            headers={**_auth(priya), "Idempotency-Key": _idem()},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "approved"

        # Try approved -> completed (invalid)
        resp3 = client.post(
            f"/api/v1/returns/{ret_id}/transition",
            json={"to": "completed", "reason": "skip"},
            headers={**_auth(priya), "Idempotency-Key": _idem()},
        )
        assert resp3.status_code == 409
        assert resp3.json()["error"]["code"] == "invalid_state"


# =======================================================================
# Customer cancel
# =======================================================================
class TestCustomerCancel:
    def test_cancel_before_approval(self):
        """Customer cancels while status is 'requested'."""
        aarav = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        _, resp = _post_return(
            "ORD-1042",
            aarav,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        resp2 = client.post(
            f"/api/v1/returns/{ret_id}/cancel",
            headers={**_auth(aarav), "Idempotency-Key": _idem()},
        )
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "cancelled"

    def test_cancel_after_approval_fails(self):
        """Customer cannot cancel after approval."""
        aarav = _token("aarav.mehta@example.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        _, resp = _post_return(
            "ORD-1042",
            aarav,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        # Seller approves
        priya = _token("priya.sharma@nestkart.test")
        client.post(
            f"/api/v1/returns/{ret_id}/transition",
            json={"to": "approved", "reason": "ok"},
            headers={**_auth(priya), "Idempotency-Key": _idem()},
        )

        # Customer tries to cancel
        resp2 = client.post(
            f"/api/v1/returns/{ret_id}/cancel",
            headers={**_auth(aarav), "Idempotency-Key": _idem()},
        )
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "invalid_state"


# =======================================================================
# GET endpoints
# =======================================================================
class TestGetReturns:
    def test_seller_sees_all_returns(self):
        """Priya (seller) sees all returns."""
        priya = _token("priya.sharma@nestkart.test")
        resp = client.get("/api/v1/returns", headers=_auth(priya))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_customer_sees_own_only(self):
        """Customer sees only their own returns."""
        aarav = _token("aarav.mehta@example.test")
        resp = client.get("/api/v1/returns", headers=_auth(aarav))
        assert resp.status_code == 200
        for r in resp.json():
            assert r["customer_id"] == "C001"

    def test_get_return_detail(self):
        """GET /returns/{id} works for owner."""
        # Use seed return RET-9050
        rohan = _token("rohan.das@example.test")
        resp = client.get(
            "/api/v1/returns/RET-9050",
            headers=_auth(rohan),
        )
        assert resp.status_code == 200
        assert resp.json()["return_id"] == "RET-9050"

    def test_get_return_nonowner_404(self):
        """GET /returns/{id} for non-owner -> uniform 404."""
        aarav = _token("aarav.mehta@example.test")
        resp = client.get(
            "/api/v1/returns/RET-9050",
            headers=_auth(aarav),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# =======================================================================
# Non-delivered order rejection
# =======================================================================
class TestNonDeliveredOrder:
    def test_return_on_non_delivered_order(self):
        """Return on ORD-1048 (status shipped) -> 409 invalid_state."""
        aarav = _token("aarav.mehta@example.test")
        with Session(db.engine) as session:
            from app.models import OrderItem

            item = session.scalar(select(OrderItem).where(OrderItem.order_id == "ORD-1048"))
            assert item is not None
            item_id = item.item_id

        _, resp = _post_return(
            "ORD-1048",
            aarav,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "invalid_state"


# =======================================================================
# Admin receive and inspect endpoints
# =======================================================================
class TestAdminReceiveAndInspect:
    def test_receive_and_inspect_flow(self):
        """Seller can receive return in_transit_back and inspect it ok."""
        aarav = _token("aarav.mehta@example.test")
        priya = _token("priya.sharma@nestkart.test")
        item_id = _ord1042_item_id()
        TestIdempotency()._cleanup_active_returns_on(item_id)

        _, resp = _post_return(
            "ORD-1042",
            aarav,
            [{"item_id": item_id, "qty": 1}],
        )
        assert resp.status_code == 201
        ret_id = resp.json()["return_id"]

        # requested -> approved -> pickup_scheduled -> in_transit_back
        for next_st in ["approved", "pickup_scheduled", "in_transit_back"]:
            r = client.post(
                f"/api/v1/returns/{ret_id}/transition",
                json={"to": next_st, "reason": "advancing"},
                headers={**_auth(priya), "Idempotency-Key": _idem()},
            )
            assert r.status_code == 200

        # Seller receives
        recv_resp = client.post(
            f"/api/v1/admin/returns/{ret_id}/receive",
            headers={**_auth(priya), "Idempotency-Key": _idem()},
        )
        assert recv_resp.status_code == 200
        assert recv_resp.json()["status"] == "received"

        # Seller inspects
        insp_resp = client.post(
            f"/api/v1/admin/returns/{ret_id}/inspect",
            json={"result": "ok", "note": "All good"},
            headers={**_auth(priya), "Idempotency-Key": _idem()},
        )
        assert insp_resp.status_code == 200
        assert insp_resp.json()["status"] == "inspected_ok"
        assert insp_resp.json()["items"][0]["condition_note"] == "All good"

    def test_customer_cannot_call_admin_receive_or_inspect(self):
        """Customer receives 403 forbidden on admin endpoints."""
        aarav = _token("aarav.mehta@example.test")
        resp1 = client.post(
            "/api/v1/admin/returns/RET-9050/receive",
            headers={**_auth(aarav), "Idempotency-Key": _idem()},
        )
        assert resp1.status_code == 403
        assert resp1.json()["error"]["code"] == "forbidden"

        resp2 = client.post(
            "/api/v1/admin/returns/RET-9050/inspect",
            json={"result": "ok"},
            headers={**_auth(aarav), "Idempotency-Key": _idem()},
        )
        assert resp2.status_code == 403
        assert resp2.json()["error"]["code"] == "forbidden"
