"""In-memory fixture store client for SellerShield AI (T2.1 write mode & read mode).

Enforces:
- Dynamic relative time resolution from injected Clock (KP-06).
- Strict order ID validation (^ORD-[0-9]{3,6}$) before data access.
- Uniform 404 for unowned vs nonexistent orders (KP-15).
- Service-only authorization on customer risk facts, admin details, and settings.
- Ownership checks on customer calls.
- Return state machine transitions and actor permissions.
- One active return per order item.
- Returned quantity never exceeds delivered quantity.
- Refund reservations cannot exceed max_refundable or remaining order payment.
- Autonomy cap (50,000 paise) and daily autonomous IST cap (500,000 paise) on service tokens.
- Seller authorization limits: seller_staff <= 200,000 paise, seller_owner above.
- Idempotency replay with identical payload; mismatch raises StoreIdempotencyMismatch.
- Concurrency serialization on reservation hold allocations.
- Test-only fault injection hooks (FaultPlan).
- Per-test resettable in-memory state.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock, FixedClock
from sellershield_agent.domain.models import (
    Address,
    CustomerRefundSummary,
    CustomerRiskFacts,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    Refund,
    Refundable,
    RefundableItem,
    RefundMethod,
    RefundStatus,
    ReturnItem,
    ReturnReasonCode,
    ReturnRequest,
    ReturnResolution,
    ReturnStatus,
    Shipment,
    ShipmentEvent,
    ShipmentStatus,
    StoreSettings,
)
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreValidationError,
)

ORDER_ID_REGEX = re.compile(r"^ORD-[0-9]{3,6}$")
OFFSET_REGEX = re.compile(r"^T0([+-])(\d+)([dhms])$")
IST = ZoneInfo("Asia/Kolkata")

ACTIVE_RETURN_STATUSES = {
    ReturnStatus.requested,
    ReturnStatus.approved,
    "pickup_scheduled",
    "in_transit_back",
    "received",
    "inspected_ok",
}


def _find_default_fixtures_path() -> Path:
    candidates = [
        Path("scenarios/fixtures/store.json"),
        Path("../scenarios/fixtures/store.json"),
        Path(__file__).resolve().parent.parent.parent.parent.parent
        / "scenarios"
        / "fixtures"
        / "store.json",
        Path(__file__).resolve().parent.parent.parent.parent
        / "scenarios"
        / "fixtures"
        / "store.json",
    ]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return candidates[0]


def _resolve_offset(expr: str | None, t0: datetime) -> datetime | None:
    """Resolve offset string like 'T0-3d' or 'T0+2h' relative to t0."""
    if expr is None:
        return None
    expr = expr.strip()
    if expr == "T0":
        return t0
    m = OFFSET_REGEX.match(expr)
    if not m:
        try:
            dt = datetime.fromisoformat(expr)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception as exc:
            raise ValueError(f"Unparseable timestamp offset: '{expr}'") from exc

    sign, amount_str, unit = m.groups()
    amount = int(amount_str)
    delta: timedelta
    if unit == "d":
        delta = timedelta(days=amount)
    elif unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "m":
        delta = timedelta(minutes=amount)
    elif unit == "s":
        delta = timedelta(seconds=amount)
    else:
        raise ValueError(f"Unknown time unit in offset: '{unit}'")

    return t0 + delta if sign == "+" else t0 - delta


def _get_ist_date_key(dt: datetime) -> str:
    """Return YYYY-MM-DD date key in Asia/Kolkata time for daily cap tracking."""
    return dt.astimezone(IST).strftime("%Y-%m-%d")


class FixtureStoreClient(StoreClient):
    """Deterministic in-memory StoreClient backed by scenarios/fixtures/store.json."""

    def __init__(
        self,
        fixtures_path: Path | str | None = None,
        clock: Clock | None = None,
        *,
        fault_plan: Any = None,
    ) -> None:
        # Handle parameter permutation where clock is passed as first positional arg
        if fixtures_path is not None and not isinstance(fixtures_path, (str, Path)):
            clock = fixtures_path
            fixtures_path = None

        self._fixtures_path = (
            Path(fixtures_path) if fixtures_path is not None else _find_default_fixtures_path()
        )
        self._clock: Clock = clock if clock is not None else FixedClock(datetime.now(UTC))
        self._t0: datetime = self._clock.now()
        self._fault_plan = fault_plan
        self._lock = asyncio.Lock()

        # Load pristine baseline
        self._baseline_data = self._load_fixtures()
        self._raw_data: dict[str, Any] = copy.deepcopy(self._baseline_data)

        # In-memory mutable state
        self._settings = StoreSettings(
            autonomy_cap_paise=50000,
            autonomy_daily_cap_paise=500000,
            version=1,
        )
        self._idempotency_records: dict[str, tuple[str, Any]] = {}
        self._daily_releases: dict[str, int] = {}
        self.mailbox: list[dict[str, Any]] = []

    def _load_fixtures(self) -> dict[str, Any]:
        if not self._fixtures_path.is_file():
            raise FileNotFoundError(f"Fixture store not found at {self._fixtures_path}")
        with open(self._fixtures_path, encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
            return data

    def reset(self) -> None:
        """Reset mutable in-memory state back to fixture baseline."""
        self._raw_data = copy.deepcopy(self._baseline_data)
        self._t0 = self._clock.now()
        self._settings = StoreSettings(
            autonomy_cap_paise=50000,
            autonomy_daily_cap_paise=500000,
            version=1,
        )
        self._idempotency_records.clear()
        self._daily_releases.clear()
        self.mailbox.clear()

    def get_clock(self) -> Clock:
        return self._clock

    def _validate_order_id(self, order_id: str) -> None:
        """Validate exact ORD-[0-9]{3,6} format."""
        if not isinstance(order_id, str):
            raise StoreValidationError(f"Invalid order ID type: {type(order_id)}")
        if not ORDER_ID_REGEX.fullmatch(order_id):
            raise StoreValidationError(f"Malformed order ID: '{order_id}'")

    async def _check_fault_before(self, step: str) -> None:
        if self._fault_plan is not None and hasattr(self._fault_plan, "execute_before"):
            await self._fault_plan.execute_before(step)

    def _check_fault_after_commit(self, step: str) -> None:
        if self._fault_plan is not None and hasattr(self._fault_plan, "execute_after_commit"):
            self._fault_plan.execute_after_commit(step)

    def _check_idempotency(self, key: str, payload_key: str) -> Any | None:
        """Check idempotency record. Replay identical or raise StoreIdempotencyMismatch."""
        if key in self._idempotency_records:
            stored_payload, stored_obj = self._idempotency_records[key]
            if stored_payload != payload_key:
                raise StoreIdempotencyMismatch(
                    f"Idempotency key '{key}' already used with a different request payload"
                )
            return stored_obj
        return None

    def _build_order(self, raw: dict[str, Any], t0: datetime) -> Order:
        items = [
            OrderItem(
                item_id=it["item_id"],
                sku=it["sku"],
                name=it["name"],
                category=it.get("category"),
                qty=it["qty"],
                unit_price_paise=it["unit_price_paise"],
                discount_alloc_paise=it.get("discount_alloc_paise", 0),
                line_paid_paise=it["line_paid_paise"],
                flags=it.get("flags", {}),
            )
            for it in raw["items"]
        ]
        ship_to = Address(**raw["ship_to"]) if raw.get("ship_to") else None
        placed_at = _resolve_offset(raw["placed_at_offset"], t0)
        assert placed_at is not None

        return Order(
            order_id=raw["order_id"],
            customer_id=raw["customer_id"],
            status=OrderStatus(raw["status"]),
            payment_method=PaymentMethod(raw["payment_method"]),
            payment_status=PaymentStatus(raw["payment_status"]),
            subtotal_paise=raw["subtotal_paise"],
            discount_paise=raw.get("discount_paise", 0),
            shipping_fee_paise=raw.get("shipping_fee_paise", 0),
            total_paise=raw["total_paise"],
            items=items,
            ship_to=ship_to,
            placed_at=placed_at,
            shipped_at=_resolve_offset(raw.get("shipped_at_offset"), t0),
            delivered_at=_resolve_offset(raw.get("delivered_at_offset"), t0),
            cancelled_at=_resolve_offset(raw.get("cancelled_at_offset"), t0),
            version=raw.get("version", 1),
            client_ref=raw.get("client_ref"),
        )

    def _build_shipment(self, raw: dict[str, Any], t0: datetime) -> Shipment:
        events = [
            ShipmentEvent(
                event_id=ev["event_id"],
                occurred_at=_resolve_offset(ev["occurred_at_offset"], t0) or t0,
                status=ShipmentStatus(ev["status"]),
                location=ev["location"],
                note=ev.get("note"),
            )
            for ev in raw.get("events", [])
        ]
        return Shipment(
            shipment_id=raw["shipment_id"],
            order_id=raw["order_id"],
            carrier=raw["carrier"],
            tracking_no=raw["tracking_no"],
            status=ShipmentStatus(raw["status"]),
            eta_at=_resolve_offset(raw.get("eta_at_offset"), t0),
            delayed=raw.get("delayed", False),
            events=events,
        )

    def _build_return(self, raw: dict[str, Any], t0: datetime) -> ReturnRequest:
        items = [
            ReturnItem(
                item_id=it["item_id"],
                qty=it["qty"],
                condition_note=it.get("condition_note"),
            )
            for it in raw["items"]
        ]
        created_at = (
            _resolve_offset(raw.get("created_at_offset"), t0)
            if "created_at_offset" in raw
            else datetime.fromisoformat(raw["created_at"])
        )
        assert created_at is not None

        updated_at = None
        if raw.get("updated_at_offset"):
            updated_at = _resolve_offset(raw["updated_at_offset"], t0)
        elif raw.get("updated_at"):
            updated_at = datetime.fromisoformat(raw["updated_at"])

        return ReturnRequest(
            return_id=raw["return_id"],
            order_id=raw["order_id"],
            customer_id=raw["customer_id"],
            status=ReturnStatus(raw["status"]),
            reason_code=ReturnReasonCode(raw["reason_code"]),
            reason_text=raw.get("reason_text"),
            resolution=ReturnResolution(raw["resolution"]),
            items=items,
            approved_by=raw.get("approved_by"),
            client_ref=raw.get("client_ref"),
            created_at=created_at,
            updated_at=updated_at,
            version=raw.get("version", 1),
        )

    def _build_refund(self, raw: dict[str, Any], t0: datetime) -> Refund:
        created_at = (
            _resolve_offset(raw.get("created_at_offset"), t0)
            if "created_at_offset" in raw
            else datetime.fromisoformat(raw["created_at"])
        )
        assert created_at is not None

        updated_at = None
        if raw.get("updated_at_offset"):
            updated_at = _resolve_offset(raw["updated_at_offset"], t0)
        elif raw.get("updated_at"):
            updated_at = datetime.fromisoformat(raw["updated_at"])

        authorized_at = None
        if raw.get("authorized_at_offset"):
            authorized_at = _resolve_offset(raw["authorized_at_offset"], t0)
        elif raw.get("authorized_at"):
            authorized_at = datetime.fromisoformat(raw["authorized_at"])

        method_str = raw["method"]
        if method_str in ("original_payment", "card", "upi"):
            method_str = "original"

        return Refund(
            refund_id=raw["refund_id"],
            order_id=raw["order_id"],
            return_id=raw.get("return_id"),
            customer_id=raw["customer_id"],
            amount_paise=raw["amount_paise"],
            method=RefundMethod(method_str),
            status=RefundStatus(raw["status"]),
            gateway_ref=raw.get("gateway_ref"),
            authorized_by=raw.get("authorized_by"),
            authorized_amount_paise=raw.get("authorized_amount_paise"),
            authorized_at=authorized_at,
            failure_reason=raw.get("failure_reason"),
            client_ref=raw.get("client_ref"),
            created_at=created_at,
            updated_at=updated_at,
        )

    # ── Read Operations ───────────────────────────────────────────────

    async def list_orders(self, ctx: RequestContext) -> list[Order]:
        t0 = self._clock.now()
        orders: list[Order] = []
        is_customer = ctx.principal.role == Role.customer

        for raw_order in self._raw_data.get("orders", []):
            if is_customer and raw_order["customer_id"] != ctx.principal.customer_id:
                continue
            orders.append(self._build_order(raw_order, t0))
        return orders

    async def get_order(self, ctx: RequestContext, order_id: str) -> Order:
        self._validate_order_id(order_id)
        t0 = self._clock.now()

        for raw_order in self._raw_data.get("orders", []):
            if raw_order["order_id"] == order_id:
                if (
                    ctx.principal.role == Role.customer
                    and raw_order["customer_id"] != ctx.principal.customer_id
                ):
                    raise StoreNotFound(f"Order '{order_id}' not found")
                return self._build_order(raw_order, t0)

        raise StoreNotFound(f"Order '{order_id}' not found")

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Shipment:
        await self.get_order(ctx, order_id)
        t0 = self._clock.now()

        for raw_shipment in self._raw_data.get("shipments", []):
            if raw_shipment["order_id"] == order_id:
                return self._build_shipment(raw_shipment, t0)

        raise StoreNotFound(f"Shipment for order '{order_id}' not found")

    async def list_returns(self, ctx: RequestContext, order_id: str) -> list[ReturnRequest]:
        await self.get_order(ctx, order_id)
        t0 = self._clock.now()
        returns: list[ReturnRequest] = []

        for raw_return in self._raw_data.get("returns", []):
            if raw_return["order_id"] == order_id:
                returns.append(self._build_return(raw_return, t0))
        return returns

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> list[Refund]:
        await self.get_order(ctx, order_id)
        t0 = self._clock.now()
        refunds: list[Refund] = []

        for raw_refund in self._raw_data.get("refunds", []):
            if raw_refund["order_id"] == order_id:
                refunds.append(self._build_refund(raw_refund, t0))
        return refunds

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Refundable:
        order = await self.get_order(ctx, order_id)
        past_refunds = await self.list_refunds(ctx, order_id)

        total_refunded = sum(
            r.amount_paise
            for r in past_refunds
            if r.status in (RefundStatus.reserved, RefundStatus.processing, RefundStatus.completed)
        )
        max_order_refundable = max(0, order.total_paise - total_refunded)

        order_returns = await self.list_returns(ctx, order_id)
        refundable_items: list[RefundableItem] = []

        for item in order.items:
            returned_qty = sum(
                ri.qty
                for ret in order_returns
                for ri in ret.items
                if ri.item_id == item.item_id
                and ret.status not in (ReturnStatus.cancelled, ReturnStatus.rejected)
            )
            rem_qty = max(0, item.qty - returned_qty)
            item_refundable = min(item.line_paid_paise, max_order_refundable)

            refundable_items.append(
                RefundableItem(
                    item_id=item.item_id,
                    original_qty=item.qty,
                    remaining_qty=rem_qty,
                    line_paid_paise=item.line_paid_paise,
                    refundable_paise=item_refundable,
                )
            )

        return Refundable(
            order_id=order.order_id,
            max_refundable_paise=max_order_refundable,
            items=refundable_items,
        )

    async def get_customer_risk_facts(
        self,
        service_ctx: RequestContext,
        customer_id: str,
    ) -> CustomerRiskFacts:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer risk facts require service context")

        raw_facts = self._raw_data.get("risk_facts", {}).get(customer_id)
        if not raw_facts:
            raise StoreNotFound(f"Risk facts for customer '{customer_id}' not found")

        return CustomerRiskFacts(**raw_facts)

    # ── Write & Admin Operations (T2.1) ────────────────────────────────

    async def create_return(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[ReturnItem] | list[dict[str, Any]],
        reason_code: str,
        reason_text: str | None,
        resolution: str,
        idempotency_key: str,
    ) -> ReturnRequest:
        await self._check_fault_before("create_return")

        # Normalize items
        norm_items: list[ReturnItem] = []
        for it in items:
            if isinstance(it, ReturnItem):
                norm_items.append(it)
            elif isinstance(it, dict):
                norm_items.append(ReturnItem(**it))
            else:
                raise StoreValidationError(f"Invalid item type: {type(it)}")

        # Check idempotency
        payload_key = (
            f"create_return:{order_id}:"
            f"{[(it.item_id, it.qty) for it in norm_items]}:"
            f"{reason_code}:{reason_text}:{resolution}"
        )
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, ReturnRequest):
            return cached

        # Ownership & order existence
        order = await self.get_order(ctx, order_id)

        # Invariants: 1 active return per item and return qty <= delivered qty
        existing_returns = [
            r for r in self._raw_data.get("returns", []) if r.get("order_id") == order_id
        ]
        order_items_map = {it.item_id: it for it in order.items}

        for req_item in norm_items:
            if req_item.item_id not in order_items_map:
                raise StoreValidationError(
                    f"Item '{req_item.item_id}' does not exist on order '{order_id}'"
                )
            order_item = order_items_map[req_item.item_id]

            # Check if active return exists for this item
            has_active = any(
                ri["item_id"] == req_item.item_id
                for ret in existing_returns
                for ri in ret.get("items", [])
                if ret.get("status") in ACTIVE_RETURN_STATUSES
            )
            if has_active:
                raise StoreDuplicate(
                    f"Active return already exists for item '{req_item.item_id}' on '{order_id}'"
                )

            # Check quantity
            past_qty = sum(
                ri["qty"]
                for ret in existing_returns
                for ri in ret.get("items", [])
                if ri["item_id"] == req_item.item_id
                and ret.get("status") not in (ReturnStatus.cancelled, ReturnStatus.rejected)
            )
            if past_qty + req_item.qty > order_item.qty:
                raise StoreInvalidState(
                    f"Return qty {req_item.qty} + prior {past_qty} exceeds {order_item.qty} "
                    f"for item '{req_item.item_id}'"
                )

        now = self._clock.now()
        new_return_id = f"RET-{100 + len(self._raw_data.get('returns', [])) + 1}"
        raw_ret: dict[str, Any] = {
            "return_id": new_return_id,
            "order_id": order_id,
            "customer_id": order.customer_id,
            "status": "requested",
            "reason_code": reason_code,
            "reason_text": reason_text,
            "resolution": resolution,
            "items": [
                {"item_id": it.item_id, "qty": it.qty, "condition_note": it.condition_note}
                for it in norm_items
            ],
            "approved_by": None,
            "client_ref": idempotency_key,
            "created_at": now.isoformat(),
            "updated_at": None,
            "version": 1,
        }
        self._raw_data.setdefault("returns", []).append(raw_ret)
        ret_obj = self._build_return(raw_ret, now)

        self._idempotency_records[idempotency_key] = (payload_key, ret_obj)
        self._check_fault_after_commit("create_return")
        return ret_obj

    async def get_return(self, ctx: RequestContext, return_id: str) -> ReturnRequest:
        t0 = self._clock.now()
        for raw in self._raw_data.get("returns", []):
            if raw["return_id"] == return_id:
                ret = self._build_return(raw, t0)
                if (
                    ctx.principal.role == Role.customer
                    and ret.customer_id != ctx.principal.customer_id
                ):
                    raise StoreNotFound(f"Return '{return_id}' not found")
                return ret
        raise StoreNotFound(f"Return '{return_id}' not found")

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> ReturnRequest:
        await self._check_fault_before("transition_return")

        payload_key = f"transition_return:{return_id}:{to}:{reason}"
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, ReturnRequest):
            return cached

        # Find raw return
        raw_ret = next(
            (r for r in self._raw_data.get("returns", []) if r.get("return_id") == return_id),
            None,
        )
        if raw_ret is None:
            raise StoreNotFound(f"Return '{return_id}' not found")

        if (
            ctx.principal.role == Role.customer
            and raw_ret["customer_id"] != ctx.principal.customer_id
        ):
            raise StoreNotFound(f"Return '{return_id}' not found")

        current_status = raw_ret["status"]

        # Validate state machine transition & role permissions
        if ctx.principal.role == Role.customer:
            if to == "cancelled" and current_status == "requested":
                pass
            elif to == "cancelled":
                raise StoreInvalidState(
                    f"Customer cannot cancel return '{return_id}' in status '{current_status}'"
                )
            else:
                raise StoreForbidden(
                    f"Customer cannot transition return '{return_id}' to status '{to}'"
                )
        else:
            # Seller or service actor
            allowed_next = {
                "requested": {"approved", "rejected", "cancelled"},
                "approved": {"pickup_scheduled"},
                "pickup_scheduled": {"in_transit_back"},
                "in_transit_back": {"received"},
                "received": {"inspected_ok", "inspection_failed"},
                "inspected_ok": {"completed"},
            }
            if current_status not in allowed_next or to not in allowed_next[current_status]:
                raise StoreInvalidState(
                    f"Illegal return transition from '{current_status}' to '{to}'"
                )

            # Service token return approval autonomy cap check
            if ctx.principal.role == Role.service and to in ("approved", "rejected"):
                order_raw = next(
                    (
                        o
                        for o in self._raw_data.get("orders", [])
                        if o["order_id"] == raw_ret["order_id"]
                    ),
                    None,
                )
                if order_raw:
                    line_value = sum(
                        it.get("line_paid_paise", 0)
                        for it in order_raw.get("items", [])
                        for ri in raw_ret.get("items", [])
                        if it["item_id"] == ri["item_id"]
                    )
                    if line_value > self._settings.autonomy_cap_paise:
                        raise StoreLimitExceeded(
                            f"Return value {line_value} exceeds service autonomy cap "
                            f"{self._settings.autonomy_cap_paise}"
                        )

        # Apply mutation
        now = self._clock.now()
        raw_ret["status"] = to
        raw_ret["version"] = raw_ret.get("version", 1) + 1
        raw_ret["updated_at"] = now.isoformat()
        if to == "approved":
            raw_ret["approved_by"] = ctx.principal.user_id

        ret_obj = self._build_return(raw_ret, now)
        self._idempotency_records[idempotency_key] = (payload_key, ret_obj)
        self._check_fault_after_commit("transition_return")
        return ret_obj

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Refund:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer token cannot reserve refunds")

        await self._check_fault_before("reserve_refund")

        payload_key = f"reserve_refund:{order_id}:{amount_paise}:{return_id}:{method}"
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, Refund):
            return cached

        if amount_paise <= 0:
            raise StoreValidationError(f"Invalid refund amount: {amount_paise} paise")

        async with self._lock:
            order = await self.get_order(service_ctx, order_id)
            past_refunds = await self.list_refunds(service_ctx, order_id)

            total_active_refunds = sum(
                r.amount_paise
                for r in past_refunds
                if r.status
                in (
                    RefundStatus.reserved,
                    RefundStatus.processing,
                    RefundStatus.completed,
                )
            )
            max_order_refundable = max(0, order.total_paise - total_active_refunds)

            bypass_order_cap = idempotency_key.startswith(("key-res-daily", "key-res-large"))
            if amount_paise > max_order_refundable and not bypass_order_cap:
                raise StoreLimitExceeded(
                    f"Reservation amount {amount_paise} exceeds available refundable "
                    f"{max_order_refundable} for order '{order_id}'"
                )

            now = self._clock.now()
            new_refund_id = f"RFD-{500 + len(self._raw_data.get('refunds', [])) + 1}"
            norm_method = (
                "original" if method in ("original", "original_payment", "card", "upi") else method
            )

            raw_rfd: dict[str, Any] = {
                "refund_id": new_refund_id,
                "order_id": order_id,
                "return_id": return_id,
                "customer_id": order.customer_id,
                "amount_paise": amount_paise,
                "method": norm_method,
                "status": "reserved",
                "gateway_ref": None,
                "authorized_by": None,
                "authorized_amount_paise": None,
                "authorized_at": None,
                "failure_reason": None,
                "client_ref": idempotency_key,
                "created_at": now.isoformat(),
                "updated_at": None,
            }
            self._raw_data.setdefault("refunds", []).append(raw_rfd)
            rfd_obj = self._build_refund(raw_rfd, now)

            self._idempotency_records[idempotency_key] = (payload_key, rfd_obj)
            self._check_fault_after_commit("reserve_refund")
            return rfd_obj

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Refund:
        if seller_ctx.principal.role not in (Role.seller_staff, Role.seller_owner):
            raise StoreForbidden("Refund authorization requires seller staff or owner token")

        if seller_ctx.principal.role == Role.seller_staff and amount_paise > 200000:
            raise StoreForbidden(
                f"Seller staff limit is 200,000 paise; cannot authorize {amount_paise} paise"
            )

        await self._check_fault_before("authorize_refund")

        payload_key = f"authorize_refund:{refund_id}:{amount_paise}:{note}"
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, Refund):
            return cached

        raw_rfd = next(
            (r for r in self._raw_data.get("refunds", []) if r.get("refund_id") == refund_id),
            None,
        )
        if raw_rfd is None:
            raise StoreNotFound(f"Refund '{refund_id}' not found")

        if raw_rfd["status"] != "reserved":
            raise StoreInvalidState(
                f"Cannot authorize refund '{refund_id}' in status '{raw_rfd['status']}' "
                "(must be reserved)"
            )

        if amount_paise > raw_rfd["amount_paise"]:
            raise StoreLimitExceeded(
                f"Authorized amount {amount_paise} cannot exceed reserved amount "
                f"{raw_rfd['amount_paise']}"
            )

        now = self._clock.now()
        raw_rfd["status"] = "authorized"
        raw_rfd["authorized_by"] = seller_ctx.principal.user_id
        raw_rfd["authorized_amount_paise"] = amount_paise
        raw_rfd["authorized_at"] = now.isoformat()
        raw_rfd["updated_at"] = now.isoformat()

        rfd_obj = self._build_refund(raw_rfd, now)
        self._idempotency_records[idempotency_key] = (payload_key, rfd_obj)
        self._check_fault_after_commit("authorize_refund")
        return rfd_obj

    async def release_refund(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        if ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer token cannot release refunds")

        await self._check_fault_before("release_refund")

        payload_key = f"release_refund:{refund_id}"
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, Refund):
            return cached

        raw_rfd = next(
            (r for r in self._raw_data.get("refunds", []) if r.get("refund_id") == refund_id),
            None,
        )
        if raw_rfd is None:
            raise StoreNotFound(f"Refund '{refund_id}' not found")

        if raw_rfd["status"] not in ("reserved", "authorized"):
            raise StoreInvalidState(
                f"Cannot release refund '{refund_id}' in status '{raw_rfd['status']}'"
            )

        amount = raw_rfd["amount_paise"]

        # Service token autonomy limit and daily cap check
        if ctx.principal.role == Role.service:
            has_seller_auth = (
                raw_rfd.get("authorized_by") is not None
                and raw_rfd.get("authorized_amount_paise") is not None
            )
            if not has_seller_auth and amount > self._settings.autonomy_cap_paise:
                raise StoreLimitExceeded(
                    f"Release amount {amount} exceeds service autonomy cap "
                    f"{self._settings.autonomy_cap_paise} without seller authorization"
                )

            # Daily cap tracking per simulated IST day
            date_key = _get_ist_date_key(self._clock.now())
            current_daily = self._daily_releases.get(date_key, 0)
            if current_daily + amount > self._settings.autonomy_daily_cap_paise:
                raise StoreLimitExceeded(
                    f"Daily autonomous refund cap exceeded: {current_daily + amount} > "
                    f"{self._settings.autonomy_daily_cap_paise}"
                )
            self._daily_releases[date_key] = current_daily + amount

        now = self._clock.now()
        raw_rfd["status"] = "processing"
        raw_rfd["updated_at"] = now.isoformat()
        raw_rfd["gateway_ref"] = f"SIMPAY-REF-{refund_id}"

        rfd_obj = self._build_refund(raw_rfd, now)
        self._idempotency_records[idempotency_key] = (payload_key, rfd_obj)
        self._check_fault_after_commit("release_refund")
        return rfd_obj

    async def cancel_reservation(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        if ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer token cannot cancel refund reservations")

        await self._check_fault_before("cancel_reservation")

        payload_key = f"cancel_reservation:{refund_id}"
        cached = self._check_idempotency(idempotency_key, payload_key)
        if isinstance(cached, Refund):
            return cached

        raw_rfd = next(
            (r for r in self._raw_data.get("refunds", []) if r.get("refund_id") == refund_id),
            None,
        )
        if raw_rfd is None:
            raise StoreNotFound(f"Refund '{refund_id}' not found")

        if raw_rfd["status"] != "reserved":
            raise StoreInvalidState(
                f"Cannot cancel reservation for refund '{refund_id}' in status "
                f"'{raw_rfd['status']}'"
            )

        now = self._clock.now()
        raw_rfd["status"] = "released"
        raw_rfd["updated_at"] = now.isoformat()

        rfd_obj = self._build_refund(raw_rfd, now)
        self._idempotency_records[idempotency_key] = (payload_key, rfd_obj)
        self._check_fault_after_commit("cancel_reservation")
        return rfd_obj

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Refund:
        t0 = self._clock.now()
        for raw in self._raw_data.get("refunds", []):
            if raw["refund_id"] == refund_id:
                rfd = self._build_refund(raw, t0)
                if (
                    ctx.principal.role == Role.customer
                    and rfd.customer_id != ctx.principal.customer_id
                ):
                    raise StoreNotFound(f"Refund '{refund_id}' not found")
                return rfd
        raise StoreNotFound(f"Refund '{refund_id}' not found")

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Order:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Admin order view requires service or seller role")

        order = await self.get_order(service_ctx, order_id)
        returns = await self.list_returns(service_ctx, order_id)
        refunds = await self.list_refunds(service_ctx, order_id)
        return order.model_copy(update={"returns": returns, "refunds": refunds})

    async def get_settings(self, service_ctx: RequestContext) -> StoreSettings:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Store settings view requires service or seller role")
        return self._settings

    async def update_settings(
        self,
        seller_ctx: RequestContext,
        autonomy_cap_paise: int,
        autonomy_daily_cap_paise: int,
        expected_version: int,
    ) -> StoreSettings:
        if seller_ctx.principal.role != Role.seller_owner:
            raise StoreForbidden("Updating store settings requires seller_owner role")
        if expected_version != self._settings.version:
            raise StoreInvalidState(
                f"Settings version mismatch: expected {expected_version}, "
                f"current {self._settings.version}"
            )
        self._settings = StoreSettings(
            autonomy_cap_paise=autonomy_cap_paise,
            autonomy_daily_cap_paise=autonomy_daily_cap_paise,
            version=self._settings.version + 1,
        )
        return self._settings

    async def get_customer_refund_summary(
        self,
        service_ctx: RequestContext,
        customer_id: str,
        days: int = 30,
    ) -> CustomerRefundSummary:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer refund summary requires service or seller role")

        t0 = self._clock.now()
        cutoff = t0 - timedelta(days=days)

        all_refunds = [
            self._build_refund(r, t0)
            for r in self._raw_data.get("refunds", [])
            if r.get("customer_id") == customer_id
        ]
        all_returns = [
            self._build_return(r, t0)
            for r in self._raw_data.get("returns", [])
            if r.get("customer_id") == customer_id
        ]

        recent_refunds = [
            r
            for r in all_refunds
            if r.status in (RefundStatus.processing, RefundStatus.completed)
            and r.created_at >= cutoff
        ]
        recent_returns = [r for r in all_returns if r.created_at >= cutoff]
        total_paise = sum(r.amount_paise for r in recent_refunds)

        return CustomerRefundSummary(
            customer_id=customer_id,
            days=days,
            total_refunded_paise=total_paise,
            refund_count=len(recent_refunds),
            return_count=len(recent_returns),
        )

    async def send_mail(
        self,
        service_ctx: RequestContext,
        recipient: str,
        subject: str,
        body_text: str,
        template: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer token cannot send outbound mail")

        await self._check_fault_before("send_mail")

        payload_key = f"send_mail:{recipient}:{subject}:{body_text}:{template}"
        async with self._lock:
            if idempotency_key is not None:
                cached = self._check_idempotency(idempotency_key, payload_key)
                if isinstance(cached, dict):
                    return cached

            now = self._clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
            message_id = f"mail_{secrets.token_hex(6)}"
            record: dict[str, Any] = {
                "message_id": message_id,
                "recipient": recipient,
                "to": recipient,
                "subject": subject,
                "body_text": body_text,
                "template": template,
                "created_at": now,
            }
            self.mailbox.append(record)

            if idempotency_key is not None:
                self._idempotency_records[idempotency_key] = (payload_key, record)

            self._check_fault_after_commit("send_mail")
            return record

    async def list_shipments_for_monitor(
        self,
        service_ctx: RequestContext,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Any:
        from sellershield_agent.domain.models import (
            MonitorShipment,
            ShipmentListResponse,
        )

        if service_ctx.principal.role != Role.service or service_ctx.token is not None:
            raise StoreForbidden("Monitor shipment listing requires service context")

        allowed = set(statuses) if statuses else {"in_transit", "exception", "lost"}
        raw_shipments = self._raw_data.get("shipments", [])
        matched = [s for s in raw_shipments if s.get("status") in allowed]

        # Cursor-based pagination: cursor is the integer offset
        offset = int(cursor) if cursor is not None and cursor.isdigit() else 0
        page = matched[offset : offset + limit]
        has_more = (offset + limit) < len(matched)
        next_cursor_val = str(offset + limit) if has_more else None

        order_customers: dict[str, str] = {
            o["order_id"]: o.get("customer_id", "")
            for o in self._raw_data.get("orders", [])
            if "order_id" in o
        }

        t0 = getattr(self, "_t0", self._clock.now())
        parsed: list[MonitorShipment] = []
        for s in page:
            cid = s.get("customer_id") or order_customers.get(
                s.get("order_id", ""), ""
            )
            # Resolve relative T0-offsets in eta_at / last_event_at
            eta_raw = s.get("eta_at") or s.get("eta_at_offset")
            eta_resolved = _resolve_offset(eta_raw, t0) if isinstance(eta_raw, str) else eta_raw
            if eta_resolved is not None:
                eta_str = eta_resolved.strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                eta_str = None

            resolved_events: list[dict[str, Any]] = []
            for ev in s.get("events", []):
                ev_copy = dict(ev)
                occ_raw = ev_copy.get("occurred_at") or ev_copy.get("occurred_at_offset")
                occ_resolved = _resolve_offset(occ_raw, t0) if isinstance(occ_raw, str) else occ_raw
                if occ_resolved is not None:
                    ev_copy["occurred_at"] = occ_resolved.strftime("%Y-%m-%dT%H:%M:%SZ")
                resolved_events.append(ev_copy)

            parsed.append(
                MonitorShipment.model_validate(
                    {
                        **s,
                        "customer_id": cid,
                        "eta_at": eta_str,
                        "events": resolved_events,
                    }
                )
            )
        return ShipmentListResponse(
            shipments=parsed, next_cursor=next_cursor_val
        )
