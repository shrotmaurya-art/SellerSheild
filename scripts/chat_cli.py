"""scripts/chat_cli.py
CLI tool to interact with SellerShield Agent chat sessions and stream SSE events.
Refuses to run unless DEV_MODE is true.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

import httpx


def check_dev_mode() -> None:
    dev_mode = False
    try:
        from sellershield_agent.config import get_settings
        dev_mode = bool(get_settings().DEV_MODE)
    except Exception:
        pass
    if not dev_mode:
        dev_mode = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")
    if not dev_mode:
        print("ERROR: scripts/chat_cli.py refuses to run when DEV_MODE is false.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    check_dev_mode()

    parser = argparse.ArgumentParser(description="SellerShield Chat CLI")
    parser.add_argument(
        "--user",
        default="c001",
        choices=["c001", "c002", "seller"],
        help="User identity",
    )
    parser.add_argument("--text", help="Message text to send")
    parser.add_argument("--action", help="Guided action to send (e.g. track_order, policy_info)")
    parser.add_argument("--order-id", help="Optional order ID for action")
    parser.add_argument("--token", help="Bearer JWT token (or set DEV_TOKEN env var)")
    parser.add_argument("--url", default="http://localhost:8001", help="Agent service base URL")

    args = parser.parse_args()

    if not args.text and not args.action:
        print("ERROR: Either --text or --action must be specified.", file=sys.stderr)
        sys.exit(1)

    token = args.token or os.getenv(f"DEV_TOKEN_{args.user.upper()}", "")
    if not token:
        token = os.getenv("DEV_TOKEN", "")

    # If no token passed, try auto-fetching from local dev_auth server
    if not token:
        try:
            auth_url = f"http://localhost:8099/token/{args.user}"
            resp = httpx.get(auth_url, timeout=1.0)
            if resp.status_code == 200:
                token = resp.json().get("token", "")
        except Exception:
            pass

    if not token:
        print(
            f"ERROR: No token provided for user '{args.user}'.\n"
            "Pass --token <token> (using the token printed by scripts/dev_auth.py) "
            f"or set $env:DEV_TOKEN_{args.user.upper()} / $env:DEV_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    client_msg_id = f"cli_{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=args.url, timeout=30.0) as client:
        # 1. Create or resume session
        print(f"[*] Creating session for user '{args.user}'...")
        try:
            sess_resp = client.post("/v1/chat/sessions", headers=headers)
        except Exception as e:
            print(f"ERROR: Failed to connect to agent at {args.url}: {e}", file=sys.stderr)
            sys.exit(1)

        if sess_resp.status_code != 200:
            print(
                f"ERROR: Create session failed ({sess_resp.status_code}): {sess_resp.text}",
                file=sys.stderr,
            )
            sys.exit(1)

        sess_data = sess_resp.json()
        session_id = sess_data["session_id"]
        print(f"[*] Session created: {session_id}")

        # 2. Build message body
        body: dict[str, Any] = {"client_msg_id": client_msg_id}
        if args.text:
            body["text"] = args.text
        if args.action:
            act_obj: dict[str, Any] = {"name": args.action}
            if args.order_id:
                act_obj["order_id"] = args.order_id
            body["action"] = act_obj

        # 3. Stream response
        print(f"[*] Sending message to {session_id}...\n--- Response Stream ---")
        headers["Accept"] = "text/event-stream"

        with client.stream(
            "POST",
            f"/v1/chat/sessions/{session_id}/messages",
            headers=headers,
            json=body,
            timeout=30.0,
        ) as stream_resp:
            if stream_resp.status_code != 200:
                body_bytes = stream_resp.read().decode("utf-8")
                print(f"ERROR: Stream failed ({stream_resp.status_code}): {body_bytes}")
                sys.exit(1)

            current_event: str | None = None
            for line in stream_resp.iter_lines():
                line = line.strip()
                if not line:
                    current_event = None
                    continue

                if line.startswith("event:"):
                    current_event = line.replace("event:", "").strip()
                elif line.startswith("data:"):
                    raw_data = line[5:].strip()
                    try:
                        payload = json.loads(raw_data)
                    except Exception:
                        payload = raw_data

                    if current_event == "tool.status":
                        print(f"\n[TOOL] {payload.get('tool_name')}: {payload.get('status')}")
                    elif current_event == "message.delta":
                        sys.stdout.write(payload.get("delta", ""))
                        sys.stdout.flush()
                    elif current_event == "card":
                        card_p = json.dumps(payload.get("payload", {}), indent=2)
                        print(f"\n[CARD] {payload.get('card_type')}: {card_p}")
                    elif current_event == "final":
                        print(f"\n\n[FINAL] Message ID: {payload.get('message_id')}")
                    elif current_event == "error":
                        print(f"\n[ERROR] {payload.get('code')}: {payload.get('safe_message')}")

    print("\n--- Stream Finished ---")


if __name__ == "__main__":
    main()

