import os
from collections.abc import Generator

import pytest
from starlette.testclient import TestClient

from sellershield_agent.config import get_settings
from sellershield_agent.main import create_app


@pytest.fixture(autouse=True)
def setup_test_env() -> Generator[None, None, None]:
    """Ensure baseline required environment variables are set for tests."""
    old_env = os.environ.copy()
    os.environ["STORE_BASE_URL"] = "http://localhost:8000"
    os.environ["STORE_JWKS_URL"] = "http://localhost:8000/.well-known/jwks.json"
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"
    os.environ["DEV_MODE"] = "true"
    os.environ["CORS_ORIGINS"] = '["http://localhost:5173"]'

    # Clear lru_cache on get_settings and reset JWKS client singleton
    get_settings.cache_clear()
    from sellershield_agent.auth.deps import reset_default_jwks_client
    reset_default_jwks_client()
    yield
    os.environ.clear()
    os.environ.update(old_env)
    get_settings.cache_clear()
    reset_default_jwks_client()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
