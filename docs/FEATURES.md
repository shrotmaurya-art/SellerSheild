# FEATURES (what each feature does and how it works; read before touching it)

Entry format:
## F-Axx Name   [planned | in-progress | done | patched]
What it does:
How it works (flow and key files):
Invariants that must stay true:
Tests (paths):
Known limits:
Do not change because:
Related: BUG ids, ADR ids, spec sections

## F-A36 Phase 3 Red-Team and Gate G3 Acceptance Suite   [done]
What it does:
Provides rigorous automated red-team security, isolation, and robustness verification across 30 attack/clumsy scenarios and 9 Gate G3 end-to-end acceptance tests.
How it works (flow and key files):
- `scenarios/redteam/phase3.yaml`: 30 scenarios defining inputs, attacker personas, categories, and expectations across streams, cases, notifications, monitor, memory, dev routes, and clumsy-user flows.
- `agent/tests/redteam/test_phase3.py`: Automated test harness mapping each scenario to a dedicated pytest test, driving the real HTTP FastAPI app with `ObedientFakeLLM`, `FixtureStoreClient`, and `FaultPlan`. Asserts cross-customer isolation, handler immutability, agent silence during human takeover, outbox deduplication, zero external network calls during transactions, zero 500 errors, cryptographic audit chain continuity, and Phase 2 `LedgerOracle` consistency.
- `agent/tests/e2e/test_gate_g3.py`: Comprehensive Gate G3 acceptance verification covering SC-12, SC-17, T13, T15, T16, T17, T18, sub-2-second approval push stream delivery, and consecutive successful execution of `scripts/demo_run.py`.
Invariants that must stay true:
- No customer can observe another customer's SSE push events or case data.
- The LLM agent never speaks on a case while assigned to human handling.
- Customer memory holds only bounded strings, IDs, and enums; instructions in text fields are never treated as commands.
- Notifications are deduplicated idempotently; mail provider failures never crash the agent or rollback business transactions.
- Zero secrets, risk scores, tokens, or forbidden words in customer-visible responses.
- Cryptographic SHA-256 audit chain remains unbroken and verifiable at all times.
Tests (paths):
- `agent/tests/redteam/test_phase3.py` (30 tests)
- `agent/tests/e2e/test_gate_g3.py` (9 tests)
Known limits:
- Tests run against write-capable fixture store and fake LLM; real LLM live execution is exercised in live demo runs.
Do not change because:
- Protects the system against regression, authorization bypass, event leakage, memory poisoning, and transaction corruption.
Related: Spec §13, §15, §21, §24, KP-15, KP-22, KP-23, KP-25.

## F-A37 Real Store Mail and Shipments Integration   [done]
What it does:
Enables production HTTP communication with the NestKart store for outbound notifications via `send_mail` and backoffice shipment monitoring via `list_shipments_for_monitor`, along with the Phase 3 handshake script (`scripts/handshake_phase3.py`).
How it works (flow and key files):
- `agent/src/sellershield_agent/store/http_client.py`:
  - `send_mail(service_ctx, to_addr, subject, body_text, channel="email", thread_id=None)`:
    - Enforces service context (`Role.service`) and immediately rejects customer bearer tokens (`StoreForbidden`).
    - Obtains an authenticated service token using client credentials grant.
    - Sends `Authorization: Bearer <service_token>`, `X-Request-ID`, and `Idempotency-Key` headers.
    - Matches OpenAPI `SendMailRequest` schema payload (`to_addr`, `subject`, `body_text`, `channel`).
    - Uses zero retries on mutating `_execute_post` writes per KP-16.
    - Maps 400, 401, 403, 404, 409 (duplicate & idempotency_mismatch), 500, and network/timeout exceptions to `StoreClientError` hierarchy.
  - `list_shipments_for_monitor(service_ctx, statuses=None, limit=50, cursor=None)`:
    - Enforces service context (`Role.service`) and blocks customer tokens (`StoreForbidden`).
    - Obtains monitor service token with client credentials grant.
    - Sends `Authorization: Bearer <monitor_token>` and `X-Request-ID` headers.
    - Queries `/admin/shipments`; gracefully falls back to `/admin/orders` + `/orders/{id}/shipment` if direct admin endpoint returns 404, mapping the order `customer_id` into `MonitorShipment`.
- `scripts/handshake_phase3.py`:
  - Standalone verification script executing 3 steps against running NestKart store:
    1. Sends test notification to C001 via `HttpStoreClient.send_mail`.
    2. Logs in as C001 and verifies notification delivery in customer mailbox `/mail/inbox`.
    3. Queries store shipments via `HttpStoreClient.list_shipments_for_monitor` and prints delayed shipments count.
Invariants that must stay true:
- StoreClient `send_mail` and `list_shipments_for_monitor` require `ServiceContext` and reject customer tokens with `StoreForbidden`.
- Mutating mail writes never retry automatically (KP-16).
- Sensitive tokens and credentials are never logged or echoed (KP-13).
Tests (paths):
- `agent/tests/test_store_http_phase3.py` (15 respx contract tests).
- `scripts/handshake_phase3.py` (live HTTP store integration).
Known limits:
- Store API `/admin/shipments` falls back to querying individual order shipments if the unified shipment route is not exposed.
Do not change because:
- Core integration enabling the notification outbox worker and proactive delay monitor to interact with the real NestKart store over HTTP.
Related: Spec §11, §13, §21, KP-04, KP-13, KP-15, KP-16, CHANGE-002.

## F-A34 Proactive Delay Monitor   [done]
What it does:
Autonomously detects late or lost carrier shipments, notifies affected customers once with guided action chips ("Wait", "Cancel if not delivered", "Talk to support"), and opens high-priority support cases for lost shipments or carrier exceptions lasting 5 days or more.
How it works (flow and key files):
- `agent/src/sellershield_agent/store/client.py`, `store/fixtures.py`, `store/http_client.py`:
  - `list_shipments_for_monitor(service_ctx, statuses, limit, cursor)`: queries in-transit, exception, and lost shipments using service context with zero customer token.
- `agent/src/sellershield_agent/monitor/service.py`:
  - `run_monitor(service_ctx, clock)`:
    - Fetches up to 200 shipments with cursor loop guard.
    - Serializes concurrent runs using `_run_lock`, returning active `run_id` to concurrent callers.
    - Evaluates delay against `Clock` and Asia/Kolkata timezone.
    - If status is `lost`: enqueues `delay.notice` notification, emits `monitor.notice` event, and opens high-priority case with deterministic brief recommendation `"reship or refund with approval"`.
    - If status is `exception` or delay >= 2 days (`options`): enqueues `delay.notice` notification and emits `monitor.notice` event with 3 guided chips; if exception >= 5 days, opens high-priority case with brief recommendation `"wait or cancel"`.
    - Deduplicates using `monitor_events` key `{order_id}:{eta_date}:{kind}`. Repeat runs produce zero new notifications or cases.
    - Writes `monitor_runs` summary and appends audit event with `event_type="monitor_run"`.
- `agent/src/sellershield_agent/monitor/router.py`:
  - `POST /v1/seller/monitor/run`: triggers run (guarded by `require_seller`, 10s cooldown returning 429 `rate_limited`).
  - `GET /v1/seller/monitor/runs(+/{id})`: returns monitor run summaries.
Invariants that must stay true:
- Monitor queries store using service credentials only; caller customer token is never forwarded.
- Deduplication keys ensure no duplicate notification or event is created for the same order, ETA date, and kind.
- High-priority cases are opened only for lost shipments or carrier exceptions of 5 days or more; in-transit shipments do not open cases.
- Notice copy and events contain zero cross-customer data, internal limits, or risk wording.
Tests (paths):
- `agent/tests/test_monitor.py` (6 tests).
Known limits:
- At most 200 shipments scanned per monitor run.
Do not change because:
- Core autonomous agent capability demonstrating proactive support and 20-second demo beat.
Related: Spec §5 (W2, W9), §9.5, §13, §21, BUG-006, KP-06, KP-16, KP-23.

## F-A35 Seller Overview, Dev Controls & End-to-End Demo Script   [done]
What it does:
Provides the unified seller dashboard overview (`GET /v1/seller/overview`), developer fault injection controls (`POST /v1/dev/faults`), state reset (`POST /v1/dev/reset`), and an automated end-to-end verification script (`scripts/demo_run.py`) executing all 4 core beats.
How it works (flow and key files):
- `agent/src/sellershield_agent/overview/router.py`:
  - `GET /v1/seller/overview` enforces seller role (`seller_staff` or `seller_owner`), partitions today's metrics using the IST (`Asia/Kolkata`) calendar date, calculates overdue approvals by comparing `due_utc <= Clock.now()`, lists open cases by priority (`high` and `normal`), returns failed/exhausted notification IDs, and reports the latest monitor run summary. Returns clean zero counts and empty lists on an empty DB, with strict ID-only values and zero customer PII.
- `agent/src/sellershield_agent/dev/faults.py` & `dev/router.py`:
  - In-memory fault injection manager supporting `llm_down`, `audit_fail`, `mail_down`, `slow_llm`, `off`. Exposed in `/readyz` health checks.
  - Guarded strictly by `require_dev_mode` returning 404 before auth/JWKS if `DEV_MODE=false`.
  - `POST /v1/dev/reset` clears 19 state tables, resets in-memory faults and cooldowns, reloads policies `v1`..`v3` (`v3` active), and resets the SHA-256 audit chain with a verified genesis event at `seq=1`.
- `scripts/demo_run.py`:
  - Dev tool runnable as `uv run python ../scripts/demo_run.py` from `agent/`. Executes system reset followed by 4 beats through real HTTP APIs (C001 order status; C001 defective return & refund lifecycle; C002 IDOR denial & C005 injection defense; proactive delay monitor run & overview check). Exits 0 on success.
Invariants that must stay true:
- Dev routes strictly return 404 when `DEV_MODE` is false, before executing any JWT verification or JWKS network fetches.
- Fault injections are kept in memory only and never survive an application restart or leak across tests.
- `/v1/seller/overview` outputs strictly entity IDs; customer names, emails, phones, and forbidden internal words are never leaked.
- Dev reset restarts the audit hash chain with a verified genesis event at `seq=1` such that `AuditService.verify_chain().ok is True`.
Tests (paths):
- `agent/tests/test_t35_overview_dev_demo.py` (6 tests covering 404 dev guard, RBAC 401/403, fault injection llm_down/guided mode, audit_fail/mail_down, dev reset & genesis chain, seller overview counts & zero PII, 4-beat end-to-end flow)
- `scripts/demo_run.py` (standalone runnable tool)
Known limits:
- Dev faults are stored in-process in memory.
Do not change because:
- Core operational dashboard, demo stability, and developer reset controls.
Related: ADR-040, KP-17, KP-24, Spec 02 §11, §14.7, §24

## F-A32 Push SSE Stream, Notification Outbox and Seller Notification Endpoints   [done]
What it does:
Delivers customer status updates in real time over Server-Sent Events (`GET /v1/chat/stream`) and cursor-paginated JSON polling (`GET /v1/me/events`), processes asynchronous email notifications cleanly via an atomic transactional outbox worker outside DB locks (`StoreClient.send_mail`) with `1 s / 5 s / 30 s` retry backoffs and fixed English HTML-escaped templates, and exposes seller monitoring/retry controls (`GET /v1/seller/notifications`, `POST /v1/seller/notifications/{id}/retry`).
How it works (flow and key files):
- `agent/src/sellershield_agent/events/emit.py` & `db.py`:
  - Validates event payloads strictly per event type (`extra="forbid"`, rejecting risk/limit/internal fields), assigns globally unique `CEV-...` IDs, and fires thread-safe post-commit wakeups (`conn.info["_after_commit"]`) to active customer `asyncio` stream queues.
- `agent/src/sellershield_agent/events/router.py`:
  - `GET /v1/chat/stream` enforces customer role, validates `Last-Event-ID` > `?since=` (`422` before stream slot acquisition), caps at `2` open streams per customer (`429`), streams typed events with `20 s` heartbeats + `2 s` DB fallback floor, and frees slots/tasks cleanly on disconnect. `GET /v1/me/events?since=&limit=` (`1..100`) mirrors the stream over JSON.
- `agent/src/sellershield_agent/outbox/enqueue.py`, `templates.py`, `worker.py`, `outbox/templates/*.txt`, `main.py`:
  - Lifespan worker checks due `channel == "email"` rows, atomically transitions claimed rows to `sending`, resolves recipients strictly from DB/store owner records (ignoring payload emails), renders fixed forbidden-word-clean templates with ID-only subjects and HTML-escaped bodies, calls `StoreClient.send_mail` (`Role.service`) strictly outside transactions, and settles `sent` / `failed` (`1 s -> 5 s -> 30 s`) / `exhausted` (`3` attempts).
- `agent/src/sellershield_agent/outbox/router.py` & `api/seller_stub.py`:
  - `GET /v1/seller/notifications` (`?status=`) and atomic `POST /v1/seller/notifications/{id}/retry` (resets `failed`/`exhausted` once with same-tx audit).
Invariants that must stay true:
- Customer event streams and polls strictly filter by verified `principal.customer_id`; cross-tenant cursors reveal zero foreign events.
- Email sends never occur inside SQLite write transactions, never reuse customer JWTs, and never roll back returns, refunds, or approvals on mail failure (`T15`).
- Recipient email is resolved strictly from database/store owner records, never from customer text or payload input fields.
- Templates contain zero forbidden words (`limit`, `threshold`, `risk`, `score`, `staff`, `owner`, `auto`, `cap`) and subjects permit strictly sanitized entity IDs only.
Tests (paths):
- `agent/tests/test_push_outbox.py` (14 tests covering real HTTP SSE latency, polling, `Last-Event-ID`, tenant isolation, forbidden fields, 429 limits, disconnect cleanup, T15 mail retries/exhaustion, dedupe, recipient safety, tx spy, template whitelist/XSS, retry endpoint, `proposal.expired`, `refund.updated`)
- `agent/tests/test_phase3_shared.py` (3 tests)
Known limits:
- Per-customer SSE stream cap (2) is enforced per worker process in memory (`_active_customer_streams`).
Do not change because:
- Core live-demo beat for real-time customer updates and fault-isolated email delivery.
Related: ADR-039, BUG-011, KP-01, KP-02, KP-17, KP-23, Spec 02 §9.6, §11, §21 finding 3 & amendment 3, §24

