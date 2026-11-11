"""StoreClient interface, execution context and error hierarchy.

Enforces:
- RequestContext holding verified Principal, request_id, and masked SecretStr token (KP-13).
- StoreClient asynchronous Protocol defining customer and backoffice store operations.
- Uniform exceptions hierarchy for store access.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from sellershield_agent.auth.models import Principal
from sellershield_agent.clock import Clock
from sellershield_agent.domain.models import (
    CustomerRefundSummary,
    CustomerRiskFacts,
    Order,
    Refund,
    Refundable,
    ReturnItem,
    ReturnRequest,
    Shipment,
    StoreSettings,
)

# ── Exceptions ──────────────────────────────────────────────────────


class StoreClientError(Exception):
    """Base exception for all store client failures."""


class StoreNotFound(StoreClientError):
    """Resource not found or unauthorized to see (uniform 404, KP-15)."""


class StoreForbidden(StoreClientError):
    """Action or resource forbidden for current context principal role."""


class StoreConflict(StoreClientError):
    """State transition conflict or duplicate mutation under idempotency."""


class StoreDuplicate(StoreConflict):
    """Resource already exists or duplicate active mutation attempted."""


class StoreLimitExceeded(StoreClientError):
    """Operation exceeds monetary, quantity, or autonomy caps."""


class StoreInvalidState(StoreClientError):
    """State transition invalid or entity not in required state."""


class StoreIdempotencyMismatch(StoreClientError):
    """Same Idempotency-Key reused with differing request payload."""


class StoreUnavailable(StoreClientError):
    """Store service temporarily unreachable or degraded."""


class StoreTimeout(StoreClientError):
    """Store request exceeded configured timeout."""


class StoreValidationError(StoreClientError, ValueError):
    """Client-side validation error on arguments (e.g. malformed order ID)."""


# ── Context ─────────────────────────────────────────────────────────


class RequestContext(BaseModel):
    """Context carried through every store call.

    Contains the verified Principal, request tracking ID, and optional
    customer bearer token stored as SecretStr (never logged).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Principal
    request_id: str
    token: SecretStr | None = None

    def __repr__(self) -> str:
        token_str = "**********" if self.token is not None else "None"
        return (
            f"RequestContext(principal={self.principal!r}, "
            f"request_id={self.request_id!r}, token={token_str})"
        )


class CustomerContext(RequestContext):
    """Context for authenticated customer requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SellerContext(RequestContext):
    """Context for authenticated seller requests carrying a seller token."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ServiceContext(RequestContext):
    """Context for backoffice service client operations (executor or monitor)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scopes: list[str] = Field(default_factory=list)


# ── Client Protocol ─────────────────────────────────────────────────


@runtime_checkable
class StoreClient(Protocol):
    """Asynchronous client protocol for communicating with NestKart Store."""

    async def list_orders(self, ctx: RequestContext) -> list[Order]:
        """List orders visible to the context principal."""
        ...

    async def get_order(self, ctx: RequestContext, order_id: str) -> Order:
        """Fetch order by ID, enforcing ownership and format checks."""
        ...

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Shipment:
        """Fetch tracking and status information for an order's shipment."""
        ...

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Refundable:
        """Calculate per-item and total refundable paise net of holds and past refunds."""
        ...

    async def list_returns(self, ctx: RequestContext, order_id: str) -> list[ReturnRequest]:
        """List returns initiated against the specified order."""
        ...

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> list[Refund]:
        """List refunds created or authorized against the specified order."""
        ...

    async def get_customer_risk_facts(
        self,
        service_ctx: RequestContext,
        customer_id: str,
    ) -> CustomerRiskFacts:
        """Fetch risk profile for a customer (requires service or seller role)."""
        ...

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
        """Create a new return request in status 'requested'."""
        ...

    async def get_return(self, ctx: RequestContext, return_id: str) -> ReturnRequest:
        """Fetch details for a specific return."""
        ...

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> ReturnRequest:
        """Advance return request along the allowed state machine."""
        ...

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Refund:
        """Reserve a monetary refund hold against an order."""
        ...

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Refund:
        """Authorize a reserved refund by seller staff or owner."""
        ...

    async def release_refund(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        """Release a reserved/authorized refund for payout."""
        ...

    async def cancel_reservation(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        """Cancel a reserved refund hold."""
        ...

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Refund:
        """Fetch details for a specific refund."""
        ...

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Order:
        """Fetch complete administrative order details including returns and refunds."""
        ...

    async def get_settings(self, service_ctx: RequestContext) -> StoreSettings:
        """Fetch store settings including autonomy cap and daily cap."""
        ...

    async def update_settings(
        self,
        seller_ctx: RequestContext,
        autonomy_cap_paise: int,
        autonomy_daily_cap_paise: int,
        expected_version: int,
    ) -> StoreSettings:
        """Update store autonomy limits and caps with optimistic version check."""
        ...

    async def get_customer_refund_summary(
        self,
        service_ctx: RequestContext,
        customer_id: str,
        days: int = 30,
    ) -> CustomerRefundSummary:
        """Fetch aggregated refund and return counts and sums over the specified days."""
        ...

    async def send_mail(
        self,
        service_ctx: RequestContext,
        recipient: str,
        subject: str,
        body_text: str,
        template: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Send an email notification via store mail service."""
        ...

    async def list_shipments_for_monitor(
        self,
        service_ctx: RequestContext,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Any:
        """List in-transit and exception shipments for proactive monitoring."""
        ...

    def get_clock(self) -> Clock:
        """Return the clock used for resolving store timestamps."""
        ...
