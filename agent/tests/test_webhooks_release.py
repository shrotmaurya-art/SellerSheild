"""Step A tests for T2.7 store webhooks and refund release.

Covers TASK T2.7 and Spec docs/spec/02-agent.md §§6.3, 9, 12, 21 (finding 14),
and docs/spec/01-playground.md §§6, 7.4:
- Constant-time HMAC-SHA256 verification over ``timestamp.raw_body``, fail-closed
  across all modes (including DEV_MODE), generic 401 envelope leaking zero hints.
- Timestamp window enforcement (-300s .. +60s against Clock.now()).
- Deduplication by X-Event-Id in ``webhook_events_seen`` (including replay with
  new valid timestamp/signature and 10x burst replay).
- Monotonic per-resource ``resource_version`` enforcement (older versions ignored).
- Fast webhook burst handling (200 events in < 5s) and > 1 MB rejection (413).
- Safe ``return.updated -> inspected_ok`` release via ServiceContext only after checking
  live store return status, refund status/authorization, exact authorized amount,
  idempotent execution guard, and live policy safety rules.
- Reconciliation of ``payment timeout_after_commit`` (no second release call) and
  ``payment fail_once`` (retry with identical idempotency_key ``{pid}:release_refund``).
- ``refund.updated`` (processing, completed, failed) updates executions/approvals/
  proposals cleanly, emits customer-safe ``refund.updated`` client_events, and never
  auto-creates a new refund on store failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr
from sqlalchemy import Engine, event, select, text, update
from starlette.testclient import TestClient

from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import _db_write_lock, get_engine, read_tx, reset_engine, write_tx
from sellershield_agent.domain.models import RefundStatus, ReturnItem
from sellershield_agent.domain.state import (
    ProposalStatus,
    approvals,
    client_events,
    executions,
    proposals,
)
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.execution.reconcile import reconcile_once
from sellershield_agent.execution.saga import Executor as SagaExecutor
from sellershield_agent.main import create_app
from sellershield_agent.policy import loader as policy_loader
from sellershield_agent.policy.loader import load_policy
from sellershield_agent.proposals.confirm import confirm_proposal
from sellershield_agent.store.client import RequestContext
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.tools.gateway import ToolGateway
from tests.fault_plan import FaultPlan

JWKS_URL = "http://localhost:8000/.well-known/jwks.json"
EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
WEBHOOK_SECRET = "whsec_test_sellershield_0123456789abcdef"
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_CUSTOMER_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


def _create_rsa_key_pair(
    kid: str = "wh-key-1",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


def _make_token(
    *,
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    now_dt: datetime,
    kid: str = "wh-key-1",
    exp_seconds: int = 3600,
    store_id: str | None = "nestkart",
) -> str:
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "iat": now_ts,
        "exp": now_ts + exp_seconds,
        "auth_time": now_ts,
        "jti": str(uuid.uuid4()),
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    if store_id is not None:
        payload["store_id"] = store_id
    return str(jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"}))


def _sign_webhook(
    *,
    raw_body: bytes,
    timestamp: str,
    secret: str = WEBHOOK_SECRET,
) -> str:
    msg = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _make_webhook_envelope(
    *,
    event_type: str,
    data: dict[str, Any],
    clock: FixedClock,
    event_id: str | None = None,
    resource_version: int = 1,
) -> tuple[str, bytes]:
    eid = event_id or str(uuid.uuid4())
    payload = {
        "event_id": eid,
        "type": event_type,
        "occurred_at": clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "resource_version": resource_version,
        "data": data,
    }
    return eid, json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _post_webhook(
    client: TestClient,
    *,
    raw_body: bytes,
    event_id: str,
    timestamp: str,
    signature: str | None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Event-Id": event_id,
        "X-Store-Timestamp": timestamp,
    }
    if signature is not None:
        headers["X-Store-Signature"] = signature
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/v1/webhooks/store", content=raw_body, headers=headers)


class _WebhookStoreSpy:
    """Store wrapper recording caller roles/tokens, idempotency keys, and tx discipline."""

    def __init__(self, inner: FixtureStoreClient, engine: Engine) -> None:
        self._inner = inner
        self._engine = engine
        self._lock = threading.Lock()
        self._active_tx_depth = 0
        self.calls: list[dict[str, Any]] = []
        self.tx_violations: list[str] = []

        @event.listens_for(engine, "begin")
        def _on_begin(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth += 1

        @event.listens_for(engine, "commit")
        def _on_commit(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth = max(0, self._active_tx_depth - 1)

        @event.listens_for(engine, "rollback")
        def _on_rollback(_conn: Any) -> None:
            with self._lock:
                self._active_tx_depth = max(0, self._active_tx_depth - 1)

    @property
    def _raw_data(self) -> dict[str, Any]:
        return self._inner._raw_data

    def _record(self, op: str, ctx: RequestContext, **extra: Any) -> None:
        with self._lock:
            tx_open = self._active_tx_depth > 0 or _db_write_lock.locked()
            if tx_open:
                self.tx_violations.append(
                    f"{op} called inside open tx "
                    f"(depth={self._active_tx_depth}, write_lock={_db_write_lock.locked()})"
                )
            token_val = ctx.token.get_secret_value() if ctx.token is not None else None
            entry: dict[str, Any] = {
                "op": op,
                "role": ctx.principal.role,
                "user_id": ctx.principal.user_id,
                "customer_id": ctx.principal.customer_id,
                "has_token": token_val is not None and len(token_val) > 0,
                "scopes": list(getattr(ctx, "scopes", [])),
            }
            entry.update(extra)
            self.calls.append(entry)

    def reset_spy(self) -> None:
        with self._lock:
            self.calls.clear()
            self.tx_violations.clear()

    def get_clock(self) -> Any:
        return self._inner.get_clock()

    async def list_orders(self, ctx: RequestContext) -> Any:
        self._record("list_orders", ctx)
        return await self._inner.list_orders(ctx)

    async def get_order(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("get_order", ctx, order_id=order_id)
        return await self._inner.get_order(ctx, order_id)

    async def get_shipment(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("get_shipment", ctx, order_id=order_id)
        return await self._inner.get_shipment(ctx, order_id)

    async def get_refundable(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[tuple[str, int]] | None = None,
    ) -> Any:
        self._record("get_refundable", ctx, order_id=order_id)
        return await self._inner.get_refundable(ctx, order_id, items=items)

    async def list_returns(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_returns", ctx, order_id=order_id)
        return await self._inner.list_returns(ctx, order_id)

    async def list_refunds(self, ctx: RequestContext, order_id: str) -> Any:
        self._record("list_refunds", ctx, order_id=order_id)
        return await self._inner.list_refunds(ctx, order_id)

    async def get_customer_risk_facts(self, service_ctx: RequestContext, customer_id: str) -> Any:
        self._record("get_customer_risk_facts", service_ctx, customer_id=customer_id)
        return await self._inner.get_customer_risk_facts(service_ctx, customer_id)

    async def create_return(
        self,
        ctx: RequestContext,
        order_id: str,
        items: list[ReturnItem] | list[dict[str, Any]],
        reason_code: str,
        reason_text: str | None,
        resolution: str,
        idempotency_key: str,
    ) -> Any:
        self._record("create_return", ctx, order_id=order_id, idempotency_key=idempotency_key)
        return await self._inner.create_return(
            ctx, order_id, items, reason_code, reason_text, resolution, idempotency_key
        )

    async def get_return(self, ctx: RequestContext, return_id: str) -> Any:
        self._record("get_return", ctx, return_id=return_id)
        return await self._inner.get_return(ctx, return_id)

    async def transition_return(
        self,
        ctx: RequestContext,
        return_id: str,
        to: str,
        reason: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "transition_return",
            ctx,
            return_id=return_id,
            to=to,
            idempotency_key=idempotency_key,
        )
        return await self._inner.transition_return(ctx, return_id, to, reason, idempotency_key)

    async def reserve_refund(
        self,
        service_ctx: RequestContext,
        order_id: str,
        amount_paise: int,
        return_id: str | None,
        method: str,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "reserve_refund",
            service_ctx,
            order_id=order_id,
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
        )
        return await self._inner.reserve_refund(
            service_ctx, order_id, amount_paise, return_id, method, idempotency_key
        )

    async def authorize_refund(
        self,
        seller_ctx: RequestContext,
        refund_id: str,
        amount_paise: int,
        note: str | None,
        idempotency_key: str,
    ) -> Any:
        self._record(
            "authorize_refund",
            seller_ctx,
            refund_id=refund_id,
            amount_paise=amount_paise,
            idempotency_key=idempotency_key,
        )
        return await self._inner.authorize_refund(
            seller_ctx, refund_id, amount_paise, note, idempotency_key
        )

    async def release_refund(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record("release_refund", ctx, refund_id=refund_id, idempotency_key=idempotency_key)
        return await self._inner.release_refund(ctx, refund_id, idempotency_key)

    async def cancel_reservation(
        self, ctx: RequestContext, refund_id: str, idempotency_key: str
    ) -> Any:
        self._record(
            "cancel_reservation", ctx, refund_id=refund_id, idempotency_key=idempotency_key
        )
        return await self._inner.cancel_reservation(ctx, refund_id, idempotency_key)

    async def get_refund(self, ctx: RequestContext, refund_id: str) -> Any:
        self._record("get_refund", ctx, refund_id=refund_id)
        return await self._inner.get_refund(ctx, refund_id)

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Any:
        self._record("get_order_admin", service_ctx, order_id=order_id)
        return await self._inner.get_order_admin(service_ctx, order_id)

    async def get_settings(self, service_ctx: RequestContext) -> Any:
        self._record("get_settings", service_ctx)
        return await self._inner.get_settings(service_ctx)

    async def get_customer_refund_summary(
        self, service_ctx: RequestContext, customer_id: str, days: int = 30
    ) -> Any:
        self._record("get_customer_refund_summary", service_ctx, customer_id=customer_id)
        return await self._inner.get_customer_refund_summary(service_ctx, customer_id, days=days)


@pytest.fixture
def rsa_keys() -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    return _create_rsa_key_pair()


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(T0_BASE)


@pytest.fixture
def app_env(
    tmp_path: Path,
    rsa_keys: tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]],
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "webhooks_release_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("STORE_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STORE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()

    priv, _, jwk = rsa_keys
    with respx.mock(assert_all_called=False) as mock_http:
        mock_http.get(JWKS_URL).respond(json={"keys": [jwk]})

        app = create_app()
        app.dependency_overrides[deps.get_clock] = lambda: fixed_clock

        fault_plan = FaultPlan()
        inner_store = FixtureStoreClient(
            fixtures_path=FIXTURES_PATH,
            clock=fixed_clock,
            fault_plan=fault_plan,
        )
        engine = get_engine()
        spy_store = _WebhookStoreSpy(inner=inner_store, engine=engine)
        app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store

        tokens = {
            "c001": _make_token(
                user_id="usr_c001", customer_id="C001", role="customer", priv=priv, now_dt=T0_BASE
            ),
            "c002": _make_token(
                user_id="usr_c002", customer_id="C002", role="customer", priv=priv, now_dt=T0_BASE
            ),
            "seller_staff": _make_token(
                user_id="usr_staff",
                customer_id=None,
                role="seller_staff",
                priv=priv,
                now_dt=T0_BASE,
            ),
            "seller_owner": _make_token(
                user_id="usr_owner",
                customer_id=None,
                role="seller_owner",
                priv=priv,
                now_dt=T0_BASE,
            ),
        }

        with TestClient(app, raise_server_exceptions=False) as client:
            yield {
                "app": app,
                "client": client,
                "clock": fixed_clock,
                "store": spy_store,
                "inner_store": inner_store,
                "fault_plan": fault_plan,
                "engine": engine,
                "tokens": tokens,
                "priv": priv,
                "tmp_path": tmp_path,
            }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    reset_default_jwks_client()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


async def _create_and_confirm_return(
    app_env: dict[str, Any],
    *,
    customer_id: str,
    user_id: str,
    order_id: str,
    item_id: str,
    qty: int = 1,
    reason_code: str = "changed_mind",
    reason_text: str = "Does not fit",
    resolution: str = "refund",
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Propose and confirm a return through ToolGateway + SagaExecutor."""
    engine: Engine = app_env["engine"]
    store: _WebhookStoreSpy = app_env["store"]
    clock: FixedClock = app_env["clock"]
    policy = load_policy()
    gateway = ToolGateway(
        store_client=store,
        policy=policy,
        clock=clock,
        engine=engine,
        service_ctx_factory=lambda req_id: build_service_ctx(clock, req_id),
    )
    cust_ctx = RequestContext(
        principal=Principal(
            user_id=user_id,
            customer_id=customer_id,
            role=Role.customer,
            jti=f"jti-{uuid.uuid4().hex[:8]}",
            token_exp=int((clock.now() + timedelta(hours=1)).timestamp()),
            auth_time=int(clock.now().timestamp()),
        ),
        request_id=f"req-prop-{uuid.uuid4().hex[:8]}",
        token=SecretStr("cust-jwt-secret"),
    )
    args: dict[str, Any] = {
        "order_id": order_id,
        "items": [{"item_id": item_id, "qty": qty}],
        "reason_code": reason_code,
        "reason_text": reason_text,
        "resolution": resolution,
    }
    if evidence_ids is not None:
        args["evidence_ids"] = evidence_ids
    prop_res = await gateway.call(
        ctx=cust_ctx,
        name="propose_return",
        raw_args=args,
        allowed={"propose_return"},
        session_id=f"sess-{customer_id.lower()}",
    )
    assert prop_res.ok and isinstance(prop_res.data, dict), f"propose_return failed: {prop_res}"
    card = prop_res.data
    assert card.get("type") == "confirmation", f"Expected confirmation card, got {card}"
    pid = str(card["proposal_id"])

    executor = SagaExecutor(store_client=store, engine=engine, clock=clock)
    confirm_res = await confirm_proposal(
        cust_ctx,
        pid,
        str(card["proposal_hash"]),
        engine=engine,
        store_client=store,
        executor=executor,
        clock=clock,
        idempotency_key=f"idem-confirm-{pid}",
    )

    with read_tx(engine) as conn:
        exec_rows = conn.execute(
            select(executions).where(executions.c.proposal_id == pid)
        ).fetchall()
        rfd_row = next((r for r in exec_rows if r.step == "reserve_refund"), None)
        apr_row = conn.execute(select(approvals).where(approvals.c.proposal_id == pid)).fetchone()

    return {
        "proposal_id": pid,
        "return_id": confirm_res.return_id,
        "refund_id": str(rfd_row.store_ref) if rfd_row and rfd_row.store_ref else None,
        "approval_id": confirm_res.approval_id,
        "approval_version": int(apr_row.version) if apr_row else None,
        "amount_paise": int(card["amounts"]["total_paise"]),
        "status": confirm_res.status,
    }


