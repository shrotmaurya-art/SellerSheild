"""Step A tests for TASK T3.4: Proactive delay monitor.

Covers Spec docs/spec/02-agent.md §§5 (W2, W9), 9.5, 13, 21 (Amendments 1, 3, 6):
1. T18: ORD-2037 is flagged once with one notification (`delay.notice`) and one `monitor.notice`
   event; a second run creates nothing new. Also verifies 3 guided action chips + live chip tap
   with LLM down, duplicate shipment deduplication in one store page, cursor loop guard,
   and 200-shipment per-run cap.
2. ORD-1070 lost shipment creates one high-priority case (`lost_shipment`, recommended next step
   `reship or refund with approval`) and one notice; repeat runs even hours later create zero
   duplicate cases; outbox worker delivers emails strictly to DB-resolved owners.
3. On-time, minor delays (< 2 days), delivered, and RTO shipments create nothing; CustomerContext
   and Role.service with non-None token are rejected (StoreForbidden) on both
   StoreClient.list_shipments_for_monitor and run_monitor; HttpStoreClient calls /admin/shipments
   with service token.
4. Run lock serializes concurrent runs (concurrent run returns running run_id with 1 store scan
   and valid SHA-256 audit chain); POST /v1/seller/monitor/run enforces seller RBAC, 422 extra
   fields, 10 s manual cooldown (429 rate_limited), never forwards caller JWT, and
   GET /v1/seller/monitor/runs(+/{id}) conforms to OpenAPI MonitorRun schema.
5. Tenant isolation, forbidden-words hygiene, PII absence, and XSS carrier/tracking sanitization
   across monitor.notice client events (/v1/me/events) and delay.notice email rendering.
6. 7-day simulated clock fast-forward dynamically flags newly late ORD-1048, escalates ORD-2037
   (exception >= 5d) to a delivery_delay case without duplicate notification, and creates zero
   duplicate notifications/cases for ORD-1070.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import uuid
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from sellershield_agent.api import chat as chat_api
from sellershield_agent.api import proposals as proposals_api
from sellershield_agent.api.chat import reset_chat_pipeline
from sellershield_agent.api.ratelimit import user_message_limiter
from sellershield_agent.audit.models import audit_events
from sellershield_agent.audit.service import AuditService
from sellershield_agent.auth import deps
from sellershield_agent.auth.deps import reset_default_jwks_client
from sellershield_agent.auth.models import Principal, Role
from sellershield_agent.clock import Clock, FixedClock
from sellershield_agent.config import get_settings
from sellershield_agent.db import get_engine, get_metadata, init_db, read_tx, reset_engine
from sellershield_agent.domain.models import Shipment, ShipmentStatus
from sellershield_agent.domain.state import (
    case_events,
    cases,
    client_events,
    notifications_outbox,
)
from sellershield_agent.events.emit import MonitorNoticePayload
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.main import create_app
from sellershield_agent.outbox.templates import render_template
from sellershield_agent.outbox.worker import process_outbox_batch
from sellershield_agent.store.client import RequestContext, StoreClient, StoreForbidden
from sellershield_agent.store.fixtures import FixtureStoreClient
from sellershield_agent.store.http_client import HttpStoreClient

EXPECTED_ISSUER = "nestkart-store"
AUDIENCE = "sellershield-agent"
T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FORBIDDEN_WORDS_RE = re.compile(
    r"\b(?:limits?|thresholds?|risks?|scores?|staff|owner|auto(?:matic|nomy)?|caps?)\b",
    re.IGNORECASE,
)


def _create_rsa_key_pair(
    kid: str = "monitor-key-1",
) -> tuple[RSAPrivateKey, RSAPublicKey, dict[str, Any]]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(pub))
    jwk_dict["kid"] = kid
    jwk_dict["alg"] = "RS256"
    jwk_dict["use"] = "sig"
    return priv, pub, jwk_dict


class _FakeJWKSClient:
    def __init__(self, jwk: dict[str, Any]) -> None:
        self._pyjwk = jwt.PyJWK.from_dict(jwk)

    def get_signing_key(self, kid: str) -> Any:
        return self._pyjwk

    def is_healthy(self) -> bool:
        return True


def _make_token(
    *,
    user_id: str,
    customer_id: str | None,
    role: str,
    priv: RSAPrivateKey,
    now_dt: datetime,
    kid: str = "monitor-key-1",
    exp_seconds: int = 3600,
) -> str:
    now_ts = int(now_dt.timestamp())
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": EXPECTED_ISSUER,
        "aud": AUDIENCE,
        "role": role,
        "store_id": "nestkart",
        "iat": now_ts,
        "exp": now_ts + exp_seconds,
        "auth_time": now_ts,
        "jti": str(uuid.uuid4()),
    }
    if customer_id is not None:
        payload["customer_id"] = customer_id
    return str(
        jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid, "alg": "RS256"})
    )


def _load_step_b_symbols() -> dict[str, Any]:
    """Deferred lookup of Step B modules/tables so pytest collection succeeds in Step A."""
    monitor_service = importlib.import_module("sellershield_agent.monitor.service")
    state_mod = importlib.import_module("sellershield_agent.domain.state")
    models_mod = importlib.import_module("sellershield_agent.domain.models")
    monitor_router = importlib.import_module("sellershield_agent.monitor.router")
    return {
        "run_monitor": monitor_service.run_monitor,
        "monitor_runs": state_mod.monitor_runs,
        "monitor_events": state_mod.monitor_events,
        "MonitorShipment": models_mod.MonitorShipment,
        "ShipmentListResponse": models_mod.ShipmentListResponse,
        "get_monitor_store_client": monitor_router.get_monitor_store_client,
    }


def _parse_sse(response_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur_event: str | None = None
    cur_data: list[str] = []
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line:
            if cur_event and cur_data:
                raw = "\n".join(cur_data)
                try:
                    data: Any = json.loads(raw)
                except Exception:
                    data = raw
                events.append({"event": cur_event, "data": data})
            cur_event = None
            cur_data = []
            continue
        if line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            cur_data.append(line.split(":", 1)[1].strip())
    if cur_event and cur_data:
        raw = "\n".join(cur_data)
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        events.append({"event": cur_event, "data": data})
    return events


@pytest.fixture
def monitor_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    db_file = tmp_path / "monitor_test.db"
    monkeypatch.setenv("STORE_MODE", "fixtures")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("STORE_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("STORE_JWKS_URL", "http://localhost:8000/.well-known/jwks.json")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file.as_posix()}")

    get_settings.cache_clear()
    reset_engine()
    reset_chat_pipeline()
    user_message_limiter.reset()
    reset_default_jwks_client()

    priv, _, jwk = _create_rsa_key_pair()
    fake_jwks = _FakeJWKSClient(jwk)
    fixed_clock = FixedClock(T0_BASE)
    store = FixtureStoreClient(clock=fixed_clock)

    app = create_app()
    app.dependency_overrides[deps.get_clock] = lambda: fixed_clock
    app.dependency_overrides[deps.get_jwks_client] = lambda: fake_jwks
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: store
    app.dependency_overrides[chat_api.get_store_client] = lambda: store

    engine = get_engine()
    init_db(engine, metadata=get_metadata())

    tokens = {
        "c001": _make_token(
            user_id="usr_c001",
            customer_id="C001",
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "c002": _make_token(
            user_id="usr_c002",
            customer_id="C002",
            role="customer",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "seller_staff": _make_token(
            user_id="SELLER-STAFF-1",
            customer_id=None,
            role="seller_staff",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "seller_staff_2": _make_token(
            user_id="SELLER-STAFF-2",
            customer_id=None,
            role="seller_staff",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "seller_owner": _make_token(
            user_id="SELLER-OWNER-1",
            customer_id=None,
            role="seller_owner",
            priv=priv,
            now_dt=T0_BASE,
        ),
        "service": _make_token(
            user_id="SVC-MONITOR",
            customer_id=None,
            role="service",
            priv=priv,
            now_dt=T0_BASE,
        ),
    }

    yield {
        "app": app,
        "engine": engine,
        "clock": fixed_clock,
        "store": store,
        "priv": priv,
        "tokens": tokens,
    }

    app.dependency_overrides.clear()
    reset_chat_pipeline()
    user_message_limiter.reset()
    reset_engine()
    get_settings.cache_clear()


class _SyntheticMonitorStore(FixtureStoreClient):
    """Test store serving custom shipment pages for cap, dedup, XSS, and concurrency drills."""

    def __init__(
        self,
        *,
        clock: Clock,
        pages: list[tuple[list[dict[str, Any]], str | None]],
        customer_map: dict[str, str] | None = None,
        pause_events: tuple[asyncio.Event, asyncio.Event] | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self._pages = pages
        self._customer_map = customer_map or {}
        self._pause_events = pause_events
        self.list_calls = 0
        self.captured_contexts: list[RequestContext] = []

    async def list_shipments_for_monitor(
        self,
        service_ctx: RequestContext,
        statuses: Sequence[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Any:
        self.captured_contexts.append(service_ctx)
        if service_ctx.principal.role != Role.service or service_ctx.token is not None:
            raise StoreForbidden("Monitor shipment listing requires service context")
        self.list_calls += 1
        if self._pause_events is not None and self.list_calls == 1:
            started_ev, release_ev = self._pause_events
            started_ev.set()
            await release_ev.wait()

        page_idx = int(cursor) if cursor is not None and cursor.isdigit() else 0
        if page_idx >= len(self._pages):
            rows, next_cur = [], None
        else:
            rows, next_cur = self._pages[page_idx]

        sym = _load_step_b_symbols()
        monitor_shp_cls = sym["MonitorShipment"]
        resp_cls = sym["ShipmentListResponse"]

        parsed = [
            monitor_shp_cls.model_validate({**row, "events": []})
            for row in rows[:limit]
        ]
        return resp_cls(shipments=parsed, next_cursor=next_cur)

    async def get_order_admin(self, service_ctx: RequestContext, order_id: str) -> Any:
        base_order = await super().get_order_admin(service_ctx, "ORD-2037")
        if order_id in self._customer_map:
            return base_order.model_copy(
                update={"order_id": order_id, "customer_id": self._customer_map[order_id]}
            )
        return await super().get_order_admin(service_ctx, order_id)


# ── Test 1 (T18): ORD-2037 flagged once; second run creates nothing ────


@pytest.mark.asyncio
async def test_t18_ord2037_flagged_once_and_second_run_creates_nothing(
    monitor_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]
    monitor_runs = sym["monitor_runs"]
    monitor_events = sym["monitor_events"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    store: FixtureStoreClient = monitor_env["store"]
    app = monitor_env["app"]

    # T0_BASE = 2026-11-08T12:00:00Z -> 17:30 IST.
    # ORD-2037 has status="exception", eta=T0-4d = 2026-11-04 (IST).
    svc_ctx = build_service_ctx(clock, "req-t18-run-1")
    res1 = await run_monitor(svc_ctx, clock, store_client=store, engine=engine)

    assert res1.run_id.startswith("MON-")
    assert res1.status == "completed"
    assert res1.scanned == 3  # SHP-1048, SHP-2037, SHP-1070
    assert res1.flagged == 2  # ORD-2037 (options), ORD-1070 (lost)
    assert res1.notified == 2
    assert res1.skipped_duplicates == 0
    assert res1.cases_created == 1  # ORD-1070 only (ORD-2037 is 4d < 5d)

    with read_tx(engine) as conn:
        run_rows = conn.execute(select(monitor_runs)).fetchall()
        assert len(run_rows) == 1
        assert run_rows[0].run_id == res1.run_id
        assert run_rows[0].scanned == 3
        assert run_rows[0].flagged == 2
        assert run_rows[0].notified == 2
        assert run_rows[0].skipped_duplicates == 0
        assert run_rows[0].cases_created == 1

        obx_2037 = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == "delay:ORD-2037:2026-11-04:options"
            )
        ).fetchall()
        assert len(obx_2037) == 1
        assert obx_2037[0].template == "delay.notice"
        assert obx_2037[0].channel == "email"
        assert obx_2037[0].customer_id == "C002"
        assert obx_2037[0].status == "queued"

        cev_2037 = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C002",
                client_events.c.type == "monitor.notice",
            )
        ).fetchall()
        assert len(cev_2037) == 1
        payload_2037 = json.loads(str(cev_2037[0].payload_json))
        assert payload_2037["order_id"] == "ORD-2037"
        assert payload_2037["kind"] == "options"
        assert str(payload_2037["eta_at"]).startswith("2026-11-04")
        chips = payload_2037.get("chips", [])
        chip_map = {str(c["label"]): c for c in chips}
        assert "Wait" in chip_map
        assert "Cancel if not delivered" in chip_map
        assert "Talk to support" in chip_map
        assert chip_map["Wait"]["action"] == "track_order"
        assert chip_map["Wait"]["order_id"] == "ORD-2037"
        assert chip_map["Cancel if not delivered"]["action"] == "talk_to_human"
        assert chip_map["Cancel if not delivered"]["order_id"] == "ORD-2037"
        assert chip_map["Talk to support"]["action"] == "talk_to_human"
        assert chip_map["Talk to support"]["order_id"] == "ORD-2037"

        mev_2037 = conn.execute(
            select(monitor_events).where(
                monitor_events.c.order_id == "ORD-2037",
            )
        ).fetchall()
        assert len(mev_2037) == 1
        assert mev_2037[0].key == "ORD-2037:2026-11-04:options"
        assert mev_2037[0].customer_id == "C002"
        assert mev_2037[0].kind == "options"

        ord2037_cases = conn.execute(
            select(cases).where(cases.c.order_id == "ORD-2037")
        ).fetchall()
        assert len(ord2037_cases) == 0

        aud_rows = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "monitor_run")
        ).fetchall()
        assert len(aud_rows) >= 1
        chain = AuditService.verify_chain(conn)
        assert chain.ok is True

    # Run 2 immediately: creates nothing new (T18)
    svc_ctx2 = build_service_ctx(clock, "req-t18-run-2")
    res2 = await run_monitor(svc_ctx2, clock, store_client=store, engine=engine)
    assert res2.run_id != res1.run_id
    assert res2.status == "completed"
    assert res2.scanned == 3
    assert res2.flagged == 2
    assert res2.notified == 0
    assert res2.skipped_duplicates == 2
    assert res2.cases_created == 0
    assert res2.actions_taken == []

    with read_tx(engine) as conn:
        assert len(conn.execute(select(monitor_runs)).fetchall()) == 2
        assert len(conn.execute(select(notifications_outbox)).fetchall()) == 2
        assert len(conn.execute(select(monitor_events)).fetchall()) == 2
        assert (
            len(
                conn.execute(
                    select(client_events).where(client_events.c.type == "monitor.notice")
                ).fetchall()
            )
            == 2
        )
        assert len(conn.execute(select(cases)).fetchall()) == 1

    # Verify guided chip actions work deterministically even when LLM is down
    monkeypatch.setenv("DEV_FAULT_LLM_DOWN", "true")
    get_settings.cache_clear()
    reset_chat_pipeline()
    c2_headers = {"Authorization": f"Bearer {monitor_env['tokens']['c002']}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        ses_resp = await client.post("/v1/chat/sessions", headers=c2_headers)
        assert ses_resp.status_code == 200
        ses_id = ses_resp.json()["session_id"]

        wait_resp = await client.post(
            f"/v1/chat/sessions/{ses_id}/messages",
            headers=c2_headers,
            json={
                "action": {
                    "name": chip_map["Wait"]["action"],
                    "order_id": chip_map["Wait"]["order_id"],
                },
                "client_msg_id": "chip-wait-2037",
            },
        )
        assert wait_resp.status_code == 200
        wait_sse = _parse_sse(wait_resp.text)
        wait_cards = [e["data"] for e in wait_sse if e["event"] == "card"]
        wait_finals = [e["data"] for e in wait_sse if e["event"] == "final"]
        assert len(wait_finals) == 1
        assert "ORD-2037" in wait_finals[0]["full_text"] or any(
            "ORD-2037" in json.dumps(c) for c in wait_cards
        )

        sup_resp = await client.post(
            f"/v1/chat/sessions/{ses_id}/messages",
            headers=c2_headers,
            json={
                "action": {
                    "name": chip_map["Cancel if not delivered"]["action"],
                    "order_id": chip_map["Cancel if not delivered"]["order_id"],
                },
                "client_msg_id": "chip-cancel-2037",
            },
        )
        assert sup_resp.status_code == 200
        sup_sse = _parse_sse(sup_resp.text)
        assert any(
            e["event"] == "card" and e["data"].get("card_type") == "case_created" for e in sup_sse
        )
    monkeypatch.setenv("DEV_FAULT_LLM_DOWN", "false")
    get_settings.cache_clear()
    reset_chat_pipeline()

    # Edge cases on fresh in-memory engine: duplicate shipment in single page,
    # repeated cursor loop guard, and 200-shipment hard cap
    from sellershield_agent.db import create_engine_from_url

    dup_engine = create_engine_from_url("sqlite:///:memory:")
    try:
        init_db(dup_engine, get_metadata())
        dup_row = {
            "shipment_id": "SHP-2037",
            "order_id": "ORD-2037",
            "customer_id": "C002",
            "carrier": "Delhivery",
            "tracking_no": "TRK-2037",
            "status": "exception",
            "eta_at": (T0_BASE - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        }
        dup_store = _SyntheticMonitorStore(
            clock=clock,
            pages=[([dup_row, dup_row], None)],
        )
        dup_res = await run_monitor(
            build_service_ctx(clock, "req-dup-page"),
            clock,
            store_client=dup_store,
            engine=dup_engine,
        )
        assert dup_res.notified == 1
        assert dup_res.skipped_duplicates >= 1

        # 250 shipments across 3 pages -> capped strictly at 200; plus repeated cursor guard
        many_rows = [
            {
                "shipment_id": f"SHP-{idx:04d}",
                "order_id": f"ORD-{idx:04d}",
                "customer_id": "C001",
                "carrier": "BlueDart",
                "tracking_no": f"TRK-{idx:04d}",
                "status": "in_transit",
                "eta_at": (T0_BASE + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "delayed": False,
            }
            for idx in range(9001, 9251)
        ]
        cap_store = _SyntheticMonitorStore(
            clock=clock,
            pages=[
                (many_rows[:100], "1"),
                (many_rows[100:200], "2"),
                (many_rows[200:250], None),
            ],
        )
        cap_engine = create_engine_from_url("sqlite:///:memory:")
        try:
            init_db(cap_engine, get_metadata())
            cap_res = await run_monitor(
                build_service_ctx(clock, "req-cap-200"),
                clock,
                store_client=cap_store,
                engine=cap_engine,
            )
            assert cap_res.scanned == 200
            assert cap_store.list_calls <= 3
        finally:
            cap_engine.dispose()

        loop_store = _SyntheticMonitorStore(
            clock=clock,
            pages=[
                (many_rows[:5], "loop"),
                (many_rows[5:10], "loop"),
            ],
        )
        loop_engine = create_engine_from_url("sqlite:///:memory:")
        try:
            init_db(loop_engine, get_metadata())
            loop_res = await run_monitor(
                build_service_ctx(clock, "req-loop-guard"),
                clock,
                store_client=loop_store,
                engine=loop_engine,
            )
            assert loop_res.scanned <= 200
            assert loop_store.list_calls <= 2
        finally:
            loop_engine.dispose()
    finally:
        dup_engine.dispose()


# ── Test 2: ORD-1070 lost shipment creates single case + outbox delivery ──


@pytest.mark.asyncio
async def test_ord1070_lost_shipment_creates_single_case_and_brief(
    monitor_env: dict[str, Any],
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    store: FixtureStoreClient = monitor_env["store"]

    res1 = await run_monitor(
        build_service_ctx(clock, "req-1070-1"),
        clock,
        store_client=store,
        engine=engine,
    )
    assert res1.cases_created == 1

    with read_tx(engine) as conn:
        case_rows = conn.execute(
            select(cases).where(cases.c.order_id == "ORD-1070")
        ).fetchall()
        assert len(case_rows) == 1
        c = case_rows[0]
        assert c.customer_id == "C001"
        assert c.category == "lost_shipment"
        assert c.priority == "high"
        assert c.status == "open"
        assert c.handler == "agent"
        brief = json.loads(str(c.brief_json))
        assert brief["recommended_next_step"] == "reship or refund with approval"
        assert brief["order_id"] == "ORD-1070"
        assert brief["customer_id"] == "C001"
        assert brief["category"] == "lost_shipment"

        evt_rows = conn.execute(
            select(case_events).where(case_events.c.case_id == c.case_id)
        ).fetchall()
        assert len(evt_rows) == 1
        assert evt_rows[0].kind == "case_opened"

        obx_1070 = conn.execute(
            select(notifications_outbox).where(
                notifications_outbox.c.dedupe_key == "delay:ORD-1070:2026-11-06:lost"
            )
        ).fetchall()
        assert len(obx_1070) == 1
        assert obx_1070[0].customer_id == "C001"

        cev_1070 = conn.execute(
            select(client_events).where(
                client_events.c.customer_id == "C001",
                client_events.c.type == "monitor.notice",
            )
        ).fetchall()
        assert len(cev_1070) == 1
        p1070 = json.loads(str(cev_1070[0].payload_json))
        assert p1070["order_id"] == "ORD-1070"
        assert p1070["kind"] == "lost"

    # Advance 2 hours (beyond open_case's 30-min dedup window) and run again -> 0 new cases
    clock.advance(timedelta(hours=2))
    res2 = await run_monitor(
        build_service_ctx(clock, "req-1070-2"),
        clock,
        store_client=store,
        engine=engine,
    )
    assert res2.notified == 0
    assert res2.cases_created == 0
    with read_tx(engine) as conn:
        rows_1070 = conn.execute(select(cases).where(cases.c.order_id == "ORD-1070")).fetchall()
        assert len(rows_1070) == 1

    # Outbox worker dispatches queued emails strictly to DB-resolved customer addresses
    sent_count = await process_outbox_batch(engine, store, clock)
    assert sent_count >= 2
    recipients = {mail["to"] for mail in store.mailbox}
    assert "aarav.mehta@example.test" in recipients
    assert "diya.nair@example.test" in recipients


# ── Test 3: On-time, minor delays, RTO/delivered & no-customer-token gate ─


@pytest.mark.asyncio
async def test_ontime_minor_delays_and_no_customer_token_enforced(
    monitor_env: dict[str, Any],
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    store: FixtureStoreClient = monitor_env["store"]

    assert isinstance(store, StoreClient)

    # Customer context & service context carrying a non-None token MUST fail closed
    customer_ctx = RequestContext(
        principal=Principal(
            user_id="usr_c001",
            role=Role.customer,
            customer_id="C001",
            jti="jti-cust",
            token_exp=int((T0_BASE + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-cust-forbid",
        token=SecretStr("cust-jwt-secret"),
    )
    tainted_svc_ctx = RequestContext(
        principal=Principal(
            user_id="svc_exec",
            role=Role.service,
            customer_id=None,
            jti="jti-svc-taint",
            token_exp=int((T0_BASE + timedelta(hours=1)).timestamp()),
        ),
        request_id="req-svc-taint",
        token=SecretStr("customer-jwt-smuggled"),
    )

    with pytest.raises(StoreForbidden):
        await store.list_shipments_for_monitor(customer_ctx)
    with pytest.raises(StoreForbidden):
        await store.list_shipments_for_monitor(tainted_svc_ctx)
    with pytest.raises(StoreForbidden):
        await run_monitor(customer_ctx, clock, store_client=store, engine=engine)
    with pytest.raises(StoreForbidden):
        await run_monitor(tainted_svc_ctx, clock, store_client=store, engine=engine)

    # Verify paginated FixtureStoreClient.list_shipments_for_monitor
    clean_svc_ctx = build_service_ctx(clock, "req-page-test")
    page1 = await store.list_shipments_for_monitor(
        clean_svc_ctx,
        statuses=["in_transit", "exception", "lost"],
        limit=2,
    )
    assert len(page1.shipments) == 2
    assert page1.next_cursor is not None
    assert all(s.customer_id is not None for s in page1.shipments)
    page2 = await store.list_shipments_for_monitor(
        clean_svc_ctx,
        statuses=["in_transit", "exception", "lost"],
        limit=2,
        cursor=page1.next_cursor,
    )
    assert len(page2.shipments) == 1
    assert page2.next_cursor is None

    # Only on_time, minor (< 2d), delivered, and rto shipments -> zero actions
    quiet_rows = [
        {
            "shipment_id": "SHP-901",
            "order_id": "ORD-901",
            "customer_id": "C001",
            "carrier": "BlueDart",
            "tracking_no": "TRK-901",
            "status": "in_transit",
            "eta_at": (T0_BASE + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": False,
        },
        {
            "shipment_id": "SHP-902",
            "order_id": "ORD-902",
            "customer_id": "C001",
            "carrier": "BlueDart",
            "tracking_no": "TRK-902",
            "status": "in_transit",
            "eta_at": (T0_BASE - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
        {
            "shipment_id": "SHP-903",
            "order_id": "ORD-903",
            "customer_id": "C002",
            "carrier": "Delhivery",
            "tracking_no": "TRK-903",
            "status": "delivered",
            "eta_at": (T0_BASE - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": False,
        },
        {
            "shipment_id": "SHP-904",
            "order_id": "ORD-904",
            "customer_id": "C002",
            "carrier": "Delhivery",
            "tracking_no": "TRK-904",
            "status": "rto",
            "eta_at": (T0_BASE - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
    ]
    quiet_store = _SyntheticMonitorStore(clock=clock, pages=[(quiet_rows, None)])
    quiet_res = await run_monitor(clean_svc_ctx, clock, store_client=quiet_store, engine=engine)
    assert quiet_res.flagged == 0
    assert quiet_res.notified == 0
    assert quiet_res.cases_created == 0

    with read_tx(engine) as conn:
        assert len(conn.execute(select(notifications_outbox)).fetchall()) == 0
        assert len(conn.execute(select(client_events)).fetchall()) == 0
        assert len(conn.execute(select(cases)).fetchall()) == 0
        assert len(conn.execute(select(sym["monitor_events"])).fetchall()) == 0

    # Verify HttpStoreClient.list_shipments_for_monitor contract + token isolation
    http_store = HttpStoreClient(
        base_url="http://localhost:8000",
        monitor_client_id="agent-monitor",
        monitor_client_secret=SecretStr("mon-secret"),
        clock=clock,
    )
    assert isinstance(http_store, StoreClient)
    with pytest.raises(StoreForbidden):
        await http_store.list_shipments_for_monitor(customer_ctx)
    with pytest.raises(StoreForbidden):
        await http_store.list_shipments_for_monitor(tainted_svc_ctx)

    with respx.mock(assert_all_called=True) as mock_api:
        token_route = mock_api.post("http://localhost:8000/api/v1/auth/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "mon-tok-123", "expires_in": 300}
            )
        )
        shipments_route = mock_api.get("http://localhost:8000/admin/shipments").mock(
            return_value=httpx.Response(
                200,
                json={
                    "shipments": [
                        {
                            "shipment_id": "SHP-2037",
                            "order_id": "ORD-2037",
                            "customer_id": "C002",
                            "carrier": "Delhivery",
                            "tracking_no": "TRK-2037",
                            "status": "exception",
                            # Store API omits tz suffix in SQLite dumps (BUG-006 regression)
                            "eta_at": "2026-11-04T12:00:00",
                            "delayed": True,
                            "events": [],
                        }
                    ],
                    "next_cursor": None,
                },
            )
        )
        http_res = await http_store.list_shipments_for_monitor(
            clean_svc_ctx,
            statuses=["in_transit", "exception", "lost"],
            limit=50,
        )
        assert token_route.call_count == 1
        assert shipments_route.call_count == 1
        sent_req = shipments_route.calls[0].request
        assert sent_req.headers["Authorization"] == "Bearer mon-tok-123"
        assert len(http_res.shipments) == 1
        assert http_res.shipments[0].order_id == "ORD-2037"
        assert http_res.shipments[0].eta_at is not None
        assert http_res.shipments[0].eta_at.tzinfo is not None


# ── Test 4: Run lock serializes concurrent runs & HTTP RBAC/rate-limit ───


@pytest.mark.asyncio
async def test_concurrent_runs_serialize_and_return_running_run_id_and_http_rate_limit(
    monitor_env: dict[str, Any],
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    app = monitor_env["app"]
    tokens = monitor_env["tokens"]

    started_ev = asyncio.Event()
    release_ev = asyncio.Event()
    slow_rows = [
        {
            "shipment_id": "SHP-2037",
            "order_id": "ORD-2037",
            "customer_id": "C002",
            "carrier": "Delhivery",
            "tracking_no": "TRK-2037",
            "status": "exception",
            "eta_at": (T0_BASE - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
        {
            "shipment_id": "SHP-1070",
            "order_id": "ORD-1070",
            "customer_id": "C001",
            "carrier": "BlueDart",
            "tracking_no": "TRK-1070",
            "status": "lost",
            "eta_at": (T0_BASE - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
    ]
    slow_store = _SyntheticMonitorStore(
        clock=clock,
        pages=[(slow_rows, None)],
        pause_events=(started_ev, release_ev),
    )

    task1 = asyncio.create_task(
        run_monitor(
            build_service_ctx(clock, "req-conc-1"),
            clock,
            store_client=slow_store,
            engine=engine,
        )
    )
    await asyncio.wait_for(started_ev.wait(), timeout=2.0)

    # Second concurrent run returns running run_id without scanning the store
    res2 = await run_monitor(
        build_service_ctx(clock, "req-conc-2"),
        clock,
        store_client=slow_store,
        engine=engine,
    )
    assert res2.status == "running"
    assert res2.run_id.startswith("MON-")
    assert slow_store.list_calls == 1

    release_ev.set()
    res1 = await asyncio.wait_for(task1, timeout=2.0)
    assert res1.run_id == res2.run_id
    assert res1.status == "completed"
    assert res1.notified == 2
    assert res1.cases_created == 1

    with read_tx(engine) as conn:
        assert AuditService.verify_chain(conn).ok is True

    # HTTP RBAC, strict schema, rate limit (1 per 10s), no seller JWT forwarding
    spy_store = _SyntheticMonitorStore(clock=clock, pages=[(slow_rows, None)])
    app.dependency_overrides[proposals_api.get_proposal_store_client] = lambda: spy_store
    app.dependency_overrides[sym["get_monitor_store_client"]] = lambda: spy_store

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Unauthenticated -> 401
        r_unauth = await client.post("/v1/seller/monitor/run")
        assert r_unauth.status_code == 401

        # Customer / Service tokens -> 403
        r_cust = await client.post(
            "/v1/seller/monitor/run",
            headers={"Authorization": f"Bearer {tokens['c001']}"},
        )
        assert r_cust.status_code == 403
        r_svc = await client.post(
            "/v1/seller/monitor/run",
            headers={"Authorization": f"Bearer {tokens['service']}"},
        )
        assert r_svc.status_code == 403

        # Unexpected body parameter -> 422
        r_bad_body = await client.post(
            "/v1/seller/monitor/run",
            headers={"Authorization": f"Bearer {tokens['seller_staff']}"},
            json={"limit": 9999},
        )
        assert r_bad_body.status_code == 422
        assert r_bad_body.json()["error"]["code"] == "validation_error"

        # Valid seller_staff manual trigger -> 200 matching OpenAPI MonitorRun exactly
        r_run1 = await client.post(
            "/v1/seller/monitor/run",
            headers={
                "Authorization": f"Bearer {tokens['seller_staff']}",
                "Idempotency-Key": "mon-btn-1",
            },
        )
        assert r_run1.status_code == 200
        body1 = r_run1.json()
        assert set(body1.keys()) == {
            "run_id",
            "checked_orders_count",
            "anomalies_detected",
            "actions_taken",
            "created_at",
        }
        assert body1["run_id"].startswith("MON-")
        assert body1["actions_taken"] == []
        # Store call used Role.service with token=None (caller seller JWT never forwarded)
        assert len(spy_store.captured_contexts) == 1
        assert spy_store.captured_contexts[0].principal.role == Role.service
        assert spy_store.captured_contexts[0].token is None

        # Spamming within 10 s (even with another staff member and new Idempotency-Key) -> 429
        clock.advance(timedelta(seconds=3))
        r_spam = await client.post(
            "/v1/seller/monitor/run",
            headers={
                "Authorization": f"Bearer {tokens['seller_staff_2']}",
                "Idempotency-Key": "mon-btn-spam",
            },
        )
        assert r_spam.status_code == 429
        assert r_spam.json()["error"]["code"] == "rate_limited"

        # Advance past 10 s cooldown -> succeeds again
        clock.advance(timedelta(seconds=7))
        r_run2 = await client.post(
            "/v1/seller/monitor/run",
            headers={
                "Authorization": f"Bearer {tokens['seller_owner']}",
                "Idempotency-Key": "mon-btn-2",
            },
        )
        assert r_run2.status_code == 200
        body2 = r_run2.json()
        assert body2["run_id"] != body1["run_id"]

        # List & get single run + 404 unknown run
        r_list = await client.get(
            "/v1/seller/monitor/runs",
            headers={"Authorization": f"Bearer {tokens['seller_staff']}"},
        )
        assert r_list.status_code == 200
        runs_body = r_list.json()
        assert set(runs_body.keys()) == {"runs"}
        run_ids = [r["run_id"] for r in runs_body["runs"]]
        assert body2["run_id"] in run_ids and body1["run_id"] in run_ids

        r_get = await client.get(
            f"/v1/seller/monitor/runs/{body1['run_id']}",
            headers={"Authorization": f"Bearer {tokens['seller_staff']}"},
        )
        assert r_get.status_code == 200
        assert r_get.json()["run_id"] == body1["run_id"]

        r_404 = await client.get(
            "/v1/seller/monitor/runs/MON-doesnotexist",
            headers={"Authorization": f"Bearer {tokens['seller_staff']}"},
        )
        assert r_404.status_code == 404
        assert r_404.json()["error"]["code"] == "not_found"


# ── Test 5: Notice isolation, no forbidden words, no PII, XSS safe ─────


@pytest.mark.asyncio
async def test_monitor_notice_has_no_other_customer_data_limits_or_risk_wording(
    monitor_env: dict[str, Any],
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    app = monitor_env["app"]
    tokens = monitor_env["tokens"]

    # MonitorNoticePayload extra='forbid' must reject internal risk/limit fields
    with pytest.raises(ValidationError):
        MonitorNoticePayload.model_validate(
            {
                "order_id": "ORD-2037",
                "kind": "options",
                "message": "Update on ORD-2037",
                "risk_score": 0.9,
            }
        )

    xss_rows = [
        {
            "shipment_id": "SHP-2037",
            "order_id": "ORD-2037",
            "customer_id": "C002",
            "carrier": "<script>alert(1)</script>Delhivery",
            "tracking_no": "TRK<img onerror=alert(1)>2037",
            "status": "exception",
            "eta_at": (T0_BASE - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
        {
            "shipment_id": "SHP-1070",
            "order_id": "ORD-1070",
            "customer_id": "C001",
            "carrier": "BlueDart",
            "tracking_no": "TRK-1070",
            "status": "lost",
            "eta_at": (T0_BASE - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "delayed": True,
        },
    ]
    xss_store = _SyntheticMonitorStore(clock=clock, pages=[(xss_rows, None)])
    await run_monitor(
        build_service_ctx(clock, "req-xss-iso"),
        clock,
        store_client=xss_store,
        engine=engine,
    )

    # Tenant isolation via /v1/me/events
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        ev_c1 = await client.get(
            "/v1/me/events?since=0",
            headers={"Authorization": f"Bearer {tokens['c001']}"},
        )
        ev_c2 = await client.get(
            "/v1/me/events?since=0",
            headers={"Authorization": f"Bearer {tokens['c002']}"},
        )
    assert ev_c1.status_code == 200 and ev_c2.status_code == 200
    c1_events = ev_c1.json()["events"]
    c2_events = ev_c2.json()["events"]
    assert len(c1_events) == 1 and len(c2_events) == 1

    c1_dump = json.dumps(c1_events)
    c2_dump = json.dumps(c2_events)
    assert "ORD-1070" in c1_dump
    assert "ORD-2037" not in c1_dump and "C002" not in c1_dump
    assert "ORD-2037" in c2_dump
    assert "ORD-1070" not in c2_dump and "C001" not in c2_dump

    # No forbidden words, XSS script tags, or customer PII in events or rendered emails
    for raw_dump in (c1_dump, c2_dump):
        assert "<script>" not in raw_dump.lower()
        assert "onerror=" not in raw_dump.lower()
        assert "aarav.mehta" not in raw_dump.lower()
        assert "diya.nair" not in raw_dump.lower()
        assert "+9198" not in raw_dump
        assert not FORBIDDEN_WORDS_RE.search(raw_dump), f"Forbidden term in event: {raw_dump}"

    with read_tx(engine) as conn:
        obx_rows = conn.execute(select(notifications_outbox)).fetchall()
        assert len(obx_rows) == 2
        for row in obx_rows:
            pld = json.loads(str(row.payload_json))
            subj, body = render_template(str(row.template), pld)
            assert "\n" not in subj and "\r" not in subj
            assert "<script>" not in body.lower() and "onerror=" not in body.lower()
            assert not FORBIDDEN_WORDS_RE.search(subj)
            assert not FORBIDDEN_WORDS_RE.search(body)
            assert "aarav.mehta" not in body.lower() and "diya.nair" not in body.lower()


# ── Test 6: 7-day fast-forward flags ORD-1048 & escalates ORD-2037 ──────


@pytest.mark.asyncio
async def test_seven_day_fast_forward_flags_newly_late_and_opens_exception_case_without_duplicates(
    monitor_env: dict[str, Any],
) -> None:
    sym = _load_step_b_symbols()
    run_monitor = sym["run_monitor"]
    monitor_events = sym["monitor_events"]

    engine = monitor_env["engine"]
    clock: FixedClock = monitor_env["clock"]
    store: FixtureStoreClient = monitor_env["store"]

    # Day 0 (T0_BASE): ORD-1048 is on-time, ORD-2037 is 4d late (< 5d), ORD-1070 is lost
    r0 = await run_monitor(
        build_service_ctx(clock, "req-ff-day0"),
        clock,
        store_client=store,
        engine=engine,
    )
    assert r0.scanned == 3
    assert r0.flagged == 2
    assert r0.notified == 2
    assert r0.cases_created == 1  # ORD-1070 only

    # Verify ORD-1048 is genuinely on_time at T0
    shp_1048_t0 = await store.get_shipment(build_service_ctx(clock, "req-chk-1048"), "ORD-1048")
    assert shp_1048_t0.status == ShipmentStatus.in_transit
    assert isinstance(shp_1048_t0, Shipment)

    # Fast-forward simulated clock by 7 days
    clock.advance(timedelta(days=7))

    # Day 7 (T0_BASE + 7d):
    # - ORD-1048 ETA (2026-11-10) is 5d late -> flagged options, notified once, 0 cases
    # - ORD-2037 ETA (2026-11-04) is exception 11d >= 5d -> 1 delivery_delay case, 0 new notif
    # - ORD-1070 lost -> 0 new notifications, 0 duplicate cases
    r7 = await run_monitor(
        build_service_ctx(clock, "req-ff-day7"),
        clock,
        store_client=store,
        engine=engine,
    )
    assert r7.scanned == 3
    assert r7.flagged == 3
    assert r7.notified == 1
    assert r7.skipped_duplicates == 2
    assert r7.cases_created == 1

    with read_tx(engine) as conn:
        all_obx = conn.execute(select(notifications_outbox)).fetchall()
        assert len(all_obx) == 3
        dedupe_keys = {str(row.dedupe_key) for row in all_obx}
        assert dedupe_keys == {
            "delay:ORD-2037:2026-11-04:options",
            "delay:ORD-1070:2026-11-06:lost",
            "delay:ORD-1048:2026-11-10:options",
        }

        all_mev = conn.execute(select(monitor_events)).fetchall()
        assert len(all_mev) == 3

        all_cases = conn.execute(select(cases)).fetchall()
        assert len(all_cases) == 2
        by_order = {str(c.order_id): c for c in all_cases}
        assert set(by_order.keys()) == {"ORD-1070", "ORD-2037"}
        c_2037 = by_order["ORD-2037"]
        assert c_2037.customer_id == "C002"
        assert c_2037.category == "delivery_delay"
        brief_2037 = json.loads(str(c_2037.brief_json))
        assert brief_2037["recommended_next_step"] == "wait or cancel"

    # Run 3 immediately at Day 7: completely idempotent (0 notifications, 0 cases, 3 skipped)
    r7_again = await run_monitor(
        build_service_ctx(clock, "req-ff-day7-repeat"),
        clock,
        store_client=store,
        engine=engine,
    )
    assert r7_again.scanned == 3
    assert r7_again.flagged == 3
    assert r7_again.notified == 0
    assert r7_again.skipped_duplicates == 3
    assert r7_again.cases_created == 0
    assert r7_again.actions_taken == []
