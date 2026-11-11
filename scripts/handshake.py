"""Dev tool: Real store handshake verification script (T1.9).

Connects to the running NestKart store, logs in as demo customer C001,
queries list_orders and get_order for ORD-1048, prints the order status only,
and exits 0 on success.

Usage:
    uv run python scripts/handshake.py

Invariants:
- Access tokens are handled as SecretStr and never printed or logged (KP-13).
- Exits 0 on success after printing the status value only.
- Exits non-zero with error message on stderr on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from pydantic import SecretStr

from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.http_client import HttpStoreClient


async def handshake() -> int:
    store_base_url = os.environ.get("STORE_BASE_URL", "http://localhost:8000").rstrip("/")
    api_url = (
        store_base_url
        if store_base_url.endswith("/api/v1")
        else f"{store_base_url}/api/v1"
    )

    email = (
        os.environ.get("DEMO_CUSTOMER_EMAIL")
        or os.environ.get("STORE_DEMO_EMAIL")
        or "aarav.mehta@example.test"
    )
    password = (
        os.environ.get("DEMO_CUSTOMER_PASSWORD")
        or os.environ.get("STORE_DEMO_PASSWORD")
        or "Demo@12345"
    )

    login_url = f"{api_url}/auth/login"
    login_payload = {"email": email, "password": password}

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        try:
            resp = await client.post(login_url, json=login_payload)
        except Exception as exc:
            sys.stderr.write(f"Store connection failed: {exc}\n")
            return 1

        if resp.status_code != 200:
            sys.stderr.write(f"Login failed: HTTP {resp.status_code}\n")
            return 1

        try:
            data = resp.json()
            access_token = str(data["access_token"])
        except Exception as exc:
            sys.stderr.write(f"Failed to parse login response: {exc}\n")
            return 1

    store_client = HttpStoreClient(base_url=store_base_url)
    ctx = RequestContext(
        principal=Principal(
            user_id="U001",
            customer_id="C001",
            role=Role.customer,
            jti="handshake-run-jti",
            token_exp=1799999999,
        ),
        request_id="handshake-req-001",
        token=SecretStr(access_token),
    )

    try:
        # Verify customer order listing
        await store_client.list_orders(ctx)

        # Fetch ORD-1048 and print status only
        order = await store_client.get_order(ctx, "ORD-1048")
        print(order.status.value)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Store handshake operation failed: {exc}\n")
        return 1


def main() -> None:
    code = asyncio.run(handshake())
    sys.exit(code)


if __name__ == "__main__":
    main()

