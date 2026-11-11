from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.testclient import TestClient

from sellershield_agent.main import create_app


class SecretPayload(BaseModel):
    account_id: str
    password: str = Field(min_length=8)
    token: str = Field(min_length=10)


def test_unknown_route_returns_envelope_404(client: TestClient) -> None:
    response = client.get("/nope-not-found")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    err = data["error"]
    assert err["code"] == "not_found"
    assert "message" in err
    assert "request_id" in err
    assert err["request_id"] == response.headers.get("X-Request-ID")


def test_wrong_http_method_returns_envelope(client: TestClient) -> None:
    response = client.post("/healthz")
    assert response.status_code in (404, 405)
    data = response.json()
    assert "error" in data
    assert "code" in data["error"]
    assert "request_id" in data["error"]


def test_invalid_json_returns_envelope(client: TestClient) -> None:
    response = client.post(
        "/healthz",
        content="this is not valid json {{{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (400, 404, 405, 422)
    data = response.json()
    assert "error" in data
    assert "request_id" in data["error"]


def test_validation_error_never_echoes_secret_fields() -> None:
    test_router = APIRouter()

    @test_router.post("/test-secret-validation")
    def submit_secret(payload: SecretPayload) -> dict[str, str]:
        return {"status": "ok"}

    app = create_app()
    app.include_router(test_router)

    with TestClient(app) as test_client:
        secret_password = "pw_1"
        secret_token = "tok_2"
        # Trigger validation errors (too short)
        response = test_client.post(
            "/test-secret-validation",
            json={
                "account_id": "ACC-1",
                "password": secret_password,
                "token": secret_token,
            },
        )
        assert response.status_code == 422
        data = response.json()
        assert "error" in data
        assert data["error"]["code"] == "validation_error"
        # Raw response text must never contain the secret values
        assert secret_password not in response.text
        assert secret_token not in response.text
        # Ensure details do not echo input values for secret-named fields
        details = str(data["error"]["details"])
        assert "password" in details
        assert secret_password not in details
        assert secret_token not in details


def test_unexpected_exception_returns_500_with_no_stack_trace() -> None:
    test_router = APIRouter()

    @test_router.get("/trigger-crash")
    def trigger_crash() -> Any:
        raise RuntimeError("Sensitive internal database connection failed: secret_db_pw=12345")

    app = create_app()
    app.include_router(test_router)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/trigger-crash")
        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        err = data["error"]
        assert err["code"] == "internal"
        assert err["message"] == "Internal server error"
        assert "request_id" in err
        # Stack trace or sensitive text must not leak
        assert "RuntimeError" not in response.text
        assert "Traceback" not in response.text
        assert "secret_db_pw" not in response.text


def test_request_body_over_1mb_returns_413(client: TestClient) -> None:
    large_payload = "a" * (1024 * 1024 + 100)  # > 1MB
    response = client.post(
        "/healthz",
        content=large_payload,
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 413
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "limit_exceeded"
    assert "request_id" in data["error"]
