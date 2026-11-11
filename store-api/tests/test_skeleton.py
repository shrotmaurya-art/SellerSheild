from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_returns_200_and_request_id() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) > 0


def test_error_format_on_unknown_route() -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    error = data["error"]
    assert error["code"] == "not_found"
    assert "message" in error
    assert "request_id" in error
    assert error["request_id"] == response.headers.get("x-request-id")


def test_request_id_echoed() -> None:
    custom_id = "my-test-1"
    response = client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers.get("x-request-id") == custom_id
