"""Resolver loop executing allowed tools with safety, budgeting, and trace tracking."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sellershield_agent.domain.models import ReturnReasonCode
from sellershield_agent.logging import get_logger
from sellershield_agent.store.client import RequestContext
from sellershield_agent.tools.base import ToolErrorCode, ToolResult
from sellershield_agent.tools.gateway import ToolGateway

logger = get_logger("sellershield_agent.orchestrator.resolver")


class ResolverBudgetExceeded(Exception):
    """Raised when resolver tool budget or time limit is exhausted."""

    pass


class ResolverLoop:
    """Executes function calling / tool resolution within strict budgets."""

    def __init__(
        self,
        gateway: ToolGateway,
        max_iterations: int = 5,
        max_tool_calls: int = 8,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.gateway = gateway
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds

    async def execute(
        self,
        ctx: RequestContext,
        intent: str,
        allowed_tools: list[str],
        slots: dict[str, Any],
        session_id: str,
        lang: str = "en",
        status_callback: Callable[[str, str], Awaitable[None]] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> list[ToolResult]:
        """Run tool calling loop for the given intent and slots."""
        start_time = time.monotonic()
        executed_calls: set[str] = set()
        results: list[ToolResult] = []
        tool_call_count = 0

        # State initial plan in trace
        plan_desc = f"Resolve {intent} using allowed tools: {', '.join(allowed_tools)}"
        if trace is not None:
            trace["plan"] = plan_desc
            trace["tool_calls"] = []

        # Determine tools to invoke based on intent and available slots
        order_id = slots.get("order_id")

        tool_sequence: list[tuple[str, dict[str, Any]]] = []
        if intent in ("order_status", "shipping_status"):
            if order_id:
                tool_sequence.append(("get_shipping_status", {"order_id": order_id}))
                tool_sequence.append(("get_order", {"order_id": order_id}))
            else:
                tool_sequence.append(("list_my_orders", {}))
        elif intent in ("refund_status", "refund_request"):
            refund_id = slots.get("refund_id")
            if refund_id:
                rfd_args: dict[str, Any] = {"refund_id": refund_id}
                if order_id:
                    rfd_args["order_id"] = order_id
                tool_sequence.append(("get_refund_status", rfd_args))
                if order_id:
                    tool_sequence.append(("get_order", {"order_id": order_id}))
            elif order_id:
                tool_sequence.append(("get_refund_status", {"order_id": order_id}))
                tool_sequence.append(("get_order", {"order_id": order_id}))
            else:
                tool_sequence.append(("list_my_orders", {}))
        elif intent in ("return_status", "return_request"):
            return_id = slots.get("return_id")
            if slots.get("return_status_only") or return_id:
                ret_args: dict[str, Any] = {}
                if return_id:
                    ret_args["return_id"] = return_id
                if order_id:
                    ret_args["order_id"] = order_id
                if ret_args:
                    tool_sequence.append(("get_return_status", ret_args))
                    if order_id:
                        tool_sequence.append(("get_order", {"order_id": order_id}))
                else:
                    tool_sequence.append(("list_my_orders", {}))
            elif order_id:
                return await self._execute_return_workflow(
                    ctx=ctx,
                    order_id=order_id,
                    allowed_tools=allowed_tools,
                    slots=slots,
                    session_id=session_id,
                    lang=lang,
                    status_callback=status_callback,
                    trace=trace,
                )
            else:
                tool_sequence.append(("list_my_orders", {}))
        elif intent == "policy_question":
            tool_sequence.append(("get_policy_summary", {}))

        # Filter against allowed_tools
        filtered_sequence = [
            (t_name, args) for t_name, args in tool_sequence if t_name in allowed_tools
        ]

        iteration = 0
        for tool_name, raw_args in filtered_sequence:
            iteration += 1
            if iteration > self.max_iterations:
                break
            if tool_call_count >= self.max_tool_calls:
                break
            if time.monotonic() - start_time > self.timeout_seconds:
                break

            call_key = f"{tool_name}:{json.dumps(raw_args, sort_keys=True)}"
            if call_key in executed_calls:
                # Anti-loop protection: identical repeated call blocked
                continue
            executed_calls.add(call_key)

            if status_callback:
                await status_callback(tool_name, "calling")

            tool_call_count += 1
            try:
                res = await self.gateway.call(
                    ctx=ctx,
                    name=tool_name,
                    raw_args=raw_args,
                    allowed=allowed_tools,
                    session_id=session_id,
                    lang=lang,
                )
                results.append(res)
                st = "completed" if res.ok else "failed"
                if status_callback:
                    await status_callback(tool_name, st)

                if trace is not None and isinstance(trace.get("tool_calls"), list):
                    trace["tool_calls"].append(
                        {
                            "tool": tool_name,
                            "args": raw_args,
                            "ok": res.ok,
                            "error_code": res.error_code,
                        }
                    )

                if not res.ok and res.error_code == ToolErrorCode.invalid_args:
                    break

            except Exception as e:
                logger.warning("Tool invocation failed", tool=tool_name, error=str(e))
                if status_callback:
                    await status_callback(tool_name, "failed")

        return results

    async def _execute_return_workflow(
        self,
        ctx: RequestContext,
        order_id: str,
        allowed_tools: list[str],
        slots: dict[str, Any],
        session_id: str,
        lang: str = "en",
        status_callback: Callable[[str, str], Awaitable[None]] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> list[ToolResult]:
        results: list[ToolResult] = []

        # 1. Fetch Order
        if "get_order" in allowed_tools:
            if status_callback:
                await status_callback("get_order", "calling")
            order_res = await self.gateway.call(
                ctx=ctx,
                name="get_order",
                raw_args={"order_id": order_id},
                allowed=allowed_tools,
                session_id=session_id,
                lang=lang,
            )
            results.append(order_res)
            if status_callback:
                await status_callback("get_order", "completed" if order_res.ok else "failed")
            if trace is not None and isinstance(trace.get("tool_calls"), list):
                trace["tool_calls"].append(
                    {
                        "tool": "get_order",
                        "args": {"order_id": order_id},
                        "ok": order_res.ok,
                        "error_code": order_res.error_code,
                    }
                )
            if not order_res.ok or not isinstance(order_res.data, dict):
                return results
            order_items = order_res.data.get("items", [])
        else:
            order_items = []

        # 2. Extract user intent parameters
        user_text = str(slots.get("user_text") or "")
        lower_text = user_text.lower()

        # Clean text by removing order ids and currency amounts to parse quantities safely
        clean_text = re.sub(r"\bORD-\d+\b", "", lower_text, flags=re.IGNORECASE)
        clean_text = re.sub(
            r"(?:refund\s+of\s*)?(?:₹|rs\.?|inr)\s*[\d,]+|\brefund\s+of\s*[\d,]+",
            "",
            clean_text,
            flags=re.IGNORECASE,
        )
        qty_matches = re.findall(r"\b(\d+)\b", clean_text)
        requested_qty: int | None = int(qty_matches[0]) if qty_matches else None

        # Determine matching items from order
        matching_items = []
        for item in order_items:
            name = item.get("name", "").lower()
            words = [
                w
                for w in re.findall(r"\b[a-z]{4,}\b", name)
                if w not in ("with", "from", "item")
            ]
            if any(w in clean_text for w in words):
                matching_items.append(item)

        chosen_items = matching_items if matching_items else order_items
        req_items = []
        for item in chosen_items:
            item_id = item.get("item_id")
            available_qty = int(item.get("qty", 1))
            if requested_qty is not None and len(chosen_items) == 1:
                item_qty = requested_qty
            else:
                item_qty = available_qty
            req_items.append({"item_id": item_id, "qty": item_qty})

        # 3. Determine reason code
        reason_code = ReturnReasonCode.changed_mind
        if any(w in lower_text for w in ["defective", "kharab", "faulty", "not working"]):
            reason_code = ReturnReasonCode.defective
        elif any(w in lower_text for w in ["damaged", "broken", "toota", "cracked", "shattered"]):
            reason_code = ReturnReasonCode.damaged
        elif any(w in lower_text for w in ["wrong", "galat"]):
            reason_code = ReturnReasonCode.wrong_item
        elif any(w in lower_text for w in ["not as described", "different"]):
            reason_code = ReturnReasonCode.not_as_described
        elif any(w in lower_text for w in ["late", "delayed"]):
            reason_code = ReturnReasonCode.arrived_late
        elif any(w in lower_text for w in ["changed mind", "changed my mind", "unwanted"]):
            reason_code = ReturnReasonCode.changed_mind
        elif slots.get("reason_code"):
            try:
                reason_code = ReturnReasonCode(str(slots["reason_code"]))
            except Exception:
                pass

        # 4. Determine refund method & evidence
        refund_method: str | None = None
        if any(w in lower_text for w in ["upi", "gpay", "phonepe", "paytm"]):
            refund_method = "upi"
        elif any(w in lower_text for w in ["credit", "store credit", "wallet"]):
            refund_method = "store_credit"

        evidence_ids = slots.get("evidence_ids") or None

        # 5. Propose return or check eligibility
        qty_invalid = any(
            int(req.get("qty", 1)) <= 0 or int(req.get("qty", 1)) > int(item.get("qty", 1))
            for req, item in zip(req_items, chosen_items, strict=False)
        )
        if "propose_return" in allowed_tools and not qty_invalid:
            if status_callback:
                await status_callback("propose_return", "calling")
            propose_args: dict[str, Any] = {
                "order_id": order_id,
                "items": req_items,
                "reason_code": reason_code.value,
                "reason_text": user_text[:200] if user_text else None,
            }
            if evidence_ids:
                propose_args["evidence_ids"] = evidence_ids
            if refund_method:
                propose_args["refund_method"] = refund_method

            prop_res = await self.gateway.call(
                ctx=ctx,
                name="propose_return",
                raw_args=propose_args,
                allowed=allowed_tools,
                session_id=session_id,
                lang=lang,
            )
            results.append(prop_res)
            if status_callback:
                await status_callback("propose_return", "completed" if prop_res.ok else "failed")
            if trace is not None and isinstance(trace.get("tool_calls"), list):
                trace["tool_calls"].append(
                    {
                        "tool": "propose_return",
                        "args": propose_args,
                        "ok": prop_res.ok,
                        "error_code": prop_res.error_code,
                    }
                )
        elif "check_return_eligibility" in allowed_tools:
            if status_callback:
                await status_callback("check_return_eligibility", "calling")
            elig_args = {
                "order_id": order_id,
                "items": req_items,
                "reason_code": reason_code.value,
                "reason_text": user_text[:200] if user_text else None,
            }
            elig_res = await self.gateway.call(
                ctx=ctx,
                name="check_return_eligibility",
                raw_args=elig_args,
                allowed=allowed_tools,
                session_id=session_id,
                lang=lang,
            )
            results.append(elig_res)
            if status_callback:
                await status_callback(
                    "check_return_eligibility",
                    "completed" if elig_res.ok else "failed",
                )
            if trace is not None and isinstance(trace.get("tool_calls"), list):
                trace["tool_calls"].append(
                    {
                        "tool": "check_return_eligibility",
                        "args": elig_args,
                        "ok": elig_res.ok,
                        "error_code": elig_res.error_code,
                    }
                )

        return results
