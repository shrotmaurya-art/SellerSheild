"""Regression tests for BUG-017: Default service credentials for executor and monitor."""

from sellershield_agent.config import Settings


def test_default_service_credentials_configured() -> None:
    settings = Settings()
    assert settings.STORE_EXECUTOR_CLIENT_ID == "agent-executor"
    assert settings.STORE_EXECUTOR_CLIENT_SECRET is not None
    assert (
        settings.STORE_EXECUTOR_CLIENT_SECRET.get_secret_value()
        == "fake_executor_secret_key_12345"
    )
    assert settings.STORE_MONITOR_CLIENT_ID == "agent-monitor"
    assert settings.STORE_MONITOR_CLIENT_SECRET is not None
    assert (
        settings.STORE_MONITOR_CLIENT_SECRET.get_secret_value()
        == "fake_monitor_secret_key_67890"
    )
    assert settings.WEBHOOK_SECRET is not None
    assert (
        settings.WEBHOOK_SECRET.get_secret_value()
        == "fake_dev_webhook_secret_do_not_use_in_prod"
    )
