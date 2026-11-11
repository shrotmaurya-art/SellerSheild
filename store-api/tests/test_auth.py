"""
Stage 4 authentication tests.

Covers:
- Login: success, wrong password, unknown email, same generic error
- Rate limiting: 5 per minute per IP+email → 429
- Lockout: 10 failures → 15-min lock
- Access JWT: valid token → /auth/me, expired → 401, wrong aud → 401,
  bad signature → 401, alg none → 401
- Refresh token rotation: old refresh token fails after rotation
- CSRF: cookie endpoints require double-submit header
- /auth/me with no token → 401
- JWKS: public key only (kty=RSA, no d)
- Service token: agent-executor and agent-monitor with correct scopes
- Role enforcement: customer token rejected on seller-only test route
- Scope enforcement: monitor token cannot use executor scopes
- Security headers: CSP, nosniff, frame-ancestors
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends
from fastapi.testclient import TestClient

from app.auth import (
    _get_private_key,
    _lockouts,
    _rate_limits,
    _refresh_store,
)
from app.config import settings
from app.deps import require_role, require_scope
from app.main import app

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

client = TestClient(app)

LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/auth/me"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
JWKS_URL = "/api/v1/.well-known/jwks.json"
TOKEN_URL = "/api/v1/auth/token"

CUSTOMER_EMAIL = "aarav.mehta@example.test"
CUSTOMER_PASSWORD = "Demo@12345"
SELLER_EMAIL = "priya.sharma@nestkart.test"


def _clear_rate_state():
    """Clear rate-limit and lockout state between tests."""
    _rate_limits.clear()
    _lockouts.clear()
    _refresh_store.clear()


def _login(email: str = CUSTOMER_EMAIL, password: str = CUSTOMER_PASSWORD) -> dict:
    """Helper: log in and return the response object."""
    _clear_rate_state()
    resp = client.post(LOGIN_URL, json={"email": email, "password": password})
    return resp


# ---------------------------------------------------------------------------
# Add test-only routes for role and scope checking
# ---------------------------------------------------------------------------


@app.get("/test/seller-only")
def _seller_only_route(principal=Depends(require_role("seller_owner", "seller_staff"))):
    return {"ok": True, "role": principal["role"]}


@app.get("/test/executor-scope")
def _executor_scope_route(principal=Depends(require_scope("refunds:reserve"))):
    return {"ok": True, "client_id": principal["client_id"]}


# ---------------------------------------------------------------------------
# Login tests
# ---------------------------------------------------------------------------


class TestLogin:
    def setup_method(self):
        _clear_rate_state()

    def test_login_success(self):
        resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": CUSTOMER_PASSWORD})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 900

        # Must have Set-Cookie with HttpOnly and SameSite=Lax
        set_cookie = resp.headers.get("set-cookie", "")
        assert "httponly" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()

    def test_login_wrong_password(self):
        resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "WrongPass"})
        assert resp.status_code == 401
        error = resp.json()["error"]
        assert error["code"] == "unauthorized"
        assert error["message"] == "Invalid email or password"

    def test_login_unknown_email(self):
        resp = client.post(LOGIN_URL, json={"email": "nobody@example.test", "password": "anything"})
        assert resp.status_code == 401
        error = resp.json()["error"]
        # Same generic error text as wrong password
        assert error["message"] == "Invalid email or password"

    def test_login_generic_error_same_text(self):
        """Wrong password and unknown email produce identical error messages."""
        resp1 = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "wrong"})
        _clear_rate_state()
        resp2 = client.post(LOGIN_URL, json={"email": "ghost@nowhere.test", "password": "wrong"})
        assert resp1.json()["error"]["message"] == resp2.json()["error"]["message"]

    def test_login_seller(self):
        resp = client.post(LOGIN_URL, json={"email": SELLER_EMAIL, "password": CUSTOMER_PASSWORD})
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        # Use token to get /me
        me_resp = client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
        assert me_resp.status_code == 200
        assert me_resp.json()["role"] == "seller_owner"
        assert me_resp.json()["customer_id"] is None


# ---------------------------------------------------------------------------
# Rate limiting and lockout
# ---------------------------------------------------------------------------


class TestRateLimit:
    def setup_method(self):
        _clear_rate_state()

    def test_throttle_after_5_attempts(self):
        """5 attempts per minute per IP+email; 6th is 429."""
        for i in range(5):
            resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "wrong"})
            assert resp.status_code == 401, f"Attempt {i+1} should be 401"

        # 6th should be rate-limited
        resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "wrong"})
        assert resp.status_code == 429

    def test_lockout_after_10_failures(self):
        """15-minute lockout after 10 failures."""
        for i in range(10):
            # Clear rate limits but keep lockout counts
            key = f"testclient:{CUSTOMER_EMAIL}"
            _rate_limits.pop(key, None)
            resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "wrong"})
            # First 5 per window are 401, then 429, but lockout accumulates
            # Just ensure we get either 401 or 429
            assert resp.status_code in (401, 429)

        # After 10 failures, any attempt should be blocked (even clearing rate limits)
        _rate_limits.clear()
        resp = client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": CUSTOMER_PASSWORD})
        assert resp.status_code == 429

    def test_different_emails_independent(self):
        """Rate limits are per email, not global."""
        # Exhaust rate limit for one email
        for _ in range(5):
            client.post(LOGIN_URL, json={"email": CUSTOMER_EMAIL, "password": "wrong"})

        # Different email should still work
        resp = client.post(LOGIN_URL, json={"email": SELLER_EMAIL, "password": CUSTOMER_PASSWORD})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Access token / /auth/me tests
# ---------------------------------------------------------------------------


class TestAccessToken:
    def setup_method(self):
        _clear_rate_state()

    def test_me_with_valid_token(self):
        resp = _login()
        token = resp.json()["access_token"]
        me = client.get(ME_URL, headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        data = me.json()
        assert data["role"] == "customer"
        assert data["customer_id"] == "C001"
        assert data["user_id"] == "C001"
        assert data["display_name"] == "Aarav Mehta"

    def test_me_no_token(self):
        resp = client.get(ME_URL)
        assert resp.status_code == 401
        error = resp.json()["error"]
        assert error["code"] == "unauthorized"
        assert "request_id" in error

    def test_me_tampered_token(self):
        """Changing one character of the token → 401."""
        resp = _login()
        token = resp.json()["access_token"]
        # Flip a character in the payload
        parts = token.split(".")
        payload_part = parts[1]
        if payload_part[-1] == "A":
            tampered = payload_part[:-1] + "B"
        else:
            tampered = payload_part[:-1] + "A"
        bad_token = f"{parts[0]}.{tampered}.{parts[2]}"
        me = client.get(ME_URL, headers={"Authorization": f"Bearer {bad_token}"})
        assert me.status_code == 401

    def test_expired_token(self):
        """An expired token → 401."""
        resp = _login()
        token = resp.json()["access_token"]

        # Decode without verification to get claims, then re-sign with expired time
        private_key = _get_private_key()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        claims["exp"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        expired_token = pyjwt.encode(claims, private_key, algorithm="RS256")

        me = client.get(ME_URL, headers={"Authorization": f"Bearer {expired_token}"})
        assert me.status_code == 401

    def test_wrong_audience(self):
        """A token with wrong audience → 401."""
        private_key = _get_private_key()
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "C001",
            "role": "customer",
            "customer_id": "C001",
            "aud": "wrong-audience",
            "iss": settings.JWT_ISSUER,
            "exp": now + timedelta(minutes=15),
            "jti": str(uuid.uuid4()),
            "auth_time": int(now.timestamp()),
        }
        bad_token = pyjwt.encode(claims, private_key, algorithm="RS256")
        me = client.get(ME_URL, headers={"Authorization": f"Bearer {bad_token}"})
        assert me.status_code == 401

    def test_bad_signature(self):
        """A token signed with a different key → 401."""
        # Generate a different RSA key
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "C001",
            "role": "customer",
            "customer_id": "C001",
            "aud": ["nestkart-api", "sellershield-agent"],
            "iss": settings.JWT_ISSUER,
            "exp": now + timedelta(minutes=15),
            "jti": str(uuid.uuid4()),
            "auth_time": int(now.timestamp()),
        }
        bad_token = pyjwt.encode(claims, other_key, algorithm="RS256")
        me = client.get(ME_URL, headers={"Authorization": f"Bearer {bad_token}"})
        assert me.status_code == 401

    def test_alg_none_rejected(self):
        """A token with alg=none MUST be rejected."""
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "C001",
            "role": "customer",
            "customer_id": "C001",
            "aud": ["nestkart-api", "sellershield-agent"],
            "iss": settings.JWT_ISSUER,
            "exp": now + timedelta(minutes=15),
            "jti": str(uuid.uuid4()),
            "auth_time": int(now.timestamp()),
        }
        # Manually create an unsigned token (alg=none)
        import base64
        import json

        header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
            .rstrip(b"=")
            .decode()
        )
        payload = (
            base64.urlsafe_b64encode(json.dumps(claims, default=str).encode()).rstrip(b"=").decode()
        )
        none_token = f"{header}.{payload}."

        me = client.get(ME_URL, headers={"Authorization": f"Bearer {none_token}"})
        assert me.status_code == 401


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------


class TestRefreshRotation:
    def setup_method(self):
        _clear_rate_state()

    def test_refresh_rotation_old_token_fails(self):
        """After refresh, the old refresh token must be revoked."""
        # Login to get initial refresh token
        login_resp = _login()
        assert login_resp.status_code == 200

        # Extract cookies
        cookies = login_resp.cookies
        refresh_token = cookies.get("refresh_token")
        csrf_token = cookies.get("nestkart_csrf")
        assert refresh_token is not None
        assert csrf_token is not None

        # Use refresh endpoint with CSRF header
        refresh_resp = client.post(
            REFRESH_URL,
            cookies={"refresh_token": refresh_token, "nestkart_csrf": csrf_token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert refresh_resp.status_code == 200
        assert "access_token" in refresh_resp.json()

        # Get the new cookies
        new_csrf = refresh_resp.cookies.get("nestkart_csrf")

        # Try using the OLD refresh token again — should fail
        old_refresh_resp = client.post(
            REFRESH_URL,
            cookies={"refresh_token": refresh_token, "nestkart_csrf": new_csrf or csrf_token},
            headers={"X-CSRF-Token": new_csrf or csrf_token},
        )
        assert old_refresh_resp.status_code == 401

    def test_refresh_without_csrf_fails(self):
        """Refresh without CSRF header must fail with 403."""
        login_resp = _login()
        cookies = login_resp.cookies
        refresh_token = cookies.get("refresh_token")

        resp = client.post(
            REFRESH_URL,
            cookies={"refresh_token": refresh_token},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# JWKS
# ---------------------------------------------------------------------------


class TestJWKS:
    def test_jwks_returns_public_key_only(self):
        resp = client.get(JWKS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert "keys" in data
        assert len(data["keys"]) == 1
        key = data["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert "n" in key
        assert "e" in key
        # Must NOT have private key fields
        assert "d" not in key
        assert "p" not in key
        assert "q" not in key
        assert "dp" not in key
        assert "dq" not in key
        assert "qi" not in key


# ---------------------------------------------------------------------------
# Service tokens
# ---------------------------------------------------------------------------


class TestServiceToken:
    def setup_method(self):
        _clear_rate_state()

    def test_executor_token(self):
        resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "agent-executor",
                "client_secret": "fake_executor_secret_key_12345",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refunds:reserve" in data["scope"]
        assert "refunds:release" in data["scope"]
        assert data["expires_in"] == 600

    def test_monitor_token(self):
        resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "agent-monitor",
                "client_secret": "fake_monitor_secret_key_67890",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "orders:read_all" in data["scope"]
        # Monitor should NOT have refund scopes
        assert "refunds:reserve" not in data["scope"]
        assert "refunds:release" not in data["scope"]

    def test_invalid_client_secret(self):
        resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "agent-executor",
                "client_secret": "wrong_secret",
            },
        )
        assert resp.status_code == 401

    def test_unknown_client(self):
        resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "unknown-client",
                "client_secret": "whatever",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def setup_method(self):
        _clear_rate_state()

    def test_customer_rejected_on_seller_only_route(self):
        """A customer token must be rejected on seller-only routes."""
        resp = _login(CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        token = resp.json()["access_token"]
        seller_resp = client.get("/test/seller-only", headers={"Authorization": f"Bearer {token}"})
        assert seller_resp.status_code == 403

    def test_seller_accepted_on_seller_only_route(self):
        resp = _login(SELLER_EMAIL, CUSTOMER_PASSWORD)
        token = resp.json()["access_token"]
        seller_resp = client.get("/test/seller-only", headers={"Authorization": f"Bearer {token}"})
        assert seller_resp.status_code == 200
        assert seller_resp.json()["role"] == "seller_owner"


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def setup_method(self):
        _clear_rate_state()

    def test_executor_has_refund_scope(self):
        token_resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "agent-executor",
                "client_secret": "fake_executor_secret_key_12345",
            },
        )
        token = token_resp.json()["access_token"]
        resp = client.get("/test/executor-scope", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["client_id"] == "agent-executor"

    def test_monitor_cannot_use_executor_scopes(self):
        """Monitor token must NOT have refunds:reserve scope."""
        token_resp = client.post(
            TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": "agent-monitor",
                "client_secret": "fake_monitor_secret_key_67890",
            },
        )
        token = token_resp.json()["access_token"]
        resp = client.get("/test/executor-scope", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
        assert "refunds:reserve" in resp.json()["error"]["message"]

    def test_user_token_rejected_on_scope_route(self):
        """A user token (not a service token) must be rejected on scope-protected routes."""
        resp = _login(CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        token = resp.json()["access_token"]
        scope_resp = client.get(
            "/test/executor-scope", headers={"Authorization": f"Bearer {token}"}
        )
        assert scope_resp.status_code == 403


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_nosniff_on_healthz(self):
        resp = client.get("/healthz")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_csp_on_healthz(self):
        resp = client.get("/healthz")
        csp = resp.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp
        assert "default-src 'self'" in csp

    def test_x_frame_options(self):
        resp = client.get("/healthz")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_security_headers_on_api(self):
        resp = client.get("/api/v1/clock")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert "frame-ancestors 'none'" in resp.headers.get("content-security-policy", "")
