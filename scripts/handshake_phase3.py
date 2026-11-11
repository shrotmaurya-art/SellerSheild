"""Dev tool: Real store Phase 3 handshake verification script (TASK T3.7).

Connects to the running NestKart store:
1. Sends a test email notification to customer C001 via HttpStoreClient.send_mail (service token).
2. Authenticates as C001 and reads the notification from C001's store inbox (/mail/inbox).
3. Lists shipments via HttpStoreClient.list_shipments_for_monitor (monitor service token)
   and prints the count of delayed shipments.
4. Exits 0 on success.

Usage:
    uv run python scripts/handshake_phase3.py
    # or from agent/:
    uv run python ../scripts/handshake_phase3.py

Invariants:
- Access tokens and secrets are handled as SecretStr and never printed or logged (KP-13).
- Exits 0 on success; exits non-zero on error.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid

import httpx
from pydantic import SecretStr

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.domain.models import ShipmentListResponse
from sellershield_agent.store.client import ServiceContext
from sellershield_agent.store.http_client import HttpStoreClient


def _clean_url(url: str, default: str) -> str:
    res = (url or default).strip().rstrip("/")
    return res


async def handshake_phase3() -> int:
    store_base = _clean_url(os.environ.get("STORE_BASE_URL", ""), "http://localhost:8000")
    api_url = f"{store_base}/api/v1" if not store_base.endswith("/api/v1") else store_base

    cust_email = (
        os.environ.get("DEMO_CUSTOMER_EMAIL")
        or os.environ.get("STORE_DEMO_EMAIL")
        or "aarav.mehta@example.test"
    )
    cust_password = (
        os.environ.get("DEMO_CUSTOMER_PASSWORD")
        or os.environ.get("STORE_DEMO_PASSWORD")
        or "Demo@12345"
    )

    executor_client_id = os.environ.get("STORE_EXECUTOR_CLIENT_ID", "agent-executor")
    executor_client_secret = SecretStr(
        os.environ.get("STORE_EXECUTOR_CLIENT_SECRET", "fake_executor_secret_key_12345")
    )
    monitor_client_id = os.environ.get("STORE_MONITOR_CLIENT_ID", "agent-monitor")
    monitor_client_secret = SecretStr(
        os.environ.get("STORE_MONITOR_CLIENT_SECRET", "fake_monitor_secret_key_67890")
    )

    print("[INIT] Starting Phase 3 Store Handshake (T3.7)...")
    print(f"[INIT] Store Base URL: {store_base}")

    store_client = HttpStoreClient(
        base_url=store_base,
        executor_client_id=executor_client_id,
        executor_client_secret=executor_client_secret,
        monitor_client_id=monitor_client_id,
        monitor_client_secret=monitor_client_secret,
    )

    # ─────────────────────────────────────────────────────────────────
    # Step 1: Send a test notification to C001 via send_mail
    # ─────────────────────────────────────────────────────────────────
    unique_run_id = uuid.uuid4().hex[:8]
    test_subject = f"Handshake Phase 3 Verification - {unique_run_id}"
    test_body = (
        "Hello Aarav Mehta,\n\n"
        f"This is an automated Phase 3 verification notice (run {unique_run_id})."
    )

    service_ctx = ServiceContext(
        principal=Principal(
            user_id="SRV-EXECUTOR",
            customer_id=None,
            role=Role.service,
            jti=f"jti-h3-send-{unique_run_id}",
            token_exp=int(time.time()) + 3600,
        ),
        request_id=f"req-h3-send-{unique_run_id}",
        token=None,
        scopes=["orders:read_all", "mail:send"],
    )

    try:
        mail_resp = await store_client.send_mail(
            service_ctx=service_ctx,
            recipient=cust_email,
            subject=test_subject,
            body_text=test_body,
            idempotency_key=f"mail:h3:{unique_run_id}",
        )
        sent_message_id = mail_resp.get("message_id", "unknown")
        print(f"[STEP 1] Sent test notification to {cust_email} (message_id: {sent_message_id})")
    except Exception as exc:
        sys.stderr.write(f"[STEP 1] send_mail failed: {exc}\n")
        return 1

    # ─────────────────────────────────────────────────────────────────
    # Step 2: Read it from C001's store inbox
    # ─────────────────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as http:
        try:
            login_resp = await http.post(
                f"{api_url}/auth/login",
                json={"email": cust_email, "password": cust_password},
            )
        except Exception as exc:
            sys.stderr.write(f"[STEP 2] Store login connection failed: {exc}\n")
            return 1

        if login_resp.status_code != 200:
            sys.stderr.write(
                f"[STEP 2] Login failed for {cust_email}: HTTP {login_resp.status_code}\n"
            )
            return 1

        try:
            token_data = login_resp.json()
            cust_token = SecretStr(str(token_data["access_token"]))
        except Exception as exc:
            sys.stderr.write(f"[STEP 2] Failed to parse access_token: {exc}\n")
            return 1

        try:
            inbox_resp = await http.get(
                f"{api_url}/mail/inbox",
                headers={
                    "Authorization": f"Bearer {cust_token.get_secret_value()}",
                    "X-Request-ID": f"req-h3-inbox-{unique_run_id}",
                },
            )
        except Exception as exc:
            sys.stderr.write(f"[STEP 2] Failed to query inbox: {exc}\n")
            return 1

        if inbox_resp.status_code != 200:
            sys.stderr.write(f"[STEP 2] Inbox query failed: HTTP {inbox_resp.status_code}\n")
            return 1

        inbox_data = inbox_resp.json()
        messages = inbox_data.get("messages", [])
        matched = [
            m for m in messages
            if m.get("message_id") == sent_message_id or m.get("subject") == test_subject
        ]

        if not matched:
            sys.stderr.write(
                f"[STEP 2] Sent message {sent_message_id} not in inbox (msgs: {len(messages)})\n"
            )
            return 1

        found_msg = matched[0]
        print(
            f"[STEP 2] Verified notification in store inbox for C001 "
            f"(message_id: {found_msg.get('message_id')}, subject: {found_msg.get('subject')})"
        )

    # ─────────────────────────────────────────────────────────────────
    # Step 3: List shipments and count delayed ones
    # ─────────────────────────────────────────────────────────────────
    monitor_ctx = ServiceContext(
        principal=Principal(
            user_id="SRV-MONITOR",
            customer_id=None,
            role=Role.service,
            jti=f"jti-h3-mon-{unique_run_id}",
            token_exp=int(time.time()) + 3600,
        ),
        request_id=f"req-h3-mon-{unique_run_id}",
        token=None,
        scopes=["orders:read_all", "mail:send"],
    )

    try:
        shipments_resp = await store_client.list_shipments_for_monitor(
            service_ctx=monitor_ctx,
            limit=50,
        )
        if not isinstance(shipments_resp, ShipmentListResponse):
            sys.stderr.write(
                f"[STEP 3] Unexpected response type: {type(shipments_resp)}\n"
            )
            return 1

        all_shipments = shipments_resp.shipments
        delayed_shipments = [s for s in all_shipments if s.delayed]
        delayed_count = len(delayed_shipments)
        total_count = len(all_shipments)

        print(f"[STEP 3] Shipments retrieved: {total_count} total.")
        for s in delayed_shipments:
            print(
                f"  - Delayed: {s.order_id} ({s.shipment_id}) | "
                f"Carrier: {s.carrier} | Status: {s.status}"
            )
        print(f"Delayed shipments count: {delayed_count}")

    except Exception as exc:
        sys.stderr.write(f"[STEP 3] list_shipments_for_monitor failed: {exc}\n")
        return 1

    print("[SUCCESS] Phase 3 Handshake verified successfully.")
    return 0


def main() -> None:
    code = asyncio.run(handshake_phase3())
    sys.exit(code)


if __name__ == "__main__":
    main()