async def _advance_return_to_inspected_ok(
    store: _WebhookStoreSpy,
    clock: FixedClock,
    return_id: str,
) -> None:
    """Advance an approved store return cleanly through inspected_ok."""
    svc_ctx = build_service_ctx(clock, f"req-adv-{uuid.uuid4().hex[:6]}")
    for target_state in ("pickup_scheduled", "in_transit_back", "received", "inspected_ok"):
        await store.transition_return(
            svc_ctx,
            return_id,
            target_state,
            f"Simulated step to {target_state}",
            f"key-adv-{return_id}-{target_state}-{uuid.uuid4().hex[:6]}",
        )


def _assert_generic_401(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert isinstance(body, dict) and "error" in body
    err = body["error"]
    assert err["code"] == "unauthenticated"
    assert err["details"] == {}
    msg_lower = str(err["message"]).lower()
    for leaky in (
        "signature",
        "hmac",
        "timestamp",
        "stale",
        "future",
        "expired",
        "secret",
        "header",
        "missing",
    ):
        assert leaky not in msg_lower, f"401 message leaked '{leaky}': {err['message']}"
    return dict(err)


# ── 1. Authentication, Replay & Input Bounds Tests ─────────────────────────────


def test_webhook_forged_signature_returns_401_generic(
    app_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forged, missing, or malformed signatures/headers return identical generic 401."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    engine: Engine = app_env["engine"]
    ts_str = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": "RET-9001", "status": "inspected_ok"},
        clock=clock,
    )

    # 1. Invalid HMAC signature
    r_bad_sig = _post_webhook(
        client, raw_body=body, event_id=eid, timestamp=ts_str, signature="0" * 64
    )
    err1 = _assert_generic_401(r_bad_sig)

    # 2. Signed with wrong secret
    r_wrong_secret = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body, timestamp=ts_str, secret="wrong-secret"),
    )
    err2 = _assert_generic_401(r_wrong_secret)

    # 3. Missing signature, timestamp, or X-Event-Id
    r_no_sig = client.post(
        "/v1/webhooks/store",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Event-Id": eid,
            "X-Store-Timestamp": ts_str,
        },
    )
    err3 = _assert_generic_401(r_no_sig)

    r_no_ts = client.post(
        "/v1/webhooks/store",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Event-Id": eid,
            "X-Store-Signature": "deadbeef" * 8,
        },
    )
    err4 = _assert_generic_401(r_no_ts)

    r_no_eid = client.post(
        "/v1/webhooks/store",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Store-Timestamp": ts_str,
            "X-Store-Signature": _sign_webhook(raw_body=body, timestamp=ts_str),
        },
    )
    err5 = _assert_generic_401(r_no_eid)

    # 4. Duplicate X-Store-Signature header smuggling -> 401
    valid_sig = _sign_webhook(raw_body=body, timestamp=ts_str)
    r_dup_hdr = client.post(
        "/v1/webhooks/store",
        content=body,
        headers=[
            ("content-type", "application/json"),
            ("x-event-id", eid),
            ("x-store-timestamp", ts_str),
            ("x-store-signature", valid_sig),
            ("x-store-signature", "0" * 64),
        ],
    )
    err_dup = _assert_generic_401(r_dup_hdr)

    # 5. Unsigned request with invalid JSON still yields 401 (never 422 before signature check)
    r_bad_json = _post_webhook(
        client, raw_body=b"{not-json", event_id=eid, timestamp=ts_str, signature="bad"
    )
    err6 = _assert_generic_401(r_bad_json)

    # 6. Empty/unset WEBHOOK_SECRET fails closed even in DEV_MODE=true & STORE_MODE=fixtures
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    r_no_secret = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body, timestamp=ts_str, secret=""),
    )
    err7 = _assert_generic_401(r_no_secret)

    messages = {e["message"] for e in (err1, err2, err3, err4, err5, err_dup, err6, err7)}
    assert len(messages) == 1

    with read_tx(engine) as conn:
        denials = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "auth_denied")
        ).fetchall()
        assert len(denials) >= 8
        for row in denials:
            assert WEBHOOK_SECRET not in str(row.details_json)
            assert "wrong-secret" not in str(row.details_json)