## F-A33 Structured Customer Memory   [done]
What it does:
Maintains visible, safe long-term customer memory holding exclusively enums, validated IDs, integer counts, and ISO-8601 timestamps (all strings `<= 64` characters, zero free text), surfaces live-lookup greeting chips in new chat sessions, isolates a validated JSON snapshot inside `<data><memory>` for query understanding, and supports atomic customer-initiated erasure via `DELETE /v1/me/memory`.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/state.py` & `memory/models.py`:
  - `customer_memory` and `memory_events` tables plus `CustomerProfile` (`extra="forbid"`, `preferred_language`, `last_order_discussed`, `open_item_ids` <= 20, `counts`, `last_seen_at`).
- `agent/src/sellershield_agent/memory/writer.py`:
  - Deterministic in-transaction writer `record_memory_event_in_tx` validates enums, ID prefixes (`ORD`, `RET`, `RFD`, `CASE`, `PRP`, `APR`), and ISO timestamps <= 64 chars, keeping counts idempotent per `ref_id`, removing terminal items, and clamping `open_item_ids` to 20.
  - Wired in-tx from `execution/finalize_db.py`, `execution/refund_webhook_db.py`, `approvals/decide_db.py`, `approvals/withdraw_db.py`, and `cases/api.py`.
- `agent/src/sellershield_agent/memory/snapshot.py`, `api/chat.py`, `orchestrator/pipeline.py`, `resolver.py`, `tools/read_tools.py`, `compose/templates.py`, `compose/factsheet.py`:
  - `snapshot(customer_id)` builds greeting chips (`Check return RET-xxxx`, `Check refund RFD-xxxx`, `Check case CASE-xxxx`) without status text; tapping a chip routes directly to live read tools/views and verified responses; `api/chat.py:get_pipeline` honors `app.dependency_overrides[get_store_client]`.
- `agent/src/sellershield_agent/understand/service.py` & `memory/router.py`:
  - `understand()` injects sanitized JSON inside `<data><memory>` using verified `ctx.customer_id` and `CustomerProfile`; `DELETE /v1/me/memory` deletes caller memory atomically with an `erase_memory` audit event and returns `{"erased": true}`.
Invariants that must stay true:
- No customer free text, `reason_text`, message text, or string longer than 64 characters is ever persisted in `customer_memory` or `memory_events`.
- Neither the policy engine (`policy/`) nor the tool gateway (`tools/`) reads or imports memory.
- Greeting chips never embed static status text and always query live store/case state on tap.
- Customer B can never read, infer, probe, or delete Customer A's memory.
Tests (paths):
- `agent/tests/test_memory.py` (6 tests, including Hypothesis free-text/64-char property test and AST coupling check)
Known limits:
- `open_item_ids` stores the 20 most recent active `RET-`, `RFD-`, and `CASE-` items.
Do not change because:
- Safe-by-design structured memory prevents stored prompt injection and cross-tenant data leakage while enabling fast return/refund/case continuity across sessions.
Related: ADR-038, KP-23, KP-25, Spec 02 §8, §13, §24

## F-A31 Support Cases, Escalation Triggers and Handler Model   [done]
What it does:
Creates durable customer support cases upon escalation triggers (human request, legal/threat keywords, policy ESCALATE), generates deterministic structured briefs for sellers without LLM text or prompt injection vulnerabilities, provides customer and seller case thread endpoints, and implements an atomic handler model so sellers and the autonomous agent never talk over each other.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/state.py`:
  - `cases` table tracks `case_id`, `customer_id`, `order_id`, `session_id`, `category`, `priority`, `status`, `handler` (`agent`/`human`), `brief_json`, `sla_due_at`, and optimistic locking `version`.
  - `case_events` logs append-only event stream of messages, status transitions, assignments, and handbacks.
- `agent/src/sellershield_agent/orchestrator/escalation.py`:
  - Evaluates escalation triggers: legal/threat keywords (high priority), policy `ESCALATE` (case), customer human request (case), and angry twice in a row (interactive `Talk to Human` chip without opening a case).
  - Enforces 30-minute idempotency per `(customer_id, category, order_id)`.
  - Records an immutable `AuditEvent` on every trigger evaluation.
- `agent/src/sellershield_agent/cases/brief.py`:
  - Builds structured deterministic brief: customer request is sanitized of secret tokens and HTML-escaped with `verified: False`.
  - Next-step recommendations derive strictly from hardcoded rule tables (`lost_shipment` -> "reship or refund with approval", `delivery_delay` -> "wait or cancel", `damaged_item`/`wrong_item` -> "review", `angry` -> "reply within the SLA"). Customer text cannot influence recommendations.
  - Zero model-generated text is placed in the brief.
- `agent/src/sellershield_agent/cases/router.py` & `api.py`:
  - Customer endpoints (`GET /v1/me/cases`, `GET /v1/me/cases/{id}`, `POST /v1/me/cases/{id}/messages`) sanitize internal seller notes and enforce uniform 404 on unowned cases.
  - Seller endpoints (`GET /v1/seller/cases`, `GET .../cases/{id}`, `POST .../messages`, `POST .../status`, `POST .../assign`, `POST .../handback`) manage thread.
  - Seller reply atomically flips `handler` to `human` via compare-and-set and syncs session state.
  - Thread events trigger `emit_client_event` and `enqueue_notification`.
- `agent/src/sellershield_agent/orchestrator/pipeline.py`:
  - When `session.handler == "human"`, agent remains silent, answering only "A team member is handling this case..." and executing zero propose tools.
Invariants that must stay true:
- The seller and autonomous agent never talk over each other.
- While `handler == "human"`, the agent executes zero propose tools.
- A customer cannot flip the handler or view internal seller notes or risk signals.
- Case brief contains no secret tokens, no cross-customer data, and no raw unescaped customer text.
- Next step recommendations derive strictly from rule tables, not customer text or model output.
- SLA due dates are calculated from the `Clock` (24h normal, 2h high).
- Case queries return uniform 404 for missing or unowned cases (KP-15).
Tests (paths):
- `agent/tests/test_cases_escalation.py` (10 tests)
- `agent/tests/test_phase3_shared.py` (3 tests)
Known limits:
- At most one case per customer, category, and order within 30 minutes.
Do not change because:
- Mandatory hackathon requirement ensuring reliable human handoff and escalation brief safety.
Related: ADR-037, KP-15, KP-23, KP-25, Spec 02 §4.3, §5 (W7), §9.3, §9.4, §11, §24

## F-A21b Money-path red team   [done]
What it does:
Defends the write path and money movement against adversarial attacks across 8 personas: Replay & tamper customer, Racer, Splitter, Greedy seller, Webhook attacker, Fault injector, Obedient model, and Seller-side injector. Verifies that all store financial invariants hold (refunds never exceed order payments, at most one active return per item, releases above the autonomy cap require seller authorization, daily autonomous release caps hold) through an independent ledger oracle.
How it works (flow and key files):
- `scenarios/redteam/money.yaml`:
  - 68 adversarial attack scenarios specifying input payloads, headers, expected status codes, error envelopes, and invariant checks.
- `agent/tests/redteam/ledger.py`:
  - Independent oracle `LedgerOracle` that inspects store fixtures and audit records after every attack and asserts universal money invariants.
- `agent/tests/redteam/money_support.py`:
  - Shared test harness providing fixture store, in-memory DB, RSA keys, JWT tokens (customers, seller staff/owner, service), and `assert_universal_invariants`.
- `agent/tests/redteam/test_money_attacks.py`:
  - Evaluates confirm abuse (replay, hash tampering, expired, IDOR, chat "yes"), parameter tampering (negative/zero qty, extra amount), caps & splitting, seller route auth barriers, webhook forgery, and seller-card customer statement HTML sanitization.
- `agent/tests/redteam/test_money_races.py`:
  - Multi-threaded concurrent attacks: 20 simultaneous confirms on one proposal, concurrent seller approvals, customer cancel vs seller approve, TOCTOU store mutations, and concurrent reservations.
- `agent/tests/redteam/test_money_faults.py`:
  - Fault injection attacks: store timeouts after commit on create_return and reserve_refund, store 500 on release_refund, audit failure aborting transactions, and IST midnight clock jump.
- `scripts/redteam_money_live.py`:
  - Standalone script replaying non-LLM attacks against live HTTP store when `STORE_MODE=http`.
Invariants that must stay true:
- Sum of released and active reserved refunds per order <= paid order amount.
- At most one active return per item on an order.
- No refund release above 50,000 paise without prior seller authorization.
- Total autonomous releases in an IST day <= 500,000 paise.
- Customer text in seller approval cards is strictly HTML-escaped.
- Customer responses never contain tokens, secrets, risk scores, or internal limits.
Tests (paths):
- `agent/tests/redteam/test_money_attacks.py` (15 tests)
- `agent/tests/redteam/test_money_races.py` (5 tests)
- `agent/tests/redteam/test_money_faults.py` (6 tests)
- `scripts/redteam_money_live.py` (68 attacks evaluated)
Known limits:
- Fault plan hooks are active on `FixtureStoreClient`; real HTTP store fault injection relies on network proxy or fault middleware.
Do not change because:
- Crucial hackathon defense proving the write path cannot be tricked into double-spending or unauthorized refunds.
Related: ADR-024, ADR-029, ADR-030, ADR-031, ADR-032, ADR-036, KP-12, KP-15, KP-16, KP-22, KP-25, KP-49, Spec 02 §13, §21, §23, §24

## F-A14c Audit views and trace   [done]
What it does:
Provides seller-facing auditable views and cryptographic verification of the agent's SHA-256 tamper-evident audit log (`GET /v1/seller/audit`, `GET /v1/seller/audit/verify`) and constructs ordered 10-stage decision traces (`GET /v1/seller/traces/{request_id}`). Assembles telemetry across LLM calls, tool executions, deterministic policy decisions, proposals, saga executions, and approvals to explain exactly why an autonomous or seller-approved action occurred, with full PII/token redaction, customer text HTML-escaping, and IDOR protection.
How it works (flow and key files):
- `agent/src/sellershield_agent/api/seller_audit.py`:
  - `get_audit_events`: Validates seller authorization (`seller_staff` or `seller_owner`), filters by `request_id`, `resource_id`, `event_type`, `from`, `to`, validates pagination parameters (clamping to 1..200), and emits cursor-paginated `AuditListResponse`.
  - `verify_audit_chain`: Sequentially traverses all audit events from genesis hash (`0` * 64), recomputing hashes and validating sequence numbers to return `AuditVerifyResponse`.
  - `get_decision_trace`: Validates seller authorization, ensures the target request exists (uniform 404), and dispatches to `build_decision_trace_sync`.
- `agent/src/sellershield_agent/audit/trace.py`:
  - Assembles `llm_calls`, `tool_calls`, `policy_decisions`, and related `audit_events`.
  - Maps timeline events into the canonical 10-stage lifecycle: `understanding` -> `plan` -> `tool_call` -> `rule_table` -> `proposal` -> `confirmation` -> `execution_step` -> `approval` -> `final_reply` -> `verifier_outcome`.
  - Redacts sensitive headers, auth tokens, client secrets, passwords, and PII (phone numbers, full emails); scrubs cross-customer objects upon auth denial (preventing IDOR information leaks); escapes customer free text with `html.escape`.
- `agent/src/sellershield_agent/audit/sanitize.py`:
  - Provides `sanitize_details` for database storage (truncating to 500 chars, raising on non-serializable objects, redacting `[REDACTED]` tokens and replacing `authorization` references).
  - Provides `sanitize_trace_text` and `sanitize_trace_data` for presentation-time sanitization and HTML entity escaping.
Invariants that must stay true:
- Only sellers can query audit views and decision traces (customers receive 403).
- Every mutation must be recorded in the audit hash chain within the same database transaction.
- Traces never expose secrets, unredacted auth tokens, or unescaped customer free text.
- If an audit write fails, the entire transaction rolls back.
Tests (paths):
- `agent/tests/test_audit.py` (12 tests)
- `agent/tests/test_audit_mutators.py` (3 tests: T19 concurrent sagas, T20 mutator rollback, tables/transition)
- `agent/tests/test_audit_mutators_coverage.py` (1 coverage test across all 39 mutators)
- `agent/tests/test_audit_views.py` (3 tests: verify chain, tamper detection, audit filters & pagination)
- `agent/tests/test_audit_views_trace.py` (2 tests: 10-stage trace ordering, PII/secret scrubbing, IDOR protection)
Known limits:
- Trace timeline events order deterministically by canonical stage order breaking ties by sequence number.
Do not change because:
- Satisfies strict hackathon requirement that every autonomous decision and financial mutation is provably logged, tamper-evident, and explainable to human sellers.
Related: ADR-018, ADR-034, KP-01, KP-13, KP-25, KP-48, Spec 02 §10, §11, §12, §21, §24

## F-A13 Store HTTP client adapter and cross-service integration   [done]
What it does:
Connects the SellerShield AI agent to the real NestKart Store API over HTTP (`STORE_MODE=http`). Bridges SQLite datatypes and Store API schemas into strict Agent Pydantic domain models, routes service and user credentials dynamically based on execution context (monitor, executor, customer, or seller), and enables full end-to-end operation across Store Web (`:5173`), Store API (`:8000`), and Agent (`:8001`).
How it works (flow and key files):
- `agent/src/sellershield_agent/store/normalizers.py`:
  - `ensure_aware_utc`: Automatically parses or appends UTC offset `+00:00` to naive ISO timestamp strings emitted by SQLite, satisfying strict Pydantic `_validate_aware_utc` checks.
  - `clean_order_dict`: Normalizes order dictionaries from `store-api`, ensuring nested `returns` and `refunds` map properly, default line item snapshots are populated, and `ship_to_json` partial addresses parse cleanly into `ShipTo`.
  - `clean_refund_dict`: Maps `store-api` refund status `"reserved"` with `authorized_by` / `authorized_amount_paise` to `"authorized"`, allowing agent pre-release safety checks to pass without altering store database models. Also normalizes `"cancelled"` to `"released"`.
  - `clean_shipment_dict`, `clean_refundable_item_dict`, `clean_store_settings_dict`, `clean_customer_refund_summary_dict`: Standardize nested store structures into agent models.
- `agent/src/sellershield_agent/store/http_client.py` & `store/__init__.py`:
  - `HttpStoreClient`: Supports `STORE_EXECUTOR_CLIENT_ID` and `STORE_EXECUTOR_CLIENT_SECRET`. Implements role-based caching of service tokens (`monitor` and `executor`).
  - Auth header routing: Automatically routes seller/customer Bearer JWTs when present in `RequestContext`, executor service client token on mutation methods (`transition_return`, `reserve_refund`, `release_refund`, `cancel_reservation`), and monitor client token on read endpoints (`list_orders`, `get_order`, `get_store_settings`).
- `store-api/app/main.py`:
  - Mounted `/api/v1/healthz` and `/api/v1/readyz` aliases alongside `/healthz` and `/readyz`.
  - Mounted `/.well-known/jwks.json` alongside `/api/v1/.well-known/jwks.json`.
- `store-web/package.json`:
  - Overrode Rollup with `@rollup/wasm-node@4.68.1` to eliminate native Windows WDAC binary crashes (`ADR-033`).
Invariants that must stay true:
- Store tokens are never logged or stored outside `SecretStr`.
- Domain models always receive timezone-aware UTC timestamps.
- Mutations never execute without verified executor service credentials or seller tokens.
Tests (paths):
- `agent/tests/test_store_http_compat.py` (3 tests)
- `store-api/tests/test_agent_compat.py` (5 tests)
- `store-web/src/tests/` (16 Vitest tests)
- `scripts/handshake.py` & `scratch/live_integration_runner.py`
Known limits:
- Sandbox subprocess isolation on Windows requires passing full system environment to preserve Winsock provider DLL access.
Do not change because:
- Allows the agent and store to run independently with zero tight-coupling or breaking contract changes.
Related: ADR-033, BUG-005, BUG-006, BUG-007, BUG-008

