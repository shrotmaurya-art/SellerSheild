"""
Authentication router: login, refresh, logout, me, JWKS, service token.

Design:
- Access JWT: RS256, 15 min, claims sub/role/customer_id/aud/iss/exp/jti/auth_time
- Refresh token: opaque, 7 days, rotating, httpOnly SameSite=Lax cookie; only hash stored
- Service JWT: RS256, 10 min, client_id/scopes/aud
- Rate limiting: 5 per minute per IP+email, 15-min lock after 10 failures
- CSRF: double-submit header on cookie endpoints
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from fastapi import APIRouter, Cookie, Request, Response
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import select
from sqlalchemy.orm import Session

import db as database
from app.clock import Clock
from app.config import settings
from app.errors import AppException
from app.models import User

# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

_private_key = None
_public_key = None
_public_key_pem = None


def _get_private_key():
    """Load the RSA private key lazily (so tests can generate keys first)."""
    global _private_key
    if _private_key is None:
        from pathlib import Path

        key_path = Path(settings.JWT_PRIVATE_KEY_PATH)
        if not key_path.is_absolute():
            # Relative to store-api/
            key_path = Path(__file__).resolve().parent.parent / key_path
        pem_data = key_path.read_bytes()
        _private_key = load_pem_private_key(pem_data, password=None)
    return _private_key


def _get_public_key():
    """Load the RSA public key lazily."""
    global _public_key, _public_key_pem
    if _public_key is None:
        from pathlib import Path

        key_path = Path(settings.JWT_PUBLIC_KEY_PATH)
        if not key_path.is_absolute():
            key_path = Path(__file__).resolve().parent.parent / key_path
        _public_key_pem = key_path.read_bytes()
        _public_key = load_pem_public_key(_public_key_pem)
    return _public_key


def reset_keys():
    """Reset cached keys — used by tests that regenerate keys."""
    global _private_key, _public_key, _public_key_pem
    _private_key = None
    _public_key = None
    _public_key_pem = None


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

_ph = PasswordHasher()

# ---------------------------------------------------------------------------
# Refresh token store (in-memory; fine for demo; persists until restart)
# ---------------------------------------------------------------------------

# token_hash -> {user_id, expires_at, revoked}
_refresh_store: dict[str, dict[str, Any]] = {}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _store_refresh_token(token: str, user_id: str, expires_at: datetime) -> None:
    h = _hash_token(token)
    _refresh_store[h] = {"user_id": user_id, "expires_at": expires_at, "revoked": False}


def _validate_refresh_token(token: str) -> dict[str, Any] | None:
    h = _hash_token(token)
    entry = _refresh_store.get(h)
    if entry is None:
        return None
    if entry["revoked"]:
        return None
    now = Clock.now().replace(tzinfo=None)
    if entry["expires_at"] < now:
        return None
    return entry


def _revoke_refresh_token(token: str) -> None:
    h = _hash_token(token)
    entry = _refresh_store.get(h)
    if entry:
        entry["revoked"] = True


def _revoke_all_for_user(user_id: str) -> None:
    for entry in _refresh_store.values():
        if entry["user_id"] == user_id:
            entry["revoked"] = True


# ---------------------------------------------------------------------------
# Rate-limiting / lockout (in-memory)
# ---------------------------------------------------------------------------

# key -> list of attempt timestamps (UTC, naive)
_rate_limits: dict[str, list[datetime]] = {}
# email -> (failure_count, locked_until)
_lockouts: dict[str, tuple[int, datetime | None]] = {}


def _check_rate_limit(ip: str, email: str) -> bool:
    """Return False if rate-limited (429). Prunes old entries."""
    now = Clock.now().replace(tzinfo=None)
    window = timedelta(minutes=1)

    # Check lockout first
    lock_info = _lockouts.get(email)
    if lock_info:
        count, locked_until = lock_info
        if locked_until and now < locked_until:
            return False

    # Check per-IP+email rate (5 per minute)
    key = f"{ip}:{email}"
    attempts = _rate_limits.get(key, [])
    # Prune old
    attempts = [t for t in attempts if now - t < window]
    _rate_limits[key] = attempts

    if len(attempts) >= 5:
        return False

    return True


def _record_attempt(ip: str, email: str) -> None:
    now = Clock.now().replace(tzinfo=None)
    key = f"{ip}:{email}"
    attempts = _rate_limits.get(key, [])
    attempts.append(now)
    _rate_limits[key] = attempts


def _record_failure(email: str) -> None:
    now = Clock.now().replace(tzinfo=None)
    lock_info = _lockouts.get(email)
    if lock_info:
        count, locked_until = lock_info
        # If lock expired, reset
        if locked_until and now >= locked_until:
            count = 0
        count += 1
    else:
        count = 1

    locked_until = None
    if count >= 10:
        locked_until = now + timedelta(minutes=15)

    _lockouts[email] = (count, locked_until)


def _reset_failure_count(email: str) -> None:
    _lockouts.pop(email, None)


# ---------------------------------------------------------------------------
# CSRF validation
# ---------------------------------------------------------------------------

_CSRF_COOKIE_NAME = "nestkart_csrf"
_CSRF_HEADER_NAME = "x-csrf-token"


def _validate_csrf(request: Request) -> None:
    """Double-submit CSRF check for cookie-based endpoints."""
    cookie_val = request.cookies.get(_CSRF_COOKIE_NAME)
    header_val = request.headers.get(_CSRF_HEADER_NAME)
    if not cookie_val or not header_val or cookie_val != header_val:
        raise AppException(
            code="forbidden",
            message="CSRF validation failed",
            status_code=403,
        )


# ---------------------------------------------------------------------------
# JWT minting
# ---------------------------------------------------------------------------

ACCESS_TOKEN_LIFETIME = timedelta(minutes=15)
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
SERVICE_TOKEN_LIFETIME = timedelta(minutes=10)

# Service client scope definitions
SERVICE_CLIENT_SCOPES = {
    "agent-executor": (
        "orders:read_all refunds:reserve refunds:release returns:transition mail:send"
    ),
    "agent-monitor": "orders:read_all mail:send",
}


def _mint_access_token(user: User, customer_id: str | None) -> str:
    """Mint an RS256 access JWT."""
    now = Clock.now()
    claims = {
        "sub": user.user_id,
        "role": user.role,
        "customer_id": customer_id,
        "aud": ["nestkart-api", "sellershield-agent"],
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": now + ACCESS_TOKEN_LIFETIME,
        "jti": str(uuid.uuid4()),
        "auth_time": int(now.timestamp()),
    }
    private_key = _get_private_key()
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "nestkart-1"})
    return token


def _mint_service_token(client_id: str, scopes: str) -> str:
    """Mint an RS256 service JWT."""
    now = Clock.now()
    claims = {
        "sub": client_id,
        "client_id": client_id,
        "scopes": scopes,
        "aud": "nestkart-api",
        "iss": settings.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": now + SERVICE_TOKEN_LIFETIME,
        "jti": str(uuid.uuid4()),
    }
    private_key = _get_private_key()
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "nestkart-1"})
    return token


def _generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # 15 min in seconds


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class MeResponse(BaseModel):
    user_id: str
    email: str
    role: str
    customer_id: str | None = None
    display_name: str


class TokenRequest(BaseModel):
    grant_type: str = "client_credentials"
    client_id: str
    client_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 600
    scope: str


class JWKSResponse(BaseModel):
    keys: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["Auth"])

# Rate limiter instance (we apply it manually, not as middleware)
limiter = Limiter(key_func=get_remote_address)


@router.post("/api/v1/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"

    # Rate limit check
    if not _check_rate_limit(ip, email):
        raise AppException(
            code="rate_limited",
            message="Too many login attempts. Please try again later.",
            status_code=429,
        )

    # Record the attempt
    _record_attempt(ip, email)

    # Generic error — same text for wrong password and unknown email
    generic_err = AppException(
        code="unauthorized",
        message="Invalid email or password",
        status_code=401,
    )

    # Look up user
    with Session(database.engine) as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            _record_failure(email)
            raise generic_err

        if user.status != "active":
            _record_failure(email)
            raise generic_err

        # Verify password
        try:
            _ph.verify(user.password_hash, body.password)
        except (VerifyMismatchError, VerificationError):
            _record_failure(email)
            raise generic_err

        # Re-hash if argon2 params changed
        if _ph.check_needs_rehash(user.password_hash):
            user.password_hash = _ph.hash(body.password)
            session.commit()

        # Resolve customer_id
        customer_id = None
        if user.role == "customer" and user.customer:
            customer_id = user.customer.customer_id

        # Reset failure count on success
        _reset_failure_count(email)

        # Mint tokens
        access_token = _mint_access_token(user, customer_id)
        refresh_token = _generate_refresh_token()
        csrf_token = secrets.token_urlsafe(32)

        # Store refresh token hash
        now = Clock.now().replace(tzinfo=None)
        _store_refresh_token(refresh_token, user.user_id, now + REFRESH_TOKEN_LIFETIME)

    # Set refresh token as httpOnly cookie
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        samesite="lax",
        secure=False,  # False for local dev; True in production
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/api/v1/auth",
    )

    # Set CSRF cookie (readable by JS for double-submit)
    response.set_cookie(
        key=_CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        samesite="lax",
        secure=False,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/",
    )

    return LoginResponse(access_token=access_token)


@router.post("/api/v1/auth/refresh", response_model=RefreshResponse)
def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
) -> RefreshResponse:
    _validate_csrf(request)

    if not refresh_token:
        raise AppException(
            code="unauthorized",
            message="Missing refresh token",
            status_code=401,
        )

    entry = _validate_refresh_token(refresh_token)
    if entry is None:
        raise AppException(
            code="unauthorized",
            message="Invalid or expired refresh token",
            status_code=401,
        )

    user_id = entry["user_id"]

    # Revoke old token (rotation)
    _revoke_refresh_token(refresh_token)

    # Look up user
    with Session(database.engine) as session:
        user = session.scalar(select(User).where(User.user_id == user_id))
        if user is None or user.status != "active":
            raise AppException(
                code="unauthorized",
                message="Invalid or expired refresh token",
                status_code=401,
            )

        customer_id = None
        if user.role == "customer" and user.customer:
            customer_id = user.customer.customer_id

        access_token = _mint_access_token(user, customer_id)
        new_refresh = _generate_refresh_token()
        csrf_token = secrets.token_urlsafe(32)

        now = Clock.now().replace(tzinfo=None)
        _store_refresh_token(new_refresh, user_id, now + REFRESH_TOKEN_LIFETIME)

    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/api/v1/auth",
    )
    response.set_cookie(
        key=_CSRF_COOKIE_NAME,
        value=csrf_token,
        httponly=False,
        samesite="lax",
        secure=False,
        max_age=int(REFRESH_TOKEN_LIFETIME.total_seconds()),
        path="/",
    )

    return RefreshResponse(access_token=access_token)


@router.post("/api/v1/auth/logout")
def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None),
) -> dict[str, str]:
    _validate_csrf(request)

    if refresh_token:
        entry = _validate_refresh_token(refresh_token)
        if entry:
            # Revoke all tokens for this user
            _revoke_all_for_user(entry["user_id"])

    response.delete_cookie("refresh_token", path="/api/v1/auth")
    response.delete_cookie(_CSRF_COOKIE_NAME, path="/")

    return {"message": "Logged out"}


@router.get("/api/v1/auth/me", response_model=MeResponse)
def me(request: Request) -> MeResponse:
    principal = get_current_principal(request)
    if principal is None or principal.get("type") != "user":
        raise AppException(
            code="unauthorized",
            message="Authentication required",
            status_code=401,
        )
    return MeResponse(
        user_id=principal["sub"],
        email=principal["email"],
        role=principal["role"],
        customer_id=principal.get("customer_id"),
        display_name=principal["display_name"],
    )


@router.get("/api/v1/.well-known/jwks.json", response_model=JWKSResponse)
@router.get("/.well-known/jwks.json", response_model=JWKSResponse, include_in_schema=False)
def jwks() -> JWKSResponse:
    """Public JWKS endpoint — exposes only the public key (no d, p, q, etc.)."""
    import base64

    pub_key = _get_public_key()
    pub_numbers = pub_key.public_numbers()

    def _int_to_b64url(n: int) -> str:
        byte_length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "nestkart-1",
        "n": _int_to_b64url(pub_numbers.n),
        "e": _int_to_b64url(pub_numbers.e),
    }
    return JWKSResponse(keys=[jwk])


@router.post("/api/v1/auth/token", response_model=TokenResponse)
def service_token(body: TokenRequest) -> TokenResponse:
    """OAuth client-credentials grant for service clients."""
    if body.grant_type != "client_credentials":
        raise AppException(
            code="validation_error",
            message="Unsupported grant_type",
            status_code=400,
        )

    # Validate client credentials
    expected_secret = settings.SERVICE_CLIENT_SECRETS.get(body.client_id)
    if expected_secret is None or body.client_secret != expected_secret:
        raise AppException(
            code="unauthorized",
            message="Invalid client credentials",
            status_code=401,
        )

    scopes = SERVICE_CLIENT_SCOPES.get(body.client_id, "")
    access_token = _mint_service_token(body.client_id, scopes)

    return TokenResponse(
        access_token=access_token,
        scope=scopes,
    )


# ---------------------------------------------------------------------------
# Token verification / principal extraction (used by deps.py)
# ---------------------------------------------------------------------------


def get_current_principal(request: Request) -> dict[str, Any] | None:
    """
    Extract and verify the principal from the Authorization header.
    Returns a dict with type, sub, role, customer_id, scopes, etc., or None.
    """
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1]

    try:
        # Decode with RS256 ONLY — reject alg=none and HS256
        payload = jwt.decode(
            token,
            _get_public_key(),
            algorithms=["RS256"],
            audience=["nestkart-api", "sellershield-agent", "nestkart-api"],
            issuer=settings.JWT_ISSUER,
            options={
                "require": ["exp", "sub", "iss", "aud"],
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

    # Determine principal type
    if "client_id" in payload:
        # Service token
        raw_scopes = payload.get("scopes", "")
        scopes_list = raw_scopes if isinstance(raw_scopes, list) else raw_scopes.split()
        return {
            "type": "service",
            "sub": payload["sub"],
            "client_id": payload["client_id"],
            "scopes": scopes_list,
        }
    else:
        # User token — look up display_name and email
        with Session(database.engine) as session:
            user = session.scalar(select(User).where(User.user_id == payload["sub"]))
            if user is None or user.status != "active":
                return None
            return {
                "type": "user",
                "sub": payload["sub"],
                "role": payload.get("role", ""),
                "customer_id": payload.get("customer_id"),
                "email": user.email,
                "display_name": user.display_name,
            }
