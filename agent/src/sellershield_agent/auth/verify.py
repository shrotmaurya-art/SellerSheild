"""Token verification engine enforcing cryptographic signatures, claims, and role invariants."""

from __future__ import annotations

from typing import Any

import jwt

from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import Clock, SystemClock
from sellershield_agent.config import get_settings
from sellershield_agent.errors import AppHTTPException
from sellershield_agent.logging import get_logger

logger = get_logger("sellershield_agent.auth.verify")


def verify_token(
    token: str,
    jwks_client: JWKSClient | None = None,
    expected_issuer: str | None = None,
    clock: Clock | None = None,
) -> Principal:
    """Verify store-issued JWT token against JWKS.

    Invariants enforced:
    - Maximum token size 64 KB.
    - Algorithm RS256 only (alg none, HS256, and others rejected).
    - Requires sub, role, iss, exp, iat, jti, and aud containing sellershield-agent.
    - Leeway 30 seconds for clock skew.
    - Validates time claims against Clock (KP-06).
    - Customer role must include non-empty customer_id.
    - Errors always return 401 unauthenticated with generic message.
    - Tokens are never logged.
    """
    if not isinstance(token, str) or not token.strip():
        logger.warning("auth_failed", reason="empty_or_non_string_token")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="empty_or_non_string_token",
        )

    if len(token) > 65536:
        logger.warning("auth_failed", reason="token_too_large", length=len(token))
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="token_too_large",
        )

    # 1. Unverified header validation
    try:
        unverified_header = jwt.get_unverified_header(token)
    except Exception as exc:
        logger.warning("auth_failed", reason="invalid_token_header", error=str(exc))
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="invalid_token_header",
        ) from None

    alg = unverified_header.get("alg")
    if alg != "RS256":
        logger.warning("auth_failed", reason="rejected_algorithm", alg=alg)
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="rejected_algorithm",
        )

    kid = unverified_header.get("kid")
    if not kid or not isinstance(kid, str) or len(kid) > 128:
        logger.warning("auth_failed", reason="missing_or_invalid_kid")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="missing_or_invalid_kid",
        )

    # 2. Key lookup from JWKS
    if jwks_client is None:
        from sellershield_agent.auth.deps import get_default_jwks_client

        jwks_client = get_default_jwks_client()

    if clock is None:
        clock = jwks_client.clock or SystemClock()

    try:
        pyjwk = jwks_client.get_signing_key(kid)
    except Exception as exc:
        logger.warning("auth_failed", reason="kid_lookup_failed", kid=kid, error=str(exc))
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="kid_lookup_failed",
        ) from None

    # 3. Signature, claim, and audience verification
    if expected_issuer is None:
        try:
            expected_issuer = get_settings().JWT_ISSUER
        except Exception:
            expected_issuer = "nestkart-store"

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key=pyjwk.key,
            algorithms=["RS256"],
            audience="sellershield-agent",
            issuer=expected_issuer,
            leeway=30,
            options={
                "require": ["sub", "role", "iss", "exp", "iat", "jti", "aud"],
                "verify_signature": True,
                "verify_exp": False,  # Evaluated below with Clock
                "verify_iat": False,  # Evaluated below with Clock
                "verify_nbf": False,  # Evaluated below with Clock
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.InvalidAudienceError:
        logger.warning("auth_failed", reason="invalid_audience")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="invalid_audience",
        ) from None
    except jwt.InvalidIssuerError:
        logger.warning("auth_failed", reason="invalid_issuer")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="invalid_issuer",
        ) from None
    except Exception as exc:
        logger.warning("auth_failed", reason="jwt_decode_error", error=str(exc))
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="jwt_decode_error",
        ) from None

    # 4. Strict time checks evaluated against domain Clock (KP-06)
    now_ts = clock.now().timestamp()
    leeway = 30.0

    exp_val = payload.get("exp")
    if not isinstance(exp_val, (int, float)):
        logger.warning("auth_failed", reason="invalid_exp_claim")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="invalid_exp_claim",
        )
    if exp_val <= (now_ts - leeway):
        logger.warning("auth_failed", reason="token_expired")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="token_expired",
        )

    iat_val = payload.get("iat")
    if not isinstance(iat_val, (int, float)):
        logger.warning("auth_failed", reason="invalid_iat_claim")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="invalid_iat_claim",
        )
    if iat_val > (now_ts + leeway):
        logger.warning("auth_failed", reason="token_issued_in_future")
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="token_issued_in_future",
        )

    nbf_val = payload.get("nbf")
    if nbf_val is not None:
        if not isinstance(nbf_val, (int, float)) or nbf_val > (now_ts + leeway):
            logger.warning("auth_failed", reason="token_not_yet_valid")
            raise AppHTTPException(
                status_code=401,
                code="unauthenticated",
                message="Authentication required",
                reason="token_not_yet_valid",
            )

    # 5. Role and identity invariants
    raw_role = payload.get("role")
    try:
        role = Role(str(raw_role))
    except ValueError:
        logger.warning("auth_failed", reason="unknown_role", role=raw_role)
        raise AppHTTPException(
            status_code=401,
            code="unauthenticated",
            message="Authentication required",
            reason="unknown_role",
        ) from None

    customer_id = payload.get("customer_id")
    if role == Role.customer:
        if not customer_id or not isinstance(customer_id, str) or not customer_id.strip():
            logger.warning("auth_failed", reason="customer_token_missing_customer_id")
            raise AppHTTPException(
                status_code=401,
                code="unauthenticated",
                message="Authentication required",
                reason="customer_token_missing_customer_id",
            )

    raw_store_id = payload.get("store_id")
    store_id = "nestkart"
    if raw_store_id is not None:
        if not isinstance(raw_store_id, str) or not raw_store_id.strip():
            logger.warning("auth_failed", reason="invalid_store_id")
            raise AppHTTPException(
                status_code=401,
                code="unauthenticated",
                message="Authentication required",
                reason="invalid_store_id",
            )
        store_id = raw_store_id.strip()

    raw_scopes = payload.get("scopes", [])
    scopes = [str(s) for s in raw_scopes] if isinstance(raw_scopes, list) else []

    auth_time_val = payload.get("auth_time")
    auth_time: int | None = None
    if auth_time_val is not None and not isinstance(auth_time_val, bool):
        if isinstance(auth_time_val, (int, float)):
            auth_time = int(auth_time_val)
        elif isinstance(auth_time_val, str):
            try:
                auth_time = int(auth_time_val)
            except ValueError:
                auth_time = None

    return Principal(
        user_id=str(payload["sub"]),
        role=role,
        customer_id=customer_id if role == Role.customer else customer_id or None,
        store_id=store_id,
        scopes=scopes,
        auth_time=auth_time,
        jti=str(payload["jti"]),
        token_exp=int(payload["exp"]),
    )
