from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sellershield_agent.auth import deps
from sellershield_agent.config import ConfigError, get_settings

router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    store_mode: str | None = None
    dev_faults: dict[str, bool] | None = None


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
async def readyz(
    jwks_client: Annotated[Any, Depends(deps.get_jwks_client)] = None,
) -> HealthResponse:
    try:
        settings = get_settings()
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
        resolved_jwks = jwks_client or deps.get_jwks_client()
        if not resolved_jwks.is_healthy():
            status = "degraded"
    except Exception:
        status = "degraded"

    # Report store reachability when STORE_MODE is http
    if settings.STORE_MODE == "http":
        try:
            import httpx

            base_url = settings.STORE_BASE_URL.rstrip("/")
            probe_url = (
                f"{base_url}/clock" if base_url.endswith("/api/v1") else f"{base_url}/api/v1/clock"
            )
            async with httpx.AsyncClient(timeout=2.0) as client:
                probe_resp = await client.get(probe_url)
                if probe_resp.status_code >= 500:
                    status = "degraded"
        except Exception:
            status = "degraded"

    dev_faults: dict[str, bool] | None = None
    if settings.DEV_MODE:
        try:
            from sellershield_agent.dev.faults import get_faults_map

            dev_faults = get_faults_map()
        except Exception:
            dev_faults = None

    now_utc = datetime.now(UTC).isoformat()
    return HealthResponse(
        status=status,
        timestamp=now_utc,
        store_mode=settings.STORE_MODE,
        dev_faults=dev_faults,
    )
