"""Stub router for seller endpoints defined in contracts/openapi-agent.yaml.

Every seller endpoint is protected by require_seller (or require_owner for policy activation)
and returns 501 in the standard error envelope. Denials write an auth_denied audit event.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from sellershield_agent.auth.deps import require_owner, require_seller
from sellershield_agent.auth.models import Principal
from sellershield_agent.errors import AppHTTPException

router = APIRouter(prefix="/seller", tags=["seller"])


def _stub() -> Any:
    raise AppHTTPException(
        status_code=501,
        code="not_implemented",
        message="Endpoint not implemented yet",
    )


# ── Operations & Activity ─────────────────────────────────────────────


@router.get("/activity")
def get_activity_stream(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.get("/analytics")
def get_analytics(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


# ── Policy Studio ─────────────────────────────────────────────────────


@router.get("/policies")
def list_policies(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.post("/policies")
def create_draft_policy(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.get("/policies/active")
def get_active_policy(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.get("/policies/{version}")
def get_policy_version(
    version: str,
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.put("/policies/{version}")
def update_draft_policy(
    version: str,
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()


@router.post("/policies/{version}/activate")
def activate_policy(
    version: str,
    principal: Annotated[Principal, Depends(require_owner)],
) -> Any:
    return _stub()


@router.post("/policies/simulate")
def simulate_policy(
    principal: Annotated[Principal, Depends(require_seller)],
) -> Any:
    return _stub()
