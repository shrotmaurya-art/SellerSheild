import json
from functools import lru_cache
from pathlib import Path
from typing import Any

<<<<<<< HEAD
from pydantic import SecretStr, ValidationError, field_validator, model_validator
=======
from pydantic import SecretStr, ValidationError, field_validator
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """Raised when configuration validation fails, listing only missing variable names."""
<<<<<<< HEAD

=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
    pass


class Settings(BaseSettings):
    """SellerShield Agent settings loaded from environment or agent/.env."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core required settings
    STORE_BASE_URL: str
    STORE_JWKS_URL: str

    # Defaults
    DATABASE_URL: str = "sqlite:///data/agent.db"
    DEV_MODE: bool = False
<<<<<<< HEAD
    STORE_MODE: str = "fixtures"
=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
    CORS_ORIGINS: list[str] = []
    JWT_ISSUER: str = "nestkart-store"

    # Store credentials
    STORE_EXECUTOR_CLIENT_ID: str | None = None
    STORE_EXECUTOR_CLIENT_SECRET: SecretStr | None = None
    STORE_MONITOR_CLIENT_ID: str | None = None
    STORE_MONITOR_CLIENT_SECRET: SecretStr | None = None
    WEBHOOK_SECRET: SecretStr | None = None

    # LLM Settings (optional for now)
    LLM_PRIMARY_BASE_URL: str | None = None
    LLM_PRIMARY_API_KEY: SecretStr | None = None
    LLM_PRIMARY_MODEL: str | None = None
    LLM_SECONDARY_BASE_URL: str | None = None
    LLM_SECONDARY_API_KEY: SecretStr | None = None
    LLM_SECONDARY_MODEL: str | None = None
    LLM_TERTIARY_BASE_URL: str | None = None
    LLM_TERTIARY_API_KEY: SecretStr | None = None
    LLM_TERTIARY_MODEL: str | None = None
    LLM_TIMEOUT_S: int = 12
    LLM_CACHE_MODE: str = "off"
    PROMPT_VERSION: str = "v1"
<<<<<<< HEAD
    DEV_FAULT_LLM_DOWN: bool = False
=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("[") and stripped.endswith("]"):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in stripped.split(",") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

<<<<<<< HEAD
    @model_validator(mode="after")
    def validate_store_mode(self) -> "Settings":
        valid_modes = {"fixtures", "http"}
        if self.STORE_MODE not in valid_modes:
            raise ValueError(f"STORE_MODE must be one of {valid_modes}, got '{self.STORE_MODE}'")
        return self

=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
    @classmethod
    def load_from_env(
        cls,
        env_dict: dict[str, Any] | None = None,
        env_file: Path | str | None = None,
    ) -> "Settings":
        """Load settings fail-fast; on missing required variables report NAMES only."""
        try:
            if env_dict is not None:
                # When an explicit env dictionary is provided, ignore disk .env file
                return cls(_env_file=env_file, **env_dict)  # type: ignore[call-arg]
            return cls()  # type: ignore[call-arg]
        except ValidationError as err:
            missing_names: set[str] = set()
            for error in err.errors():
                loc = error.get("loc", ())
                if loc:
                    missing_names.add(str(loc[0]))
            sorted_names = ", ".join(sorted(missing_names))
            raise ConfigError(
                f"Missing or invalid required configuration variables: {sorted_names}"
            ) from None


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings.load_from_env()