def test_webhook_stale_and_future_timestamp_rejected_401(app_env: dict[str, Any]) -> None:
    """Timestamps > 5 min old or > 1 min in future are rejected with generic 401; boundary ok."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    now_ts = int(clock.now().timestamp())

    # 301s old -> 401
    eid_stale, body_stale = _make_webhook_envelope(
        event_type="shipment.updated",
        data={"shipment_id": "SHP-1", "status": "in_transit"},
        clock=clock,
        resource_version=1,
    )
    ts_stale = str(now_ts - 301)
    r_stale = _post_webhook(
        client,
        raw_body=body_stale,
        event_id=eid_stale,
        timestamp=ts_stale,
        signature=_sign_webhook(raw_body=body_stale, timestamp=ts_stale),
    )
    _assert_generic_401(r_stale)

    # 61s in future -> 401
    eid_future, body_future = _make_webhook_envelope(
        event_type="shipment.updated",
        data={"shipment_id": "SHP-1", "status": "in_transit"},
        clock=clock,
        resource_version=2,
    )
    ts_future = str(now_ts + 61)
    r_future = _post_webhook(
        client,
        raw_body=body_future,
        event_id=eid_future,
        timestamp=ts_future,
        signature=_sign_webhook(raw_body=body_future, timestamp=ts_future),
    )
    _assert_generic_401(r_future)

    # Non-digit whitespace-padded timestamp -> 401
    r_ws = _post_webhook(
        client,
        raw_body=body_future,
        event_id=eid_future,
        timestamp=f" {now_ts} ",
        signature=_sign_webhook(raw_body=body_future, timestamp=f" {now_ts} "),
    )
    _assert_generic_401(r_ws)

    # Exact boundaries: -300s and +60s -> 200
    eid_min, body_min = _make_webhook_envelope(
        event_type="shipment.updated",
        data={"shipment_id": "SHP-2", "status": "picked_up"},
        clock=clock,
        resource_version=1,
    )
    ts_min = str(now_ts - 300)
    assert (
        _post_webhook(
            client,
            raw_body=body_min,
            event_id=eid_min,
            timestamp=ts_min,
            signature=_sign_webhook(raw_body=body_min, timestamp=ts_min),
        ).status_code
        == 200
    )

    eid_max, body_max = _make_webhook_envelope(
        event_type="shipment.updated",
        data={"shipment_id": "SHP-2", "status": "in_transit"},
        clock=clock,
        resource_version=2,
    )
    ts_max = str(now_ts + 60)
    assert (
        _post_webhook(
            client,
            raw_body=body_max,
            event_id=eid_max,
            timestamp=ts_max,
            signature=_sign_webhook(raw_body=body_max, timestamp=ts_max),
        ).status_code
        == 200
    )


def test_webhook_duplicate_event_id_ignored(app_env: dict[str, Any]) -> None:
    """Duplicate X-Event-Id (delivered 10 times) is processed once cleanly."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    engine: Engine = app_env["engine"]
    ts_str = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="order.updated",
        data={"order_id": "ORD-1042", "status": "delivered"},
        clock=clock,
        resource_version=2,
    )
    sig = _sign_webhook(raw_body=body, timestamp=ts_str)

    # Header vs body event_id mismatch rejected 400 and never recorded as seen
    r_mismatch = _post_webhook(
        client,
        raw_body=body,
        event_id=str(uuid.uuid4()),
        timestamp=ts_str,
        signature=sig,
    )
    assert r_mismatch.status_code == 400

    for _ in range(10):
        resp = _post_webhook(client, raw_body=body, event_id=eid, timestamp=ts_str, signature=sig)
        assert resp.status_code == 200
        assert resp.json() == {"received": True, "event_id": eid}

    with read_tx(engine) as conn:
        seen_count = conn.execute(
            text("SELECT count(*) FROM webhook_events_seen WHERE event_id = :eid"),
            {"eid": eid},
        ).scalar()
        assert seen_count == 1
        success_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.resource_id == eid,
                audit_events.c.outcome == "success",
            )
        ).fetchall()
        assert len(success_audits) == 1


