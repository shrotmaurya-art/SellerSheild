import pytest

from sellershield_agent.config import ConfigError, Settings


def test_missing_env_var_lists_variable_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear environment of all settings
    monkeypatch.delenv("STORE_BASE_URL", raising=False)
    monkeypatch.delenv("STORE_JWKS_URL", raising=False)

    with pytest.raises(ConfigError) as exc_info:
        Settings.load_from_env(env_dict={}, env_file=None)

    err_msg = str(exc_info.value)
    # Must explicitly mention the missing names
    assert "STORE_BASE_URL" in err_msg
    assert "STORE_JWKS_URL" in err_msg
    # Must NOT contain passwords, placeholders, or sensitive dumps
    assert "replace-me" not in err_msg
    assert "input" not in err_msg.lower()


def test_valid_env_loads_successfully() -> None:
    valid_env = {
        "STORE_BASE_URL": "http://localhost:8000",
        "STORE_JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
        "DATABASE_URL": "sqlite:///data/agent.db",
        "DEV_MODE": "true",
        "CORS_ORIGINS": '["http://localhost:5173"]',
        "STORE_EXECUTOR_CLIENT_SECRET": "secret-value-12345",
    }

    settings = Settings.load_from_env(valid_env)
    assert settings.STORE_BASE_URL == "http://localhost:8000"
    assert settings.DEV_MODE is True
    assert settings.STORE_EXECUTOR_CLIENT_SECRET is not None
    # SecretStr protects secret from raw str representation
    assert "secret-value-12345" not in str(settings.STORE_EXECUTOR_CLIENT_SECRET)
    assert settings.STORE_EXECUTOR_CLIENT_SECRET.get_secret_value() == "secret-value-12345"
