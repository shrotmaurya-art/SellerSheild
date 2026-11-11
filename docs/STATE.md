# STATE (handoff note; update at the end of every session)
<<<<<<< HEAD
Updated: 2026-11-11 16:38 IST
Phase: 3 (Autonomous Operations & Seller Tools) - COMPLETE (pending human freeze & commit)
Current task: TASK T3.6 - Lean red-team and gate tests (DONE - NO COMMIT)
Last completed:
- T3.6: Lean red-team and gate tests (`[NO COMMIT]`).
  - `scenarios/redteam/phase3.yaml`: 30 scenarios across 7 categories (streams: 6, cases & handler: 6, notifications: 4, monitor: 4, memory: 4, dev routes: 2, clumsy-user flows: 4).
  - `agent/tests/redteam/test_phase3.py`: 30 automated red-team tests exercising each scenario using real HTTP app, `ObedientFakeLLM`, write-capable `FixtureStoreClient`, and `FaultPlan`. Invariants verified: tenant isolation across parallel customers, handler immutability, bounded memory, outbox deduplication, no network calls inside write tx, zero forbidden words/secrets in output, zero 500s, cryptographic SHA-256 audit chain verified, and Phase 2 `LedgerOracle` re-computation verified.
  - `agent/tests/e2e/test_gate_g3.py`: 9 Gate G3 tests covering SC-12, SC-17, T13, T15, T16, T17, T18, approval push event to live stream in < 2.0s, and `scripts/demo_run.py` passing all 4 beats twice consecutively.
  - Sabotage drills: verified 3 defenses (monitor run lock, outbox dedupe key, template HTML escaping) fail tests immediately upon sabotage and pass upon restoration.
- T3.7: Real store: mail and shipments (`[NO COMMIT]`).
  - `store/http_client.py`: Extended `HttpStoreClient` with `send_mail` and `list_shipments_for_monitor`. Enforced `ServiceContext` (`Role.service`) and immediate rejection of customer bearer tokens with `StoreForbidden`. Mints authenticated service tokens (`executor` and `monitor`), passes `X-Request-ID` and `Idempotency-Key` headers, sends exact OpenAPI `SendMailRequest` payload, applies zero retries on mutating `POST` writes, and provides fallback to `/admin/orders` + `/orders/{oid}/shipment` mapping `customer_id`.
  - `scripts/handshake_phase3.py`: Standalone verification script (`uv run python scripts/handshake_phase3.py` or from `agent/` as `uv run python ../scripts/handshake_phase3.py`). Dispatches test notification to C001, reads and verifies delivery in C001 store inbox (`/mail/inbox`), queries monitor shipments, and prints delayed shipment count. Zero token logging (KP-13).
  - `agent/tests/test_store_http_phase3.py`: 15 respx contract tests passing in 6.32s.
  - Live verification against running NestKart store: sent notification `MSG-444D499C`, verified in inbox, retrieved 19 shipments, detected 2 delayed (`ORD-1070` lost, `ORD-2037` exception), and exited 0.
- T3.4: Proactive delay monitor (`[NO COMMIT]`).
  - `domain/state.py`, `domain/models.py`: `monitor_runs`, `monitor_events` tables, `MonitorShipment`, `ShipmentListResponse` schemas.
  - `store/client.py`, `store/fixtures.py`, `store/http_client.py`: `list_shipments_for_monitor(service_ctx, statuses, limit, cursor)` with relative T0 offset resolution, cursor pagination, customer mapping, and service role authorization with zero customer token (`StoreForbidden`).
  - `monitor/service.py`: `run_monitor(service_ctx, clock)` scanning up to 200 shipments with loop guard and run lock (`_run_lock`), categorizing timeliness, enqueuing `delay.notice` outbox notifications with strict dedupe key `delay:{order_id}:{eta_date}:{kind}`, emitting `monitor.notice` client events with 3 guided action chips ("Wait", "Cancel if not delivered", "Talk to support"), and opening high-priority cases with deterministic briefs for lost shipments and carrier exceptions >= 5 days.
  - `monitor/router.py`, `main.py`, `api/seller_stub.py`: `POST /v1/seller/monitor/run` (10s cooldown returning 429 `rate_limited`) and `GET /v1/seller/monitor/runs(+/{id})`.
  - `agent/tests/test_monitor.py`: 6/6 tests passing in 1.56s.
  - Sabotage drills: verified dedupe key and run lock removals fail tests immediately.
