from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sellershield_agent.api.health import router as health_router
from sellershield_agent.config import get_settings
from sellershield_agent.errors import register_error_handlers
from sellershield_agent.logging import configure_logging
from sellershield_agent.middleware import (
    AccessLogMiddleware,
    BodyLimitMiddleware,
    RequestIDMiddleware,
)


def create_app() -> FastAPI:
    """Application factory for the SellerShield Agent service."""
    # Initialize structured JSON logging
    configure_logging()

    # Load configuration
    settings = get_settings()

    # Create FastAPI app with docs restricted to DEV_MODE
    app = FastAPI(
        title="SellerShield AI Agent",
        version="0.1.0",
        docs_url="/docs" if settings.DEV_MODE else None,
        redoc_url="/redoc" if settings.DEV_MODE else None,
        openapi_url="/openapi.json" if settings.DEV_MODE else None,
    )

    # Register standard error envelope handlers
    register_error_handlers(app)

    # Wire routers
    app.include_router(health_router)

    # Register middlewares (executed in reverse order of addition)
    # Order of execution: CORS (if enabled) -> RequestID -> BodyLimit -> AccessLog -> App
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # CORS configuration (off by default; enabled only if CORS_ORIGINS is specified)
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("sellershield_agent.main:create_app", factory=True, port=8001, reload=True)

