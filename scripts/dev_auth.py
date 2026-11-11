"""scripts/dev_auth.py
In-memory JWKS authentication server for local developer testing.
Refuses to run unless DEV_MODE is true. Never writes secrets to disk.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


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
        print("ERROR: scripts/dev_auth.py refuses to run when DEV_MODE is false.", file=sys.stderr)
        sys.exit(1)


def generate_keys() -> tuple[rsa.RSAPrivateKey, dict[str, str]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk["kid"] = "dev-key-1"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return priv, jwk


def sign_token(
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: rsa.RSAPrivateKey,
    kid: str = "dev-key-1",
) -> str:
    now = datetime.now(UTC)
    import uuid

    issuer = "nestkart-store"
    try:
        from sellershield_agent.config import get_settings
        issuer = get_settings().JWT_ISSUER
    except Exception:
        pass

    payload = {
        "sub": user_id,
        "iss": issuer,
        "aud": "sellershield-agent",
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=24)).timestamp()),
        "auth_time": int(now.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if customer_id:
        payload["customer_id"] = customer_id
    return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid})


def main() -> None:
    check_dev_mode()
    priv, jwk = generate_keys()

    issuer = "nestkart-store"
    try:
        from sellershield_agent.config import get_settings
        issuer = get_settings().JWT_ISSUER
    except Exception:
        pass

    jwks_payload = json.dumps({"keys": [jwk]}).encode("utf-8")

    c001_tok = sign_token("usr_c001", "C001", "customer", priv)
    c002_tok = sign_token("usr_c002", "C002", "customer", priv)
    seller_tok = sign_token("usr_seller", None, "seller_owner", priv)

    tokens_map = {
        "/token/c001": c001_tok,
        "/token/c002": c002_tok,
        "/token/seller": seller_tok,
    }

    class JWKSHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            clean_path = self.path.rstrip("/")
            if clean_path in ("/.well-known/jwks.json", "/.well-known/jwks"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(jwks_payload)))
                self.end_headers()
                self.wfile.write(jwks_payload)
            elif clean_path in tokens_map:
                data = json.dumps({"token": tokens_map[clean_path]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            # Suppress normal access logging to keep terminal clean
            pass

    print("=== SellerShield Dev Auth Server ===")
    print("Serving JWKS at http://localhost:8099/.well-known/jwks.json")
    print(f"Issuer: {issuer}")
    print()
    print("Tokens (valid 24 hours):")
    print(f"C001 Token:   {c001_tok}")
    print()
    print(f"C002 Token:   {c002_tok}")
    print()
    print(f"Seller Token: {seller_tok}")
    print("====================================")
    print("Press Ctrl+C to stop.")

    server = ThreadingHTTPServer(("localhost", 8099), JWKSHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Dev Auth Server...")
        server.server_close()


if __name__ == "__main__":
    main()

