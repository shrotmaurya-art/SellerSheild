from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sellershield_agent.config import ConfigError, get_settings

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str
    timestamp: str


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Always returns 200 when the process is alive.",
)
async def healthz() -> HealthResponse:
    now_utc = datetime.now(UTC).isoformat()
    return HealthResponse(status="ok", timestamp=now_utc)


@router.get(
    "/readyz",
    response_model=HealthResponse,
    summary="Readiness probe",
    description="Returns 200 when ready, or 200 degraded when store JWKS is down.",
)
async def readyz() -> HealthResponse:
    try:
        get_settings()
    except ConfigError as err:
        raise HTTPException(
            status_code=503,
            detail=f"Service not ready: {err}",
        ) from err
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Service not ready: configuration check failed",
        ) from exc

    status = "ok"
    try:
        from sellershield_agent.auth import deps

        jwks_client = deps.get_jwks_client()
        if not jwks_client.is_healthy():
            status = "degraded"
    except Exception:
        status = "degraded"

    now_utc = datetime.now(UTC).isoformat()
    return HealthResponse(status=status, timestamp=now_utc)
