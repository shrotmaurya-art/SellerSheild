import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response
from pydantic import BaseModel, Field, SecretStr

from sellershield_agent.clock import FixedClock
from sellershield_agent.config import ConfigError
from sellershield_agent.llm.cache import CacheMiss, LLMCache
from sellershield_agent.llm.chain import LLMChain, LLMUnavailable
from sellershield_agent.llm.config import ProviderCapabilities, ProviderConfig
from sellershield_agent.llm.structured import StructuredOutputError, generate_structured


class ReadyModel(BaseModel):
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


@pytest.fixture
def sample_providers() -> list[ProviderConfig]:
    return [
        ProviderConfig(
            name="primary",
            base_url="https://primary.example.com/v1",
            api_key=SecretStr("primary-secret-key-12345"),
            model="gemini-2.5-flash",
            capabilities=ProviderCapabilities(
                supports_json_schema=True,
                supports_tools=True,
                parallel_tools=True,
            ),
        ),
        ProviderConfig(
            name="secondary",
            base_url="https://secondary.example.com/v1",
            api_key=SecretStr("secondary-secret-key-67890"),
            model="llama-3.3-70b-versatile",
            capabilities=ProviderCapabilities(
                supports_json_schema=False,
                supports_tools=True,
                parallel_tools=False,
            ),
        ),
        ProviderConfig(
            name="tertiary",
            base_url="https://tertiary.example.com/v1",
            api_key=SecretStr("tertiary-secret-key-abcde"),
            model="meta-llama/llama-3.3-70b-instruct:free",
            capabilities=ProviderCapabilities(
                supports_json_schema=False,
                supports_tools=False,
                parallel_tools=False,
            ),
        ),
    ]


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC))


