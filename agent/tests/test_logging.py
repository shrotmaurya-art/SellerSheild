import io
import logging

from starlette.testclient import TestClient

from sellershield_agent.logging import redact_sensitive_processor


def test_redact_sensitive_keys() -> None:
    event_dict = {
        "event": "test_event",
        "authorization": "Bearer secret-token-xyz",
        "api_key": "sk-1234567890",
        "token": "tok_abcdef",
        "access_token": "acc_987654",
        "password": "my-secret-password",
        "secret": "top-secret-val",
        "cookie": "session=xyz123",
        "nested": {
            "Authorization": "Bearer nested-secret",
            "api_key": "nested-key",
            "safe_field": "safe_value",
        },
        "safe_key": "safe_data",
    }

    result = redact_sensitive_processor(None, "info", event_dict)

    assert result["authorization"] == "[REDACTED]"
    assert result["api_key"] == "[REDACTED]"
    assert result["token"] == "[REDACTED]"
    assert result["access_token"] == "[REDACTED]"
    assert result["password"] == "[REDACTED]"
    assert result["secret"] == "[REDACTED]"
    assert result["cookie"] == "[REDACTED]"
    assert result["nested"]["Authorization"] == "[REDACTED]"
    assert result["nested"]["api_key"] == "[REDACTED]"
    assert result["nested"]["safe_field"] == "safe_value"
    assert result["safe_key"] == "safe_data"


def test_redact_email_values() -> None:
    event_dict = {
        "event": "contact_user",
        "user_email": "customer@example.com",
        "message": "Notification sent to user.test+filter@domain.co.in successfully",
        "nested": ["item1", "admin@internal.corp"],
    }

    result = redact_sensitive_processor(None, "info", event_dict)

    assert "customer@example.com" not in str(result)
    assert "user.test+filter@domain.co.in" not in str(result)
    assert "admin@internal.corp" not in str(result)
    assert "[REDACTED_EMAIL]" in str(result)


def test_captured_logs_never_contain_authorization_header(client: TestClient) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    secret_auth_header = "Bearer secret-token-should-never-appear-in-logs"
    try:
        response = client.get(
            "/healthz",
            headers={"Authorization": secret_auth_header},
        )
        assert response.status_code == 200
    finally:
        root_logger.removeHandler(handler)

    logs_text = stream.getvalue()
    assert secret_auth_header not in logs_text
    assert "secret-token-should-never-appear-in-logs" not in logs_text