@pytest.mark.asyncio
async def test_webhook_same_event_id_replayed_with_new_timestamp_ignored(
    app_env: dict[str, Any],
) -> None:
    """Replaying the same event_id with a fresh valid timestamp and signature is ignored."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
        resolution="store_credit",
    )
    ret_id = str(created["return_id"])
    await _advance_return_to_inspected_ok(store, clock, ret_id)
    store.reset_spy()

    ts1 = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=10,
    )
    r1 = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts1,
        signature=_sign_webhook(raw_body=body, timestamp=ts1),
    )
    assert r1.status_code == 200
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1

    # Replay same X-Event-Id 30 seconds later with new valid timestamp and valid signature
    clock.set(clock.now() + timedelta(seconds=30))
    ts2 = str(int(clock.now().timestamp()))
    r2 = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts2,
        signature=_sign_webhook(raw_body=body, timestamp=ts2),
    )
    assert r2.status_code == 200
    assert r2.json() == {"received": True, "event_id": eid}
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1

    with read_tx(engine) as conn:
        seen_count = conn.execute(
            text("SELECT count(*) FROM webhook_events_seen WHERE event_id = :eid"),
            {"eid": eid},
        ).scalar()
        assert seen_count == 1


@pytest.mark.asyncio
async def test_webhook_older_resource_version_ignored(app_env: dict[str, Any]) -> None:
    """Out-of-order lower resource_version for the same resource is ignored; other resources ok."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]
    ts_str = str(int(clock.now().timestamp()))

    # Deliver v5 for RET-VER-1 first
    eid_v5, body_v5 = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": "RET-VER-1", "order_id": "ORD-2032", "status": "received"},
        clock=clock,
        resource_version=5,
    )
    r_v5 = _post_webhook(
        client,
        raw_body=body_v5,
        event_id=eid_v5,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body_v5, timestamp=ts_str),
    )
    assert r_v5.status_code == 200
    store.reset_spy()

    # Now deliver older v3 for RET-VER-1 claiming inspected_ok -> must be ignored
    eid_v3, body_v3 = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": "RET-VER-1", "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=3,
    )
    r_v3 = _post_webhook(
        client,
        raw_body=body_v3,
        event_id=eid_v3,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body_v3, timestamp=ts_str),
    )
    assert r_v3.status_code == 200
    assert r_v3.json() == {"received": True, "event_id": eid_v3}
    assert len(store.calls) == 0

    # A different resource (RFD-VER-1) at v1 must NOT be blocked by RET-VER-1 v5
    eid_rfd, body_rfd = _make_webhook_envelope(
        event_type="refund.updated",
        data={"refund_id": "RFD-VER-1", "return_id": "RET-VER-1", "status": "processing"},
        clock=clock,
        resource_version=1,
    )
    r_rfd = _post_webhook(
        client,
        raw_body=body_rfd,
        event_id=eid_rfd,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body_rfd, timestamp=ts_str),
    )
    assert r_rfd.status_code == 200

    with read_tx(engine) as conn:
        stale_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.resource_id == eid_v3,
                audit_events.c.outcome == "denied",
            )
        ).fetchall()
        assert len(stale_audits) == 1
        assert AuditService.verify_chain(conn).ok is True


