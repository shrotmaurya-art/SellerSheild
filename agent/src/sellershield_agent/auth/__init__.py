"""Auth package providing models, JWKS verification, and FastAPI dependencies."""

from sellershield_agent.auth.deps import (
    get_clock,
    get_jwks_client,
    get_principal,
    require_customer,
    require_fresh_auth,
    require_owner,
    require_seller,
)
from sellershield_agent.auth.jwks import JWKSClient
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.auth.verify import verify_token

__all__ = [
    "JWKSClient",
    "Principal",
    "Role",
    "get_clock",
    "get_jwks_client",
    "get_principal",
    "require_customer",
    "require_fresh_auth",
    "require_owner",
    "require_seller",
    "verify_token",
]