## F-A12b Webhooks and release   [done]
What it does:
Receives cryptographically signed NestKart store webhooks (`POST /v1/webhooks/store`), protects against forgery, clock-skew replay, duplicate delivery, and out-of-order versions, dispatches `return.updated=inspected_ok` through deterministic pre-release safety gates to release approved/autonomous refunds via `ServiceContext`, reconciles transient release failures with fixed idempotency keys, and processes `refund.updated` lifecycle transitions (`processing`, `completed`, `failed`) into customer-safe `client_events` and terminal proposal states without auto-creating replacement refunds.
How it works (flow and key files):
- `agent/src/sellershield_agent/api/webhooks.py` & `execution/webhook_db.py`:
  - Checks 1 MB size limit (`413 limit_exceeded`), single-valued headers via `request.headers.raw` (`X-Event-Id`, `X-Store-Timestamp`, `X-Store-Signature`), fail-closed `WEBHOOK_SECRET` (even in `DEV_MODE=true`), strict timestamp/hex-signature format, constant-time `hmac.compare_digest` over `f"{ts}.{raw_body}"`, and `-300s .. +60s` replay window against `Clock.now()`, returning uniform `401 unauthenticated` (`"Authentication failed"`, `details={}`) and writing redacted `AUTH_DENIED` audits on any failure.
  - Validates `WebhookEnvelope` (`extra="forbid"`, `StrictInt resource_version >= 0`, tz-aware `occurred_at`) and `X-Event-Id == envelope.event_id` (`400`), atomically deduplicates `webhook_events_seen`, enforces monotonic `webhook_resource_versions` per `(resource_type, resource_id)` namespace, and audits `EventType.WEBHOOK_RECEIVED` in a single short `write_tx`.
  - Resolves `StoreClient` lazily only when `return.updated` arrives with `status == "inspected_ok"`, keeping audit-only bursts (`order.*`, `shipment.*`, `mail.inbound`) free of Store/JWKS overhead.
- `agent/src/sellershield_agent/execution/release_guards.py`, `release.py`, & `release_db.py`:
  - `handle_return_inspected_ok()` loads `ReleaseContext` (`read_tx`), fetches live Store order/return/refund/settings/summary outside DB locks, and runs `verify_release_safety()`: requires `proposal_type == "return"`, eligible non-cancelled order, `ReturnStatus.inspected_ok`, `RefundStatus` in `reserved`/`authorized` (blocking `processing`/`completed`), `refund.amount_paise == proposal.amount_paise <= adjusted_refundable.max_refundable_paise`, seller `ApprovalStatus.approved` + `store_authorization_ref` + `RefundStatus.authorized` + exact match with `authorized_amount_paise` and `approvals.amount_paise` on `NEEDS_APPROVAL` paths (or `<= store_settings.autonomy_cap_paise` on `ALLOW`), and passes live `decide()` under both active and proposal policy versions. Denials skip Store calls and audit `EXECUTION_STEP` (`release_refund_blocked`, `outcome="denied"`).
  - Executes `store_client.release_refund(svc_ctx, refund_id, f"{proposal_id}:release_refund")` with `ServiceContext` (`Role.service`, `token=None`) using strict two-phase commits (`executions` + `approvals.refund_status`).
- `agent/src/sellershield_agent/execution/refund_webhook_db.py`, `reconcile_db.py`, `reconcile.py`, `reconcile_steps.py`, `domain/state.py`, & `db.py`:
  - `apply_refund_updated_sync()` ignores status regressions after `failed`/`completed`, requires approved seller authorization + `release_refund` step before `processing`/`completed`, updates `executions`, `approvals.refund_status`, and `proposals` (`executing -> succeeded` on `completed`; `executing|unknown|succeeded -> failed` on `failed`), writes deduplicated neutral `client_events` (`type="refund.updated"`, scrubbing hostile `failure_reason`), audits `EXECUTION_STEP`, and never calls `reserve_refund`/`release_refund`.
  - Reconciler (`reconcile_steps.py` + `prepare_release_reconcile` in `release.py`) resolves store-committed `processing`/`completed` on `timeout_after_commit` without a second `release_refund` call, re-runs `verify_release_safety` before retrying `fail_once` with the identical `{proposal_id}:release_refund` key, and finalizes terminal failures without triggering store compensation.
Invariants that must stay true:
- Signature verification never fails open (empty `WEBHOOK_SECRET` returns `401`), duplicate auth/webhook headers are rejected, and `401` envelopes never disclose failure reason.
- Duplicate `X-Event-Id` (even with new timestamps) and lower `resource_version` per `(resource_type, resource_id)` never invoke Store mutations or state transitions.
- Refund release NEVER executes without live Store `ReturnStatus.inspected_ok`, valid `RefundStatus`, exact authorized/proposal amount verification, seller approval on `NEEDS_APPROVAL` paths, and passing live policy checks.
- Failed store refunds (`refund.updated=failed`) remain terminal `failed`, notify customers safely without leaking gateway details, and NEVER trigger automatic replacement refunds.
- Retried `release_refund` steps always reuse `f"{proposal_id}:release_refund"` via `ServiceContext` outside open DB locks.
Tests (paths):
- `agent/tests/test_webhooks_release.py` (16 tests, unfrozen pending human `just freeze-tests`).
Known limits:
- `store/http_client.py` currently builds empty headers when `ServiceContext` is passed to `release_refund`; bearer service tokens for live HTTP Store calls are targeted for M3 integration.
Do not change because:
- Signed, replay-safe webhook ingestion combined with live deterministic pre-release safety gates ensures unauthorized, premature, stale, or over-authorized payouts can never occur.
Related: ADR-032, KP-01, KP-16, KP-47, Spec 01 §§6, 7.4, Spec 02 §§6.3, 9, 12, 21 (finding 14), 24.

## F-A13b Approvals and decision   [done]
What it does:
Implements the complete store-verified seller approval portal and customer withdrawal boundary (Spec 02 sections 9.1, 9.2, 11, 13 rows 6/8/16/17, 21 amendments 2/9, 23, 24; Token Map Document 6 §0): paginated `GET /v1/seller/approvals`, deterministic `GET /v1/seller/approvals/{id}` approval cards, `POST /v1/seller/approvals/{id}/decision` bound to live store/policy revalidation and verified seller JWT pass-through on store mutations, non-approving expiry sweeps in `reconcile_once`, and `POST /v1/me/approvals/{proposal_id}/withdraw` (`CHANGE-003`).
How it works (flow and key files):
- `agent/src/sellershield_agent/api/seller_approvals.py` & `api/customer_approvals.py`:
  - Replaces `/seller/approvals*` 501 stubs (removed from `api/seller_stub.py`, mounted under `/v1` and `""` in `main.py`).
  - `GET /seller/approvals` filters by `status` and caller `principal.store_id` with bounded offset/limit pagination; `GET /seller/approvals/{id}` enforces store scoping (`404 not_found` + `AUTH_DENIED` audit on foreign or missing IDs); `POST /seller/approvals/{id}/decision` validates `require_seller`, ASCII `Idempotency-Key`, and strict `ApprovalDecisionRequest` (`extra="forbid"`, `StrictInt expected_version >= 1`, required reason on reject).
  - `POST /me/approvals/{proposal_id}/withdraw` binds ownership to `principal.customer_id` + `Idempotency-Key` and releases reserved refund/return holds via `ServiceContext`.
- `agent/src/sellershield_agent/approvals/card.py`, `card_db.py`, `helpers.py`, `revalidation.py`, `models.py`:
  - `build_approval_card()` fetches live Store state via `ServiceContext` outside DB locks, builds PII-free `order`/items via `sanitize_order_for_card()`, recomputes pre-hold `adjusted_refundable` excluding the approval's own return/refund hold, strips admin-nested `order.returns`/`order.refunds` to match proposal `get_order()` shape, HTML-escapes/link-defangs/truncates (`<=200` chars) `Unverified customer statement`, evaluates `decide()`, sets `recommendation="approve"` only when `R-09` alone fails on a low-risk eligible return without drift (otherwise `"review"`, zero dependence on `reason_text`), counts `similar_past_decisions`, and surfaces `revalidation_diff`.
- `agent/src/sellershield_agent/approvals/decide.py`, `approve.py`, `reject.py`, `withdraw.py`, `common.py`, `common_db.py`, `decide_db.py`, `withdraw_db.py`:
  - `decide_approval()` checks store scope, enforces `seller_owner` above `staff_approval_limit_paise` (default `200000`) plus 15-minute `auth_time` freshness (`401 step_up_required` + `AUTH_DENIED` audit) *before* seller-scoped `Idempotency-Key` replay, validates status/expected_version/7-day expiry, and delegates cleanly:
    - Approve (`approve.py`): live revalidation under both stored and active policy versions (`needs_revalidation` + `409` diff on fact drift, policy rule/outcome tightening, or role escalation for staff), atomic `pending -> approving`, `authorize_refund` (exact reserved amount) and `transition_return("approved")` forwarding ONLY verified `exec_seller_ctx` (seller JWT), rolling back `approving -> pending` with `last_error` + partial `store_authorization_ref` on Store failure, then finalizing `approved`, parent `succeeded`, `approval.decided` `client_event`, and `APPROVAL_DECIDED` audit.
    - Reject (`reject.py`): requires non-empty sanitized reason, calls `transition_return("rejected")` and `cancel_reservation` with verified seller JWT, emits neutral `approval.decided` message (no internal notes), sets `rejected` / parent `failed`, and writes `APPROVAL_DECIDED` audit.
- `agent/src/sellershield_agent/approvals/expire.py` & `execution/reconcile.py`:
  - `expire_stale_approvals()` scans overdue `pending`/`needs_revalidation` approvals during `reconcile_once`, releases holds with `ServiceContext`, transitions `expired` + parent `failed`, emits `proposal.expired`, and never calls `authorize_refund`.
Invariants that must stay true:
- Store write calls on `approve` (`authorize_refund`, `transition_return`) and `reject` (`transition_return`, `cancel_reservation`) MUST always use the verified seller JWT (`exec_seller_ctx`), never `ServiceContext`.
- `staff_approval_limit_paise` (`200000` paise) and 15-min `auth_time` step-up MUST execute before idempotency replay and block both approve and reject for `seller_staff` above limit (`403` + audit).
- Authorized refund amount MUST always equal the reserved amount; extra body fields (`amount`, `amount_paise`, `customer_id`) are rejected (`422`).
- Reject MUST always call `cancel_reservation` and never expose `decision_reason` to customers; expiry MUST NEVER auto-approve.
- Unknown and foreign-store approval IDs return identical `404 not_found` envelopes; `reason_text` never influences `recommendation` or refund math.
Tests (paths):
- `agent/tests/test_seller_approvals.py` (22 tests, unfrozen pending human `just freeze-tests`).
Known limits:
- Freezing `contracts/openapi-agent.yaml` (`CHANGE-003`), `agent/tests/test_seller_approvals.py`, and handling frozen Phase 1 `test_gate_g1.py#L382` (`GET /v1/seller/approvals` stub `501` check now returning `200`) await human action before T2.7 Gate G2 completion.
Do not change because:
- Store-bound dual verification ("store verifies the seller, not just us"), live revalidation before irreversible capture, and deterministic zero-model recommendations protect merchant funds against stale states, privilege escalation, and prompt injection.
Related: ADR-031, CHANGE-003, KP-15, KP-16, KP-23, KP-46, Spec 02 §§9.1, 9.2, 11, 13 (rows 6, 8, 16, 17), 21 (amendments 2, 9), 23, 24.

## F-A11c Confirm handler   [done]
What it does:
Implements the complete customer confirmation and cancellation boundary, approval card pipeline, and decoupled `Executor` invocation contract (Spec §2 steps 3–5, §5 W3/W4, §10, §11, §13 rows 1/6/18, §15 T05/T14/T20, §21, §23, §24): emits strictly allowlisted, XSS-sanitized `confirmation` and `return_preview` SSE cards with grounded single-sentence replies, ensures typed chat text (`"yes"`, `"confirm"`, `"I agree"`, `"ok"`) and forged `confirm_proposal` chat actions never confirm proposals, and exposes `POST /v1/proposals/{id}/confirm` and `POST /v1/proposals/{id}/cancel` (plus `confirm_proposal(...)` domain handler) with ownership-first uniform 404 defense, constant-time hash verification, atomic single-winner concurrency claim, store/policy staleness revalidation with replacement previews, per-customer HTTP idempotency, and unified `proposal_confirmed` / `proposal_cancelled` auditing.
How it works (flow and key files):
- `agent/src/sellershield_agent/proposals/executor.py`:
  - Defines frozen `ExecutionResult` (`status` in `executing` | `succeeded` | `pending_approval` | `unknown` | `failed`, `return_id`, `approval_id`, `message_key`), `@runtime_checkable class Executor(Protocol)`, and default pre-T2.5 `PendingExecutor`.
- `agent/src/sellershield_agent/proposals/integrity.py`:
  - `validate_stored_proposal()` checks exact 4-key non-bool integer `amounts_json`, compares recomputed `canonical_card()` hash against stored `proposal_hash` and embedded card hash, validates client `proposal_hash` via UTF-8 `hmac.compare_digest` before regex (`hash_mismatch` even on Unicode), checks expiry against `Clock` (`expired`), and enforces `status == "proposed"`.
  - `check_overlap_guard()` blocks confirming a second proposal for an `(order_id, item_id)` already in `confirmed`, `executing`, or `unknown` status for the same customer.
  - `load_proposal_decision_snapshot()` loads stored `policy_decisions.facts_json`, risk snapshot, and 30d refund totals for staleness comparison.
- `agent/src/sellershield_agent/proposals/revalidate.py`:
  - `revalidate_against_store()` fetches live order, refundable, active returns, shipment, and evidence outside DB write locks (`KP-01`), strips temporal/clock fields via `normalize_non_temporal_facts()`, and compares normalized facts, active `load_policy().version`, decision strictness, and canonical amounts against stored proposal truth (`build_non_proposal_preview()` when no longer `ALLOW`/`NEEDS_APPROVAL`, keeping sanitized text free of internal/risk terms).
- `agent/src/sellershield_agent/proposals/confirm_audit.py`, `confirm_store.py`, & `confirm.py`:
  - `confirm_proposal(ctx, proposal_id, proposal_hash, ...)` enforces `Role.customer` + non-empty `customer_id` (`403` on seller/service tokens), then runs `prepare_action_sync` (ownership-first uniform `404` + `AUTH_DENIED`, idempotency lookup, hash/expiry/overlap validation, expiring timed-out `proposed -> expired` rows with `DENIED` audit).
  - On `reval.is_stale`, `record_stale_and_preview_sync` keeps the stale row unconfirmed (`proposed`), creates a replacement proposal (when eligible, using `exclude_proposal_ids={old_id}` + `expected_*` matching in `proposals/service.py`) alongside `proposal_confirmed (outcome=FAILED, details={"reason": "stale_state", ...})` inside one `write_tx` with zero store writes, and returns `409 invalid_state` with `fresh_preview`.
  - Otherwise `claim_confirm_sync` atomically transitions `proposed -> confirmed -> executing` inside `write_tx` (`expected_version`, `confirmed_at`), writes `proposal_confirmed (outcome=SUCCESS)` audit and initial `http_idempotency_keys` record, releases the DB lock, invokes `executor.execute(...)` outside all transactions (catching unexpected errors as `unknown`), and runs `finalize_confirm_sync` to commit terminal/unknown status + final idempotency JSON.
  - `_commit_cancel_sync` restricts `/cancel` strictly to `proposed -> cancelled` (`409 invalid_state` if confirmed/executing/expired).
