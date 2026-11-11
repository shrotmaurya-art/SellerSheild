"""Structured output generation with JSON Schema / JSON mode fallback and retry."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from sellershield_agent.llm.chain import LLMChain, LLMError
from sellershield_agent.llm.client import LLMResult


class StructuredOutputError(LLMError):
    """Raised when LLM output fails schema validation after retry."""

    pass


async def generate_structured[T: BaseModel](
    chain: LLMChain,
    messages: list[dict[str, Any]],
    model_cls: type[T],
) -> tuple[T, LLMResult]:
    """Generate structured output adhering to a Pydantic model.

    Features:
    - Uses JSON schema mode if the active provider supports it.
    - Otherwise falls back to JSON mode with explicit schema guidance.
    - Validates with Pydantic model.
    - On validation error, retries ONCE appending the validation error text.
    - Raises StructuredOutputError if second attempt also fails validation.
    """
    schema = model_cls.model_json_schema()

    # Determine whether primary candidate supports json_schema
    supports_schema = False
    if chain.providers and chain.providers[0].capabilities.supports_json_schema:
        supports_schema = True

    if supports_schema:
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {
                "name": model_cls.__name__,
                "schema": schema,
                "strict": True,
            },
        }
    else:
        response_format = {"type": "json_object"}

    # If fallback JSON mode, ensure prompt mentions JSON and schema
    call_messages = list(messages)
    if not supports_schema:
        schema_prompt = (
            f"\nYou must return a valid JSON object matching this schema:\n"
            f"{json.dumps(schema, separators=(',', ':'))}"
        )
        # Append instruction to the last user message or add as a system message
        if call_messages and call_messages[-1].get("role") == "user":
            call_messages[-1] = {
                **call_messages[-1],
                "content": str(call_messages[-1].get("content", "")) + schema_prompt,
            }
        else:
            call_messages.append({"role": "user", "content": schema_prompt})

    # First attempt
    result = await chain.chat(
        messages=call_messages,
        response_format=response_format,
        schema_for_cache=schema,
    )

    try:
        if not result.text or not result.text.strip():
            raise ValueError("Empty response text from LLM")
        parsed = model_cls.model_validate_json(result.text)
        return parsed, result
    except (ValidationError, ValueError, json.JSONDecodeError) as first_err:
        # Retry once with error text appended
        error_summary = str(first_err)
        retry_messages = list(call_messages) + [
            {"role": "assistant", "content": result.text or ""},
            {
                "role": "user",
                "content": (
                    f"The previous response was invalid. "
                    f"Validation error: {error_summary}. "
                    f"Reply ONLY with a valid JSON object matching the required schema."
                ),
            },
        ]

        retry_result = await chain.chat(
            messages=retry_messages,
            response_format=response_format,
            schema_for_cache=schema,
        )

        try:
            if not retry_result.text or not retry_result.text.strip():
                raise ValueError("Empty response text from LLM on retry")
            parsed = model_cls.model_validate_json(retry_result.text)
            return parsed, retry_result
        except (ValidationError, ValueError, json.JSONDecodeError) as second_err:
            raise StructuredOutputError(
                f"Structured output validation failed after retry: {second_err}"
            ) from second_err
