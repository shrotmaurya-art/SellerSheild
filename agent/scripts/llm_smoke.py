#!/usr/bin/env python3
"""Smoke test for configured LLM providers.

For each configured provider, requests a structured response conforming to:
    {answer: str, confidence: float between 0 and 1}
for the instruction: "Reply with the word ready".

Prints provider, model, latency_ms, and the parsed result.
Never prints keys or secrets. Exits 1 if no provider works.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure src is on sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import BaseModel, Field  # noqa: E402

from sellershield_agent.llm.chain import LLMChain  # noqa: E402
from sellershield_agent.llm.config import load_providers_from_settings  # noqa: E402
from sellershield_agent.llm.structured import generate_structured  # noqa: E402


class SmokeResult(BaseModel):
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


async def main() -> int:
    providers = load_providers_from_settings()
    if not providers:
        print("ERROR: No LLM providers configured in environment.")
        return 1

    success_count = 0
    instruction = "Reply with the word ready"

    for provider in providers:
        print(f"Testing provider: {provider.name} (model: {provider.model})...")
        single_chain = LLMChain(providers=[provider])
        try:
            parsed, result = await generate_structured(
                chain=single_chain,
                messages=[{"role": "user", "content": instruction}],
                model_cls=SmokeResult,
            )
            print(
                f"  SUCCESS [provider={result.provider}, model={result.model}, "
                f"latency_ms={result.latency_ms}]: "
                f"answer='{parsed.answer}' confidence={parsed.confidence:.2f}"
            )
            success_count += 1
        except Exception as exc:
            # Safe reporting: never echo keys or secret tokens
            err_name = type(exc).__name__
            print(
                f"  FAILED [provider={provider.name}, model={provider.model}]: {err_name} - {exc}"
            )
        finally:
            await single_chain.aclose()

    if success_count > 0:
        print(f"\nSmoke test passed: {success_count}/{len(providers)} provider(s) working.")
        return 0
    else:
        print(f"\nSmoke test FAILED: 0/{len(providers)} providers working.")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