def test_webhook_valid_event_processed_once_and_non_action_events_audited(
    app_env: dict[str, Any],
) -> None:
    """Valid audit-only events record seen entry + audit event once with zero store mutations."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]
    ts_str = str(int(clock.now().timestamp()))

    sample_events = [
        ("order.created", {"order_id": "ORD-1042", "status": "placed"}, 1),
        ("shipment.updated", {"shipment_id": "SHP-1042", "status": "in_transit"}, 2),
        ("shipment.delayed", {"shipment_id": "SHP-1042", "order_id": "ORD-1042"}, 3),
        ("mail.inbound", {"message_id": "MSG-99", "thread_id": "THR-99"}, 1),
    ]
    event_ids: list[str] = []
    for ev_type, data, ver in sample_events:
        eid, body = _make_webhook_envelope(
            event_type=ev_type, data=data, clock=clock, resource_version=ver
        )
        sig = _sign_webhook(raw_body=body, timestamp=ts_str)
        r1 = _post_webhook(client, raw_body=body, event_id=eid, timestamp=ts_str, signature=sig)
        assert r1.status_code == 200
        assert r1.json() == {"received": True, "event_id": eid}
        # Repeat same event -> processed once
        r2 = _post_webhook(client, raw_body=body, event_id=eid, timestamp=ts_str, signature=sig)
        assert r2.status_code == 200
        event_ids.append(eid)

    assert len(store.calls) == 0
    with read_tx(engine) as conn:
        seen_rows = conn.execute(
            text("SELECT count(*) FROM webhook_events_seen WHERE event_id IN (:e0, :e1, :e2, :e3)"),
            {"e0": event_ids[0], "e1": event_ids[1], "e2": event_ids[2], "e3": event_ids[3]},
        ).scalar()
        assert seen_rows == 4
        assert AuditService.verify_chain(conn).ok is True


# ── 2. Refund Release & Pre-Release Safety Gate Tests ──────────────────────────


@pytest.mark.asyncio
async def test_webhook_inspected_ok_allow_path_releases_once(app_env: dict[str, Any]) -> None:
    """inspected_ok on ALLOW path releases refund once with ServiceContext and fixed key."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
        resolution="store_credit",
    )
    assert created["status"] == "succeeded"
    pid = created["proposal_id"]
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])
    await _advance_return_to_inspected_ok(store, clock, ret_id)
    store.reset_spy()

    ts_str = str(int(clock.now().timestamp()))
    eid1, body1 = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    resp1 = _post_webhook(
        client,
        raw_body=body1,
        event_id=eid1,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body1, timestamp=ts_str),
    )
    assert resp1.status_code == 200
    assert resp1.json() == {"received": True, "event_id": eid1}

    rel_calls = [c for c in store.calls if c["op"] == "release_refund"]
    assert len(rel_calls) == 1
    assert rel_calls[0]["role"] == Role.service
    assert rel_calls[0]["has_token"] is False
    assert rel_calls[0]["idempotency_key"] == f"{pid}:release_refund"
    assert store.tx_violations == []

    svc_ctx = build_service_ctx(clock, "req-chk-allow")
    live_rfd = await store.get_refund(svc_ctx, rfd_id)
    assert live_rfd.status == RefundStatus.processing
    assert live_rfd.amount_paise == 49900

    # A subsequent higher-version return.updated(inspected_ok) must NOT call release_refund again
    eid2, body2 = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=7,
    )
    resp2 = _post_webhook(
        client,
        raw_body=body2,
        event_id=eid2,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body2, timestamp=ts_str),
    )
    assert resp2.status_code == 200
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1

    with read_tx(engine) as conn:
        exec_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_row is not None
        assert exec_row.status == "succeeded"
        assert exec_row.idempotency_key == f"{pid}:release_refund"
        assert exec_row.store_ref == rfd_id
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
async def test_webhook_inspected_ok_needs_approval_before_approval_does_nothing_and_audits(
    app_env: dict[str, Any],
) -> None:
    """Before seller approval (even <= autonomy cap), inspected_ok blocks release & audits."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    # 1. High-value NEEDS_APPROVAL (ORD-1042, 120000 paise)
    high = await _create_and_confirm_return(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        item_id="ITEM-1042-1",
        reason_code="defective",
    )
    # 2. Low-value <= autonomy_cap NEEDS_APPROVAL (ORD-1061, 45000 paise, damaged without evidence)
    low = await _create_and_confirm_return(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1061",
        item_id="ITEM-1061-1",
        reason_code="damaged",
        evidence_ids=["EVD-1061-A"],
    )
    # Even if raw store return status were forged/moved to inspected_ok prematurely:
    for raw_ret in store._raw_data["returns"]:
        if raw_ret["return_id"] in (high["return_id"], low["return_id"]):
            raw_ret["status"] = "inspected_ok"

    store.reset_spy()
    ts_str = str(int(clock.now().timestamp()))
    for item in (high, low):
        eid, body = _make_webhook_envelope(
            event_type="return.updated",
            data={
                "return_id": item["return_id"],
                "order_id": "ORD-1042" if item is high else "ORD-1061",
                "status": "inspected_ok",
            },
            clock=clock,
            resource_version=2,
        )
        resp = _post_webhook(
            client,
            raw_body=body,
            event_id=eid,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body, timestamp=ts_str),
        )
        assert resp.status_code == 200

    assert [c for c in store.calls if c["op"] == "release_refund"] == []
    svc_ctx = build_service_ctx(clock, "req-chk-pre")
    assert (await store.get_refund(svc_ctx, str(high["refund_id"]))).status == RefundStatus.reserved
    assert (await store.get_refund(svc_ctx, str(low["refund_id"]))).status == RefundStatus.reserved

    with read_tx(engine) as conn:
        denied_exec_audits = conn.execute(
            select(audit_events).where(
                audit_events.c.event_type == "execution_step",
                audit_events.c.outcome == "denied",
            )
        ).fetchall()
        assert len(denied_exec_audits) >= 2
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
async def test_webhook_inspected_ok_after_approval_releases_exact_authorized_amount(
    app_env: dict[str, Any],
) -> None:
    """After seller approves NEEDS_APPROVAL, inspected_ok releases exact authorized amount."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        item_id="ITEM-1042-1",
        reason_code="defective",
    )
    pid = created["proposal_id"]
    aid = str(created["approval_id"])
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])

    r_apr = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['seller_staff']}",
            "Idempotency-Key": f"idem-apr-{aid}",
        },
        json={"decision": "approve", "reason": "Verified defective", "expected_version": 1},
    )
    assert r_apr.status_code == 200

    await _advance_return_to_inspected_ok(store, clock, ret_id)
    store.reset_spy()

    ts_str = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-1042", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    resp = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body, timestamp=ts_str),
    )
    assert resp.status_code == 200

    rel_calls = [c for c in store.calls if c["op"] == "release_refund"]
    assert len(rel_calls) == 1
    assert rel_calls[0]["role"] == Role.service
    assert rel_calls[0]["has_token"] is False
    assert rel_calls[0]["idempotency_key"] == f"{pid}:release_refund"

    svc_ctx = build_service_ctx(clock, "req-chk-post-apr")
    live_rfd = await store.get_refund(svc_ctx, rfd_id)
    assert live_rfd.status == RefundStatus.processing
    assert live_rfd.amount_paise == live_rfd.authorized_amount_paise == 120000

    with read_tx(engine) as conn:
        exec_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_row is not None and exec_row.status == "succeeded"


