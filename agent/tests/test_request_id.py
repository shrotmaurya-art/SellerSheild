import re

from starlette.testclient import TestClient

REQUEST_ID_REGEX = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def test_request_id_generated_when_missing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    req_id = response.headers.get("X-Request-ID")
    assert req_id is not None
    assert REQUEST_ID_REGEX.match(req_id)


def test_request_id_echoed_when_valid(client: TestClient) -> None:
    custom_id = "req-client-valid-12345"
    response = client.get("/healthz", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == custom_id


def test_request_id_replaced_when_invalid(client: TestClient) -> None:
    # Too short (<8 chars)
    resp_short = client.get("/healthz", headers={"X-Request-ID": "short"})
    req_id = resp_short.headers.get("X-Request-ID")
    assert req_id is not None
    assert req_id != "short"
    assert REQUEST_ID_REGEX.match(req_id)

    # Overlong (>64 chars)
    overlong = "a" * 65
    resp_long = client.get("/healthz", headers={"X-Request-ID": overlong})
    req_id = resp_long.headers.get("X-Request-ID")
    assert req_id != overlong
    assert REQUEST_ID_REGEX.match(req_id)

    # Newline injection attempt
    newline_id = "req-valid-prefix\ninjected:true"
    resp_nl = client.get("/healthz", headers={"X-Request-ID": newline_id})
    req_id = resp_nl.headers.get("X-Request-ID")
    assert "\n" not in req_id
    assert REQUEST_ID_REGEX.match(req_id)

    # Disallowed characters (symbols, spaces)
    resp_chars = client.get("/healthz", headers={"X-Request-ID": "req-12345;drop table;"})
    req_id = resp_chars.headers.get("X-Request-ID")
    assert ";" not in req_id
    assert REQUEST_ID_REGEX.match(req_id)