- T3.5: Overview, dev controls and demo run script (`[NO COMMIT]`).
  - `overview/router.py`, `main.py`: `GET /v1/seller/overview` returning pending approvals (Clock overdue calculation), open cases by priority, failed notification IDs, last monitor run summary, and today's metrics using IST (`Asia/Kolkata`) date partition. Strictly ID-only output with zero customer PII and zero forbidden internal words.
  - `dev/faults.py`, `dev/router.py`, `api/health.py`: In-memory fault injection (`llm_down`, `audit_fail`, `mail_down`, `slow_llm`, `off`) shown in `/readyz`. Guarded strictly by `require_dev_mode` returning 404 before auth/JWKS if `DEV_MODE=false`.
  - `dev/router.py`, `audit/service.py`: `POST /v1/dev/reset` clears 19 tables, resets faults/cooldowns, reloads policies `v1`..`v3` (`v3` active), and restarts the SHA-256 audit chain with verified genesis event at `seq=1`.
  - `scripts/demo_run.py`: Standalone dev tool runnable as `uv run python ../scripts/demo_run.py` from `agent/`. Executes system reset followed by 4 beats through real HTTP APIs (C001 order status query; C001 defective return & refund lifecycle; C002 IDOR denial & C005 injection defense; proactive delay monitor run & overview check). Exits 0 on success.
  - `agent/tests/test_t35_overview_dev_demo.py`: 6/6 tests passing in `40.31s`.
  - Repeatability verification: `uv run python ../scripts/demo_run.py` run twice consecutively from `agent/`, both runs completely passed on all 4 beats.
- T3.2: Push stream, notification outbox and notification endpoints (`[NO COMMIT]`).
  - `events/router.py`, `events/emit.py`, `db.py`: Implemented `GET /v1/chat/stream` (SSE push + `Last-Event-ID`/`?since=` resume + `422` validation before slot acquisition + `20 s` heartbeat + `2` streams/customer `429` cap + `2 s` DB fallback + post-commit cross-thread `ClientEventNotifier` via `write_tx`), `GET /v1/me/events?since=&limit=` (`1..100`), strict `extra="forbid"` payloads rejecting risk/internal fields, and wired `approval.decided`, `refund.updated`, `proposal.expired`, and `notification.new`.
  - `outbox/worker.py`, `outbox/templates.py`, `outbox/enqueue.py`, `outbox/templates/*.txt`, `main.py`, `store/*`: Lifespan outbox worker claiming `channel == "email"` atomically (`queued`/`failed`/`retry_scheduled` -> `sending`), calling `StoreClient.send_mail` (`Role.service`) strictly outside DB locks (`1 s / 5 s / 30 s` backoff -> `exhausted` after 3 attempts), DB-owner strict recipient resolution ignoring request/payload emails, and 5 fixed English HTML-escaped, ID-only-subject templates passing forbidden-word checks.
  - `outbox/router.py` & `api/seller_stub.py`: Implemented `GET /v1/seller/notifications` (`?status=`) and atomic `POST /v1/seller/notifications/{id}/retry` (resets `failed`/`exhausted` once with same-tx audit); removed shadowed `501` stubs.
  - `agent/tests/test_push_outbox.py` (14/14 passing) & `agent/tests/test_audit_mutators.py` (updated `test_t19` outbox row assertion for parallel `seller_stub` + `email` rows).
  - Live curl check (`scripts/dev_auth.py` + `uvicorn :8001` + `curl.exe -N /v1/chat/stream`) received live `approval.decided` SSE event in `0.047 s`; all 4 sabotage drills passed cleanly.
- T3.3: Structured customer memory (`DONE - NO COMMIT`; `agent/tests/test_memory.py` 6/6 passing).
- T3.1: Cases, escalation triggers and handler model (`DONE - NO COMMIT`; `agent/tests/test_cases_escalation.py` 10/10 passing).
Verification:
- `just agent-check`: PASS (`ruff check .` clean, `mypy src` clean across `143` source files, `pytest -q` `735 passed in 334.70s`, `test_guard.py check` PASS).
- Named T3.6 red-team and gate checks (`uv run pytest tests/redteam tests/e2e/test_gate_g3.py -q`): PASS (`168 passed in 145.81s`).
- Live demo script (`uv run python ../scripts/demo_run.py` run twice consecutively): PASS (both runs passed all 4 beats).
Next step:
- Phase 3 is fully implemented and verified (T3.0 - T3.7 all DONE). Human runs `just freeze-tests` and commits the Phase 3 milestone.
Open questions for the human:
1. None.
Broken or half-done: none.
Files in flux (do not touch): none (`[NO COMMIT]` preserved across all Phase 3 tasks).
Reminder: no GitHub remote exists until 11 Nov. Never push.
=======
<<<<<<< HEAD
Updated: 2026-11-11 11:15 IST
Phase: 0 (Foundations, agent path). Gate G0: ready for evaluation
Current task: T0.11
Last completed: T0.10 (LLM adapter and provider chain)
Next step: T0.11 (Gate G0, local commit and tag)
Open questions for the human: Run `uv run python scripts/llm_smoke.py` with real keys in `agent/.env` (never let agent open .env) to verify live provider connectivity.
Broken or half-done: none
Files in flux (do not touch): none
Reminder: no GitHub remote exists until 11 Nov. Never push.
=======
Updated: YYYY-MM-DD HH:MM IST
Phase: 0 (Foundations, agent path). Gate G0: not passed
Current task: T0.1
Last completed: none
Next step: (exact next action)
Open questions for the human: none
Broken or half-done: none
Files in flux (do not touch): none
Reminder: no GitHub remote exists until 11 Nov. Never push.
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
