#!/usr/bin/env python3
"""
Webhook Listener (spec section 6, 7.4, 8, 16)

A tiny receiver on port 9000 that:
- Verifies X-Store-Signature (HMAC-SHA256 of 'timestamp.body' using WEBHOOK_SECRET)
- Verifies the 5-minute timestamp window
- Rejects duplicate event ids
- Prints 'signature OK: <event_name>' or 'REJECTED: <reason>'
"""

from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import time

# Try loading WEBHOOK_SECRET from .env if present
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
WEBHOOK_SECRET = "fake_dev_webhook_secret_do_not_use_in_prod"

if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("WEBHOOK_SECRET="):
                WEBHOOK_SECRET = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

SEEN_EVENT_IDS: set[str] = set()


def verify_webhook_envelope(
    headers: dict[str, str],
    body_str: str,
    secret: str = WEBHOOK_SECRET,
    seen_ids: set[str] = SEEN_EVENT_IDS,
    current_time: float | None = None,
) -> tuple[bool, int, str]:
    """
    Verify webhook signature, timestamp window (5 min), and event-id deduplication.
    Returns (is_valid, status_code, message).
    """
    # Case-insensitive header lookup
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    sig_header = normalized_headers.get("x-store-signature")
    ts_header = normalized_headers.get("x-store-timestamp")
    event_id = normalized_headers.get("x-event-id")

    if not sig_header or not ts_header or not event_id:
        return False, 400, "REJECTED: missing required webhook headers"

    # 1. Signature verification
    message = f"{ts_header}.{body_str}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig_header, expected_sig):
        return False, 401, f"REJECTED: invalid signature ({event_id})"

    # 2. Timestamp window (5 minutes / 300 seconds)
    try:
        ts_val = float(ts_header)
    except ValueError:
        try:
            ts_val = datetime.fromisoformat(ts_header).timestamp()
        except Exception:
            return False, 400, f"REJECTED: invalid timestamp format ({event_id})"

    now_ts = current_time if current_time is not None else time.time()
    if abs(now_ts - ts_val) > 300:
        return False, 400, f"REJECTED: timestamp outside 5-minute window ({event_id})"

    # 3. Duplicate event ID rejection
    if event_id in seen_ids:
        return False, 409, f"REJECTED: duplicate event id ({event_id})"

    seen_ids.add(event_id)
    try:
        payload = json.loads(body_str)
        event_type = payload.get("type", "unknown")
    except Exception:
        event_type = "unknown"

    return True, 200, f"signature OK: {event_type} ({event_id})"


class WebhookRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        body_str = raw_body.decode("utf-8")

        headers_dict = dict(self.headers.items())
        is_valid, status_code, message = verify_webhook_envelope(headers_dict, body_str)

        print(message, flush=True)

        resp_body = json.dumps({"status": "ok" if is_valid else "error", "message": message}).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def log_message(self, format: str, *args) -> None:
        # Suppress default HTTP server access logs so output matches spec expectation
        pass


def run_listener(port: int = 9000, host: str = "0.0.0.0") -> None:
    server_address = (host, port)
    httpd = HTTPServer(server_address, WebhookRequestHandler)
    print(f"Webhook listener running on http://localhost:{port}/v1/webhooks/store", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nWebhook listener stopped.", flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    run_listener(port=port)

