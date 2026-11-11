"""Record-replay LLM response cache."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from sellershield_agent.llm.client import LLMResult

CacheMode = Literal["off", "record", "replay"]


class CacheMiss(Exception):
    """Raised when an LLM call is attempted in replay mode without a recorded cache entry."""
    pass


def make_cache_key(
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    schema: dict[str, Any] | None = None,
) -> str:
    """Generate canonical SHA-256 cache key from request parameters."""
    normalized = {
        "provider": provider,
        "model": model,
        "messages": messages,
        "tools": tools or [],
        "schema": schema or {},
    }
    canonical_json = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class LLMCache:
    """Persistent LLM cache supporting off, record, and replay modes."""

    def __init__(
        self,
        mode: CacheMode = "off",
        cache_dir: Path | str | None = None,
    ) -> None:
        self.mode = mode
        if cache_dir is None:
            # Default to agent/.llm_cache
            self.cache_dir = Path(__file__).resolve().parent.parent.parent.parent / ".llm_cache"
        else:
            self.cache_dir = Path(cache_dir)

        if self.mode != "off":
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, key: str) -> LLMResult | None:
        """Retrieve cached result or raise CacheMiss in replay mode."""
        if self.mode == "off":
            return None

        path = self._file_path(key)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return LLMResult(
                    text=data.get("text"),
                    tool_calls=data.get("tool_calls", []),
                    usage=data.get("usage", {}),
                    provider=data.get("provider", "unknown"),
                    model=data.get("model", "unknown"),
                    latency_ms=data.get("latency_ms", 0),
                    cached=True,
                )
            except Exception as err:
                # Corrupted cache file is treated as a miss
                if self.mode == "replay":
                    msg = f"LLM cache miss in replay mode for key {key} (corrupted entry)"
                    raise CacheMiss(msg) from err
                return None

        if self.mode == "replay":
            raise CacheMiss(f"LLM cache miss in replay mode for key {key}")

        return None

    def put(self, key: str, result: LLMResult) -> None:
        """Save result to cache file atomically in record mode."""
        if self.mode != "record":
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._file_path(key)

        data = {
            "text": result.text,
            "tool_calls": result.tool_calls,
            "usage": result.usage,
            "provider": result.provider,
            "model": result.model,
            "latency_ms": result.latency_ms,
            "cached": True,
        }

        # Write to temporary file in same directory and replace atomically
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path_str, path)
        except Exception:
            if os.path.exists(tmp_path_str):
                try:
                    os.unlink(tmp_path_str)
                except OSError:
                    pass
