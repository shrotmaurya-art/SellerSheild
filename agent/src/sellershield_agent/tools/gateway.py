"""Centralized Tool Gateway enforcing all security and operational policies.

Spec §6.1, §6.4, §13, §21 (Amendment 1):
- Allowlist verification
- Strict argument validation (extra fields forbidden)
- ID format sanity checks
- Customer-scoped authority only
- 8-second execution timeout
- Response minimization and store error mapping
- Single tamper-evident audit event per call written strictly after store execution
- Concurrency-capped parallel dispatch (call_many)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy.engine import Engine

from sellershield_agent.audit.models import ActorType, AuditEvent, EventType, Outcome
from sellershield_agent.audit.service import AuditService
from sellershield_agent.clock import Clock
from sellershield_agent.db import write_tx
from sellershield_agent.logging import get_logger
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.policy.models import PolicyDocument
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
)
from sellershield_agent.tools.base import (
    Tool,
    ToolContext,
    ToolErrorCode,
    ToolKind,
    ToolResult,
)
from sellershield_agent.tools.cache import OrderDigestCache
from sellershield_agent.tools.messages import get_customer_safe_message
from sellershield_agent.tools.propose_tools import build_propose_tools
from sellershield_agent.tools.read_tools import build_read_tools

logger = get_logger("sellershield_agent.tools.gateway")

ORDER_ID_PATTERN = re.compile(r"^ORD-[0-9]{3,6}$")
RETURN_ID_PATTERN = re.compile(r"^RET-[0-9]{3,6}$")
REFUND_ID_PATTERN = re.compile(r"^RFD-[0-9]{3,6}$")


class ToolGateway:
    """The singular gateway through which all agent tools run."""

    def __init__(
        self,
        store_client: StoreClient,
        engine: Engine,
        clock: Clock,
        policy: PolicyDocument | None = None,
        service_ctx_factory: Callable[[str], RequestContext] | None = None,
        tools: dict[str, Tool] | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.store_client = store_client
        self.engine = engine
        self.clock = clock
        self.policy = policy if policy is not None else load_policy()
        self.service_ctx_factory = service_ctx_factory
        self.tools = tools if tools is not None else {**build_read_tools(), **build_propose_tools()}
        self.timeout_seconds = timeout_seconds
        self.digest_cache = OrderDigestCache(clock=clock, ttl_seconds=60.0)

    def invalidate_session(self, session_id: str) -> None:
        """Invalidate order digest cache for a specific session."""
        self.digest_cache.invalidate_session(session_id)

    def invalidate_order(self, order_id: str) -> None:
        """Invalidate order digest cache entries matching an order ID."""
        self.digest_cache.invalidate(order_id)

    async def call(
        self,
        ctx: RequestContext,
        name: str,
        raw_args: dict[str, Any],
        allowed: set[str] | list[str],
        session_id: str | None = None,
        lang: str = "en",
    ) -> ToolResult:
        """Execute a single tool with complete safety and audit guarantees."""
        start_time = time.perf_counter()
        allowed_set = set(allowed)

        outcome: Outcome = Outcome.SUCCESS
        event_type: EventType = EventType.TOOL_CALL
        error_code: ToolErrorCode | None = None
        result: ToolResult | None = None

        arg_canonical = json.dumps(raw_args, sort_keys=True, separators=(",", ":"))
        arg_hash = hashlib.sha256(arg_canonical.encode("utf-8")).hexdigest()

        details: dict[str, Any] = {
            "tool": name,
            "arg_hash": arg_hash,
        }
        if "order_id" in raw_args:
            details["order_id"] = str(raw_args["order_id"])

        try:
            # 1. Allowlist check
            if name not in allowed_set or name not in self.tools:
                outcome = Outcome.DENIED
                event_type = EventType.AUTH_DENIED
                error_code = ToolErrorCode.not_permitted
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                )
                return result

            tool = self.tools[name]

            # 2. Strict argument validation (extra='forbid')
            try:
                validated_args = tool.args_model.model_validate(raw_args)
            except Exception:
                outcome = Outcome.FAILED
                error_code = ToolErrorCode.invalid_args
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                )
                return result

            # 3. ID sanity check
            for field_name, pattern in [
                ("order_id", ORDER_ID_PATTERN),
                ("return_id", RETURN_ID_PATTERN),
                ("refund_id", REFUND_ID_PATTERN),
            ]:
                val = getattr(validated_args, field_name, None)
                if val is not None and isinstance(val, str) and val.strip():
                    if not pattern.match(val):
                        outcome = Outcome.FAILED
                        error_code = ToolErrorCode.invalid_args
                        result = ToolResult(
                            ok=False,
                            error_code=error_code,
                            error_message=get_customer_safe_message(error_code, lang),
                        )
                        return result

            # 4. Digest cache lookup (READ tools only; PROPOSE must check live DB dedup/limits)
            cache_key = f"{name}:{arg_canonical}"
            if session_id and tool.kind == ToolKind.READ:
                cached_data = self.digest_cache.get(session_id, cache_key)
                if cached_data is not None:
                    result = cached_data
                    return result

            # 5. Handler execution with customer context only and 8s timeout
            tool_ctx = ToolContext(
                store_client=self.store_client,
                customer_ctx=ctx,
                clock=self.clock,
                policy=self.policy,
                service_ctx_factory=self.service_ctx_factory,
                session_id=session_id,
                engine=self.engine,
            )

            try:
                async with asyncio.timeout(self.timeout_seconds):
                    result = await tool.handler(tool_ctx, validated_args)
                    if not result.ok:
                        outcome = Outcome.FAILED
                        error_code = result.error_code
            except StoreNotFound:
                outcome = Outcome.FAILED
                error_code = ToolErrorCode.not_found
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                )
            except (StoreTimeout, TimeoutError):
                outcome = Outcome.FAILED
                error_code = ToolErrorCode.upstream_unavailable
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                    retryable=True,
                )
            except StoreUnavailable:
                outcome = Outcome.FAILED
                error_code = ToolErrorCode.upstream_unavailable
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                    retryable=True,
                )
            except Exception:
                logger.exception("tool_handler_error", tool=name, request_id=ctx.request_id)
                outcome = Outcome.FAILED
                error_code = ToolErrorCode.internal
                result = ToolResult(
                    ok=False,
                    error_code=error_code,
                    error_message=get_customer_safe_message(error_code, lang),
                    retryable=False,
                )

            # Store in cache on success (READ tools only)
            if result.ok and session_id and tool.kind == ToolKind.READ:
                order_ids: set[str] = set()
                if hasattr(validated_args, "order_id") and validated_args.order_id:
                    order_ids.add(validated_args.order_id)
                self.digest_cache.set(session_id, cache_key, result, order_ids=order_ids)

            return result

        finally:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            details["latency_ms"] = latency_ms
            details["status"] = outcome.value
            if error_code is not None:
                details["error_code"] = error_code.value

            actor_id = ctx.principal.customer_id or ctx.principal.user_id
            event = AuditEvent(
                event_id=f"evt-{uuid4().hex[:12]}",
                ts_utc=self.clock.now().isoformat(),
                request_id=ctx.request_id,
                session_id=session_id,
                actor_type=ActorType.CUSTOMER,
                actor_id=actor_id,
                event_type=event_type,
                action="tool_call",
                resource_type="tool",
                resource_id=name,
                outcome=outcome,
                policy_version=self.policy.version,
                details=details,
            )

            # Append audit event and persist telemetry in an independent short transaction
            from sellershield_agent.domain.state import (
                generate_tool_call_id,
                sanitize_policy_facts,
                tool_calls,
            )

            call_id = generate_tool_call_id()
            clean_args = sanitize_policy_facts(raw_args)
            args_hash = hashlib.sha256(
                json.dumps(clean_args, sort_keys=True).encode("utf-8")
            ).hexdigest()
            tool_status = "success" if (result is not None and result.ok) else "failed"

            with write_tx(self.engine) as tx:
                tx.execute(
                    tool_calls.insert().values(
                        call_id=call_id,
                        request_id=ctx.request_id,
                        session_id=session_id,
                        tool=name,
                        args_hash=args_hash,
                        args_redacted_json=json.dumps(clean_args, sort_keys=True),
                        status=tool_status,
                        error_code=error_code.value if error_code else None,
                        latency_ms=int(latency_ms),
                        created_at=event.ts_utc,
                    )
                )
                AuditService.append(tx, event)

    async def call_many(
        self,
        ctx: RequestContext,
        calls: list[tuple[str, dict[str, Any]]],
        allowed: set[str] | list[str],
        session_id: str | None = None,
        lang: str = "en",
    ) -> list[ToolResult]:
        """Dispatch independent tool calls concurrently with concurrency capped at 4."""
        semaphore = asyncio.Semaphore(4)

        async def _bounded_call(tool_name: str, args: dict[str, Any]) -> ToolResult:
            async with semaphore:
                return await self.call(
                    ctx=ctx,
                    name=tool_name,
                    raw_args=args,
                    allowed=allowed,
                    session_id=session_id,
                    lang=lang,
                )

        return await asyncio.gather(*[_bounded_call(name, args) for name, args in calls])
