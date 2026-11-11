"""Regression tests for pre-dispatch order cancellation authorization by agent-executor."""

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import db
from app.auth import SERVICE_CLIENT_SCOPES, _mint_service_token
from app.main import app
from app.models import EventsOutbox, IdempotencyKey, Order, StoreAudit
from tests.test_orders_and_admin import _get_user_token

client = TestClient(app)


def test_executor_can_cancel_placed_order_and_monitor_or_customer_forbidden() -> None:
    """agent-executor can cancel a placed order while agent-monitor and customers get 403."""
    cust_token = _get_user_token("meera.iyer@example.test")
    monitor_token = _mint_service_token("agent-monitor", SERVICE_CLIENT_SCOPES["agent-monitor"])
    exec_token = _mint_service_token("agent-executor", SERVICE_CLIENT_SCOPES["agent-executor"])

    with Session(db.engine) as session:
        orig_order = session.scalar(select(Order).where(Order.order_id == "ORD-4021"))
        assert orig_order is not None
        orig_status = orig_order.status
        orig_cancelled_at = orig_order.cancelled_at
        orig_version = orig_order.version

    try:
        # Customer cannot call admin cancel (gets 403 even without Idempotency-Key)
        res_cust = client.post(
            "/api/v1/admin/orders/ORD-4021/cancel",
            json={"reason": "Customer self test"},
            headers={"Authorization": f"Bearer {cust_token}"},
        )
        assert res_cust.status_code == 403

        # agent-monitor cannot call admin cancel
        res_mon = client.post(
            "/api/v1/admin/orders/ORD-4021/cancel",
            json={"reason": "Monitor forbidden"},
            headers={
                "Authorization": f"Bearer {monitor_token}",
                "Idempotency-Key": "bt1-mon-cancel-4021",
            },
        )
        assert res_mon.status_code == 403

        # agent-executor cancels placed ORD-4021 with idempotency
        res_exec = client.post(
            "/api/v1/admin/orders/ORD-4021/cancel",
            json={"reason": "Customer confirmed pre-dispatch cancellation"},
            headers={
                "Authorization": f"Bearer {exec_token}",
                "Idempotency-Key": "bt1-exec-cancel-4021",
            },
        )
        assert res_exec.status_code == 200
        body = res_exec.json()
        assert body["order_id"] == "ORD-4021"
        assert body["status"] == "cancelled"

        # Idempotent replay returns exact same cancelled state
        res_replay = client.post(
            "/api/v1/admin/orders/ORD-4021/cancel",
            json={"reason": "Customer confirmed pre-dispatch cancellation"},
            headers={
                "Authorization": f"Bearer {exec_token}",
                "Idempotency-Key": "bt1-exec-cancel-4021",
            },
        )
        assert res_replay.status_code == 200
        assert res_replay.json()["status"] == "cancelled"
    finally:
        with Session(db.engine) as session:
            ord_row = session.scalar(select(Order).where(Order.order_id == "ORD-4021"))
            if ord_row is not None:
                ord_row.status = orig_status
                ord_row.cancelled_at = orig_cancelled_at
                ord_row.version = orig_version
            session.execute(
                delete(IdempotencyKey).where(IdempotencyKey.key == "bt1-exec-cancel-4021")
            )
            session.execute(
                delete(EventsOutbox).where(
                    EventsOutbox.type == "order.updated",
                    EventsOutbox.payload_json.contains("ORD-4021"),
                )
            )
            session.execute(
                delete(StoreAudit).where(StoreAudit.resource == "orders/ORD-4021")
            )
            session.execute(
                delete(StoreAudit).where(StoreAudit.resource == "/api/v1/admin/orders/ORD-4021/cancel")
            )
            session.commit()