def _make_chat_response(content: str, model: str = "test-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


# 1. Primary 429 then secondary used
@respx.mock
@pytest.mark.asyncio
async def test_primary_429_then_secondary_used(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=429,
        json={"error": {"message": "Rate limit reached", "type": "rate_limit_error"}},
    )
    respx.post("https://secondary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("Hello from secondary", model="llama-3.3-70b-versatile"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    result = await chain.chat(messages=[{"role": "user", "content": "hello"}])

    assert result.provider == "secondary"
    assert result.model == "llama-3.3-70b-versatile"
    assert result.text == "Hello from secondary"
    assert result.cached is False
    assert result.latency_ms >= 0


# 2. Primary timeout then secondary used
@respx.mock
@pytest.mark.asyncio
async def test_primary_timeout_then_secondary(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=504,
        json={"error": {"message": "Gateway Timeout"}},
    )
    respx.post("https://secondary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("Recovered via secondary", model="llama-3.3-70b-versatile"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    result = await chain.chat(messages=[{"role": "user", "content": "ping"}])

    assert result.provider == "secondary"
    assert result.text == "Recovered via secondary"


# 3. Both/all fail gives LLMUnavailable
@respx.mock
@pytest.mark.asyncio
async def test_both_fail_gives_llm_unavailable(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(status_code=500)
    respx.post("https://secondary.example.com/v1/chat/completions").respond(status_code=503)
    respx.post("https://tertiary.example.com/v1/chat/completions").respond(status_code=429)

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    with pytest.raises(LLMUnavailable) as exc_info:
        await chain.chat(messages=[{"role": "user", "content": "ping"}])

    assert "All configured LLM providers failed" in str(exc_info.value)


# 4. 401 or 403 gives ConfigError without the key text
@respx.mock
@pytest.mark.asyncio
async def test_401_gives_config_error_without_key_text(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=401,
        json={"error": {"message": "Invalid API key provided", "type": "invalid_request_error"}},
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    with pytest.raises(ConfigError) as exc_info:
        await chain.chat(messages=[{"role": "user", "content": "hello"}])

    err_msg = str(exc_info.value)
    assert "primary" in err_msg
    assert "primary-secret-key-12345" not in err_msg
    assert "67890" not in err_msg


# 4b. 404 model not found gives clear ConfigError
@respx.mock
@pytest.mark.asyncio
async def test_404_model_not_found_gives_config_error(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=404,
        json={"error": {"message": "The model `gemini-2.5-flash` does not exist"}},
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    with pytest.raises(ConfigError) as exc_info:
        await chain.chat(messages=[{"role": "user", "content": "hello"}])

    err_msg = str(exc_info.value)
    assert "primary" in err_msg
    assert "gemini-2.5-flash" in err_msg
    assert "primary-secret-key-12345" not in err_msg


# 5. Invalid JSON then valid on retry
@respx.mock
@pytest.mark.asyncio
async def test_invalid_json_then_valid_on_retry(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    # First attempt: invalid JSON
    # Second attempt: valid JSON
    route = respx.post("https://primary.example.com/v1/chat/completions")
    route.side_effect = [
        Response(200, json=_make_chat_response("not valid json at all", model="gemini-2.5-flash")),
        Response(
            200,
            json=_make_chat_response(
                json.dumps({"answer": "ready", "confidence": 0.95}),
                model="gemini-2.5-flash",
            ),
        ),
    ]

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    parsed, result = await generate_structured(
        chain=chain,
        messages=[{"role": "user", "content": "Reply with ready"}],
        model_cls=ReadyModel,
    )

    assert isinstance(parsed, ReadyModel)
    assert parsed.answer == "ready"
    assert parsed.confidence == 0.95
    assert result.provider == "primary"
    assert route.call_count == 2


# 6. Invalid twice gives StructuredOutputError
@respx.mock
@pytest.mark.asyncio
async def test_invalid_twice_gives_structured_output_error(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    route = respx.post("https://primary.example.com/v1/chat/completions")
    route.side_effect = [
        Response(200, json=_make_chat_response("invalid attempt 1")),
        Response(200, json=_make_chat_response("still invalid attempt 2")),
    ]

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    with pytest.raises(StructuredOutputError) as exc_info:
        await generate_structured(
            chain=chain,
            messages=[{"role": "user", "content": "Reply with ready"}],
            model_cls=ReadyModel,
        )

    assert "Structured output validation failed after retry" in str(exc_info.value)
    assert route.call_count == 2


# 7. Breaker opens after 3 failures, skips primary for 60 seconds, then half-opens
@respx.mock
@pytest.mark.asyncio
async def test_breaker_opens_after_3_failures_skips_primary_60s_then_half_opens(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    primary_route = respx.post("https://primary.example.com/v1/chat/completions")
    primary_route.respond(status_code=500)

    secondary_route = respx.post("https://secondary.example.com/v1/chat/completions")
    secondary_route.respond(
        status_code=200,
        json=_make_chat_response("Secondary ok", model="llama-3.3-70b-versatile"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)

    # Call 1: primary fails -> secondary succeeds
    r1 = await chain.chat(messages=[{"role": "user", "content": "1"}])
    assert r1.provider == "secondary"
    assert primary_route.call_count == 1

    # Call 2: primary fails -> secondary succeeds
    fixed_clock.set(fixed_clock.now() + timedelta(seconds=10))
    r2 = await chain.chat(messages=[{"role": "user", "content": "2"}])
    assert r2.provider == "secondary"
    assert primary_route.call_count == 2

    # Call 3: primary fails (3rd failure within 60s -> breaker OPENS) -> secondary succeeds
    fixed_clock.set(fixed_clock.now() + timedelta(seconds=10))
    r3 = await chain.chat(messages=[{"role": "user", "content": "3"}])
    assert r3.provider == "secondary"
    assert primary_route.call_count == 3

    # Call 4: 20s later (still within 60s cooldown) -> primary is SKIPPED entirely!
    fixed_clock.set(fixed_clock.now() + timedelta(seconds=20))
    r4 = await chain.chat(messages=[{"role": "user", "content": "4"}])
    assert r4.provider == "secondary"
    assert primary_route.call_count == 3  # Not called!

    # Advance clock by 45s (now > 60s since breaker opened) -> HALF-OPEN
    # Primary will be tried again. Let primary succeed this time!
    fixed_clock.set(fixed_clock.now() + timedelta(seconds=45))
    primary_route.respond(
        status_code=200,
        json=_make_chat_response("Primary recovered!", model="gemini-2.5-flash"),
    )

    r5 = await chain.chat(messages=[{"role": "user", "content": "5"}])
    assert r5.provider == "primary"
    assert primary_route.call_count == 4
    assert r5.text == "Primary recovered!"


# 8. Cache record then replay returns same result with no HTTP call
@respx.mock
@pytest.mark.asyncio
async def test_cache_record_then_replay_returns_same_result_no_http_call(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / ".llm_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    route = respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("First time output", model="gemini-2.5-flash"),
    )

    # 1. Record mode
    record_cache = LLMCache(mode="record", cache_dir=cache_dir)
    chain_record = LLMChain(providers=sample_providers, clock=fixed_clock, cache=record_cache)

    messages = [{"role": "user", "content": "cache me"}]
    res1 = await chain_record.chat(messages=messages)
    assert res1.text == "First time output"
    assert res1.cached is False
    assert route.call_count == 1

    # Verify a cache file was written
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    # 2. Replay mode
    replay_cache = LLMCache(mode="replay", cache_dir=cache_dir)
    chain_replay = LLMChain(providers=sample_providers, clock=fixed_clock, cache=replay_cache)

    res2 = await chain_replay.chat(messages=messages)
    assert res2.text == "First time output"
    assert res2.cached is True
    # Crucial: NO HTTP call was made during replay!
    assert route.call_count == 1


# 9. Replay miss raises CacheMiss
@pytest.mark.asyncio
async def test_replay_miss_raises_cache_miss(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / ".llm_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    replay_cache = LLMCache(mode="replay", cache_dir=cache_dir)
    chain_replay = LLMChain(providers=sample_providers, clock=fixed_clock, cache=replay_cache)

    with pytest.raises(CacheMiss) as exc_info:
        await chain_replay.chat(messages=[{"role": "user", "content": "unseen prompt"}])

    assert "LLM cache miss in replay mode" in str(exc_info.value)


# 10. Logs and exception strings never contain the API key
@respx.mock
@pytest.mark.asyncio
async def test_logs_and_exception_strings_never_contain_api_key(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_key = "primary-secret-key-12345"
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=401,
        json={"error": {"message": f"Unauthorized key: {secret_key}"}},
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    try:
        await chain.chat(messages=[{"role": "user", "content": "test"}])
    except Exception as exc:
        exc_str = str(exc)
        assert secret_key not in exc_str

    for record in caplog.records:
        assert secret_key not in record.message
        if hasattr(record, "args") and record.args:
            assert secret_key not in str(record.args)


# 11. Unknown provider capabilities default to JSON mode
def test_unknown_provider_capabilities_default_to_json_mode() -> None:
    caps = ProviderCapabilities()
    assert caps.supports_json_schema is False
    assert caps.supports_tools is False
    assert caps.parallel_tools is False

    unknown_config = ProviderConfig(
        name="custom_local",
        base_url="http://localhost:11434/v1",
        api_key=SecretStr("dummy"),
        model="custom-model",
    )
    assert unknown_config.capabilities.supports_json_schema is False
    assert unknown_config.capabilities.supports_tools is False
    assert unknown_config.capabilities.parallel_tools is False


# 12. Break test: Provider returns HTML on 502 or 500 fails over to secondary
@respx.mock
@pytest.mark.asyncio
async def test_break_provider_returns_html_502_fails_over(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=502,
        text="<html><head><title>502</title></head><body>Bad Gateway</body></html>",
    )
    respx.post("https://secondary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("Recovered from HTML 502", model="llama-3.3-70b-versatile"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    result = await chain.chat(messages=[{"role": "user", "content": "ping"}])
    assert result.provider == "secondary"
    assert result.text == "Recovered from HTML 502"


# 13. Break test: Provider returns HTML or empty body on 200 fails over or raises
@respx.mock
@pytest.mark.asyncio
async def test_break_provider_returns_html_on_200_fails_over(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    # 200 OK but body is HTML (e.g. captive portal or proxy)
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=200,
        text="<html><body>Login to Wifi</body></html>",
        headers={"Content-Type": "text/html"},
    )
    respx.post("https://secondary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("Recovered from captive portal", model="llama-3.3-70b-versatile"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    result = await chain.chat(messages=[{"role": "user", "content": "ping"}])
    assert result.provider == "secondary"
    assert result.text == "Recovered from captive portal"


# 14. Break test: Enormous response is handled safely
@respx.mock
@pytest.mark.asyncio
async def test_break_enormous_response_handled_safely(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
) -> None:
    huge_text = "A" * (2 * 1024 * 1024)  # 2 MB response
    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response(huge_text, model="gemini-2.5-flash"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock)
    result = await chain.chat(messages=[{"role": "user", "content": "give me lots"}])
    assert result.provider == "primary"
    assert result.text is not None
    assert len(result.text) == 2 * 1024 * 1024


# 15. Break test: Twenty simultaneous calls handle concurrency safely
@respx.mock
@pytest.mark.asyncio
async def test_break_twenty_simultaneous_calls(
    sample_providers: list[ProviderConfig],
    fixed_clock: FixedClock,
    tmp_path: Path,
) -> None:
    import asyncio

    cache_dir = tmp_path / ".llm_cache"
    cache = LLMCache(mode="record", cache_dir=cache_dir)

    respx.post("https://primary.example.com/v1/chat/completions").respond(
        status_code=200,
        json=_make_chat_response("concurrent response", model="gemini-2.5-flash"),
    )

    chain = LLMChain(providers=sample_providers, clock=fixed_clock, cache=cache)

    async def call_llm(idx: int) -> str:
        res = await chain.chat(messages=[{"role": "user", "content": f"call {idx}"}])
        return str(res.text)

    tasks = [call_llm(i) for i in range(20)]
    results = await asyncio.gather(*tasks)

    assert len(results) == 20
    assert all(r == "concurrent response" for r in results)
    # Cache files created safely without collisions
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 20