@pytest.mark.asyncio
async def test_webhook_release_safety_gates_block_tampered_amount_status_and_policy(
    app_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release is blocked/audited if return!=inspected_ok, amount>authorized, or policy fails."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]
    tmp_path: Path = app_env["tmp_path"]
    ts_str = str(int(clock.now().timestamp()))

    # Case (a): Approved return, but live store refund amount_paise > authorized_amount_paise
    approved = await _create_and_confirm_return(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        item_id="ITEM-1042-1",
        reason_code="defective",
    )
    r_apr = client.post(
        f"/v1/seller/approvals/{approved['approval_id']}/decision",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['seller_staff']}",
            "Idempotency-Key": "idem-apr-gate-a",
        },
        json={"decision": "approve", "reason": "Approve exact amount", "expected_version": 1},
    )
    assert r_apr.status_code == 200
    await _advance_return_to_inspected_ok(store, clock, str(approved["return_id"]))
    for raw_rfd in store._raw_data["refunds"]:
        if raw_rfd["refund_id"] == approved["refund_id"]:
            raw_rfd["amount_paise"] = 135000  # above 120000 authorized_amount_paise

    store.reset_spy()
    eid_a, body_a = _make_webhook_envelope(
        event_type="return.updated",
        data={
            "return_id": approved["return_id"],
            "order_id": "ORD-1042",
            "status": "inspected_ok",
        },
        clock=clock,
        resource_version=6,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_a,
            event_id=eid_a,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_a, timestamp=ts_str),
        ).status_code
        == 200
    )
    assert [c for c in store.calls if c["op"] == "release_refund"] == []

    # Case (b): Webhook payload claims inspected_ok when live store return is still 'approved'
    allow_c003 = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
    )
    store.reset_spy()
    eid_b, body_b = _make_webhook_envelope(
        event_type="return.updated",
        data={
            "return_id": allow_c003["return_id"],
            "order_id": "ORD-2032",
            "status": "inspected_ok",
        },
        clock=clock,
        resource_version=3,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_b,
            event_id=eid_b,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_b, timestamp=ts_str),
        ).status_code
        == 200
    )
    assert [c for c in store.calls if c["op"] == "release_refund"] == []

    # Case (c): Advance ORD-2032 return to inspected_ok, but tighten active policy limit < 43900
    await _advance_return_to_inspected_ok(store, clock, str(allow_c003["return_id"]))
    tmp_versions = tmp_path / "policy_versions"
    tmp_versions.mkdir()
    v3_doc = json.loads((policy_loader.VERSIONS_DIR / "v3.json").read_text(encoding="utf-8"))
    (tmp_versions / "v3.json").write_text(json.dumps(v3_doc), encoding="utf-8")
    v4_doc = dict(v3_doc)
    v4_doc["version"] = "v4"
    v4_doc["auto_limit_paise"] = 30000
    (tmp_versions / "v4.json").write_text(json.dumps(v4_doc), encoding="utf-8")
    monkeypatch.setattr(policy_loader, "VERSIONS_DIR", tmp_versions)
    monkeypatch.setenv("POLICY_ACTIVE_VERSION", "v4")

    store.reset_spy()
    eid_c, body_c = _make_webhook_envelope(
        event_type="return.updated",
        data={
            "return_id": allow_c003["return_id"],
            "order_id": "ORD-2032",
            "status": "inspected_ok",
        },
        clock=clock,
        resource_version=6,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_c,
            event_id=eid_c,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_c, timestamp=ts_str),
        ).status_code
        == 200
    )
    assert [c for c in store.calls if c["op"] == "release_refund"] == []

    with read_tx(engine) as conn:
        denied_rows = conn.execute(
            select(audit_events).where(
                audit_events.c.event_type == "execution_step",
                audit_events.c.outcome == "denied",
            )
        ).fetchall()
        assert len(denied_rows) >= 3
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
async def test_webhook_payment_timeout_after_commit_reconciled_without_second_release(
    app_env: dict[str, Any],
) -> None:
    """timeout_after_commit marks release_refund unknown; reconciler resolves without 2nd call."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    fault_plan: FaultPlan = app_env["fault_plan"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
    )
    pid = created["proposal_id"]
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])
    await _advance_return_to_inspected_ok(store, clock, ret_id)

    fault_plan.timeout_after_commit("release_refund")
    store.reset_spy()
    ts_str = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    resp = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body, timestamp=ts_str),
    )
    assert resp.status_code == 200
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1

    with read_tx(engine) as conn:
        exec_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_row is not None
        assert exec_row.status == "unknown"
        assert exec_row.idempotency_key == f"{pid}:release_refund"

    # Store already committed 'processing'; advance clock +5s and reconcile cleanly
    fault_plan._timeout_after_commit_steps.clear()
    clock.set(clock.now() + timedelta(seconds=6))
    summary = await reconcile_once(clock, store_client=store, engine=engine, min_age_seconds=5)
    assert summary["resolved"] >= 1

    # Critically, reconciler MUST NOT issue a second release_refund call
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1
    with read_tx(engine) as conn:
        exec_after = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_after is not None
        assert exec_after.status == "succeeded"
        assert exec_after.store_ref == rfd_id


@pytest.mark.asyncio
async def test_webhook_payment_fail_once_retries_with_same_key_and_succeeds(
    app_env: dict[str, Any],
) -> None:
    """fail_once leaves release_refund unknown; reconciler retries with identical key."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    fault_plan: FaultPlan = app_env["fault_plan"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
    )
    pid = created["proposal_id"]
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])
    await _advance_return_to_inspected_ok(store, clock, ret_id)

    fault_plan.fail_once("release_refund")
    store.reset_spy()
    ts_str = str(int(clock.now().timestamp()))
    eid, body = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    resp = _post_webhook(
        client,
        raw_body=body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body, timestamp=ts_str),
    )
    assert resp.status_code == 200
    assert len([c for c in store.calls if c["op"] == "release_refund"]) == 1

    svc_ctx = build_service_ctx(clock, "req-chk-fail1")
    assert (await store.get_refund(svc_ctx, rfd_id)).status == RefundStatus.reserved

    clock.set(clock.now() + timedelta(seconds=6))
    summary = await reconcile_once(clock, store_client=store, engine=engine, min_age_seconds=5)
    assert summary["resolved"] >= 1

    rel_calls = [c for c in store.calls if c["op"] == "release_refund"]
    assert len(rel_calls) == 2
    assert rel_calls[0]["idempotency_key"] == f"{pid}:release_refund"
    assert rel_calls[1]["idempotency_key"] == f"{pid}:release_refund"
    assert rel_calls[1]["role"] == Role.service
    assert rel_calls[1]["has_token"] is False

    assert (await store.get_refund(svc_ctx, rfd_id)).status == RefundStatus.processing
    with read_tx(engine) as conn:
        exec_after = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_after is not None and exec_after.status == "succeeded"