- `agent/src/sellershield_agent/api/proposals.py`, `domain/session.py`, `orchestrator/cards.py`, `compose/factsheet.py`, `templates.py`, & `orchestrator/pipeline.py`:
  - `api/proposals.py` binds customer role and single valid ASCII `Idempotency-Key` before manual body parsing, injects `get_proposal_executor()`, and mounts `/v1/proposals/...` and `/proposals/...`.
  - `ChatActionName(StrEnum)` blocks forged `confirm_proposal` actions; `sanitize_text()`, `FactSheet`, and `templates.proposal_confirmation()` keep chat cards and SSE replies verified and sanitized.
Invariants that must stay true:
- Typed chat confirmation text (`"yes"`, `"confirm"`, `"I agree"`, `"ok"`) never transitions proposal state or sets `confirmed_at`.
- Foreign customer proposals always return byte-identical `404 not_found` + `AUTH_DENIED` audit; seller/service tokens get `403`.
- Executor is never invoked before the atomic `proposed -> confirmed -> executing` claim commits, nor invoked at all on hash mismatch, expiry, staleness, cancellation, or lost race.
- Concurrent confirmations race atomically via `write_tx` + `expected_version` (20 parallel confirms -> 1 `200`, 19 `409`, 1 executor call).
- Every confirm outcome (`success`, `denied`, `failed` for stale state) persists a `proposal_confirmed` audit row in the same DB transaction as any state/preview write; customer errors/previews never leak `limit`, `threshold`, `risk`, `score`, `staff`, or `owner`.
Tests (paths):
- `agent/tests/test_proposals_api.py` (17 frozen tests covering cards, T05 bypass, HTTP headers/body order, expiry, drift, overlap, cancel, idempotency, concurrency, audit rollback, SSE pipeline).
- `agent/tests/test_confirm_handler.py` (11 T2.4 tests covering `Executor` protocol/status mapping, hash/copied-hash 409, expiry transition, replay 409, uniform IDOR 404, seller 403, 20-parallel SC-14 confirm race, order-change & policy-v1-to-v3 stale_state with fresh preview and zero store writes, cancel/post-confirm cancel 409, chat non-confirm, `proposal_confirmed` audits for all outcomes, and forbidden-word absence).
Known limits:
- Default `PendingExecutor` keeps confirmed proposals in `status="executing"` (`return_id=None`, `approval_id=None`) until T2.5 implements the multi-step execution saga.
- Expired confirmations and `stale_state` confirmations return HTTP `409 invalid_state` with `details.reason` (`"expired"` / `"stale_state"`) per Option A to preserve compatibility with `contracts/openapi-agent.yaml` and frozen tests (`ADR-029`).
Do not change because:
- Binding confirmation to a verified `proposal_hash`, atomic DB state claim before executor invocation, and live store/policy revalidation guarantees no unconsented, double-executed, or stale money movement can ever occur.
Related: ADR-028, ADR-029, KP-01, KP-15, KP-16, KP-22, KP-42, KP-43, KP-44, Spec 02 sections 2, 5 (W3, W4), 10, 11, 13 (rows 1, 6, 18), 15 (T05, T14, T20), 21, 23, 24.

## F-A11b Propose tools   [done]
What it does:
Provides the only LLM-accessible write-initiation surface (`propose_return`, `propose_refund`) registered as `ToolKind.PROPOSE`. Rather than executing store mutations directly, propose tools deterministically evaluate policy (`decide()`), create durable agent-DB proposals with canonical SHA-256 hashes and 10-minute expiries, enforce open/daily rate limits and deduplication, and return customer-safe confirmation card payloads.
How it works (flow and key files):
- `agent/src/sellershield_agent/tools/propose_tools.py`:
  - `ProposeReturnArgs`: strict schema (`extra="forbid"`, `frozen=True`) accepting `order_id`, non-empty unique `items`, `reason_code`, `reason_text`, `resolution`, optional `refund_method`, and optional `evidence_ids` (max 3, validated format). Zero `amount`, `customer_id`, or `role` fields exist.
  - `ProposeRefundArgs`: strict schema (`extra="forbid"`, `frozen=True`) accepting `order_id`, `cause` (`lost` or `undelivered`), optional unique `items`, and optional `refund_method`.
  - `sanitize_reason_text()`: iteratively strips HTML tags, angle brackets, and control characters, truncating to 200 characters for storage only.
  - `handle_propose_return()` and `handle_propose_refund()`: bind ownership strictly to `ctx.customer_ctx.principal.customer_id` (raising uniform `StoreNotFound` on mismatch), check dedup & limits, fetch store facts via `StoreClient`, call `decide()`, return non-persisted `deny`/`needs_info`/`escalate` responses when not approvable, or persist `allow`/`needs_approval` proposals. `propose_refund` always enforces `needs_approval` even when under the per-refund auto limit.
- `agent/src/sellershield_agent/proposals/service.py`:
  - `find_open_or_check_limits()` and `check_limits_readonly()`: deduplicate active open proposals for `(customer_id, order_id, canonical sorted items)` before checking max 3 open proposals and max 10 per simulated IST day (`Asia/Kolkata`).
  - `persist_proposal_and_build_card()`: computes `canonical_card(...)` and `proposal_hash`, writes `policy_decisions`, `proposals` (`status="proposed"`, 10-minute UTC expiry `%Y-%m-%dT%H:%M:%SZ`), and `audit_events` (`proposal_created`) atomically inside a single `write_tx` with TOCTOU limit re-validation, and builds the customer-safe confirmation card (amounts in int paise + `format_inr` display, neutral seller review SLA sentence when `needs_approval`).
- `agent/src/sellershield_agent/tools/gateway.py` & `agent/src/sellershield_agent/tools/__init__.py`:
  - Default registry includes `build_all_tools()` (`READ` + `PROPOSE`), passes `engine` into `ToolContext`, and restricts `OrderDigestCache` solely to `ToolKind.READ`.
- `agent/src/sellershield_agent/domain/state.py`:
  - Added optional `created_at` column on `proposals` to support accurate IST daily quota counting.
Invariants that must stay true:
- Propose tools NEVER call any store write method (`create_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`); they write exclusively to the agent DB (`policy_decisions`, `proposals`, `audit_events`).
- Zero amount, customer_id, or role parameters exist on tool schemas; `extra="forbid"` rejects model-injected fields with `invalid_args`.
- `reason_text` is never passed to `decide()` or `canonical_card()`, leaving refund amounts, outcomes, and `proposal_hash` invariant under prompt injections.
- Card `proposal_hash` equals stored `proposals.proposal_hash` and re-hashes identically from displayed card fields.
- Customer-visible payloads never contain forbidden words (`limit`, `threshold`, `risk`, `score`, `staff`, `owner`, `auto`).
- Unowned orders return the exact uniform `not_found` error as nonexistent orders and create zero rows.
Tests (paths):
- `agent/tests/test_propose_tools.py` (26 tests covering SC-04..SC-17, forbidden args, dedup, 3-open / 10-IST-day caps, card hash integrity, word neutrality, uniform IDOR 404, static AST checks, and runtime SpyStore isolation).
Known limits:
- Orchestrator chat intent wiring and confirmation execution endpoints (`POST /proposals/{id}/confirm`, `POST /proposals/{id}/cancel`) are implemented in upcoming Phase 2 tasks (T2.3b / T2.4).
Do not change because:
- Guaranteeing that the LLM can only create bounded, hash-committed proposals (never execute mutations) is the core write-safety architectural boundary of SellerShield AI.
Related: ADR-027, KP-12, KP-23, KP-25, KP-40, KP-41, Spec 02 sections 5 (W3, W4), 6.2, 7, 21.

## F-A21 Red-team suite: attackers and clumsy users   [done]
What it does:
Executable red-team attack and resilience test suite verifying that even under a completely compromised LLM (`ObedientFakeLLM`), deterministic security guardrails (allowlists, context scoping, FactSheet verifier, Pydantic input models, rate limiting, and SHA-256 audit chaining) hold without leakage, money manipulation, IDOR, crash, or 500 errors.
How it works (flow and key files):
- `scenarios/redteam/attacks.yaml`: 80 structured adversarial attack definitions covering 9 personas: direct injection, indirect injection, roleplay/jailbreak, multilingual/obfuscated, data exfiltration, money manipulation, identity spoofing, protocol abuse, and clumsy honest users.
- `agent/tests/redteam/fakes.py`: `ObedientFakeLLM` simulates an obedient compromised model obeying attacker requests, attempting unauthorized tool calls, leaking prompts, emitting 50k characters, and returning invalid output schemas.
- `agent/tests/redteam/test_attacks.py`: Tests all 80 attacks end-to-end through real HTTP FastAPI application with `ObedientFakeLLM` and `FixtureStoreClient`.
- `agent/tests/redteam/test_protocol.py`: Verifies clean error handling on malformed JSON, wrong content types, oversized headers, duplicate auth headers, null bytes, unicode/padded order IDs, and extra body fields.
- `agent/tests/redteam/test_flood.py`: Verifies sliding-window rate limiters (429), failed-auth IP rate limiting, 50x deduplication on identical `client_msg_id`, and event loop task cleanliness on client disconnection.
- `agent/tests/redteam/test_clumsy.py`: Verifies polite, safe handling for whitespace, pure emoji, order ID typos, multiple orders, Hindi Devanagari numerals, ALL-CAPS, Hinglish code-switching, 1000-character messages, and polite goodbyes without order guessing.
- `scripts/redteam_live.py`: Real-key replay harness recording model completions, enforcing category checks, and outputting evidence reports to `agent/.evidence/`.
Invariants that must stay true:
- The LLM never writes to the store; tool execution is strictly read/propose.
- Tool Gateway strictly enforces tool allowlists and customer-scoped context.
- Verifier strictly validates all SSE delta emissions against grounded FactSheets.
- Zero unverified tokens, prompts, internal auto-limits, or foreign customer data are leaked.
- Every attack finishes in <= 5.0 seconds.
- Audit chain integrity verifies and injection attempts emit `INJECTION_FLAG` events.
Tests (paths):
- `agent/tests/redteam/test_attacks.py` (80 tests)
- `agent/tests/redteam/test_protocol.py` (9 tests)
- `agent/tests/redteam/test_flood.py` (4 tests)
- `agent/tests/redteam/test_clumsy.py` (10 tests)
Known limits:
- Mocked red-team suite uses `ObedientFakeLLM`; live model evaluation is driven by `scripts/redteam_live.py`.
Do not change because:
- Core security specification proving architecture resilience under model failure and hostile inputs (Spec §13, §21, §23).
Related: ADR-024, BUG-003, BUG-004, Spec §13, §21, §23.

## F-A11 Gate G1 authorization boundaries and stub routers   [done]
What it does:
Provides executable proof of the Phase 1 Gate (G1): protects seller and development routes against unauthorized access, enforces role boundaries (`customer`, `seller_staff`, `seller_owner`), logs deterministic `auth_denied` audit events on every authentication or authorization failure, and registers stub routers returning 501 for all operations defined in `contracts/openapi-agent.yaml`.
How it works (flow and key files):
- `agent/src/sellershield_agent/api/seller_stub.py`: 26 stub routes covering seller approvals, cases, operational overview, activity SSE, traces, audit log and verification, notifications, monitor runs, and Policy Studio. Protected by `require_seller`, with policy activation protected by `require_owner`. Returns 501 Not Implemented in the standard API error envelope.
- `agent/src/sellershield_agent/api/dev_stub.py`: Stub routes covering dev faults and evaluation suite execution/history (`/dev/faults`, `/dev/eval/*`). Protected by `require_dev_mode` and `require_owner`, returning 501.
- `agent/src/sellershield_agent/auth/deps.py`:
  - `record_auth_denied()`: Creates and commits an `auth_denied` audit event to `audit_events` inside a short `write_tx()` transaction. Details record only the clean reason code and path; tokens and Authorization headers are strictly never recorded.
  - Dependencies `get_principal`, `require_customer`, `require_seller`, `require_owner`, `RequireFreshAuth`, and `require_dev_mode` trigger `record_auth_denied()` on denial.
- `agent/tests/e2e/test_gate_g1.py`:
  - T01: SC-01 live own order lookup.
  - T02: SC-02 cross-customer access uniform 404 with zero foreign customer data leaked.
  - T03: SC-03 spoofed identity or admin claims ignored in chat.
  - T04: Seller route auth boundaries: customer token gives 403, missing token gives 401, expired token gives 401, staff on owner gives 403, and owner returns 501. Audit chain verifies.
  - Guided fallback: offline LLM returns guided chips menu, and deterministic `track_order` action executes.
Invariants that must stay true:
- Denials always write an `auth_denied` audit row before rejecting.
- Tokens, passwords, and Authorization headers are never written to audit tables or logs (KP-13, KP-28).
- Identity comes strictly from verified JWT claims, never from user messages or body fields.
Tests (paths):
- `agent/tests/e2e/test_gate_g1.py`
Known limits:
- Seller mutations and approvals are stubs returning 501; implemented in Phase 2.
Do not change because:
- First mandatory gate requirement proving deterministic role boundaries and tamper-evident audit logging.
Related: Spec §15 (T01-T04), §23 Amendment 7, Gate G1.

