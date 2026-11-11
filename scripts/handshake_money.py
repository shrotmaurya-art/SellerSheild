"""Dev tool: Real store money flow and full-flow handshake script (T2.9).

Connects to the running NestKart store and SellerShield Agent, executes the complete
end-to-end money lifecycle for order ORD-1042, and exits 0 only when the refund
reaches 'completed' state.

Lifecycle:
1. Customer C001 logs in to store API.
2. Customer initiates chat session and requests return for ORD-1042 (cookware set, defective).
3. Customer receives proposal card (PRP-...) with deterministic proposal_hash.
4. Customer confirms proposal -> saga creates store return (RET-...) and refund reservation
   (RFD-...). Autonomy limit ₹500 triggered (ORD-1042 is ₹1,200), resulting in pending seller
   approval (APR-...).
5. Seller owner (Priya) logs in to store API.
6. Seller approves APR-... on agent API -> saga authorizes refund and marks return approved.
7. Return lifecycle advances in store admin:
   - pickup_scheduled
   - in_transit_back
   - received at warehouse (admin receive)
   - inspected_ok at warehouse (admin inspect)
8. Refund release triggered and SimPay completes refund.
9. Polls refund status until 'completed' and exits 0.

Usage:
    uv run python scripts/handshake_money.py

Invariants:
- Access tokens are handled as SecretStr and never printed or logged (KP-13).
- Every state transition is printed cleanly to stdout.
- Exits 0 on success; exits non-zero on failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx
from pydantic import SecretStr


def _clean_url(url: str, default: str) -> str:
    res = (url or default).strip().rstrip("/")
    return res


async def _login(client: httpx.AsyncClient, api_url: str, email: str, password: str) -> SecretStr:
    resp = await client.post(
        f"{api_url}/auth/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Login failed for {email}: HTTP {resp.status_code} - {resp.text}")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Login response missing access_token for {email}")
    return SecretStr(str(token))


async def handshake_money() -> int:
    store_base = _clean_url(os.environ.get("STORE_BASE_URL", ""), "http://localhost:8000")
    store_api = f"{store_base}/api/v1" if not store_base.endswith("/api/v1") else store_base

    agent_base = _clean_url(os.environ.get("AGENT_BASE_URL", ""), "http://localhost:8001")

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

    seller_email = (
        os.environ.get("DEMO_SELLER_EMAIL")
        or os.environ.get("STORE_SELLER_EMAIL")
        or "priya.sharma@nestkart.test"
    )
    seller_password = (
        os.environ.get("DEMO_SELLER_PASSWORD")
        or os.environ.get("STORE_SELLER_PASSWORD")
        or "Demo@12345"
    )

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
        print("[INIT] Starting SellerShield full-flow handshake (T2.9)...")
        print(f"[INIT] Store API: {store_api}")
        print(f"[INIT] Agent API: {agent_base}")

        # Step 0: Optional demo reset if reset endpoint is accessible
        try:
            seller_tok_init = await _login(http, store_api, seller_email, seller_password)
            reset_resp = await http.post(
                f"{store_api}/demo/reset",
                headers={"Authorization": f"Bearer {seller_tok_init.get_secret_value()}"},
            )
            if reset_resp.status_code == 200:
                print("[STATE] Store demo state reset successfully")
        except Exception:
            # Non-fatal if demo reset is disabled or store not in demo mode
            pass

        # Step 1: Customer C001 logs in
        try:
            cust_tok = await _login(http, store_api, cust_email, cust_password)
            print("[STATE] Logged in as customer C001")
        except Exception as exc:
            sys.stderr.write(f"Customer login failed: {exc}\n")
            return 1

        cust_headers = {
            "Authorization": f"Bearer {cust_tok.get_secret_value()}",
            "Content-Type": "application/json",
            "X-Request-ID": f"req-hs-{uuid.uuid4().hex[:8]}",
        }

        # Step 2: Create chat session on Agent
        try:
            ses_resp = await http.post(f"{agent_base}/v1/chat/sessions", headers=cust_headers)
            if ses_resp.status_code != 200:
                sys.stderr.write(
                    f"Create chat session failed: HTTP {ses_resp.status_code} {ses_resp.text}\n"
                )
                return 1
            session_id = ses_resp.json()["session_id"]
            print(f"[STATE] Chat session created: {session_id}")
        except Exception as exc:
            sys.stderr.write(f"Failed to create chat session: {exc}\n")
            return 1

        # Step 3: Send chat message requesting return for ORD-1042 and parse proposal card
        proposal_id: str | None = None
        proposal_hash: str | None = None

        msg_payload = {
            "client_msg_id": f"msg-{uuid.uuid4().hex[:8]}",
            "text": "My cookware set is defective, I want to return it for order ORD-1042",
        }

        try:
            async with http.stream(
                "POST",
                f"{agent_base}/v1/chat/sessions/{session_id}/messages",
                headers=cust_headers,
                json=msg_payload,
                timeout=30.0,
            ) as sse_stream:
                if sse_stream.status_code != 200:
                    sys.stderr.write(f"Send chat message failed: HTTP {sse_stream.status_code}\n")
                    return 1

                current_event = ""
                async for line in sse_stream.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_str = line[len("data:") :].strip()
                        if current_event == "card":
                            try:
                                card_data = json.loads(data_str)
                                payload = card_data.get("payload", {})
                                if "proposal_id" in payload and "proposal_hash" in payload:
                                    proposal_id = payload["proposal_id"]
                                    proposal_hash = payload["proposal_hash"]
                            except Exception:
                                pass
        except Exception as exc:
            sys.stderr.write(f"Chat message SSE stream failed: {exc}\n")
            return 1

        if not proposal_id or not proposal_hash:
            sys.stderr.write("Failed to receive proposal card from chat stream\n")
            return 1

        print(f"[STATE] Proposal created: {proposal_id} (hash: {proposal_hash[:16]}...)")

        # Step 4: Confirm proposal
        confirm_idem = f"confirm-prp-{uuid.uuid4().hex[:12]}"
        confirm_headers = {
            **cust_headers,
            "Idempotency-Key": confirm_idem,
            "X-Request-ID": f"req-confirm-{uuid.uuid4().hex[:8]}",
        }
        try:
            confirm_resp = await http.post(
                f"{agent_base}/v1/proposals/{proposal_id}/confirm",
                headers=confirm_headers,
                json={"proposal_hash": proposal_hash},
            )
            if confirm_resp.status_code != 200:
                sys.stderr.write(
                    f"Confirm failed: HTTP {confirm_resp.status_code} {confirm_resp.text}\n"
                )
                return 1
            confirm_data = confirm_resp.json()
            c_status = confirm_data.get("status")
            approval_id = confirm_data.get("approval_id")
            return_id = confirm_data.get("return_id")
            refund_id = confirm_data.get("refund_id")
            print(
                f"[STATE] Proposal confirmed: status={c_status}, approval_id={approval_id}, "
                f"return_id={return_id}, refund_id={refund_id}"
            )
        except Exception as exc:
            sys.stderr.write(f"Proposal confirmation error: {exc}\n")
            return 1

        if not approval_id or not return_id or not refund_id:
            sys.stderr.write("Missing approval_id, return_id, or refund_id from confirmation\n")
            return 1

        # Step 5: Seller logs in
        try:
            seller_tok = await _login(http, store_api, seller_email, seller_password)
            print("[STATE] Logged in as seller staff/owner")
        except Exception as exc:
            sys.stderr.write(f"Seller login failed: {exc}\n")
            return 1

        seller_headers = {
            "Authorization": f"Bearer {seller_tok.get_secret_value()}",
            "Content-Type": "application/json",
            "X-Request-ID": f"req-sel-{uuid.uuid4().hex[:8]}",
        }

        # Step 6: Seller approves pending approval card on Agent
        approve_idem = f"approve-apr-{uuid.uuid4().hex[:12]}"
        try:
            app_resp = await http.post(
                f"{agent_base}/v1/seller/approvals/{approval_id}/decision",
                headers={**seller_headers, "Idempotency-Key": approve_idem},
                json={"decision": "approve", "expected_version": 1},
            )
            if app_resp.status_code != 200:
                sys.stderr.write(
                    f"Seller approval failed: HTTP {app_resp.status_code} {app_resp.text}\n"
                )
                return 1
            app_data = app_resp.json()
            app_status = app_data.get("status")
            print(f"[STATE] Seller approved: approval_id={approval_id}, status={app_status}")
        except Exception as exc:
            sys.stderr.write(f"Seller approval error: {exc}\n")
            return 1

        # Step 7: Advance return lifecycle in Store Admin
        print("[STATE] Advancing return lifecycle in store admin...")
        trans_1_resp = await http.post(
            f"{store_api}/returns/{return_id}/transition",
            headers={**seller_headers, "Idempotency-Key": f"trans-1-{uuid.uuid4().hex[:8]}"},
            json={"to": "pickup_scheduled", "reason": "Courier pickup scheduled"},
        )
        if trans_1_resp.status_code != 200:
            sys.stderr.write(
                f"Pickup failed: HTTP {trans_1_resp.status_code} {trans_1_resp.text}\n"
            )
            return 1
        print("[STATE] Return status: pickup_scheduled")

        trans_2_resp = await http.post(
            f"{store_api}/returns/{return_id}/transition",
            headers={**seller_headers, "Idempotency-Key": f"trans-2-{uuid.uuid4().hex[:8]}"},
            json={"to": "in_transit_back", "reason": "Package in transit back to warehouse"},
        )
        if trans_2_resp.status_code != 200:
            sys.stderr.write(
                f"Transit failed: HTTP {trans_2_resp.status_code} {trans_2_resp.text}\n"
            )
            return 1
        print("[STATE] Return status: in_transit_back")

        recv_resp = await http.post(
            f"{store_api}/admin/returns/{return_id}/receive",
            headers={**seller_headers, "Idempotency-Key": f"recv-{uuid.uuid4().hex[:8]}"},
            json={},
        )
        if recv_resp.status_code != 200:
            sys.stderr.write(
                f"Admin receive failed: HTTP {recv_resp.status_code} {recv_resp.text}\n"
            )
            return 1
        print("[STATE] Return status: received at warehouse")

        inspect_resp = await http.post(
            f"{store_api}/admin/returns/{return_id}/inspect",
            headers={**seller_headers, "Idempotency-Key": f"inspect-{uuid.uuid4().hex[:8]}"},
            json={"outcome": "inspected_ok", "note": "Verified defective cookware item"},
        )
        if inspect_resp.status_code != 200:
            sys.stderr.write(
                f"Admin inspect failed: HTTP {inspect_resp.status_code} {inspect_resp.text}\n"
            )
            return 1
        print("[STATE] Return status: inspected_ok")

        # Step 8: Poll refund status until 'completed'
        print("[STATE] Awaiting refund completion via webhook and SimPay gateway...")
        last_status = "unknown"
        release_attempted = False

        for poll_iter in range(30):
            await asyncio.sleep(1.0)
            rfd_resp = await http.get(
                f"{store_api}/refunds/{refund_id}",
                headers=seller_headers,
            )
            if rfd_resp.status_code == 200:
                rfd_obj = rfd_resp.json()
                current_status = rfd_obj.get("status", "unknown")
                if current_status != last_status:
                    print(f"[STATE] Refund status: {current_status}")
                    last_status = current_status

                if current_status == "completed":
                    gw = rfd_obj.get("gateway_ref")
                    print(f"[STATE] Refund {refund_id} completed successfully! Gateway: {gw}")
                    return 0

                # Fallback: If outbox webhook worker is not active, release directly after 5s
                if current_status == "reserved" and poll_iter >= 5 and not release_attempted:
                    print("[NOTICE] Triggering direct release fallback from seller...")
                    release_attempted = True
                    await http.post(
                        f"{store_api}/refunds/{refund_id}/release",
                        headers={
                            **seller_headers,
                            "Idempotency-Key": f"release-fallback-{uuid.uuid4().hex[:8]}",
                        },
                        json={"note": "Handshake test release"},
                    )

        sys.stderr.write(f"Timeout awaiting refund completion. Final status: {last_status}\n")
        return 1


def main() -> None:
    code = asyncio.run(handshake_money())
    sys.exit(code)


if __name__ == "__main__":
    main()