# ── 3. refund.updated Lifecycle Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_refund_completed_writes_client_event_and_ends_proposal_succeeded(
    app_env: dict[str, Any],
) -> None:
    """refund.updated (processing, completed) updates records, client_event, ends succeeded."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C001",
        user_id="usr_c001",
        order_id="ORD-1042",
        item_id="ITEM-1042-1",
        reason_code="defective",
    )
    pid = created["proposal_id"]
    aid = str(created["approval_id"])
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])

    r_apr = client.post(
        f"/v1/seller/approvals/{aid}/decision",
        headers={
            "Authorization": f"Bearer {app_env['tokens']['seller_staff']}",
            "Idempotency-Key": "idem-apr-comp",
        },
        json={"decision": "approve", "reason": "Approved", "expected_version": 1},
    )
    assert r_apr.status_code == 200
    await _advance_return_to_inspected_ok(store, clock, ret_id)

    # Set proposal to 'executing' as in release-pending state to verify executing -> succeeded
    with write_tx(engine) as conn:
        conn.execute(
            update(proposals)
            .where(proposals.c.proposal_id == pid)
            .values(status=ProposalStatus.executing.value)
        )

    ts_str = str(int(clock.now().timestamp()))
    eid_insp, body_insp = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-1042", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_insp,
            event_id=eid_insp,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_insp, timestamp=ts_str),
        ).status_code
        == 200
    )

    eid_proc, body_proc = _make_webhook_envelope(
        event_type="refund.updated",
        data={
            "refund_id": rfd_id,
            "order_id": "ORD-1042",
            "return_id": ret_id,
            "status": "processing",
            "amount_paise": 120000,
            "method": "original",
        },
        clock=clock,
        resource_version=1,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_proc,
            event_id=eid_proc,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_proc, timestamp=ts_str),
        ).status_code
        == 200
    )

    eid_done, body_done = _make_webhook_envelope(
        event_type="refund.updated",
        data={
            "refund_id": rfd_id,
            "order_id": "ORD-1042",
            "return_id": ret_id,
            "status": "completed",
            "amount_paise": 120000,
            "method": "original",
            "gateway_ref": f"SIMPAY-REF-{rfd_id}",
        },
        clock=clock,
        resource_version=2,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_done,
            event_id=eid_done,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_done, timestamp=ts_str),
        ).status_code
        == 200
    )

    with read_tx(engine) as conn:
        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid)
        ).fetchone()
        assert prop_row is not None
        assert prop_row.status == ProposalStatus.succeeded.value

        exec_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_row is not None and exec_row.status == "succeeded"

        apr_data = conn.execute(
            text(
                "SELECT status, refund_status, last_error FROM approvals WHERE approval_id = :aid"
            ),
            {"aid": aid},
        ).fetchone()
        assert apr_data is not None
        assert apr_data[0] == "approved"
        assert apr_data[1] == "completed"
        assert apr_data[2] is None

        cev_rows = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C001",
                client_events.c.type == "refund.updated",
            )
        ).fetchall()
        statuses = [json.loads(str(r.payload_json))["status"] for r in cev_rows]
        assert "processing" in statuses and "completed" in statuses
        for row in cev_rows:
            payload_str = str(row.payload_json)
            assert not FORBIDDEN_CUSTOMER_RE.search(payload_str)
            assert rfd_id in payload_str
        assert AuditService.verify_chain(conn).ok is True


@pytest.mark.asyncio
async def test_webhook_refund_failed_stays_failed_with_client_event_and_no_new_refund(
    app_env: dict[str, Any],
) -> None:
    """refund.updated (failed) stays failed with safe client_event & audit; never creates refund."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    store: _WebhookStoreSpy = app_env["store"]
    engine: Engine = app_env["engine"]

    created = await _create_and_confirm_return(
        app_env,
        customer_id="C002",
        user_id="usr_c002",
        order_id="ORD-2032",
        item_id="ITEM-2032-1",
        reason_code="changed_mind",
    )
    pid = created["proposal_id"]
    ret_id = str(created["return_id"])
    rfd_id = str(created["refund_id"])
    await _advance_return_to_inspected_ok(store, clock, ret_id)

    ts_str = str(int(clock.now().timestamp()))
    eid_insp, body_insp = _make_webhook_envelope(
        event_type="return.updated",
        data={"return_id": ret_id, "order_id": "ORD-2032", "status": "inspected_ok"},
        clock=clock,
        resource_version=6,
    )
    assert (
        _post_webhook(
            client,
            raw_body=body_insp,
            event_id=eid_insp,
            timestamp=ts_str,
            signature=_sign_webhook(raw_body=body_insp, timestamp=ts_str),
        ).status_code
        == 200
    )
    store.reset_spy()

    # Hostile/internal failure_reason from payment simulator must never leak to customer event
    eid_fail, body_fail = _make_webhook_envelope(
        event_type="refund.updated",
        data={
            "refund_id": rfd_id,
            "order_id": "ORD-2032",
            "return_id": ret_id,
            "status": "failed",
            "amount_paise": 43900,
            "method": "original",
            "failure_reason": "SIMPAY internal token limit threshold exceeded",
        },
        clock=clock,
        resource_version=2,
    )
    r_fail = _post_webhook(
        client,
        raw_body=body_fail,
        event_id=eid_fail,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=body_fail, timestamp=ts_str),
    )
    assert r_fail.status_code == 200

    # Store must NOT receive any new reserve_refund or release_refund
    assert [
        c for c in store.calls if c["op"] in ("reserve_refund", "release_refund", "create_return")
    ] == []
    svc_ctx = build_service_ctx(clock, "req-chk-fail-after")
    order_admin = await store.get_order_admin(svc_ctx, "ORD-2032")
    assert len(order_admin.refunds) == 1

    with read_tx(engine) as conn:
        prop_row = conn.execute(
            select(proposals).where(proposals.c.proposal_id == pid)
        ).fetchone()
        assert prop_row is not None and prop_row.status == ProposalStatus.failed.value

        exec_row = conn.execute(
            select(executions).where(
                executions.c.proposal_id == pid,
                executions.c.step == "release_refund",
            )
        ).fetchone()
        assert exec_row is not None and exec_row.status == "failed"

        cev_rows = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C002",
                client_events.c.type == "refund.updated",
            )
        ).fetchall()
        assert len(cev_rows) == 1
        payload_text = str(cev_rows[0].payload_json)
        assert json.loads(payload_text)["status"] == "failed"
        assert not FORBIDDEN_CUSTOMER_RE.search(payload_text)
        assert "SIMPAY" not in payload_text
        assert AuditService.verify_chain(conn).ok is True


