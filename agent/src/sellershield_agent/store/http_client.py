"""Asynchronous HTTP store client for NestKart Store API (T1.9).

Enforces:
- Full StoreClient asynchronous Protocol implementation.
- Passthrough of customer token and X-Request-ID headers (KP-13).
- Tokens are SecretStr and never logged (KP-13).
- Uniform 404 for unowned and nonexistent orders (KP-15).
- Service token caching for client credentials (60 s pre-expiry refresh).
- Error mapping: 404->StoreNotFound, 401/403->StoreForbidden, 409->StoreConflict,
  5xx/network->StoreUnavailable, timeouts->StoreTimeout, malformed ID->StoreValidationError.
- Bounded retry on idempotent GET (2 tries with jitter on 502/503/504 and connection errors).
- Unknown response fields are dropped; invalid shapes raise StoreUnavailable
  and are logged without the body.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from pydantic import SecretStr, ValidationError

from sellershield_agent.auth.models import Role
from sellershield_agent.clock import Clock, StoreClock
from sellershield_agent.domain.models import (
    AdminOrderDetail,
    CustomerRefundSummary,
    CustomerRiskFacts,
    MonitorShipment,
    Order,
    Refund,
    Refundable,
    RefundableItem,
    ReturnItem,
    ReturnRequest,
    Shipment,
    ShipmentListResponse,
    StoreSettings,
)
from sellershield_agent.store.client import (
    RequestContext,
    StoreClient,
    StoreConflict,
    StoreDuplicate,
    StoreForbidden,
    StoreIdempotencyMismatch,
    StoreInvalidState,
    StoreLimitExceeded,
    StoreNotFound,
    StoreTimeout,
    StoreUnavailable,
    StoreValidationError,
)
from sellershield_agent.store.normalizers import (
    clean_customer_refund_summary_dict,
    clean_order_dict,
    clean_refund_dict,
    clean_refundable_item_dict,
    clean_return_dict,
    clean_shipment_dict,
    clean_store_settings_dict,
    ensure_aware_utc,
)

logger = structlog.get_logger(__name__)

ORDER_ID_REGEX = re.compile(r"^ORD-[0-9]{3,6}$")


class HttpStoreClient(StoreClient):
    """Real HTTP client communicating with NestKart Store API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        monitor_client_id: str | None = None,
        monitor_client_secret: SecretStr | None = None,
        clock: Clock | None = None,
        http_client: httpx.AsyncClient | None = None,
        executor_client_id: str | None = None,
        executor_client_secret: SecretStr | None = None,
    ) -> None:
        cleaned_url = base_url.rstrip("/")
        self._api_base_url = (
            cleaned_url if cleaned_url.endswith("/api/v1") else f"{cleaned_url}/api/v1"
        )
        self._monitor_client_id = monitor_client_id
        self._monitor_client_secret = monitor_client_secret
        self._executor_client_id = executor_client_id
        self._executor_client_secret = executor_client_secret
        self._clock: Clock = clock if clock is not None else StoreClock()
        self._custom_client = http_client

        # Service token cache per role ("monitor" | "executor")
        self._token_cache: dict[str, tuple[str, datetime]] = {}
        self._token_locks: dict[str, asyncio.Lock] = {
            "monitor": asyncio.Lock(),
            "executor": asyncio.Lock(),
        }

    def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        return httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=2.0),
            follow_redirects=False,
        )

    def get_clock(self) -> Clock:
        """Return the clock used for resolving store timestamps."""
        return self._clock

    def _validate_order_id(self, order_id: str) -> None:
        """Validate order ID format ^ORD-[0-9]{3,6}$ immediately."""
        if not isinstance(order_id, str) or not ORDER_ID_REGEX.fullmatch(order_id):
            raise StoreValidationError(f"Malformed order ID: '{order_id}'")

    async def _get_service_token(self, role: str = "monitor") -> str:
        """Fetch or return cached service token using client credentials."""
        role_key = "executor" if role == "executor" else "monitor"
        now = ensure_aware_utc(self._clock.now())
        async with self._token_locks[role_key]:
            cached = self._token_cache.get(role_key)
            if cached is not None:
                cached_tok, cached_exp = cached
                if now < (cached_exp - timedelta(seconds=60)):
                    return cached_tok

            if role_key == "executor":
                client_id = self._executor_client_id
                client_secret = self._executor_client_secret
                if not client_id or not client_secret:
                    raise StoreForbidden("STORE_EXECUTOR_CLIENT_ID or Secret not configured")
            else:
                client_id = self._monitor_client_id
                client_secret = self._monitor_client_secret
                if not client_id or not client_secret:
                    raise StoreForbidden("STORE_MONITOR_CLIENT_ID or Secret not configured")

            token_url = f"{self._api_base_url}/auth/token"
            payload = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret.get_secret_value(),
            }

            try:
                client = self._get_client()
                if self._custom_client is not None:
                    resp = await client.post(token_url, json=payload)
                else:
                    async with client as c:
                        resp = await c.post(token_url, json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise StoreUnavailable(f"Store token endpoint unreachable: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise StoreTimeout(f"Store token endpoint timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise StoreUnavailable(f"Store token network error: {exc}") from exc

            if resp.status_code in (401, 403):
                raise StoreForbidden("Service client authentication rejected by store")
            if resp.status_code >= 500:
                raise StoreUnavailable(f"Store auth service error: status {resp.status_code}")
            if resp.status_code != 200:
                raise StoreUnavailable(f"Failed to issue service token: status {resp.status_code}")

            try:
                data = resp.json()
                access_token = str(data["access_token"])
                expires_in = int(data.get("expires_in", 600))
            except Exception as exc:
                logger.error("store_auth_invalid_response", status_code=resp.status_code)
                raise StoreUnavailable("Invalid token response from store") from exc

            self._token_cache[role_key] = (access_token, now + timedelta(seconds=expires_in))
            return access_token

    async def _execute_get(
        self,
        endpoint_path: str,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute idempotent GET with bounded 2-try retry and jitter on 5xx/connection."""
        url = f"{self._api_base_url}{endpoint_path}"
        last_exc: Exception | None = None

        for attempt in range(1, 3):
            client = self._get_client()
            try:
                if self._custom_client is not None:
                    response = await client.get(url, headers=headers, params=params)
                else:
                    async with client as c:
                        response = await c.get(url, headers=headers, params=params)

                if response.status_code in (502, 503, 504) and attempt < 2:
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                    continue

                return response

            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                    continue
                raise StoreUnavailable(f"Store connection failed after 2 attempts: {exc}") from exc
            except httpx.ReadTimeout as exc:
                raise StoreTimeout(f"Store read timed out: {exc}") from exc
            except httpx.TimeoutException as exc:
                raise StoreTimeout(f"Store request timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise StoreUnavailable(f"Store network error: {exc}") from exc

        if last_exc:
            raise StoreUnavailable(f"Store request failed: {last_exc}") from last_exc
        raise StoreUnavailable("Store request failed after bounded retries")

    async def _execute_post(
        self,
        endpoint_path: str,
        headers: dict[str, str],
        json_data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute single mutating POST request without retry (KP-16)."""
        url = f"{self._api_base_url}{endpoint_path}"
        client = self._get_client()
        try:
            if self._custom_client is not None:
                return await client.post(url, headers=headers, json=json_data)
            else:
                async with client as c:
                    return await c.post(url, headers=headers, json=json_data)
        except httpx.TimeoutException as exc:
            raise StoreTimeout(f"Store request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise StoreUnavailable(f"Store network error: {exc}") from exc

    async def _execute_put(
        self,
        endpoint_path: str,
        headers: dict[str, str],
        json_data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute single mutating PUT request without retry (KP-16)."""
        url = f"{self._api_base_url}{endpoint_path}"
        client = self._get_client()
        try:
            if self._custom_client is not None:
                return await client.put(url, headers=headers, json=json_data)
            else:
                async with client as c:
                    return await c.put(url, headers=headers, json=json_data)
        except httpx.TimeoutException as exc:
            raise StoreTimeout(f"Store request timed out: {exc}") from exc
        except httpx.NetworkError as exc:
            raise StoreUnavailable(f"Store network error: {exc}") from exc

    def _handle_status_code(self, response: httpx.Response, action_description: str) -> None:
        """Map HTTP status codes and error payloads to domain StoreClientError hierarchy."""
        status = response.status_code
        if status in (200, 201):
            return

        error_code = None
        error_msg = None
        try:
            err_body = response.json()
            if isinstance(err_body, dict):
                err_data = err_body.get("error", err_body)
                if isinstance(err_data, dict):
                    error_code = err_data.get("code")
                    error_msg = err_data.get("message")
        except Exception:
            pass

        if error_code == "duplicate":
            raise StoreDuplicate(f"{action_description}: {error_msg or 'duplicate resource'}")
        if error_code == "limit_exceeded":
            raise StoreLimitExceeded(f"{action_description}: {error_msg or 'limit exceeded'}")
        if error_code == "invalid_state":
            raise StoreInvalidState(f"{action_description}: {error_msg or 'invalid state'}")
        if error_code == "idempotency_mismatch":
            raise StoreIdempotencyMismatch(
                f"{action_description}: {error_msg or 'idempotency mismatch'}"
            )
        if error_code == "not_found" or status == 404:
            raise StoreNotFound(f"{action_description}: {error_msg or 'resource not found'}")
        if error_code == "forbidden" or status in (401, 403):
            raise StoreForbidden(f"{action_description}: {error_msg or 'access forbidden'}")
        if error_code == "validation_error":
            raise StoreValidationError(f"{action_description}: {error_msg or 'validation error'}")

        if status == 412:
            raise StoreInvalidState(
                f"{action_description}: precondition failed (version mismatch)"
            )
        if status == 409:
            raise StoreConflict(f"{action_description}: {error_msg or 'conflict'}")
        if status >= 500:
            raise StoreUnavailable(f"{action_description}: store unavailable (status {status})")
        if status >= 400:
            raise StoreUnavailable(f"{action_description}: store client error (status {status})")

    async def _build_auth_headers(
        self,
        ctx: RequestContext,
        *,
        service_role: str | None = None,
    ) -> dict[str, str]:
        """Build headers forwarding ctx.token or minting service token for Role.service."""
        headers = {"X-Request-ID": ctx.request_id}
        if ctx.token is not None:
            headers["Authorization"] = f"Bearer {ctx.token.get_secret_value()}"
        elif service_role is not None and ctx.principal.role == Role.service:
            token = await self._get_service_token(service_role)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def sync_clock(self) -> datetime:
        """Sync with store simulated clock using service token."""
        token = await self._get_service_token("monitor")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": f"sync-clock-{int(datetime.now(UTC).timestamp())}",
        }
        resp = await self._execute_get("/clock", headers=headers)
        self._handle_status_code(resp, "sync_clock")

        try:
            data = resp.json()
            now_str = str(data["now_utc"])
            mode = str(data.get("mode", "simulated"))
            offset_seconds = data.get("offset_seconds")
            store_dt = datetime.fromisoformat(now_str)
            if store_dt.tzinfo is None:
                store_dt = store_dt.replace(tzinfo=UTC)
            else:
                store_dt = store_dt.astimezone(UTC)
        except Exception as exc:
            logger.error("store_clock_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid clock response from store") from exc

        if hasattr(self._clock, "set_sync"):
            self._clock.set_sync(store_dt, mode=mode, offset_seconds=offset_seconds)
        elif hasattr(self._clock, "set"):
            self._clock.set(store_dt)

        return store_dt

    async def list_orders(self, ctx: RequestContext) -> list[Order]:
        headers = await self._build_auth_headers(ctx, service_role=None)
        resp = await self._execute_get("/orders", headers=headers)
        self._handle_status_code(resp, "list_orders")

        try:
            data = resp.json()
            if isinstance(data, dict):
                raw_orders = data["orders"] if "orders" in data else data.get("items", [])
            else:
                raw_orders = data
            return [Order.model_validate(clean_order_dict(raw)) for raw in raw_orders]
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_orders_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid orders response from store") from exc

    async def get_order(self, ctx: RequestContext, order_id: str) -> Order:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get(f"/orders/{order_id}", headers=headers)
        self._handle_status_code(resp, f"get_order {order_id}")

        try:
            data = resp.json()
            return Order.model_validate(clean_order_dict(data))
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_order_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid order response from store") from exc

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Shipment:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get(f"/orders/{order_id}/shipment", headers=headers)
        self._handle_status_code(resp, f"get_shipment {order_id}")

        try:
            data = resp.json()
            raw_shipment = data.get("shipment", data) if isinstance(data, dict) else data
            return Shipment.model_validate(clean_shipment_dict(raw_shipment))
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_shipment_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid shipment response from store") from exc

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Refundable:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        params: dict[str, str] | None = None
        if items:
            params = {"items": ",".join(f"{it[0]}:{it[1]}" for it in items)}

        resp = await self._execute_get(
            f"/orders/{order_id}/refundable", headers=headers, params=params
        )
        self._handle_status_code(resp, f"get_refundable {order_id}")

        try:
            data = resp.json()
            raw_items = data.get("items", [])
            parsed_items = [
                RefundableItem.model_validate(clean_refundable_item_dict(it)) for it in raw_items
            ]
            return Refundable(
                order_id=data["order_id"],
                max_refundable_paise=data["max_refundable_paise"],
                items=parsed_items,
            )
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_refundable_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid refundable response from store") from exc

    async def list_returns(self, ctx: RequestContext, order_id: str) -> list[ReturnRequest]:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get("/returns", headers=headers)
        self._handle_status_code(resp, f"list_returns {order_id}")

        try:
            data = resp.json()
            raw_returns = data.get("returns", []) if isinstance(data, dict) else data
            all_returns = [
                ReturnRequest.model_validate(clean_return_dict(raw)) for raw in raw_returns
            ]
            return [r for r in all_returns if r.order_id == order_id]
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_returns_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid returns response from store") from exc

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> list[Refund]:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get(f"/orders/{order_id}/refunds", headers=headers)
        self._handle_status_code(resp, f"list_refunds {order_id}")

        try:
            data = resp.json()
            raw_refunds = data.get("refunds", []) if isinstance(data, dict) else data
            return [Refund.model_validate(clean_refund_dict(raw)) for raw in raw_refunds]
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_refunds_invalid_shape", status_code=resp.status_code)
            raise StoreUnavailable("Invalid refunds response from store") from exc

    async def get_customer_risk_facts(
        self,
        service_ctx: RequestContext,
        customer_id: str,
    ) -> CustomerRiskFacts:
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer risk facts requires service context")

        headers = await self._build_auth_headers(service_ctx, service_role="monitor")

        # Query admin customer detail
        admin_resp = await self._execute_get(f"/admin/customers/{customer_id}", headers=headers)
        self._handle_status_code(admin_resp, f"get_customer_risk_facts admin {customer_id}")

        # Query customer refund summary (30 days)
        summary_resp = await self._execute_get(
            f"/customers/{customer_id}/refund-summary",
            headers=headers,
            params={"days": 30},
        )
        self._handle_status_code(summary_resp, f"get_customer_risk_facts summary {customer_id}")

        try:
            admin_data = admin_resp.json()
            summary_data = summary_resp.json()

            returns_30d = int(admin_data["returns_30d"])
            account_age_days = int(admin_data["account_age_days"])
            refund_ratio_90d = float(admin_data.get("refund_ratio_90d", 0.0))
            refund_ratio_90d_bp = round(refund_ratio_90d * 10000)
            refunded_raw = (
                summary_data["total_refunded_paise"]
                if "total_refunded_paise" in summary_data
                else summary_data["total_refunds_paise"]
            )
            refunded_30d_paise = int(refunded_raw)

            return CustomerRiskFacts(
                customer_id=customer_id,
                returns_30d=returns_30d,
                refund_ratio_90d_bp=refund_ratio_90d_bp,
                account_age_days=account_age_days,
                refunded_30d_paise=refunded_30d_paise,
            )
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.error("store_risk_facts_invalid_shape", status_code=admin_resp.status_code)
            raise StoreUnavailable("Invalid risk facts response from store") from exc

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
        self._validate_order_id(order_id)
        if ctx.principal.role != Role.customer:
            raise StoreForbidden("Customer context required for create_return")
        if ctx.token is None:
            raise StoreForbidden("Customer token required for create_return")

        headers = {
            "Authorization": f"Bearer {ctx.token.get_secret_value()}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": ctx.request_id,
        }

        norm_items: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, ReturnItem):
                norm_items.append(it.model_dump())
            elif isinstance(it, dict):
                norm_items.append({
                    "item_id": it["item_id"],
                    "qty": it.get("qty", it.get("quantity", 1)),
                    "condition_note": it.get("condition_note"),
                })
        payload = {
            "items": norm_items,
            "reason_code": reason_code,
            "reason_text": reason_text or "",
            "resolution": resolution,
        }
        resp = await self._execute_post(
            f"/orders/{order_id}/returns", headers=headers, json_data=payload
        )
        self._handle_status_code(resp, f"create_return {order_id}")
        try:
            return ReturnRequest.model_validate(clean_return_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid return response from store") from exc

    async def get_return(self, ctx: RequestContext, return_id: str) -> ReturnRequest:
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get(f"/returns/{return_id}", headers=headers)
        self._handle_status_code(resp, f"get_return {return_id}")
        try:
            return ReturnRequest.model_validate(clean_return_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid return response from store") from exc

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> ReturnRequest:
        if ctx.principal.role in (Role.seller_staff, Role.seller_owner):
            if ctx.token is None:
                raise StoreForbidden("Seller token required for transition_return")
            token = ctx.token.get_secret_value()
        elif ctx.principal.role == Role.service:
            token = await self._get_service_token("executor")
        else:
            raise StoreForbidden("Only sellers or executor can transition returns")

        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": ctx.request_id,
        }
        payload = {"to": to, "reason": reason}
        resp = await self._execute_post(
            f"/returns/{return_id}/transition", headers=headers, json_data=payload
        )
        self._handle_status_code(resp, f"transition_return {return_id}")
        try:
            return ReturnRequest.model_validate(clean_return_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid return response from store") from exc

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Refund:
        self._validate_order_id(order_id)
        if service_ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer cannot reserve refunds")
        token = await self._get_service_token("executor")
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": service_ctx.request_id,
        }
        payload = {
            "amount_paise": amount_paise,
            "return_id": return_id,
            "method": method,
        }
        resp = await self._execute_post(
            f"/orders/{order_id}/refund-reservations", headers=headers, json_data=payload
        )
        self._handle_status_code(resp, f"reserve_refund {order_id}")
        try:
            return Refund.model_validate(clean_refund_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid refund reservation response from store") from exc

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Refund:
        if seller_ctx.principal.role not in (Role.seller_staff, Role.seller_owner):
            raise StoreForbidden("Refund authorization requires seller role")
        if seller_ctx.token is None:
            raise StoreForbidden("Seller token required for refund authorization")

        headers = {
            "Authorization": f"Bearer {seller_ctx.token.get_secret_value()}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": seller_ctx.request_id,
        }
        payload = {"amount_paise": amount_paise, "note": note}
        resp = await self._execute_post(
            f"/refunds/{refund_id}/authorize", headers=headers, json_data=payload
        )
        self._handle_status_code(resp, f"authorize_refund {refund_id}")
        try:
            return Refund.model_validate(clean_refund_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid refund authorization response from store") from exc

    async def release_refund(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        if ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer cannot release refunds")
        if ctx.principal.role in (Role.seller_staff, Role.seller_owner) and ctx.token is not None:
            token = ctx.token.get_secret_value()
        else:
            token = await self._get_service_token("executor")

        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": ctx.request_id,
        }
        resp = await self._execute_post(f"/refunds/{refund_id}/release", headers=headers)
        self._handle_status_code(resp, f"release_refund {refund_id}")
        try:
            return Refund.model_validate(clean_refund_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid refund release response from store") from exc

    async def cancel_reservation(
        self,
        ctx: RequestContext,
        refund_id: str,
        idempotency_key: str,
    ) -> Refund:
        if ctx.principal.role == Role.customer:
            raise StoreForbidden("Customer cannot cancel refund reservations")
        if ctx.principal.role in (Role.seller_staff, Role.seller_owner) and ctx.token is not None:
            token = ctx.token.get_secret_value()
        else:
            token = await self._get_service_token("executor")

        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": ctx.request_id,
        }
        resp = await self._execute_post(f"/refunds/{refund_id}/cancel-reservation", headers=headers)
        self._handle_status_code(resp, f"cancel_reservation {refund_id}")
        try:
            return Refund.model_validate(clean_refund_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid refund cancellation response from store") from exc

    async def update_settings(
        self,
        seller_ctx: RequestContext,
        autonomy_cap_paise: int,
        autonomy_daily_cap_paise: int,
        expected_version: int,
    ) -> StoreSettings:
        if seller_ctx.principal.role not in (Role.seller_owner, Role.seller_staff):
            raise StoreForbidden("Updating store settings requires seller role")
        if seller_ctx.token is None:
            raise StoreForbidden("Seller token required for update_settings")

        headers = {
            "Authorization": f"Bearer {seller_ctx.token.get_secret_value()}",
            "If-Match": str(expected_version),
            "X-Request-ID": seller_ctx.request_id,
        }
        payload = {
            "autonomy_cap_paise": autonomy_cap_paise,
            "autonomy_daily_cap_paise": autonomy_daily_cap_paise,
        }
        resp = await self._execute_put("/admin/settings", headers=headers, json_data=payload)
        self._handle_status_code(resp, "update_settings")
        try:
            return StoreSettings.model_validate(clean_store_settings_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid store settings response from store") from exc

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Refund:
        headers = await self._build_auth_headers(ctx, service_role="monitor")
        resp = await self._execute_get(f"/refunds/{refund_id}", headers=headers)
        self._handle_status_code(resp, f"get_refund {refund_id}")
        try:
            return Refund.model_validate(clean_refund_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid refund response from store") from exc

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Order:
        self._validate_order_id(order_id)
        headers = await self._build_auth_headers(service_ctx, service_role="monitor")
        resp = await self._execute_get(f"/admin/orders/{order_id}", headers=headers)
        self._handle_status_code(resp, f"get_order_admin {order_id}")
        try:
            return AdminOrderDetail.model_validate(clean_order_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid admin order response from store") from exc

    async def get_settings(self, service_ctx: RequestContext) -> StoreSettings:
        headers = await self._build_auth_headers(service_ctx, service_role="monitor")
        resp = await self._execute_get("/admin/settings", headers=headers)
        self._handle_status_code(resp, "get_settings")
        try:
            return StoreSettings.model_validate(clean_store_settings_dict(resp.json()))
        except Exception as exc:
            raise StoreUnavailable("Invalid store settings response from store") from exc

    async def get_customer_refund_summary(
        self,
        service_ctx: RequestContext,
        customer_id: str,
        days: int = 30,
    ) -> CustomerRefundSummary:
        headers = await self._build_auth_headers(service_ctx, service_role="monitor")
        resp = await self._execute_get(
            f"/customers/{customer_id}/refund-summary",
            headers=headers,
            params={"days": days},
        )
        self._handle_status_code(resp, f"get_customer_refund_summary {customer_id}")
        try:
            return CustomerRefundSummary.model_validate(
                clean_customer_refund_summary_dict(resp.json())
            )
        except Exception as exc:
            raise StoreUnavailable("Invalid refund summary response from store") from exc

    async def send_mail(
        self,
        service_ctx: RequestContext,
        recipient: str,
        subject: str,
        body_text: str,
        template: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if service_ctx.principal.role != Role.service or service_ctx.token is not None:
            raise StoreForbidden("Outbound mail requires service context with zero customer token")

        service_role = "executor" if self._executor_client_id else "monitor"
        token = await self._get_service_token(role=service_role)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": service_ctx.request_id,
            "Idempotency-Key": idempotency_key or f"mail:{service_ctx.request_id}",
        }

        payload: dict[str, Any] = {
            "to_addr": recipient,
            "subject": subject,
            "body_text": body_text,
            "channel": "email",
        }
        resp = await self._execute_post("/mail/send", headers=headers, json_data=payload)
        self._handle_status_code(resp, f"send_mail {recipient}")
        try:
            return resp.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise StoreUnavailable("Invalid mail response from store") from exc

    async def list_shipments_for_monitor(
        self,
        service_ctx: RequestContext,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> ShipmentListResponse:
        if service_ctx.principal.role != Role.service or service_ctx.token is not None:
            raise StoreForbidden(
                "Monitor shipment listing requires service context with zero customer token"
            )

        token = await self._get_service_token(role="monitor")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": service_ctx.request_id,
        }
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if statuses:
            stat_str = ",".join(statuses) if isinstance(statuses, (list, tuple)) else str(statuses)
            params["statuses"] = stat_str

        root_url = self._api_base_url.replace("/api/v1", "")
        shipments_url = f"{root_url}/admin/shipments"

        async def _fetch(url: str, qparams: dict[str, Any] | None = None) -> httpx.Response:
            try:
                if self._custom_client is not None:
                    return await self._custom_client.get(url, headers=headers, params=qparams)
                async with self._get_client() as c:
                    return await c.get(url, headers=headers, params=qparams)
            except httpx.TimeoutException as exc:
                raise StoreTimeout(f"Store request timed out: {exc}") from exc
            except httpx.NetworkError as exc:
                raise StoreUnavailable(f"Store network error: {exc}") from exc

        resp = await _fetch(shipments_url, params)

        if resp.status_code == 200:
            try:
                data = resp.json()
                shipments = [
                    MonitorShipment.model_validate(s) for s in data.get("shipments", [])
                ]
                return ShipmentListResponse(
                    shipments=shipments,
                    next_cursor=data.get("next_cursor"),
                )
            except Exception as exc:
                raise StoreUnavailable("Invalid shipments response from store") from exc
        elif resp.status_code == 404:
            # Fallback for store implementations exposing shipments through /admin/orders
            ord_resp = await _fetch(
                f"{self._api_base_url}/admin/orders", {"limit": limit}
            )

            if ord_resp.status_code == 200:
                try:
                    ord_data = ord_resp.json()
                    found: list[MonitorShipment] = []
                    for item in ord_data.get("items", []):
                        oid = item.get("order_id")
                        cid = item.get("customer_id", "")
                        if oid:
                            s_resp = await _fetch(
                                f"{self._api_base_url}/orders/{oid}/shipment"
                            )
                            if s_resp.status_code == 200:
                                s_dict = s_resp.json()
                                if not s_dict.get("customer_id"):
                                    s_dict["customer_id"] = cid
                                found.append(MonitorShipment.model_validate(s_dict))
                    return ShipmentListResponse(shipments=found, next_cursor=None)
                except Exception as exc:
                    raise StoreUnavailable("Invalid order shipments response from store") from exc

        self._handle_status_code(resp, "list_shipments_for_monitor")
        return ShipmentListResponse(shipments=[], next_cursor=None)
