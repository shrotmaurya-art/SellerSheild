import json
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DEMO_MODE: bool = True
    DATABASE_URL: str = "sqlite:///data/store.db"
    JWT_PRIVATE_KEY_PATH: str = "keys/jwt_private.pem"
    JWT_PUBLIC_KEY_PATH: str = "keys/jwt_public.pem"
    JWT_ISSUER: str = "nestkart-store"
    WEBHOOK_SECRET: str = "fake_dev_webhook_secret_do_not_use_in_prod"
    AGENT_WEBHOOK_URL: str = "http://localhost:8001/v1/webhooks/store"
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    SERVICE_CLIENT_SECRETS: dict[str, str] = {
        "agent-executor": "fake_executor_secret_key_12345",
        "agent-monitor": "fake_monitor_secret_key_67890",
    }
    UPLOAD_DIR: str = "uploads"
    MAX_UPLOAD_BYTES: int = 2097152

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except Exception:
                return [origin.strip() for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(item) for item in v]
        return ["http://localhost:5173"]

    @field_validator("SERVICE_CLIENT_SECRETS", mode="before")
    @classmethod
    def parse_service_client_secrets(cls, v: Any) -> dict[str, str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return {str(k): str(val) for k, val in parsed.items()}
            except Exception:
                pass
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items()}
        return {}

    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parent.parent.parent / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
