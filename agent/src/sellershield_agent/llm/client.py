"""Async wrapper over OpenAI client returning standardized LLMResult."""

from __future__ import annotations

import time
from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ConfigDict, Field

from sellershield_agent.config import ConfigError
from sellershield_agent.llm.config import ProviderConfig


class LLMResult(BaseModel):
    """Standardized response from an LLM call."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    provider: str
    model: str
    latency_ms: int
    cached: bool = False


class LLMClient:
    """Async wrapper over openai.AsyncOpenAI with safety constraints."""

    def __init__(
        self,
        config: ProviderConfig,
        timeout: float = 12.0,
        http_client: Any = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._owned_http_client: httpx.AsyncClient | None = None

        if http_client is None:
            self._owned_http_client = httpx.AsyncClient(timeout=timeout)
            client_to_use: Any = self._owned_http_client
        else:
            client_to_use = http_client

        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key.get_secret_value(),
            timeout=timeout,
            max_retries=0,
            http_client=client_to_use,
        )

    async def aclose(self) -> None:
        """Close client and any owned transports."""
        if self._owned_http_client is not None:
            await self._owned_http_client.aclose()
        await self._client.close()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Call the provider endpoint and return standardized LLMResult."""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format

        start_time = time.perf_counter()
        try:
            resp = await self._client.chat.completions.create(**kwargs)
            latency_ms = int((time.perf_counter() - start_time) * 1000)

            choice = resp.choices[0] if resp.choices else None
            text = choice.message.content if choice and choice.message else None

            tool_calls: list[dict[str, Any]] = []
            if choice and choice.message and choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    tc_dict: dict[str, Any] = {
                        "id": tc.id,
                        "type": tc.type,
                    }
                    if tc.function:
                        tc_dict["function"] = {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    tool_calls.append(tc_dict)

            usage_dict: dict[str, int] = {}
            if resp.usage:
                usage_dict = {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                }

            return LLMResult(
                text=text,
                tool_calls=tool_calls,
                usage=usage_dict,
                provider=self.config.name,
                model=self.config.model,
                latency_ms=latency_ms,
                cached=False,
            )

        except (AuthenticationError, PermissionDeniedError):
            # 401 or 403: configuration/credential failure naming provider, never the key
            raise ConfigError(
                f"Authentication failed for LLM provider '{self.config.name}'. "
                "Check API key and permission configuration."
            ) from None

        except BadRequestError as err:
            err_str = str(err).lower()
            if "api key" in err_str or "api_key" in err_str or "invalid api key" in err_str:
                raise ConfigError(
                    f"Authentication failed for LLM provider '{self.config.name}'. "
                    "Check API key and permission configuration."
                ) from None
            raise err

        except NotFoundError:
            # 404: model or endpoint not found
            raise ConfigError(
                f"Model or endpoint not found for LLM provider '{self.config.name}' "
                f"(model: '{self.config.model}'). Check model configuration."
            ) from None

        except (
            RateLimitError,
            InternalServerError,
            APITimeoutError,
            APIConnectionError,
            APIResponseValidationError,
        ) as err:
            # Retryable upstream failures (429, 5xx, timeout, connection)
            # Re-raise so LLMChain catches them and triggers failover / circuit breaker
            raise err