# ── 4. Throughput & Size Limit Tests ───────────────────────────────────────────


def test_webhook_200_event_burst_within_5_seconds(app_env: dict[str, Any]) -> None:
    """A burst of 200 signed valid events is ingested and audited in under 5 seconds."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    engine: Engine = app_env["engine"]
    ts_str = str(int(clock.now().timestamp()))

    prepared: list[tuple[str, bytes, str]] = []
    for idx in range(200):
        eid, body = _make_webhook_envelope(
            event_type="shipment.updated",
            data={"shipment_id": f"SHP-BURST-{idx % 20}", "status": "in_transit"},
            clock=clock,
            resource_version=(idx // 20) + 1,
        )
        prepared.append((eid, body, _sign_webhook(raw_body=body, timestamp=ts_str)))

    t_start = time.perf_counter()
    for eid, body, sig in prepared:
        resp = _post_webhook(client, raw_body=body, event_id=eid, timestamp=ts_str, signature=sig)
        assert resp.status_code == 200
    elapsed = time.perf_counter() - t_start
    assert elapsed < 5.0, f"200-event webhook burst took {elapsed:.2f}s (expected < 5.0s)"

    with read_tx(engine) as conn:
        count = conn.execute(text("SELECT count(*) FROM webhook_events_seen")).scalar()
        assert count == 200


def test_webhook_body_over_1mb_rejected_413(app_env: dict[str, Any]) -> None:
    """Webhook payload larger than 1 MB is rejected cleanly with 413 limit_exceeded."""
    client: TestClient = app_env["client"]
    clock: FixedClock = app_env["clock"]
    ts_str = str(int(clock.now().timestamp()))
    eid, base_body = _make_webhook_envelope(
        event_type="order.updated",
        data={"order_id": "ORD-1042", "blob": "x" * (1024 * 1024 + 512)},
        clock=clock,
        resource_version=2,
    )
    assert len(base_body) > 1024 * 1024
    resp = _post_webhook(
        client,
        raw_body=base_body,
        event_id=eid,
        timestamp=ts_str,
        signature=_sign_webhook(raw_body=base_body, timestamp=ts_str),
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "limit_exceeded"
