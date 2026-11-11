"""LLM provider configuration and capabilities."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sellershield_agent.config import Settings, get_settings


class ProviderCapabilities(BaseModel):
    """Provider feature flags; all default to False for unknown providers."""

    model_config = ConfigDict(extra="forbid")

    supports_json_schema: bool = False
    supports_tools: bool = False
    parallel_tools: bool = False


class ProviderConfig(BaseModel):
    """Configuration for an individual LLM provider."""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    api_key: SecretStr
    model: str
    capabilities: ProviderCapabilities = Field(default_factory=ProviderCapabilities)


def detect_capabilities(name: str, base_url: str, model: str) -> ProviderCapabilities:
    """Detect known provider capabilities, falling back to all-False for unknown providers."""
    url_lower = base_url.lower()
    name_lower = name.lower()

    if "googleapis" in url_lower or "google" in name_lower:
        # Gemini OpenAI-compatible endpoint
        return ProviderCapabilities(
            supports_json_schema=True,
            supports_tools=True,
            parallel_tools=True,
        )
    elif "groq" in url_lower or "groq" in name_lower:
        # Groq endpoint
        return ProviderCapabilities(
            supports_json_schema=False,
            supports_tools=True,
            parallel_tools=False,
        )
    elif "openai.com" in url_lower or "openai" in name_lower:
        # OpenAI endpoint
        return ProviderCapabilities(
            supports_json_schema=True,
            supports_tools=True,
            parallel_tools=True,
        )
    elif "openrouter" in url_lower or "openrouter" in name_lower:
        # OpenRouter endpoint
        return ProviderCapabilities(
            supports_json_schema=False,
            supports_tools=True,
            parallel_tools=False,
        )

    # Unknown providers default to all-False
    return ProviderCapabilities()


def load_providers_from_settings(settings: Settings | None = None) -> list[ProviderConfig]:
    """Build list of configured providers from settings / environment."""
    if settings is None:
        settings = get_settings()

    providers: list[ProviderConfig] = []

    # Primary provider
    if (
        settings.LLM_PRIMARY_BASE_URL
        and settings.LLM_PRIMARY_API_KEY
        and settings.LLM_PRIMARY_MODEL
    ):
        providers.append(
            ProviderConfig(
                name="primary",
                base_url=settings.LLM_PRIMARY_BASE_URL,
                api_key=settings.LLM_PRIMARY_API_KEY,
                model=settings.LLM_PRIMARY_MODEL,
                capabilities=detect_capabilities(
                    "primary",
                    settings.LLM_PRIMARY_BASE_URL,
                    settings.LLM_PRIMARY_MODEL,
                ),
            )
        )

    # Secondary provider
    if (
        settings.LLM_SECONDARY_BASE_URL
        and settings.LLM_SECONDARY_API_KEY
        and settings.LLM_SECONDARY_MODEL
    ):
        providers.append(
            ProviderConfig(
                name="secondary",
                base_url=settings.LLM_SECONDARY_BASE_URL,
                api_key=settings.LLM_SECONDARY_API_KEY,
                model=settings.LLM_SECONDARY_MODEL,
                capabilities=detect_capabilities(
                    "secondary",
                    settings.LLM_SECONDARY_BASE_URL,
                    settings.LLM_SECONDARY_MODEL,
                ),
            )
        )

    # Optional tertiary provider
    if (
        settings.LLM_TERTIARY_BASE_URL
        and settings.LLM_TERTIARY_API_KEY
        and settings.LLM_TERTIARY_MODEL
    ):
        providers.append(
            ProviderConfig(
                name="tertiary",
                base_url=settings.LLM_TERTIARY_BASE_URL,
                api_key=settings.LLM_TERTIARY_API_KEY,
                model=settings.LLM_TERTIARY_MODEL,
                capabilities=detect_capabilities(
                    "tertiary",
                    settings.LLM_TERTIARY_BASE_URL,
                    settings.LLM_TERTIARY_MODEL,
                ),
            )
        )

    return providers
