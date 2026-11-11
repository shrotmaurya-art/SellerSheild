import respx
from starlette.testclient import TestClient


def test_healthz_endpoint(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data
    assert "X-Request-ID" in response.headers


@respx.mock
def test_readyz_endpoint(client: TestClient) -> None:
    # Minimal mock RSA JWK
    mock_jwk = {
        "kty": "RSA",
        "kid": "k1",
        "alg": "RS256",
        "use": "sig",
        "n": "u1SU63wQ2A08VCE457Ju2JOr61wE5YK9uQnNSuV7ucTX_9GsEvqWBk88e0U6fNuL",
        "e": "AQAB",
    }
    respx.get("http://localhost:8000/.well-known/jwks.json").respond(json={"keys": [mock_jwk]})
    response = client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "timestamp" in data
    assert "X-Request-ID" in response.headers


@respx.mock
def test_readyz_endpoint_degraded_when_store_down(client: TestClient) -> None:
    respx.get("http://localhost:8000/.well-known/jwks.json").respond(status_code=500)
    response = client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "degraded"
    assert "timestamp" in data
    assert "X-Request-ID" in response.headers

