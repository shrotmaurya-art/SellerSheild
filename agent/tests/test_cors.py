import os

from starlette.testclient import TestClient

from sellershield_agent.config import get_settings
from sellershield_agent.main import create_app


def test_cors_allowed_origin(client: TestClient) -> None:
    response = client.options(
        "/healthz",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_random_origin_blocked(client: TestClient) -> None:
    response = client.options(
        "/healthz",
        headers={
            "Origin": "http://malicious-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") is None


def test_docs_available_only_when_dev_mode() -> None:
    # 1. Dev mode True
    os.environ["DEV_MODE"] = "true"
    get_settings.cache_clear()
    app_dev = create_app()
    with TestClient(app_dev) as client_dev:
        resp = client_dev.get("/docs")
        assert resp.status_code == 200

    # 2. Dev mode False
    os.environ["DEV_MODE"] = "false"
    get_settings.cache_clear()
    app_prod = create_app()
    with TestClient(app_prod) as client_prod:
        resp = client_prod.get("/docs")
        assert resp.status_code == 404