## F-A10b Orchestrator, SSE chat and guided actions   [done]
What it does:
Provides customer-facing conversational chat endpoints (`POST /v1/chat/sessions`, `GET /v1/chat/sessions/{id}`, `POST /v1/chat/sessions/{id}/messages`) streaming verified answers via Server-Sent Events (SSE). Supports read-only customer questions with grounded FactSheet verification, deterministic fallback templates, structured card payloads (`order_summary`, `shipment_timeline`, `chips`), sliding-window rate limiting, IDOR prevention, and zero-LLM guided actions and offline fallback.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/session.py`: Table definitions for `sessions` and `messages`, tracking customer session state, language, active orders, and client message idempotency with composite unique constraint `(session_id, client_msg_id)`.
- `agent/src/sellershield_agent/api/ratelimit.py`: Sliding window rate limiter enforcing 20 messages per minute per customer, and 60 failed authentication attempts per minute per IP.
- `agent/src/sellershield_agent/api/chat.py`: FastAPI routes:
  - `POST /v1/chat/sessions`: Creates a customer session, verifies customer role, yields initial chips.
  - `GET /v1/chat/sessions/{id}`: Returns session metadata and message history; verifies session ownership (uniform 404 on IDOR).
  - `POST /v1/chat/sessions/{id}/messages`: Streams SSE events (`text/event-stream`) using `sse-starlette`. Validates message length (max 1000 chars, 422 error on 1001 chars), strips control characters, checks idempotency via `client_msg_id`.
- `agent/src/sellershield_agent/orchestrator/cards.py`: Builds and recursively sanitizes structured interactive cards (`order_summary`, `shipment_timeline`, `chips`). HTML-escapes and truncates all string fields.
- `agent/src/sellershield_agent/orchestrator/resolver.py`: `ResolverLoop` with strict execution budgeting (max 5 iterations, 8 tool calls, 20s budget), initial plan generation in trace, duplicate tool call prevention.
- `agent/src/sellershield_agent/orchestrator/pipeline.py`: Implements Stages 0 to 6:
  - Stage 0: Session lookup with customer ownership verification (uniform 404). Client message deduplication returning cached response with zero duplicate tool executions.
  - Stage 1: Understanding via `understand()`, regex order ID extraction, and heuristic prompt injection scanning.
  - Stage 2: Resolver loop with tools allowed for the intent. Non-supported scopes (`human_request`, `damaged_item`, `wrong_item`, `cancellation`) return honest not-available templates and audit escalation triggers.
  - Stage 3: FactSheet extraction from tool results.
  - Stage 4: Composition & Verifier gate. All model drafts are verified before emission; if violations occur, the pipeline falls back directly to deterministic verified templates (`not_found`, `shipping_status`, `order_status`, `refund_status`, `policy_summary`).
  - Stage 5: Safe streaming (Amendment 2): Emits `tool.status` events during resolution; verified text emitted in `message.delta` chunks; structured interactive cards emitted as `card` events; terminal `final` event carrying `message_id` and full verified text.
  - Stage 6: Updates session state and persists turns.
- Guided actions & Dev flags:
  - Requests carrying `action` run deterministic tool calls and template replies with zero LLM calls.
  - Fallback to guided mode with chips menu when `LLMUnavailable` is raised or when `DEV_FAULT_LLM_DOWN` is enabled with `DEV_MODE=true`.
Invariants that must stay true:
- Raw model tokens are never streamed before verification passes (Amendment 2).
- Zero writes to the store: LLM tools are strictly read-only tools.
- Verification: Every claim, amount, ID, and date must exist in the FactSheet.
- Money is integer paise, times in UTC/IST, identity comes solely from verified JWT (never from user text or body).
- Rate limits (20/min per user) and IDOR isolation (uniform 404) are strictly enforced.
- Logging at INFO level never logs customer message text or tokens.
Tests (paths):
- `agent/tests/test_chat.py` (14 tests covering SC-01, SC-02, SC-03, verifier fallback, LLM down guided mode, zero-LLM guided action, idempotency, 1001-char limit, rate limit, uniform 404 on IDOR, seller token 403, dev flag gating, card HTML escaping, and INFO-level logging hygiene).
Known limits:
- Return requests provide eligibility previews only; submission is disabled in this build.
- Live agent chat, cancellations, and damage case creation return honest not-available templates.
Do not change because:
- Safe streaming with verifier gating and zero-LLM fallback ensures that customer-facing answers are 100% truthful, verifiable, and resilient to provider outages.
Related: CHANGE-001, ADR-018, ADR-021, KP-12, KP-15, KP-17, KP-18, KP-21, Spec 02 sections 2, 4.1-4.9, 5, 11, 23.

## F-A10a Understanding stage and deterministic router   [done]
What it does:
Classifies customer messages into structured `Understanding` objects using schema-constrained LLM generation, with a zero-network deterministic fallback router, prompt injection heuristic tagging, prompt version SHA-256 audit logging, and hallucinated order ID pruning.
How it works (flow and key files):
- `agent/src/sellershield_agent/understand/schema.py`: Defines flat Pydantic model `Understanding` (`extra="forbid"`) with `Intent`, `ReasonCode`, `Language`, `Sentiment`, `ClarificationSlot`, and `SafetyFlag` enums.
- `agent/prompts/understand.v1.md`: Versioned system prompt placing customer text strictly within `<customer_message>` tags as untrusted data, providing 6 diverse few-shot examples (including Hinglish and prompt injection).
- `agent/src/sellershield_agent/understand/injection.py`: `detect_injection_heuristics()` performs advisory pattern matching for injection phrases (`ignore previous`, `system prompt`, `you are now`, `approve my refund`, `as admin`, `developer mode`, etc.) and returns safety flags without altering execution flow.
- `agent/src/sellershield_agent/understand/router.py`: `DeterministicRouter` performs offline keyword/regex classification. Extracts `ORD-\d+` IDs; detects language (`hi` from Devanagari script, `hinglish` from vocabulary overlap, `en` from Latin text); checks policy `escalation_keywords` and human tokens (*insaan*, *agent*, *manager*); caps confidence at $\le 0.5$ (triggering clarification gating). Achieves 100% accuracy on golden evaluation utterances.
- `agent/src/sellershield_agent/understand/service.py`: `understand()` orchestrates the full pipeline:
  1. Computes SHA-256 prompt hash.
  2. Runs injection heuristics; logs `injection_flag` audit event if triggered.
  3. Executes `generate_structured(chain, messages, Understanding)`.
  4. On any provider failure (`LLMUnavailable`, `StructuredOutputError`, `ConfigError`, or connection drop), writes `llm_fallback` audit event and falls back to `DeterministicRouter`.
  5. Enforces confidence gate: confidence $< 0.6$ sets `needs_clarification = True`.
  6. Enforces order ID integrity: Drops any hallucinated ID not present in either the message or customer order digest, and merges regex-extracted order IDs.
  7. Writes prompt SHA-256 hash to the audit trail.
  8. Truncates and redacts logs; never logs customer text at INFO level.
Invariants that must stay true:
- The LLM only determines the fields in `Understanding`; it never mutates data, decides money amounts, or bypasses authorization.
- The router confidence is strictly capped at $\le 0.5$, ensuring deterministic fallbacks always trigger clarification gating.
- Any order ID generated by the model that does not exist in either the customer message or customer order digest is strictly dropped.
- Customer messages are treated as untrusted data in prompts and redacted/truncated in log output.
- All fallback and injection events are recorded in the tamper-evident audit log.
Tests (paths):
- `agent/tests/test_understand.py` (10 tests: valid LLM output, retry on schema error then fallback, provider down fallback, invented order ID dropping, low confidence clarification gating, injection heuristic flag & audit event, Hinglish routing, prompt hash audit recording, and 30-case golden intents evaluation).
- `scenarios/golden/intents.yaml` (30 labelled golden utterances).
Known limits:
- Deterministic router relies on keyword and regex rules; complex multi-hop queries fall back with `needs_clarification = True`.
Do not change because:
- Resilient understanding with zero-downtime offline fallback is vital for customer support continuity when LLM providers experience rate-limiting or outages.
Related: ADR-021, KP-09, KP-18, KP-25, KP-27, KP-31, Spec 02 sections 4.1, 4.2, 4.8, 14.1.

## F-A08 FactSheet, verifier and templated replies   [done]
What it does:
Enforces that no customer-facing model output is emitted unless every claim, amount, ID, date, and status is grounded in deterministic tool execution results. Provides immutable FactSheet extraction, strict deterministic draft verification, structured regeneration correction prompts, and deterministic bilingual fallback templates passing verification by construction.
How it works (flow and key files):
- `agent/src/sellershield_agent/compose/factsheet.py`: Builds frozen `FactSheet` from `ToolResult` items. Collects allowed entity IDs (`ORD`, `RET`, `RFD`, `CASE`, `APR`, `PRP`), integer paise amounts, IST calendar dates via `to_ist()`, relative day offsets (`eta_in_days`, `delivered_days_ago`), customer-safe rule texts, and supported action claims (`shipped`, `delivered`, `refunded`, `processed`, `approved`, `created`, `cancelled`).
- `agent/src/sellershield_agent/compose/verifier.py`: Pure deterministic Python `verify(draft, factsheet, language) -> VerificationResult`. Normalizes Unicode NFKC, strips zero-width/control characters, normalizes dash variants. Checks: (1) currency amounts in any format (₹, Rs, rupees, rupaye, Devanagari digits, split lines) equal a FactSheet amount; (2) spelled-out amounts (hundred, thousand, lakh, sau, hazaar next to currency words) strictly rejected; (3) entity IDs (case-insensitive) exist in allowed IDs; (4) dates match FactSheet IST dates; (5) relative temporal words require matching relative facts; (6) action claim words require supporting fact tokens; (7) promise words require supporting promise facts; (8) forbidden content (tool names, prompt markers, risk scores, auto-limits, thresholds, customer IDs, JWT/API keys, URLs, HTML, Markdown images/links) strictly rejected; (9) language matching (Devanagari present for `hi`, Latin for `en`); (10) length <= 1200 characters.
- `agent/src/sellershield_agent/compose/templates.py`: Deterministic bilingual English and Hindi templates built strictly from `FactSheet` for: `order_status`, `shipping_status`, `delay_options`, `return_eligibility_preview`, `refund_status`, `policy_summary`, `not_found`, `store_unavailable`, `human_handoff_unavailable`, `guided_mode_menu`. Pass verifier by construction across all scenarios.
- `agent/src/sellershield_agent/compose/regenerate.py`: `build_regeneration_prompt()` builds a structured correction message listing specific violations and ground facts for one regeneration attempt before falling back to templates.
Invariants that must stay true:
- Verifier code is pure deterministic code with zero LLM or network dependency.
- All monetary arithmetic uses integer paise (KP-05); never float.
- Every customer-facing response must pass verification before sending to the customer (AGENTS.md rule 2.8, KP-12, KP-30).
- Customer-facing text never leaks risk scores, internal limits, prompts, tool names, or customer IDs.
- Deterministic templates pass `verify()` by construction for all scenario fixtures in both English and Hindi.
Tests (paths):
- `agent/tests/test_verifier.py` (26 tests covering adversarial amounts, formatting variants, spelled-out rejection, ID validation, customer ID exclusion, dates, relative days, claims, promises, internal terms, URLs/markdown, language boundaries, length limit, 5 nasty breakdown tests, hypothesis format_inr property, all templates across all 16 scenario orders in en and hi, and regeneration prompt construction).
Known limits:
- Model regeneration is capped at one attempt in the agent orchestrator before template fallback.
Do not change because:
- The verifier is the central safety barrier guaranteeing that hallucinations, invented refunds, or false commitments can never reach online shoppers.
Related: ADR-019, KP-05, KP-12, KP-15, KP-25, KP-30, Spec 02 sections 4.5, 4.6, 21.

## F-A09 Tool gateway and read tools   [done]
What it does:
Provides the unified, secure execution gateway for all agent tools, ensuring allowlist enforcement, strict argument validation, customer-scoped authority, 8s timeout, response minimization, digest caching, and tamper-evident audit logging. Exposes the 8 safe read tools and LLM tool formatting utilities.
How it works (flow and key files):
- `agent/src/sellershield_agent/tools/base.py`: Defines `Tool`, `ToolContext`, `ToolResult`, `ToolKind` (`read`, `propose`), and `ToolErrorCode`. Encapsulates `StoreClient` so handlers never inspect customer tokens directly.
- `agent/src/sellershield_agent/tools/messages.py`: Localized customer-safe messages in English and Hindi for every error code, free of internal limits or scores.
- `agent/src/sellershield_agent/tools/cache.py`: `OrderDigestCache` storing order digests per session for 60s driven by `Clock`, with `invalidate()` and `invalidate_session()` methods.
- `agent/src/sellershield_agent/tools/read_tools.py`: Implements the 8 read tools (`list_my_orders`, `get_order`, `get_shipping_status`, `get_return_status`, `get_refund_status`, `get_policy_summary`, `check_return_eligibility`, `preview_refund`). Outputs are minimized to IDs, statuses, item names, dates, amounts, and customer-safe rule texts. Completely strips PII, risk metrics, and internal limits.
- `agent/src/sellershield_agent/tools/formatting.py`: Generates flat OpenAI function specs with usage constraints via `tools_for_llm` and wraps outputs in tagged untrusted data blocks (`tool_results_block`) stripping HTML, control characters, and truncating strings to 200 characters.
- `agent/src/sellershield_agent/tools/gateway.py`: `ToolGateway.call()` validates allowlist, validates args (`extra='forbid'`), validates ID format, executes with 8s timeout, maps store errors (`StoreNotFound` -> uniform `not_found`), and appends an audit event in an independent `write_tx` after execution. `call_many()` runs independent reads in parallel with a concurrency cap of 4.
Invariants that must stay true:
- Tools exposed to the LLM are strictly `read` or `propose`; no tool writes directly to the store.
- Tool input models strictly exclude `customer_id`, `user_id`, `token`, `amount`, and `paise`.
- Tool outputs never contain customer addresses, phone numbers, or email addresses.
- Risk scores, rule weights, and internal limits never leak in outputs or audit details.
- Querying unowned orders returns the exact same `not_found` error and customer text as nonexistent orders, and foreign customer IDs are scrubbed in audit events.
- Never hold a database transaction across an await or store call. Exactly one audit event is written per tool call.
Tests (paths):
- `agent/tests/test_tools.py` (13 tests covering uniform 404, allowlist enforcement, ID sanity, PII/risk exclusion, audit atomicity, digest cache, parallel concurrency cap, exception handling, static invariants, and prompt injection sanitization).
Known limits:
- Propose tools are added in Phase 2; write tools run only in the executor layer.
Do not change because:
- The centralized tool gateway is the core boundary preventing agent hallucination, privilege escalation, and unauthorized store mutation.
Related: ADR-004, ADR-018, KP-15, KP-23, KP-25, Spec 02 sections 6.1, 6.2, 6.4, 13, 21.

## F-A07a Domain models, StoreClient, and Real HTTP Integration   [done]
What it does:
Provides the agent's strongly typed view of the NestKart store, the asynchronous StoreClient Protocol with RequestContext, an in-memory FixtureStoreClient with seed data covering scenarios SC-01 through SC-17, and the production-ready HttpStoreClient with token passthrough, service credentials caching, and reachability reporting.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/models.py`: Frozen Pydantic v2 models (`extra='forbid'`, zero floats, int paise, aware UTC datetimes) mirroring `contracts/openapi-store.yaml`: `Order`, `OrderItem`, `Address`, `Shipment`, `ShipmentEvent`, `ReturnRequest` (`Return`), `ReturnItem`, `Refund`, `RefundableItem`, `Refundable`, and `CustomerRiskFacts`. Enums: `OrderStatus`, `PaymentMethod`, `PaymentStatus`, `ShipmentStatus`, `ReturnStatus`, `ReturnReasonCode`, `ReturnResolution`, `RefundStatus`, `RefundMethod`.
- `agent/src/sellershield_agent/store/client.py`: Asynchronous `StoreClient` protocol (`list_orders`, `get_order`, `get_shipment`, `get_refundable`, `list_returns`, `list_refunds`, `get_customer_risk_facts`, `get_clock`). Defines `RequestContext` carrying verified `Principal`, `request_id`, and masked `SecretStr` token.
- `agent/src/sellershield_agent/store/http_client.py`: Real asynchronous HTTP client using `httpx.AsyncClient` implementing `StoreClient`. Normalizes base URL from `STORE_BASE_URL` with `/api/v1` routes; applies 2 s connect and 8 s read timeouts; bounded 2-try retry with jitter for idempotent `GET` on 502/503/504 and network errors; zero automatic retries on mutating `POST`/`PUT` writes. Implements all write operations (`create_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, `update_settings` with `If-Match`). Enforces context-isolated token routing (customer token on customer steps, seller token on seller steps, client-credentials service token on service steps cached until 60s before expiry).
- `agent/src/sellershield_agent/store/fixtures.py`: In-memory implementation of `StoreClient` resolving relative times from injected `Clock` (KP-06). Enforces strict order ID format validation (`^ORD-[0-9]{3,6}$`) before data access, uniform 404 on unowned or missing orders (KP-15), and service-role gating on customer risk facts.
- `agent/src/sellershield_agent/store/__init__.py`: Provides `get_store_client(clock, settings)` factory routing between `HttpStoreClient` and `FixtureStoreClient` based on `STORE_MODE`.
- `agent/src/sellershield_agent/config.py` & `main.py`: Enforces `STORE_MODE` (`fixtures` | `http`). Startup refuses `STORE_MODE=fixtures` outside `DEV_MODE` (KP-24). Logs active mode on startup.
- `agent/src/sellershield_agent/api/health.py`: `readyz` returns active `store_mode` and reports `status="degraded"` if HTTP store is unreachable.
- `scripts/handshake.py`: Developer CLI tool to perform authenticated handshake with the live store.
- `scripts/handshake_money.py`: Full end-to-end money lifecycle handshake driving chat proposal, confirmation, seller approval, store admin advancement, and SimPay completion for ORD-1042.
Invariants that must stay true:
- Money fields strictly end in `_paise` and are integer. Ratios are basis points (`refund_ratio_90d_bp`). Zero float fields exist on any domain model.
- Every order satisfies `line_paid_paise == (qty * unit_price_paise) - discount_alloc_paise` and `order.total_paise == sum(line_paid_paise) + shipping_fee_paise`.
- Customer tokens are never logged or persisted (KP-13); service tokens are refreshed before expiry.
- An order that belongs to another customer raises `StoreNotFound` with the exact same exception type and message as a nonexistent order ID (KP-15).
- Unknown response fields are dropped; invalid response shapes raise `StoreUnavailable` without logging payload.
- `STORE_MODE=fixtures` is strictly prohibited outside `DEV_MODE` (KP-24).
Tests (paths):
- `agent/tests/test_fixtures.py` (10 tests covering fixture store behavior and invariants).
- `agent/tests/test_store_http.py` (24 tests covering contract validation, token forwarding, error mappings, bounded retry, service token caching, and health reachability).
- `agent/tests/test_store_http_writes.py` (14 tests covering write contract headers, service token 60s pre-refresh, token isolation, error mappings, and zero write retries).
- `agent/tests/test_store_write.py` (28 tests covering store write operations, caps, idempotency, and concurrency).
Known limits:
- StoreClient mutation methods are implemented on FixtureStoreClient for offline testing; HTTP store write path integrates in executor saga (T2.4).
Do not change because:
- The dual-mode store architecture guarantees seamless offline testing and genuine live API integration required for hackathon submission.
Related: ADR-015, ADR-023, ADR-025, KP-04, KP-05, KP-06, KP-13, KP-15, KP-24, Spec 01 sections 4, 6, 16.

## F-A07d Store write methods and simulator   [done]
What it does:
Provides the write methods on the StoreClient Protocol, Context types, domain write models, and a write-capable in-memory FixtureStoreClient simulator that strictly enforces all NestKart store invariants, return state machines, refund reservation caps, autonomy caps, seller authorization boundaries, simulated IST daily limits, and strict payload idempotency.
How it works (flow and key files):
- `agent/src/sellershield_agent/store/client.py`: Protocol write operations: `create_return`, `get_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, `get_refund`, `get_order_admin`, `get_settings`, `get_customer_refund_summary`. Defines `CustomerContext`, `SellerContext` (`seller_staff` or `seller_owner`), and `ServiceContext` (scopes). Introduces `StoreDuplicate`, `StoreLimitExceeded`, `StoreInvalidState`, and `StoreIdempotencyMismatch`.
- `agent/src/sellershield_agent/domain/models.py`: Defines `StoreSettings` (autonomy caps), `CustomerRefundSummary` (metrics and basis points), and `AdminOrderDetail` (order with embedded returns and refunds lists). Adds `client_ref` (creation `Idempotency-Key`) to `ReturnRequest` and `Refund`.
- `agent/src/sellershield_agent/store/fixtures.py`: In-memory mutable state supporting `reset()` for per-test isolation. Enforces:
  1. Ownership checks on customer calls (uniform 404 on unowned orders).
  2. Return state machine: Customer can only cancel pending returns; seller or service advances through pickup, transit, inspection, and completion.
  3. Single active return per item and return qty <= delivered qty.
  4. Refund reservation limit: active refunds cannot exceed remaining order total.
  5. Service token return approval and refund release cap: 50,000 paise maximum without seller authorization.
  6. Seller authorization: staff allowed up to 200,000 paise; owner required above; amount cannot exceed reservation.
  7. Daily autonomous refund cap: 500,000 paise per simulated IST day (`_get_ist_date_key`), resetting at IST midnight.
  8. Idempotency: identical key and payload replays cached result; same key with differing payload raises `StoreIdempotencyMismatch`.
  9. Concurrency: `asyncio.Lock` serializes reservations, preventing race condition over-reservations.
- `agent/tests/fault_plan.py`: Test-only fault injection helper (never imported in production): supports `fail_once`, `timeout_after_commit`, `unavailable`, `conflict`, and `slow`.
- `agent/src/sellershield_agent/clock.py`: `StoreClock` with 60s offset caching and fallback on store failure.
Invariants that must stay true:
- The LLM never writes to the store directly; write methods are called only by the execution layer or tests.
- Stored money amounts are int paise; times are aware UTC; daily cap cycles use Asia/Kolkata date boundaries.
- Stored authorizations require verified seller tokens (`seller_staff` up to 200,000 paise, `seller_owner` above).
- Retried writes with identical idempotency key and payload return identical objects without side effects; differing payload raises `StoreIdempotencyMismatch`.
- Timeout after commit leaves the store mutation applied; subsequent queries or idempotent retries reflect the change.
Tests (paths):
- `agent/tests/test_store_write.py` (28 tests covering all write operations, caps, idempotency, faults, and concurrency).
Known limits:
- Fixture store state is in-memory; persists for process lifetime or until `reset()` is called.
Do not change because:
- The store simulator serves as the authoritative oracle for testing multi-step execution sagas, timeouts, and edge cases safely.
Related: ADR-015, ADR-025, CHANGE-002, KP-01, KP-06, KP-15, KP-16, Spec 01 sections 5, 6, 16.

## F-A22 Test guard   [done]
What it does:
Enforces mechanical test suite integrity so that tests cannot be weakened, skipped, deselect-filtered, or bypassed by an agent or human.
How it works (flow and key files):
- `scripts/test_guard.py`: CLI supporting `check`, `freeze`, and `status`. Computes SHA-256 hashes, file sizes, and AST assertion counts for all files matching frozen globs.
- `scripts/test_guard.toml`: Declares frozen globs (`agent/tests/**/*.py`, `tests/guard/**/*.py`, `scenarios/fixtures/**`, `scenarios/golden/**`, `scenarios/redteam/**`, `contracts/*.yaml`, `agent/src/sellershield_agent/policy/versions/*.json`, `scripts/test_guard.py`, `scripts/test_guard.toml`).
- `tests/FROZEN.sha256`: Flat manifest recording `<sha256>  <size>  <asserts>  <path>` for every frozen file.
- `tests/FROZEN.count`: Monotonically increasing collected test count threshold.
- `docs/TEST_CHANGES.md`: Dated audit log appended whenever `--refreeze PATH --reason TEXT` is used to update a frozen file.
- `justfile`: `agent-check` executes `agent-lint` (ruff), `agent-types` (mypy), `agent-test` (plain `uv run pytest -q`), and `test-guard` in exact order.
Invariants that must stay true:
- No test file may contain `skip`, `skipif`, `xfail`, `importorskip`, `pytest.skip`, `unittest.skip`, or unapproved markers (only `live` and `store` permitted for test categorization).
- Config files (`pyproject.toml`, `pytest.ini`, `tox.ini`, `setup.cfg`) must never contain `-k`, `--deselect`, `--ignore`, `-x`, `--maxfail`, `--lf`, or `--co` in `addopts`.
- Pytest in `agent-check` must run plain `uv run pytest -q`.
- No test may contain `assert True`, `assert 1`, self-comparison expressions, or pass-only bodies.
- Frozen test files may not reduce assert statement count below freeze-time count.
- Existing manifest entries cannot be modified without human `--refreeze` and explicit `--reason`.
- Test count can only stay equal or increase; it can never drop below `FROZEN.count`.
Tests (paths):
- `tests/guard/test_guard.py` (12 tests covering all tampering and integrity rules)
Known limits:
- Requires standard pytest invocation and clean Python 3.12 syntax.
Do not change because:
- Test integrity is the core guarantee preventing agents from cheating tests during autonomous code generation.
Related: ADR-014, KP-10, KP-22, AGENTS.md rule 8.

## F-A00 Contracts   [done]
What it does:
Defines and freezes the OpenAPI 3.1 specifications for Store and Agent services, establishing the authoritative contract for W1 (backend), W2 (frontend), and A (agent).
How it works (flow and key files):
- contracts/openapi-store.yaml defines all 49 Store API operations across 8 tags, plus 7 webhook outbox schemas.
- contracts/openapi-agent.yaml defines all 48 Agent API operations across 8 tags, including SSE streaming, card schemas, and push streams.
- docs/spec/CONTRACT_COVERAGE.md maps every endpoint in 01-playground.md (Section 6 & 16) and 02-agent.md (Section 11 & 21) to its operationId.
- scripts/contract_check.py enforces AST validation for unique operationIds, security, Idempotency-Key headers on mutating POSTs, integer money types, and uniform error schemas.
Invariants that must stay true:
- All money fields end in _paise and are type integer.
- Mutating POSTs require Idempotency-Key header.
- Error responses reference #/components/schemas/Error.
- Orders not owned return uniform 404 (same text as not found).
- additionalProperties: false on all request bodies.
- SSE chat stream documents x-sse-events and structured cards (order_summary, shipment_timeline, return_preview, confirmation, case_created, chips).
Tests (paths):
- uvx openapi-spec-validator contracts/openapi-store.yaml
- uvx openapi-spec-validator contracts/openapi-agent.yaml
- uv run --with pyyaml python scripts/contract_check.py contracts/openapi-store.yaml
- uv run --with pyyaml python scripts/contract_check.py contracts/openapi-agent.yaml
Known limits:
- Milestone M0 contracts frozen; store and agent backend implementations follow in subsequent Phase 0 and Phase 1 tasks.
Do not change because:
- Milestone M0 freeze: W1 and W2 build against this contract. Any breaking modification requires team agreement and an ADR.
Related: ADR-008, ADR-009, 01-playground.md sections 4, 5, 6, 16, 02-agent.md sections 11, 21.

## F-A01 Config and logging   [done]
What it does:
Provides fail-fast configuration management with secret masking via Pydantic Settings, and safe structured JSON logging via Structlog that automatically redacts sensitive keys, headers, and email addresses.
How it works (flow and key files):
- `agent/src/sellershield_agent/config.py`: Loads configuration from `agent/.env` and environment variables. Secrets are typed with `SecretStr`. On validation errors, reports missing or invalid variable names only, without leaking values, secrets, or inputs.
- `agent/src/sellershield_agent/logging.py`: Configures standard JSON structured logging. Recursively redacts keys matching `authorization`, `api_key`, `token`, `access_token`, `password`, `secret`, `cookie` to `[REDACTED]`, and masks email-like strings to `[REDACTED_EMAIL]`. Binds `request_id` via contextvars.
Invariants that must stay true:
- Secrets are never logged or stored as plaintext strings.
- Missing configuration variables fail fast and list variable names only.
- Logs are strictly JSON formatted and scrubbed before output.
Tests (paths):
- `agent/tests/test_config.py`
- `agent/tests/test_logging.py`
Known limits:
- Optional LLM credentials and store service tokens are defined with defaults and validated at startup or when their respective adapters initialize.
Do not change because:
- Security invariant: sensitive tokens and credentials must never appear in repository, test outputs, or application logs (KP-13, KP-14).
Related: ADR-010, KP-13, KP-14, 02-agent.md sections 0A, 3, 12.

## F-A02 Error envelope and request ids   [done]
What it does:
Enforces uniform error responses matching `contracts/openapi-agent.yaml` (`{error: {code, message, request_id, details}}`), strict `X-Request-ID` generation/echoing, request body size limits (1 MB), and liveness/readiness health probes.
How it works (flow and key files):
- `agent/src/sellershield_agent/middleware.py`:
  - `RequestIDMiddleware`: Validates incoming `X-Request-ID` against `^[A-Za-z0-9-]{8,64}$`; replaces invalid or missing IDs with generated UUIDs; echoes in response headers.
  - `AccessLogMiddleware`: Logs structured request completion telemetry (`method`, `path`, `status`, `latency_ms`).
  - `BodyLimitMiddleware`: Pure ASGI middleware enforcing 1 MB limit, returning HTTP 413 with `code: limit_exceeded`.
- `agent/src/sellershield_agent/errors.py`: Global handlers for `HTTPException`, `RequestValidationError` (sanitizing details so passwords/tokens are never echoed), and unhandled exceptions (HTTP 500, `code: internal`, zero stack trace).
- `agent/src/sellershield_agent/api/health.py`: `GET /healthz` (200 ok) and `GET /readyz` (200 when settings are valid).
Invariants that must stay true:
- Every error response conforms to the standard Error envelope with a valid `request_id`.
- Request validation errors never echo user input for sensitive fields.
- 500 errors never leak tracebacks or database connection strings.
- CORS is disabled by default unless origins are explicitly configured.
- OpenAPI docs are only served when `DEV_MODE=True`.
Tests (paths):
- `agent/tests/test_health.py`
- `agent/tests/test_request_id.py`
- `agent/tests/test_errors.py`
- `agent/tests/test_cors.py`
Known limits:
- `/readyz` currently validates configuration load; subsequent tasks (T0.8, T0.9) will chain database and JWKS connectivity.
Do not change because:
- Contract stability: uniform error formats and correlation IDs are essential for frontend handling and audit logging.
Related: ADR-009, ADR-010, 02-agent.md sections 2, 11, 12.

## F-A03 Database layer   [done]
What it does:
Provides thread-safe SQLite engine initialization using the Section 16 safety recipe (WAL mode, busy_timeout=5000ms, foreign_keys=ON, isolation_level=None, and BEGIN IMMEDIATE transaction execution), plus write_tx and read_tx context managers.
How it works (flow and key files):
- `agent/src/sellershield_agent/db.py`: `create_engine_from_url` ensures parent directories exist, configures connect listener for PRAGMAs, and begin listener for `BEGIN IMMEDIATE`. `write_tx` executes in an exclusive transaction, committing on success or rolling back on exception. `init_db` idempotently runs DDL and seeds the `_meta` table schema_version.
Invariants that must stay true:
- Write transactions must never be held across an `await` or HTTP call (KP-01).
- Isolation level is disabled at connect so the driver never issues implicit BEGINs.
- Engines must be disposed before deleting database files on Windows (KP-21).
Tests (paths):
- `agent/tests/test_db.py`
Known limits:
- Demo database uses local SQLite; distributed replication is out of scope.
Do not change because:
- Crucial for SQLite concurrency under concurrent requests; avoids SQLITE_BUSY deadlocks.
Related: ADR-001, ADR-011, KP-01, KP-21, 01-playground.md section 16.

## F-A04 Money and time helpers   [done]
What it does:
Implements exact integer Paise currency representation, formatting, Decimal-based rupee string parsing, largest-remainder line allocation, Clock abstraction protocol, and Asia/Kolkata timezone window validation.
How it works (flow and key files):
- `agent/src/sellershield_agent/clock.py`: `Clock` protocol with `now()` returning aware UTC datetimes; `SystemClock`, `FixedClock` (testing), and `StoreClock` (stub).
- `agent/src/sellershield_agent/money.py`: `Paise = int`. `format_inr` formats rupees without decimals for whole numbers and two decimals for fractions. `parse_rupees` validates string Decimal format with at most 2 decimal places and rejects floats. `allocate_largest_remainder` allocates paise across item weights using the Hamilton largest-remainder method with deterministic index tie-breaking.
- `agent/src/sellershield_agent/timeutil.py`: `to_ist`, `ist_day_end` calculating 23:59:59.999999 IST end-of-day converted to UTC, and `is_within_window` boundary checks. Naive datetimes are strictly rejected.
Invariants that must stay true:
- Money values are always integer paise, never float (KP-05).
- Domain code receives a Clock and never calls datetime.now() directly (KP-06).
- All policy window calculations evaluate against Asia/Kolkata (IST, UTC+05:30) (KP-03).
Tests (paths):
- `agent/tests/test_money.py`
- `agent/tests/test_timeutil.py`
Known limits:
- StoreClock will be wired to store API time endpoint in Phase 1.
Do not change because:
- Prevents money drift and dispute over policy window cutoffs.
Related: ADR-011, KP-03, KP-05, KP-06, 02-agent.md sections 7.1-7.4.

## F-A05 Token and JWKS Verification   [done]
What it does:
Verifies store-issued RS256 JWT tokens against store JWKS with caching, rate-limited rotation, resilience to store downtime, strict role-based access dependencies, and step-up auth checks.
How it works (flow and key files):
- `agent/src/sellershield_agent/auth/models.py`: Defines `Role(StrEnum)` (`customer`, `seller_staff`, `seller_owner`, `service`) and immutable `Principal` model (`user_id`, `role`, `customer_id`, `scopes`, `auth_time`, `jti`, `token_exp`).
- `agent/src/sellershield_agent/auth/jwks.py`: `JWKSClient` lazily fetches store public keys, caches them for 1 hour, enforces a 30s rate limit on unknown kid refreshes, retries 3 times with backoff on failure, retains last known good keys when store is unreachable, rejects plain HTTP unless `DEV_MODE=True`, and disables redirects.
- `agent/src/sellershield_agent/auth/verify.py`: Validates tokens up to 64 KB, inspects unverified header to restrict `alg` strictly to `RS256`, requires non-empty `kid`, decodes signature via `PyJWK`, evaluates `exp`, `iat`, `nbf` against domain `Clock` with 30s leeway, verifies `aud` contains `sellershield-agent`, verifies `iss`, ensures `customer` tokens carry non-empty `customer_id`, and redacts tokens from all error messages and logs.
- `agent/src/sellershield_agent/auth/deps.py`: FastAPI dependencies `get_principal`, `require_customer`, `require_seller`, `require_owner`, and `require_fresh_auth(max_age_seconds=900)` raising 401 `step_up_required` for stale authentication.
- `agent/src/sellershield_agent/api/health.py`: `/readyz` probe queries `jwks_client.is_healthy()`, returning 200 with `status="degraded"` if store JWKS is down, preventing startup crashes.
Invariants that must stay true:
- RS256 is the only allowed algorithm; `alg: none` and `HS256` key confusion are strictly rejected (KP-04).
- Identity comes strictly from verified JWT claims (`customer_id`, `role`), never from user input or LLM text.
- Customer tokens are rejected if `customer_id` is missing.
- Private keys must never be written to disk; test keys are created in memory only.
- Raw token strings are never logged or returned in error bodies (KP-13).
Tests (paths):
- `agent/tests/test_auth.py`
- `agent/tests/test_health.py`
Known limits:
- Background tasks (saga, monitor) use service credentials, not customer tokens (01 section 16 finding 2).
Do not change because:
- Secures customer tenant isolation (Customer A cannot access Customer B's orders) and prevents administrative escalation.
Related: ADR-006, ADR-012, KP-04, KP-13, KP-23, 01-playground.md section 4 & 16, 02-agent.md section 21.

## F-A06 LLM provider chain   [done]
What it does:
Provides a unified, resilient abstraction for OpenAI-compatible LLMs featuring automatic multi-provider fallback (primary -> secondary -> tertiary), deterministic circuit breaking via Clock, safe record/replay caching, flat Pydantic structured output with one-shot self-healing retry, and key-safe error handling.
How it works (flow and key files):
- `agent/src/sellershield_agent/llm/config.py`: Defines `ProviderConfig` and `ProviderCapabilities` (`supports_json_schema`, `supports_tools`, `parallel_tools`, all defaulting to `False` for unknown providers). Parses configuration from `Settings` for primary, secondary, and optional tertiary providers.
- `agent/src/sellershield_agent/llm/client.py`: Async wrapper over `openai.AsyncOpenAI` configured with 12s timeout and 0 retries. Injects `httpx.AsyncClient` to allow offline respx mocking. Catches 401/403 and raises `ConfigError` naming the provider without echoing keys (KP-13). Catches 404 and raises clear model configuration `ConfigError`. Returns standardized `LLMResult`.
- `agent/src/sellershield_agent/llm/cache.py`: Canonical SHA-256 JSON hashing across `(provider, model, messages, tools, schema)`. Writes atomically to `agent/.llm_cache/` in `record` mode; returns cached results with no network calls in `replay` mode; raises `CacheMiss` on replay cache miss.
- `agent/src/sellershield_agent/llm/chain.py`: Implements `CircuitBreaker` using `Clock` (KP-06) that marks a provider unhealthy for 60 seconds after 3 consecutive failures within 60 seconds, then permits a trial attempt in half-open state. Tries candidate providers sequentially on 429, 5xx, timeout, or connection errors. Emits structlog events containing `provider`, `model`, `latency_ms`, `cached`, `attempt` without prompts or keys. Raises `LLMUnavailable` if all providers fail or are tripped.
- `agent/src/sellershield_agent/llm/structured.py`: `generate_structured[T: BaseModel]` uses JSON schema mode when supported, otherwise falls back to JSON object mode with prompt schema instructions. Validates strictly with Pydantic; on validation error, retries once appending the validation error text; raises `StructuredOutputError` if second attempt fails.
- `agent/scripts/llm_smoke.py`: Standalone CLI testing script querying `{answer: str, confidence: float}` for "Reply with the word ready", reporting latencies and parsed output without leaking secrets.
Invariants that must stay true:
- The LLM never writes to the store; tool calling is read and propose only (Architecture Invariant 1).
- Prompts, messages, tool arguments, and API keys are never logged (Architecture Invariant 10, KP-13).
- Model output never decides money, permissions, or identity (Architecture Invariant 2).
- Circuit breaker timing evaluates strictly against the domain `Clock`, never `datetime.now()` (KP-06).
- 401, 403, and 404 configuration errors must never expose secret keys in error messages or logs.
- Unknown provider capabilities must default all feature flags to `False`.
- LLM output schemas must be flat Pydantic models (strings, numbers, booleans, enums, lists of simple objects; no unions or recursion).
Tests (paths):
- `agent/tests/test_llm.py`
Known limits:
- Evaluation harness uses `LLM_CACHE_MODE=record` or `replay` to conserve free-tier quota (KP-18).
Do not change because:
- Multi-provider fallback and deterministic reliability form the core story for demo stability and evaluation scoring.
Related: ADR-003, ADR-013, KP-06, KP-09, KP-13, KP-18, 02-agent.md sections 3, 4.2, 14.7, 21 amendments 7 & 8.

## F-A07b Policy documents and refund calculator   [done]
What it does:
Provides immutable versioned policy documents (`v1`, `v2`, `v3`) with strict validation, customer-safe policy summarization without internal limits or risk leakage, and an exact integer-paise refund calculator implementing Spec §7.4.
How it works (flow and key files):
- `agent/src/sellershield_agent/policy/models.py`: Frozen Pydantic v2 models (`extra='forbid'`, `frozen=True`): `PolicyDocument`, `ReturnShippingPolicy`, `RiskPolicy`, `CustomerFacingText`, `RefundBreakdown`, and `ItemAmounts` with zero float fields and non-negative integer paise/basis points validation.
- `agent/src/sellershield_agent/policy/versions/`: Versioned policy configurations (`v1.json` auto limit ₹1,000, `v2.json` ₹750, `v3.json` ₹500 default active version).
- `agent/src/sellershield_agent/policy/loader.py`: `load_policy(version=None)` selects active version via `POLICY_ACTIVE_VERSION` (default `v3`), validates against `PolicyDocument`, and rejects unknown or negative values.
- `agent/src/sellershield_agent/policy/summary.py`: `customer_safe_summary(policy)` outputs structured customer-safe text containing return windows by category, return shipping responsibility, and refund timing only, strictly excluding forbidden words (`limit`, `threshold`, `risk`, `approval`, `score`, `staff`, `owner`, `auto`).
- `agent/src/sellershield_agent/policy/refund.py`: Pure function `compute_refund(...)` calculating line-item unit allocations with remainder paise on the first unit, shipping refunds (whole-order seller fault or lost shipment only), return shipping fee deduction (non-free reason and refund resolution only), zero floor, and store `max_refundable_paise` capping.
Invariants that must stay true:
- Money math uses integer arithmetic only; zero `float`, `round()`, or `Decimal` division (Architecture Invariant 3, KP-05).
- Model output never calculates or decides refund amounts (Architecture Invariant 2).
- The refund calculator is pure and deterministic; it never calls an LLM, the store, or system clock (Architecture Invariant 1).
- Customer-facing text never discloses risk thresholds, internal auto limits, staff roles, or risk scores (Architecture Invariant 10, KP-23).
- Policy models strictly reject unknown fields (`extra='forbid'`) and negative values (`ge=0`).
Tests (paths):
- `agent/tests/test_policy.py`
Known limits:
- Decision engine rule evaluation (R-01 through R-15) builds on top of this calculator in T1.3.
Do not change because:
- Exact integer refund math and non-leaking customer summaries form the foundational financial guardrails for all returns and customer claims.
Related: ADR-016, KP-05, KP-23, 02-agent.md sections 7.1, 7.2, 7.4, 21 amendment 6.

## F-A14a Audit core   [done]
What it does:
Provides the tamper-evident SHA-256 hash-chained audit service (`AuditService.append`, `AuditService.verify_chain`) and sanitization layer (`sanitize_details`). Ensures that every business action, tool call, security event, and state transition writes an immutable, sequentially linked audit record inside the caller's write transaction.
How it works (flow and key files):
- `agent/src/sellershield_agent/audit/models.py`: Defines the `audit_events` table registered on shared `get_metadata()`. Columns: `seq` (PK autoincrement), `event_id`, `ts_utc`, `request_id`, `session_id`, `actor_type`, `actor_id`, `event_type`, `resource_type`, `resource_id`, `action`, `outcome`, `policy_version`, `details_json`, `prev_hash`, `hash`. Enforces a unique index on `prev_hash` to cryptographically prevent chain forks.
- `agent/src/sellershield_agent/audit/sanitize.py`: Implements `sanitize_details` rejecting non-JSON types, redacting sensitive keys and Bearer tokens, stripping email addresses, eliminating the word `Authorization` in all casings, enforcing a 500-character string limit, and scrubbing foreign customer IDs from denial events.
- `agent/src/sellershield_agent/audit/service.py`: `AuditService.append(tx, event)` runs inside the caller's `write_tx` under `BEGIN IMMEDIATE` SQLite lock, reads the previous row's hash (genesis `"0" * 64` if empty), computes `hash = SHA-256(canonical_json(event_fields + prev_hash))`, and atomically commits with the business change. `verify_chain(conn)` iterates from genesis and reports `ChainStatus(ok, checked, first_broken_seq)`.
Invariants that must stay true:
- Audit rows must be written in the exact same transaction as the state change they describe (Architecture Invariant 6). If the audit insert fails, the business change aborts.
- `prev_hash` has a unique index, preventing chain forks.
- Genesis event uses `0` * 64 as `prev_hash`.
- Details JSON is sanitized: tokens, keys, emails redacted; strings capped at 500 characters; no foreign customer IDs in denial records.
- The word `Authorization` never appears anywhere in stored `details_json`.
Tests (paths):
- `agent/tests/test_audit.py` (12 tests covering genesis, 100 sequential events, column tampering, middle row deletion, 8-thread concurrency on real SQLite, rollback atomicity, sanitization, and denial ID scrubbing).
Known limits:
- Seller audit query endpoints (`GET /v1/seller/audit`, `GET /v1/seller/audit/verify`) and webhook correlation follow in Phase 2.
Do not change because:
- "Every action is logged and tamper-evident" is a fundamental security invariant and hackathon differentiator; tampering must be cryptographically detectable.
Related: ADR-011, ADR-018, KP-01, KP-07, KP-13, KP-21, 02-agent.md sections 10, 12, 23.

## F-A07c Decision engine   [done]
What it does:
Provides the deterministic decision engine `decide(facts, policy, now) -> Decision` evaluating pure rules R-01 through R-15, computing customer risk indicators, assessing shipment delivery delay, and synthesizing immutable decisions with strict precedence (`DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW`). Guarantees customer-facing safety with zero forbidden words, zero float math, zero LLM calls, zero I/O, and zero clock reads.
How it works (flow and key files):
- `agent/src/sellershield_agent/policy/facts.py`: Frozen immutable `DecisionFacts` model holding evaluated order, items, reason code, resolution, refund method, evidence status, active returns, historical refunds, store refundable amounts, customer risk facts, shipment info, and timestamp. `compute_facts_hash(facts)` calculates a deterministic SHA-256 hash over canonical JSON strictly excluding `reason_text` to guarantee immunity against prompt injection and arbitrary customer text changes (KP-27).
- `agent/src/sellershield_agent/policy/rules.py`: Pure functions for rules R-01 through R-15 returning `RuleResult(id, passed, blocking, outcome_if_failed, customer_text, seller_text, facts, missing_slots)`. All customer-facing text is verified neutral and strictly contains zero forbidden words (`limit`, `threshold`, `risk`, `score`, `staff`, `owner`, `auto`) to prevent policy boundary leakage (KP-23).
- `agent/src/sellershield_agent/policy/risk.py`: Spec §7.5 risk assessment computing additive signals (`returns_30d >= 5` [+50] or `>= 3` [+30]; `refund_ratio_90d_bp > 4000` [+20]; `order_value_paise >= 200000` [+20]; `account_age_days < 14` [+15]; `suspected_injection` [+10]). Risk bands: low (< 30), medium (30-59), high (60+). Risk never causes DENY; high risk forces `NEEDS_APPROVAL`, medium sets `recommendation_review` flag.
- `agent/src/sellershield_agent/policy/delay.py`: `assess_delay(shipment, now, policy) -> DelayAssessment(kind, days_late)` computing calendar-day delay in policy timezone (Asia/Kolkata), mapping < 2 days to `minor`, >= 2 days to `options`, and carrier status `lost` to `lost`.
- `agent/src/sellershield_agent/policy/decide.py`: Pure function `decide(facts, policy, now) -> Decision` orchestrating rule evaluation, precedence synthesis, refund computation via `compute_refund()`, approver role routing (`seller_staff` up to ₹2,000 / 200,000 paise; `seller_owner` above; `ESCALATE` at ₹5,000 / 500,000 paise hard ceiling), slot collection, and flag assignment (`capped`, `recommendation_review`).
Invariants that must stay true:
- The decision engine is pure and deterministic: it never calls an LLM, the store, or system clock (Architecture Invariants 1 & 2).
- Precedence order is strictly enforced: `DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW`.
- All monetary amounts use integer paise only (`_paise`); zero float, round(), or Decimal arithmetic (Architecture Invariant 3, KP-05).
- Customer-facing text never discloses risk thresholds, internal limits, staff roles, or risk scores (Architecture Invariant 10, KP-23).
- `reason_text` is untrusted data and is never read or evaluated by any rule, and is strictly excluded from `facts_hash` (Architecture Invariant 2, KP-27).
- High risk never causes `DENY`; risk only escalates approval requirements (Spec §7.5).
- A lost shipment refund request always yields `NEEDS_APPROVAL` with `seller_staff` (Spec §7.6).
- Reasons `damaged`, `wrong_item`, and `not_as_described` require evidence: without evidence yields `NEEDS_INFO` with `missing_slots=['evidence']`; with evidence yields `NEEDS_APPROVAL` (Spec §7.3).
Tests (paths):
- `agent/tests/test_decide.py` (35 tests covering SC-04 through SC-17, rule coverage R-01 to R-15, customer text safety, precedence, facts hash invariance, adversarial break-it tests, and Hypothesis property tests).
Known limits:
- Orchestrator tool gateway binding and customer interaction loops follow in T1.4, T1.5, and T1.6.
Do not change because:
- The decision engine provides the core deterministic proof that all money, permissions, and returns are strictly governed by backend business logic rather than LLM hallucinations.
Related: ADR-016, ADR-017, KP-05, KP-23, KP-27, Spec 01 section 10, Spec 02 sections 7.3, 7.5, 7.6, 23 amendments 1, 3, 7.

### Scenario Expectation Table (SC-04 to SC-17)

| Scenario | Order ID | Action | Items / Qty | Reason | Policy | Decision | Total Refund | Approver Role | Failed Rules / Missing Slots | Flags |
|---|---|---|---|---|---|---|---|---|---|---|
| SC-04 | ORD-1042 | return | 1x P-101 | defective | v3 | NEEDS_APPROVAL | ₹1,200 (120,000p) | seller_staff | [R-09] | free return shipping |
| SC-05 | ORD-1043 | return | 1x P-102 | changed_mind | v3 | ALLOW | ₹439 (43,900p) | None | [] | return fee ₹60 deducted |
| SC-06 | ORD-1044 | return | 1x P-103 | changed_mind | v3 | DENY | ₹0 (0p) | None | [R-03] | window expired (>7d) |
| SC-07 | ORD-1045 | return | 1x P-201 | changed_mind | v3 | DENY | ₹0 (0p) | None | [R-03, R-04] | hygiene non-returnable |
| SC-08 | ORD-2031 | return | 1x P-101 | defective | v3 | NEEDS_APPROVAL | ₹598.99 (59,899p) | seller_staff | [R-09] | auto limit ₹500 exceeded |
| SC-08 | ORD-2031 | return | 1x P-101 | defective | v1 | ALLOW | ₹598.99 (59,899p) | None | [] | within v1 auto limit (₹1,000) |
| SC-09 | ORD-1041 | return | 1x P-101 | defective | v3 | DENY | ₹0 (0p) | None | [R-06] | duplicate return RET-9001 |
| SC-10 | ORD-2040 | return | 1x P-101 | defective | v3 | ALLOW | ₹500 (50,000p) | None | [] | capped=True (max refundable) |
| SC-11 | ORD-1046 | return | 1x P-101 | damaged | v3 | NEEDS_INFO | ₹0 (0p) | None | missing_slots=[evidence] | evidence required |
| SC-11 | ORD-1046 | return | 1x P-101 | damaged | v3 | NEEDS_APPROVAL | ₹1,200 (120,000p) | seller_staff | [R-09] | evidence provided |
| SC-12 | ORD-1060 | tracking | N/A | delayed | v3 | DelayAssessment | N/A | None | kind=options, days_late=4 | delivery delayed by 4 days |
| SC-13 | ORD-1043 | return | 1x P-102 | injection in text | v3 | ALLOW | ₹439 (43,900p) | None | [] | facts_hash invariant, text ignored |
| SC-15 | ORD-3010 | return | 1x P-101 | defective | v3 | NEEDS_APPROVAL | ₹1,200 (120,000p) | seller_staff | [R-09, R-11] | risk 70 (high), never DENY |
| SC-15 | ORD-3011 | return | 1x P-101 | defective | v3 | NEEDS_APPROVAL | ₹1,200 (120,000p) | seller_staff | [R-09, R-11] | risk 90 (high), never DENY |
| SC-16 | ORD-4020 | return | 1x P-301 | changed_mind | v3 | NEEDS_INFO | ₹0 (0p) | None | missing_slots=[refund_method] | COD refund method missing |
| SC-16 | ORD-4020 | return | 1x P-301 | changed_mind | v3 | NEEDS_APPROVAL | ₹639 (63,900p) | seller_staff | [R-09] | store_credit selected, fee waived |
| SC-16 | ORD-4021 | cancel | all items | cancellation | v3 | ALLOW | ₹699 (69,900p) | None | [] | pre-dispatch cancellation |
| SC-17 | ORD-1070 | refund | all items | lost | v3 | NEEDS_APPROVAL | ₹899 (89,900p) | seller_staff | [R-09, R-12] | carrier lost, full refund |

## F-A11a Proposal state   [done]
What it does:
Provides durable, tested storage and deterministic state transitions for customer-facing proposals (`proposals` table), along with canonical proposal card extraction and compact SHA-256 agreement hashing (`proposals/hashing.py`). Guarantees the architectural invariant: "what you saw is what you approved".
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/state.py`:
  - `proposals` table: `proposal_id` (PRP- with 8 random hex characters from `secrets.token_hex(4)`), `customer_id`, `session_id`, `type`, `payload_json`, `decision`, `amounts_json`, `policy_version`, `facts_hash`, `proposal_hash`, `status`, `expires_at`, `confirmed_at`, `version`.
  - States: `proposed`, `confirmed`, `cancelled`, `expired`, `executing`, `succeeded`, `failed`, `unknown`.
  - Allowed transitions: `proposed -> {confirmed, cancelled, expired}`, `confirmed -> {executing}`, `executing -> {succeeded, failed, unknown}`, `unknown -> {succeeded, failed, executing}`.
  - `transition_proposal()`: Executes atomic single UPDATE with version and valid from_status in WHERE clause; increments version, emits audit record in same write_tx, and raises `VersionMismatch` / `InvalidTransition` on race or mismatch.
- `agent/src/sellershield_agent/proposals/hashing.py`:
  - `canonical_card(...)`: Extracts only verified customer-visible elements (type, order_id, sorted items, integer paise amounts breakdown, method, decision, policy_version, ISO-8601 UTC expiry). Strictly omits model text and customer reason text.
  - `compute_proposal_hash(...)`: Computes deterministic SHA-256 hash over sorted, compact canonical JSON (`sort_keys=True`, `separators=(',', ':')`).
Invariants that must stay true:
- Proposal IDs are cryptographically random (`secrets.token_hex(4)`), never autoincrement.
- Any change to amounts, quantities, items, method, policy version, or expiry alters the proposal hash.
- Model text or commentary never alters the proposal hash.
- Transition and audit event commit in the same transaction; an audit failure rolls back the transition.
Tests (paths):
- `agent/tests/test_state.py` (transitions, optimistic lock winner, hash sensitivity, model text immunity, amount sabotage drill, 10,000 PRP ID uniqueness draws, FixedClock 10-minute expiry boundary).
Related: ADR-026, Spec §2, §6.1, §10, KP-01, KP-27.

## F-A13a Approval state   [done]
What it does:
Provides durable, tested storage and deterministic state transitions for seller approvals (`approvals` table), with role checks, SLA deadlines, expiry tracking, and store authorization reference linking.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/state.py`:
  - `approvals` table: `approval_id` (APR- with 8 digits), `proposal_id` (unique), `order_id`, `return_id`, `refund_id`, `amount_paise`, `required_role`, `status`, `policy_version`, `facts_hash`, `due_at`, `expires_at`, `decided_by`, `decided_at`, `decision_reason`, `store_authorization_ref`, `version`.
  - States: `pending`, `approving`, `approved`, `rejected`, `expired`, `needs_revalidation`, `withdrawn`.
  - Allowed transitions: `pending -> {approving, rejected, expired, needs_revalidation, withdrawn}`, `approving -> {approved, pending}` (failure fallback per amendment 2), `needs_revalidation -> {pending, rejected, expired, withdrawn}`.
  - `transition_approval()`: Executes atomic single UPDATE checking version and rowcount, emits audit record in the same write transaction, and raises `VersionMismatch` / `InvalidTransition` on failure.
  - Expiry and SLA helpers: `is_approval_expired(...)` and `is_approval_due(...)` based on policy `approval_sla_hours` (due) and `approval_expiry_days` (expiry).
Invariants that must stay true:
- Approvals cannot move directly from `pending` to `approved` without passing through `approving`.
- Failed approval execution returns to `pending` with an audit record (amendment 2).
- Optimistic locking (`expected_version`) prevents concurrent overwrites.
Tests (paths):
- `agent/tests/test_state.py` (legal/illegal approval transitions, approval SLA due and expiry boundaries).
Related: ADR-026, Spec §9.1, §21 amendment 2, §21 amendment 4.

## F-A14b Policy decisions   [done]
What it does:
Provides durable, PII-sanitized storage for evaluated policy decisions (`policy_decisions` table), preserving rule results and fact snapshots for Decision Trace and policy simulation without storing personal data.
How it works (flow and key files):
- `agent/src/sellershield_agent/domain/state.py`:
  - `policy_decisions` table: `decision_id`, `request_id`, `customer_id`, `order_id`, `policy_version`, `facts_json`, `decision_json`, `created_at`.
  - `record_policy_decision(...)`: Strips customer addresses (street, city, pincode), phone numbers, email addresses, and JWT tokens before JSON serialization.
  - `executions` table: `execution_id`, `proposal_id`, `step`, `idempotency_key` (unique), `status`, `store_ref`, `error_code`, `started_at`, `finished_at`.
  - `client_events` table: `event_id`, `customer_id`, `type`, `payload_json`, `created_at`.
Invariants that must stay true:
- `policy_decisions` rows never contain personal data (address, phone, e-mail, tokens).
- `executions.idempotency_key` is unique across all execution steps.
- Stored times are timezone-aware UTC.
Tests (paths):
- `agent/tests/test_state.py` (PII sanitization verification, schema constraints verification).
Related: ADR-026, Spec §10, §21 amendment 4.

## F-A12 Executor saga and reconciler   [done]
What it does:
Carries out confirmed `ALLOW` and `NEEDS_APPROVAL` proposals reliably across `agent.db` and NestKart Store using two-phase writes, deterministic idempotency keys (`proposal_id:step`), reverse-order compensation, a startup/background reconciler (`5s -> 30s -> 2m -> stuck`), truthful customer-safe status messaging (`succeeded`, `pending_approval`, `unknown`, `failed`), and strict customer-token isolation.
How it works (flow and key files):
- `agent/src/sellershield_agent/execution/saga.py` & `steps.py`: `Executor.execute()` and `Executor.run()` run `create_return` (customer token inside confirm request) -> `transition_return("approved")` (for `ALLOW`, `ServiceContext`) -> `reserve_refund` (`ServiceContext`). On `NEEDS_APPROVAL` (including `propose_refund` without a return), creates an `approvals` row (`due_at` from `approval_sla_hours`, `expires_at` from `approval_expiry_days`, `seller_owner` above `staff_approval_limit_paise`), emits `client_events` `notification.new`, and enqueues a deduplicated `notifications_outbox` `seller.approval_needed` stub preserving `resolution` and `refund_method`.
- `agent/src/sellershield_agent/execution/db_ops.py` & `finalize_db.py`: Split each step cleanly into Phase (a) `write_tx` (`executions` row `status="executing"` + `execution_step_started` audit) -> commit -> Phase (c) `StoreClient` call with NO open DB transaction -> Phase (d) `write_tx` (`succeeded`/`failed`/`unknown` + `store_ref` + audit). Transient `StoreTimeout`/`StoreUnavailable` transitions to `"unknown"`; terminal errors stop the forward saga, look up duplicate active returns if needed (`proposal.failed_duplicate`), and run `compensate_proposal()` (`cancel_reservation` -> `cancel_return`) via `ServiceContext`, alerting sellers on compensation failures.
- `agent/src/sellershield_agent/execution/reconcile.py`, `reconcile_db.py`, `reconcile_steps.py`: `reconcile_once(clock, ...)` inspects `executing`/`unknown` steps on the `5s -> 30s -> 2m -> stuck` backoff schedule plus crashed orphan `executing`/`unknown` proposals (ignoring proposals already awaiting seller approval), queries store truth via `get_order_admin` (`ServiceContext`, matching `client_ref == idempotency_key`), resolves step state, and resumes forward steps or compensation.
- `agent/src/sellershield_agent/main.py`, `proposals/confirm.py`, `api/proposals.py`: Runs `reconcile_once(..., min_age_seconds=0)` on FastAPI startup before serving, schedules a periodic 5s background task during lifespan, and wires `SagaExecutor` as the default `Executor` dependency.
Invariants that must stay true:
- No `StoreClient` call ever executes inside an open DB transaction (`write_tx` or `read_tx`).
- Retried steps reuse the exact `f"{proposal_id}:{step}"` idempotency key; never mint a new key on retry.
- `StoreTimeout` and `StoreUnavailable` produce `unknown` (`"We are verifying this request with the store..."`), never `failed` or premature `succeeded`.
- Customer tokens are used solely for in-request `create_return` and never stored in DB rows, audit details, outbox payloads, client events, or logs, nor used by reconciler or compensation (`ServiceContext` only).
- Audit write failure on any step aborts that step's DB commit and leaves `executing` cleanly repairable by `reconcile_once`.
Tests (paths):
- `agent/tests/test_execution_saga.py` (12 tests covering `ALLOW`/`NEEDS_APPROVAL` sagas, reverse compensation + compensation failure alerts, `timeout_after_commit` & `unavailable` `5s/30s/2m/stuck` reconciliation, mid-step & between-step crash recovery, lifespan periodic loop, double confirm idempotency, audit rollback recovery, duplicate return & daily cap neutrality, open-tx spy checks, and customer JWT redaction/isolation).
Known limits:
- Seller approval execution (`authorize_refund`, `release_refund`) follows in T2.6; `store/http_client.py` currently needs service bearer auth wired for `transition_return`/`release_refund`/`cancel_reservation` before M3 live HTTP integration.
Do not change because:
- Core reliability & money-safety invariant ("never fake success" across split-brain store failures and crashes).
Related: ADR-030, KP-01, KP-07, KP-16, KP-45, Spec §2, §6.3, §9.1, §21 amendments 1 & 2, §24, Document 6 §0.

Registry (done / planned):
F-A00 Contracts (done)                    F-A11 Proposals and confirm (F-A11a, F-A11b, F-A11c done)
F-A01 Config and logging (done)           F-A12 Executor saga and reconciler (done)
F-A12b Webhooks and release (done)
F-A02 Error envelope and request ids (done) F-A13 Approvals (F-A13a, F-A13b done)
F-A03 Database layer (done)               F-A14a Audit core (done) / F-A14b Policy decisions (done)
F-A04 Money and time helpers (done)       F-A15 Cases and handler model
F-A05 JWT and JWKS verification (done)     F-A16 Memory
F-A06 LLM provider chain (done)            F-A17 Notification outbox
F-A07 Policy engine (done)                F-A18 Proactive monitor
F-A08 FactSheet and verifier (done)       F-A19 Push stream
F-A09 Tool gateway (done)                 F-A20 Evaluation harness
F-A10 Orchestrator (done)
