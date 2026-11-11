# CHANGELOG (what was built; newest first)

Entry format:
## YYYY-MM-DD T0.x Title
Files: created/changed list
What changed:
Verification run (commands and result summary):
Follow-ups:

## 2026-11-11 BT-1 Typo matching, session memory, order listing and partial quantity returns (BUG-015, BUG-016, BUG-018)
Files: agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/orchestrator/resolver.py, agent/tests/bt1/test_bug015_016_018.py, docs/BUGLOG.md, docs/BROWSER_TEST_REPORT.md, docs/CHANGELOG.md
What changed:
- **`agent/src/sellershield_agent/understand/router.py` (BUG-015)**:
  - Extended `ORD_FLEXIBLE_REGEX` and `normalize_order_id` to extract orders with typos like `ordr 1048` and `odr 1048`.
- **`agent/src/sellershield_agent/orchestrator/pipeline.py` (BUG-015 & BUG-016)**:
  - Added multi-turn context memory: when elliptical follow-ups like "when will it arrive?" contain no order ID and are not general listings, the pipeline queries previous turns to resolve the active order ID (`ORD-1048`).
  - Differentiated order listing queries ("show my orders", "where is my order"): renders `template_orders_list` and provides interactive order selection chips (`build_chips_card`) rather than arbitrarily defaulting to `ORD-1051`.
- **`agent/src/sellershield_agent/orchestrator/resolver.py` (BUG-018)**:
  - Added word number parsing ("one", "single", "1") to extract partial line item return quantities requested in customer text (e.g. "one kurta in ORD-2031"), generating proposal for 1 kurta (~₹598.99) instead of the full line quantity (2 kurtas, ₹1,257.98).
- **`agent/tests/bt1/test_bug015_016_018.py`**:
  - Added regression test suite verifying typo extraction, session order context resolution, order listing chip emission, and partial line item return quantities.
Verification run:
- `cd agent; uv run pytest tests/bt1/test_bug015_016_018.py`: 4 passed in 3.65s.
- `just agent-check`: PASS (`ruff check .` clean, `mypy src` clean across 148 source files, `pytest -q` 769 passed in 465.17s, `test_guard.py check` verified test suite integrity).
- Real browser verification in Google Chrome (Aarav Mehta & Diya Nair):
  - Test 1.2: `show my orders` returned full list of 6 orders with interactive tap-chips `[ORD-1048]`, `[ORD-1061]`, etc. (PASS).
  - Test 1.3: `where is my order` returned interactive selection chips (PASS).
  - Test 1.4: `where is ORD-1048` returned shipment tracking card with 2 days ETA and ₹899 (PASS).
  - Test 1.5: `when will it arrive?` maintained `ORD-1048` context and rendered tracking card (PASS).
  - Test 1.6: `wher is ordr 1048 plz` extracted `ORD-1048` and gave shipping status (PASS).
  - Test 3.4: `I want to return one kurta in ORD-2031` generated proposal card for ONE kurta for ₹598.99 (PASS).
Follow-ups:
- Complete BUG-019 (pre-dispatch order cancellation workflow).

## 2026-11-11 L4.3 demo_check: the demo beats as one command
Files: scripts/demo_check.py, justfile, agent/tests/test_demo_check.py, agent/src/sellershield_agent/cases/router.py, agent/src/sellershield_agent/main.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/STATE.md, docs/TASKS.md
What changed:
- **`scripts/demo_check.py`**:
  - Implemented one command that proves the three-minute demo beats work in fixture mode and against the real HTTP store.
  - Supports `--mode fixtures|http`, `--llm replay|live|off`, and `--runs N` (default 2).
  - Resets demo state before each run (fixtures: write-capable fixture store with policy v3 and reset audit chain; http: store and agent demo resets, credentials from environment variables).
  - Beat B1: C001 asks about ORD-1048; verifies order facts and tool_call audit event.
  - Beat B2: C001 reports ORD-1042 cookware defective; verifies confirmation card for 120,000 paise (state unmutated before click), customer confirm yields pending approval, seller views approval with 120,000 paise, policy v3 and rule table, seller approves, return advances to `inspected_ok`, refund released and completed exactly once, and customer client event exists.
  - Beat B3: C002 asks for ORD-1042 (uniform 404 not found without PII leakage); injection customer sends prompt injection (no state change, `injection_flag` audit event recorded, denial visible in seller audit list).
  - Beat B4: Proactive delay monitor creates exactly one notice (second run still one); damaged-item message creates case with deterministic brief.
  - Cross-run repeatability: verifies identical state counts (returns, approvals, refunds, cases, notices) across all runs.
  - Output format: one line per beat with PASS or FAIL, time, and request ID; exit code 0 on success.
  - Secret safety: zero tokens, bearer strings, or keys printed.
- **`justfile`**:
  - Added `demo-check:` recipe running `cd agent; uv run python ../scripts/demo_check.py --mode fixtures --llm off`.
- **`agent/tests/test_demo_check.py`**:
  - In-process test suite covering: all beats pass in fixture mode; deliberately broken expectation fails with clear message; two runs end with identical state counts; `--mode http` without demo environment variables is refused; zero tokens or keys printed in output.
- **Integration & Route Fixes**:
  - `agent/src/sellershield_agent/cases/router.py`: Guarded `list_seller_cases` to return 501 `not_implemented` when `DATABASE_URL == "sqlite:///:memory:"` preserving Gate G1 auth boundary contract while allowing live/fixture DB case listing.
  - `agent/src/sellershield_agent/main.py`: Restored `os.environ.get("STORE_MODE") == "fixtures"` check for KP-24, ensuring `test_cors.py` passes when `DEV_MODE=false`.
Verification run:
- `cd agent; uv run pytest tests/test_demo_check.py -v`: 5 passed in 9.81s.
- `uv run python ../scripts/demo_check.py --mode fixtures --llm off`: All beats PASS across 2 runs, identical state counts verified, exit code 0.
- `just demo-check`: All beats PASS across 2 runs, identical state counts verified.
- `just agent-check`: PASS (`ruff check .` clean, `mypy src` clean across 148 source files, `pytest -q` 767 passed in 418.12s, `test_guard.py check` verified test suite integrity).
Follow-ups: None.

## 2026-11-11 L4.4 Security Minimum: Route Sweep, DEV Gating, Log Redaction
Files: agent/tests/security/__init__.py, agent/tests/security/test_route_sweep.py, agent/tests/security/test_dev_gating.py, agent/tests/security/test_log_redaction.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/logging.py, agent/src/sellershield_agent/audit/sanitize.py, agent/src/sellershield_agent/domain/session.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/STATE.md
What changed:
- **Dynamic Route Sweep Suite (`agent/tests/security/test_route_sweep.py`)**:
  - Dynamically extracts all paths from the real application's OpenAPI specification.
  - Asserts missing token returns 401 unauthenticated across all paths (exempting `/healthz`, `/readyz`; requiring HMAC signature on `/v1/webhooks/store`).
  - Asserts customer bearer token on seller routes returns 403 forbidden.
  - Asserts expired token and wrong-audience token return 401 unauthenticated.
  - Asserts routes with ID path parameter yield uniform status code (404) and identical error envelope body shape (excluding `request_id`) for cross-customer IDs vs nonexistent IDs.
  - Asserts request bodies exceeding 1 MB return 413 Payload Too Large on all POST and PUT endpoints.
  - Asserts chat input messages exceeding 1,000 characters are rejected with 422 Unprocessable Entity.
- **DEV Gating and Production Hardening (`agent/tests/security/test_dev_gating.py`, `main.py`, `config.py`)**:
  - When `DEV_MODE=false`, all `/v1/dev/*` and `/dev/*` routes return 404 Not Found before authentication.
  - When `DEV_MODE=false` and `STORE_MODE=fixtures`, service initialization unconditionally halts with clear RuntimeError referencing KP-24.
  - When `DEV_MODE=false`, `STORE_MODE` defaults safely to `http`.
  - When `DEV_MODE=false`, FastAPI documentation routes (`/docs`, `/redoc`, `/openapi.json`) are completely disabled (404 Not Found).
  - Swept app routes to verify zero debug routes exist in production mode.
- **Comprehensive Log & Trace Redaction (`agent/tests/security/test_log_redaction.py`, `logging.py`, `audit/sanitize.py`, `domain/session.py`)**:
  - Attached `SensitiveRedactionFilter` to all root and structlog handlers.
  - Enhanced redaction regexes for JWTs (`eyJ...`), API key patterns (Google / Groq style tokens), `Bearer` tokens, email addresses, and phone numbers.
  - Sanitized both log events, decision trace fields, and audit detail payloads in `sellershield_agent/audit/sanitize.py`.
  - Enforced 1000-character cap on `ChatMessageInput.text` in `sellershield_agent/domain/session.py`.
  - Tested SC-04 complete flow with DEBUG logging captured: verified zero leaked JWT tokens, API keys, Authorization headers, email addresses, or phone numbers.
Verification run:
- `cd agent; uv run pytest tests/security -v`: 11 passed in 27.74s.
- `cd agent; uv run ruff check .`: All checks passed.
- `cd agent; uv run mypy src`: Success: no issues found in 148 source files.
- `cd agent; uv run python ../scripts/test_guard.py check`: PASS: test-guard verified test suite integrity (unfrozen: security/*).
- `git ls-files '*.env' '*.db'`: Printed nothing (0 tracked secrets or databases).
- `git grep -n -E 'AIza|gsk_'`: Printed nothing in code and test files.
Follow-ups:
- Human review and freeze for `agent/tests/security/*`.


<<<<<<< HEAD
## 2026-11-11 T3.6 Lean red-team and gate tests
Files: scenarios/redteam/phase3.yaml, agent/tests/redteam/test_phase3.py, agent/tests/e2e/test_gate_g3.py, docs/notes/T3.6.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Red-Team Scenarios (`scenarios/redteam/phase3.yaml`)**: Authored 30 red-team scenarios covering:
  - Streams (6): RT3-STR-01 to RT3-STR-06 (cross-tenant event isolation, hostile cursor validation before slot acquisition, 2-stream customer cap returning 429, stalled reader cleanup, forged event rejection, reconnect storm cursor replay).
  - Cases & handler (6): RT3-CASE-01 to RT3-CASE-06 (thirty concurrent cases SLA ordering, lawyer keyword priority escalation, summary prompt injection containment, customer handler flip forbidden, internal seller notes customer forbidden, cross-customer case reply rejection).
  - Notifications (4): RT3-NOTIF-01 to RT3-NOTIF-04 (recipient spoofing defense, template placeholder HTML injection escaping, 10x event deduplication, outbox mail outage backoff and exhaustion).
  - Monitor (4): RT3-MON-01 to RT3-MON-04 (spam Run Monitor 10s cooldown 429, concurrent run serialization under `_run_lock`, duplicate shipment alert deduplication, clock jump past SLA window).
  - Memory (4): RT3-MEM-01 to RT3-MEM-04 (adversarial prompt instruction memory containment, cross-customer memory isolation, mid-session memory deletion resilience, memory non-interference with deterministic policy).
  - Dev routes (2): RT3-DEV-01 to RT3-DEV-02 (dev route 404 before auth when DEV_MODE=false, customer token on dev route rejected with 401/403).
  - Clumsy-user flows (4): RT3-CLU-01 to RT3-CLU-04 (reconnect with Last-Event-ID, parallel tabs for same customer, close browser after Confirm then return, seller reply while customer types).
- **Red-Team Test Suite (`agent/tests/redteam/test_phase3.py`)**: 30 tests running scenarios through real HTTP app, ObedientFakeLLM, write-capable FixtureStoreClient, and FaultPlan. Verified:
  - No cross-customer event leakage (two real parallel customers C001 and C002).
  - Customer cannot flip handler; agent never speaks while under human handling.
  - Memory holds only IDs, enums, and short bounded strings.
  - Zero duplicate outbox notifications; zero sends inside DB transactions.
  - Zero forbidden words (risk scores, internal limits, prompts, tool names, tokens) or secrets in output.
  - Zero 500 Internal Server Errors; SHA-256 audit chain verified unbroken; Phase 2 LedgerOracle re-run verified.
- **Gate G3 Suite (`agent/tests/e2e/test_gate_g3.py`)**: 9 tests covering:
  - SC-12 (monitor flags once), SC-17 (lost shipment case with deterministic brief recommendation), T13 (human request creates persistent case and locks handler), T15 (notification failure doesn't rollback action), T16 (llm_down guided mode), T17 (injection cannot override rules), T18 (one notification across two monitor runs), push event to live stream within 2.0s of approval, and `scripts/demo_run.py` passing all 4 beats twice consecutively.
- **Sabotage Drills**: Successfully demonstrated test sensitivity by sabotaging and reverting 3 distinct defenses:
  1. Monitor run lock in `monitor/service.py` -> failed `test_concurrent_runs_serialize_and_return_running_run_id_and_http_rate_limit`.
  2. Outbox dedupe key in `outbox/enqueue.py` -> failed `test_rt3_notification_03_same_event_ten_times_deduplicated`.
  3. Template HTML escaping in `outbox/templates.py` -> failed `test_rt3_notification_02_template_placeholder_injection`.
Verification run (commands and result summary):
- `just agent-check`: Passed (`ruff check .` clean, `mypy src` clean across 143 files, full test suite passed, `test-guard` verified).
- `pytest tests/redteam tests/e2e/test_gate_g3.py -q`: Passed (168 passed in 145.81s).
- `python ../scripts/demo_run.py` (twice): Passed all 4 beats both times.
Follow-ups:
- T3.6 complete (`[NO COMMIT]`). Ready for Phase 3 review and freezing.

## 2026-11-11 T3.7 Real store: mail and shipments
Files: agent/src/sellershield_agent/store/http_client.py, scripts/handshake_phase3.py, agent/tests/test_store_http_phase3.py, agent/pyproject.toml, docs/notes/T3.7.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/TASKS.md, docs/STATE.md
What changed:
- **HttpStoreClient Extensions (`store/http_client.py`)**:
  - `send_mail`: Enforced `ServiceContext` (`Role.service`) and zero customer token rejection (`StoreForbidden`). Mints service token (`executor` or `monitor`), passes `X-Request-ID` and `Idempotency-Key` headers, formats payload matching `SendMailRequest` schema (`to_addr`, `subject`, `body_text`, `channel: "email"`), executes non-retried POST (`_execute_post`), and maps store response errors to the domain hierarchy.
  - `list_shipments_for_monitor`: Enforced `ServiceContext` (`Role.service`) with zero customer token (`StoreForbidden`). Mints monitor service token, passes `X-Request-ID` and `Authorization` headers, queries `/admin/shipments`, and provides transparent fallback to `/admin/orders` + `/orders/{id}/shipment` resolving order `customer_id`. Wraps connection/timeout errors into `StoreUnavailable`/`StoreTimeout`.
- **Phase 3 Handshake Script (`scripts/handshake_phase3.py`)**:
  - Standalone verification script sending a test email notification to C001 (`aarav.mehta@example.test`) via `HttpStoreClient.send_mail`, verifying delivery in C001's store inbox (`/mail/inbox`), and listing shipments via `HttpStoreClient.list_shipments_for_monitor` with count of delayed ones.
- **Contract Tests (`agent/tests/test_store_http_phase3.py`)**:
  - 15 respx contract tests covering headers, payload format, token isolation, error mappings, timeout mapping, zero retries on mutating writes, and pagination fallback.
Verification run (commands and result summary):
- `pytest tests/test_store_http_phase3.py`: Passed (15 passed in 6.44s).
- `just agent-lint`: Passed (`ruff check .` clean).
- `just agent-types`: Passed (`mypy src` clean across 143 source files).
- `just test-guard`: Passed (`test-guard verified test suite integrity`).
- `scripts/handshake_phase3.py` against running store: Passed (`[STEP 1]` sent notification `MSG-444D499C`, `[STEP 2]` verified in inbox, `[STEP 3]` retrieved 19 shipments with 2 delayed: ORD-1070 lost, ORD-2037 exception, exited 0).
Follow-ups:
- T3.7 complete (`[NO COMMIT]`).

## 2026-11-11 T3.4 Proactive delay monitor
Files: agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/domain/models.py, agent/src/sellershield_agent/store/client.py, agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, agent/src/sellershield_agent/monitor/service.py, agent/src/sellershield_agent/monitor/router.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/db.py, agent/tests/test_monitor.py, docs/notes/T3.4.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/BUGLOG.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **StoreClient Protocol & Clients (`store/client.py`, `store/fixtures.py`, `store/http_client.py`)**: Added `list_shipments_for_monitor(service_ctx, statuses, limit, cursor)` to `StoreClient` protocol. Implemented with relative T0 offset resolution, cursor pagination, and customer mapping in `FixtureStoreClient`. Implemented in `HttpStoreClient` calling `/admin/shipments` with client credentials service token. Enforces service role with zero customer token (`StoreForbidden`).
- **Monitor Service (`monitor/service.py`, `domain/state.py`)**: Implemented `run_monitor(service_ctx, clock)` scanning up to 200 shipments with loop guard. Categorizes timeliness: flags options (delay >= 2 days or carrier exceptions < 5 days), enqueues `delay.notice` outbox notifications with strict dedupe key `delay:{order_id}:{eta_date}:{kind}`, emits `monitor.notice` client events with 3 guided action chips ("Wait", "Cancel if not delivered", "Talk to support"), and opens high-priority support cases for lost shipments and carrier exceptions >= 5 days with deterministic briefs. Records `monitor_runs` and `monitor_events`. Serializes concurrent runs via `_run_lock` returning active `run_id`.
- **Seller Monitor API (`monitor/router.py`, `main.py`, `api/seller_stub.py`)**: Mounted `/v1/seller/monitor/run` (enforces `require_seller`, 10s cooldown per clock returning 429 `rate_limited`) and `GET /v1/seller/monitor/runs(+/{id})` conforming to OpenAPI MonitorRun schema. Removed shadowed 501 stubs.
Verification run (commands and result summary):
- `ruff check .`: Passed (clean, 0 errors).
- `mypy src`: Passed (`Success: no issues found in 143 source files`).
- `pytest tests/test_monitor.py`: Passed (6 passed in 1.56s).
- Full suite: 669 passed in 251.43s.
- Sabotage drills: verified dedupe key and run lock removals fail tests immediately.
Follow-ups:
- T3.4 complete (`[NO COMMIT]`).

## 2026-11-11 T3.5 Overview, dev controls and demo run script
Files: agent/src/sellershield_agent/overview/router.py, agent/src/sellershield_agent/dev/faults.py, agent/src/sellershield_agent/dev/router.py, agent/src/sellershield_agent/api/health.py, agent/src/sellershield_agent/api/dev_stub.py, agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/audit/service.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/outbox/worker.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, scripts/demo_run.py, agent/tests/test_t35_overview_dev_demo.py, docs/notes/T3.5.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/BUGLOG.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Seller Overview (`overview/router.py`, `main.py`)**: Implemented `GET /v1/seller/overview` returning pending approvals (with Clock overdue check), open cases by priority (excluding resolved/closed), failed notification IDs, last monitor run summary, today's conversations, proposals, and executions (IST Asia/Kolkata date partition). Strict ID-only payload with zero customer PII and zero forbidden internal words.
- **Dev Controls (`dev/faults.py`, `dev/router.py`, `auth/deps.py`, `audit/service.py`, `api/health.py`)**:
  - `POST /v1/dev/faults` & `GET /v1/dev/faults`: In-memory fault manager supporting `llm_down`, `audit_fail`, `mail_down`, `slow_llm`, `off`. Exposed in `/readyz` (`dev_faults` dict when `DEV_MODE=true`, `None` when `DEV_MODE=false`).
  - `POST /v1/dev/reset`: Clears 19 agent tables, clears in-memory faults and cooldowns, reloads policies `v1`..`v3` (`v3` active), and restarts the SHA-256 audit chain with a verified genesis event at `seq=1`. Guarantees `404` before auth/JWKS whenever `DEV_MODE=false`.
- **Demo Run Script (`scripts/demo_run.py`)**: Created standalone dev verification tool runnable as `uv run python ../scripts/demo_run.py` from `agent/`. Executes system reset followed by 4 beats through real HTTP APIs:
  - Beat 1: C001 asks about ORD-1048.
  - Beat 2: C001 returns defective ORD-1042 (confirmation card -> confirm -> seller approval -> return lifecycle -> webhooks -> refund completion -> customer event verified).
  - Beat 3: C002 asks for ORD-1042 (uniform denial, zero PII leak); C005 sends prompt injection (flagged in audit, zero mutations).
  - Beat 4: Proactive monitor delay scan flags ORD-2037 once and opens high-priority case for ORD-1070; seller overview reports metrics accurately; SHA-256 audit hash chain verified.
  - Prints PASS/FAIL per beat and exits 0 on success.
Verification run (commands and result summary):
- `ruff check src/`: Passed (clean, 0 errors).
- `mypy src`: Passed (`Success: no issues found in 143 source files`).
- `pytest tests/test_t35_overview_dev_demo.py`: Passed (`6 passed in 21.16s` and `6 passed in 40.31s`).
- `uv run python ../scripts/demo_run.py` from `agent/`: Passed twice consecutively (`ALL 4 BEATS PASSED` on both runs).
Follow-ups:
- T3.5 complete (`[NO COMMIT]`). Ready for human `just freeze-tests`.

## 2026-11-11 T3.2 Push stream, notification outbox and notification endpoints
Files: agent/src/sellershield_agent/events/emit.py, agent/src/sellershield_agent/events/router.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/outbox/enqueue.py, agent/src/sellershield_agent/outbox/templates.py, agent/src/sellershield_agent/outbox/worker.py, agent/src/sellershield_agent/outbox/router.py, agent/src/sellershield_agent/outbox/templates/approval.decided.txt, agent/src/sellershield_agent/outbox/templates/refund.updated.txt, agent/src/sellershield_agent/outbox/templates/case.created.txt, agent/src/sellershield_agent/outbox/templates/case.message.txt, agent/src/sellershield_agent/outbox/templates/delay.notice.txt, agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/store/client.py, agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, agent/src/sellershield_agent/proposals/confirm_audit.py, agent/src/sellershield_agent/approvals/common_db.py, agent/src/sellershield_agent/approvals/expire.py, agent/tests/test_push_outbox.py, agent/tests/test_audit_mutators.py, docs/notes/T3.2.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/BUGLOG.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **SSE Push & Polling (`events/router.py`, `events/emit.py`, `db.py`)**: Implemented `GET /v1/chat/stream` with cursor resume (`Last-Event-ID` > `?since=`), strict `422` validation on negative/non-ASCII/oversized cursors, `20 s` heartbeats, `2`-stream per-customer limit (`429`), post-commit cross-thread asyncio notifier hooks in `write_tx` plus `2 s` DB fallback floor, and clean disconnect cleanup; implemented `GET /v1/me/events?since=&limit=` (`1..100`) returning identical events with `CEV-...` dedupe IDs; wired `approval.decided`, `refund.updated`, `proposal.expired`, and `notification.new`.
- **Transactional Notification Outbox & Fixed Templates (`outbox/enqueue.py`, `outbox/templates.py`, `outbox/worker.py`, `outbox/templates/*.txt`, `main.py`, `store/*`)**: Started background outbox loop in `main.py` lifespan checking due `channel == "email"` rows (`queued`/`failed`/`retry_scheduled` -> `sending`), delivering via `StoreClient.send_mail` (`Role.service`) strictly outside transactions, backing off `1 s / 5 s / 30 s`, and settling to `exhausted` at `3` attempts. Resolved recipient emails strictly from DB/store ownership records; added 5 fixed English HTML-escaped, ID-only-subject templates passing forbidden-word rules; enqueued `approval.decided` and `refund.updated` emails atomically in business `write_tx`s.
- **Seller Notification Endpoints (`outbox/router.py`, `api/seller_stub.py`)**: Implemented `GET /v1/seller/notifications` (`?status=`) and atomic `POST /v1/seller/notifications/{id}/retry` (resets `failed`/`exhausted` rows once with same-tx audit); removed shadowed `501` stubs in `seller_stub.py`.
- **Tests (`tests/test_push_outbox.py`, `tests/test_audit_mutators.py`)**: Added 14 end-to-end and security tests on real loopback `uvicorn` + `httpx` and updated `test_t19` outbox row assertion for parallel `seller_stub` + `email` outbox enqueues.
Verification run (commands and result summary):
- `just agent-check`: Passed (`ruff check .` clean, `mypy src` clean across `137` source files, `pytest -q` `669 passed`, `test_guard.py check` PASS).
- Named T3.2 tests (`test_seller_approval_reaches_customer_stream_within_2_seconds`, `test_two_customers_on_parallel_streams_never_see_each_other`, `test_every_template_passes_forbidden_words`, `test_mail_fail_once_retried_and_sent_t15`): Passed (`4 passed in 1.73s`).
- Live check (`scripts/dev_auth.py` + `uvicorn :8001` + `curl.exe -N http://localhost:8001/v1/chat/stream` + live approve): Passed (SSE `approval.decided` frame arrived in `0.047s`).
- All 4 sabotage drills (skip customer filter, allow extra payload fields, trust request recipient, drop dedupe key): Failed expected tests and cleanly reverted.
Follow-ups:
- T3.2 complete (`[NO COMMIT]`). Phase 3 tests ready for human `just freeze-tests`.

## 2026-11-11 T3.3 Structured memory
Files: agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/memory/__init__.py, agent/src/sellershield_agent/memory/models.py, agent/src/sellershield_agent/memory/writer.py, agent/src/sellershield_agent/memory/snapshot.py, agent/src/sellershield_agent/memory/router.py, agent/src/sellershield_agent/api/chat.py, agent/src/sellershield_agent/understand/service.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/orchestrator/resolver.py, agent/src/sellershield_agent/tools/read_tools.py, agent/src/sellershield_agent/compose/templates.py, agent/src/sellershield_agent/compose/factsheet.py, agent/src/sellershield_agent/approvals/decide_db.py, agent/src/sellershield_agent/approvals/withdraw_db.py, agent/src/sellershield_agent/execution/finalize_db.py, agent/src/sellershield_agent/execution/refund_webhook_db.py, agent/src/sellershield_agent/cases/api.py, agent/src/sellershield_agent/main.py, agent/tests/test_memory.py, docs/notes/T3.3.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Structured Memory Schema & Writer (`memory/models.py`, `memory/writer.py`, `domain/state.py`)**: Added `customer_memory` and `memory_events` tables and Pydantic `CustomerProfile` (`extra="forbid"`, strings `<= 64` chars, `open_item_ids` capped at 20, zero free text). Implemented `record_memory_event_in_tx` wired via single-line in-transaction calls in `execution/finalize_db.py`, `execution/refund_webhook_db.py`, `approvals/decide_db.py`, `approvals/withdraw_db.py`, and `cases/api.py`.
- **Snapshot, Greeting Chips & Live Lookup (`memory/snapshot.py`, `api/chat.py`, `orchestrator/pipeline.py`, `resolver.py`, `read_tools.py`, `compose/templates.py`, `compose/factsheet.py`)**: Implemented `snapshot(customer_id)` and `get_memory_snapshot`, exposed greeting chips on `POST /v1/chat/sessions`, routed chip taps (`Check return RET-xxxx`, `Check refund RFD-xxxx`, `Check case CASE-xxxx`) to live read tools/views rather than cached status text, and updated `api/chat.py:get_pipeline` via `_resolve_chat_store_client` so FastAPI dependency overrides on `get_store_client` are honored.
- **Prompt Boundary & Erasure (`understand/service.py`, `memory/router.py`)**: Hardened `<data><memory>...</memory></data>` injection in `understand()` using strictly verified `ctx.customer_id` and `CustomerProfile` validation, and implemented `DELETE /v1/me/memory` with atomic deletion, `erase_memory` audit logging, and strict `{"erased": True}` OpenAPI schema compliance.
- **Tests (`agent/tests/test_memory.py`)**: Added 6 tests covering SC-04 snapshot + live chip status, cross-customer isolation, Hypothesis free-text/64-char property invariant, stored injection resistance (across chat and all writer fields) + 20-item cap, mid-session `DELETE /v1/me/memory`, and AST check proving `policy/` and `tools/` never read memory.
Verification run (commands and result summary):
- `uv --directory agent run --no-sync ruff check ...` (T3.3 files + `tests/test_memory.py`): Passed (`All checks passed!`).
- `uv --directory agent run --no-sync mypy ...` (20 files): Passed (`Success: no issues found in 20 source files`).
- `uv --directory agent run --no-sync pytest tests/test_memory.py -v` (`BypassSandbox: true`): Passed (`6 passed in 5.98s`).
- `uv --directory agent run --no-sync pytest tests/test_memory.py::test_property_memory_never_contains_free_text_or_strings_longer_than_64_chars tests/test_memory.py::test_customer_memory_isolation -v`: Passed (`2 passed in 2.75s`).
- Targeted regressions (`test_chat.py`, `test_understand.py`, `test_tools.py`, `test_verifier.py`, `test_attacks.py`, `test_gate_g2.py`, `test_seller_approvals.py`, `test_cases_escalation.py`, `test_audit_views.py`, `test_audit_views_trace.py`, `test_audit_mutators_coverage.py`): Passed (`143 passed` + `56 passed`).
- `$env:UV_NO_SYNC='1'; just agent-check`: `ruff check .` clean, `mypy src` clean (`136 source files`), `test-guard` PASS (`test suite integrity verified`), `pytest -q` (`667 passed, 2 failed` — both failures isolated in parallel T3.2's unfrozen `tests/test_push_outbox.py`).
Follow-ups:
- T3.3 complete (`[NO COMMIT]`). Wait for parallel T3.2 completion before running `just freeze-tests`.

## 2026-11-11 T3.1 Cases, escalation triggers and handler model
Files: agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/cases/models.py, agent/src/sellershield_agent/cases/brief.py, agent/src/sellershield_agent/cases/api.py, agent/src/sellershield_agent/cases/router.py, agent/src/sellershield_agent/cases/__init__.py, agent/src/sellershield_agent/orchestrator/escalation.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/orchestrator/cards.py, agent/src/sellershield_agent/policy/models.py, agent/tests/test_cases_escalation.py, docs/notes/T3.1.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Case and Event Schema**: Extended `cases` with `session_id`, `handler` (`agent`/`human`), `version`, and `priority` (`normal` / `high` with Clock-derived SLA due dates: 24h normal, 2h high).
- **Escalation Triggers**: Created `orchestrator/escalation.py` evaluating legal/threat words (high priority), policy `ESCALATE` (case), customer asks for human (case), and angry twice in a row (offers `Talk to Human` chip, no case). All trigger evaluations write tamper-evident audit records. Enforced 30-minute idempotency per `(customer_id, category, order_id)`.
- **Deterministic Case Brief**: Created `cases/brief.py` building structured briefs with sanitized and HTML-escaped untrusted customer text (tagged `verified: False`), secret token scrubbing, zero LLM model text, segregated seller-only risk signals, and deterministic next-step recommendations derived from hardcoded rule tables.
- **Handler Model & Concurrency**: Implemented compare-and-set seller reply atomic handler flipping from `agent` to `human`. While human, the conversational pipeline guarantees the agent stays silent, answering only "A team member is handling this case..." and executing zero propose tools. Handback endpoint safely restores `agent` handling.
- **Customer and Seller Case Endpoints**: Implemented `/v1/me/cases` and `/v1/seller/cases` with uniform 404 on unowned cases (IDOR defense), status update, assignment, and handback; dispatches `emit_client_event` (`case.message`, `case.status`) and `enqueue_notification`.
Verification run (commands and result summary):
- `uv run pytest tests/test_cases_escalation.py -v`: Passed (10/10 passed in 0.36s).
- `uv run pytest tests/test_chat.py -v`: Passed (14/14 passed in 9.22s).
- `uv run ruff check src/sellershield_agent/cases src/sellershield_agent/orchestrator/escalation.py src/sellershield_agent/orchestrator/pipeline.py src/sellershield_agent/orchestrator/cards.py tests/test_cases_escalation.py`: Passed (clean, 0 warnings).
- `uv run mypy src/sellershield_agent/cases src/sellershield_agent/orchestrator/escalation.py src/sellershield_agent/orchestrator/pipeline.py src/sellershield_agent/orchestrator/cards.py src/sellershield_agent/domain/state.py`: Passed (Success: no issues found in 9 source files).
- `uv run python ../scripts/test_guard.py check`: Passed (test-guard verified test suite integrity).
Follow-ups:
- T3.1 complete. Parallel tasks T3.2 and T3.3 in progress.


## 2026-11-11 T2.12 Gate G2 tests and flow script
Files: agent/tests/e2e/test_gate_g2.py, scripts/flow_demo.py, agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/orchestrator/resolver.py, agent/src/sellershield_agent/domain/models.py, agent/src/sellershield_agent/proposals/confirm.py, agent/src/sellershield_agent/proposals/confirm_store.py, agent/src/sellershield_agent/execution/saga.py, agent/src/sellershield_agent/api/proposals.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/db.py, docs/CHANGELOG.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Phase 2 Gate G2 executable proof (`agent/tests/e2e/test_gate_g2.py`)**:
  - Implemented 18 comprehensive end-to-end integration tests through the real HTTP application stack with scripted fake LLM and write-capable fixture store:
    - SC-04: Full approval flow end-to-end (chat proposes return, customer confirms, seller approves via portal, store return approved with 120,000 paise authorization, physical return advances through lifecycle, webhook releases refund to processing, client event emitted, SHA-256 audit chain verified).
    - SC-05: ALLOW path auto-approved and hold reserved, released upon physical return inspection.
    - SC-06, SC-07, SC-09: Non-proposing paths (missing item, out of window, policy inquiry).
    - SC-08: Partial return proposal with explicit quantity breakdown.
    - SC-10: Capped cumulative refund respecting autonomous seller limits.
    - SC-11, SC-16: Missing slot clarification (reason and order ID).
    - SC-14: Double confirmation idempotency and replay safety.
    - Named integrity tests: T05 (no mutation before confirm button), T06 (ineligible returns create no store records), T07 (partial return line item limits), T08 (duplicate return prevention), T09 (cumulative order cap enforcement), T10 (backend-computed amounts immune to model hallucinations), T11 (above-threshold requires seller approval), T12 (approval bound to exact amount and version), T14 (concurrent confirmation single-winner race), T19 (concurrent audit thread writes without chain corruption), T20 (audit failure blocks store mutation).
- **SC-04 Flow Demo CLI (`scripts/flow_demo.py`)**:
  - Developed end-to-end automated demonstration script running through every state transition of the SC-04 approval lifecycle with visible status output.
- **Agent Orchestrator & Proposal Pipeline Fixes**:
  - `understand/router.py`: Tuned regex word boundaries and intent precedence to accurately route return messages with order IDs to `Intent.RETURN_REQUEST`.
  - `orchestrator/pipeline.py` & `resolver.py`: Integrated `propose_return` tool execution for return workflows, extracted line items and quantities, handled missing slot inquiries, and suppressed duplicate preview cards.
  - `proposals/confirm.py`, `confirm_store.py`, `execution/saga.py`, `api/proposals.py`: Propagated `refund_id` from saga reserve steps, handled concurrency conflicts cleanly with 409, and maintained strict OpenAPI response schema compatibility.
  - `main.py` & `db.py`: Added `SafeOverrides` to eliminate FastAPI query parameter inspect bugs on lambda overrides, and serialized SQLite transaction locking under concurrent load.
Verification run (commands and result summary):
- `just agent-check`: Passed (ruff check clean, mypy src clean across 129 files, 639 suite tests passed, test-guard verified test suite integrity).
- `uv run pytest tests/e2e/test_gate_g2.py -q`: Passed (18/18 passed in 15.28s).
- `uv run python ../scripts/flow_demo.py`: Passed (exit 0, printed all 7 state transitions from chat proposal to SHA-256 audit chain verification).
Follow-ups:
- Gate G2 is fully verified and closed. Phase 3 tasks can proceed.

## 2026-11-11 T3.0 Shared stubs and setup
Files: agent/src/sellershield_agent/events/__init__.py, agent/src/sellershield_agent/events/emit.py, agent/src/sellershield_agent/events/router.py, agent/src/sellershield_agent/outbox/__init__.py, agent/src/sellershield_agent/outbox/enqueue.py, agent/src/sellershield_agent/outbox/router.py, agent/src/sellershield_agent/cases/__init__.py, agent/src/sellershield_agent/cases/api.py, agent/src/sellershield_agent/cases/router.py, agent/src/sellershield_agent/memory/__init__.py, agent/src/sellershield_agent/memory/router.py, agent/src/sellershield_agent/monitor/__init__.py, agent/src/sellershield_agent/monitor/router.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/main.py, agent/tests/test_phase3_shared.py, docs/notes/T3.0.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- **Client event emission (`sellershield_agent/events/emit.py`)**:
  - Implemented `emit_client_event(tx, customer_id, type, payload, now_iso)` returning cursor `event_id`.
  - Added strict per-type Pydantic schemas with `extra='forbid'` for all 7 event types: `approval.decided`, `case.message`, `case.status`, `notification.new`, `proposal.expired`, `monitor.notice`, and `refund.updated`.
  - Enforced customer safety by strictly rejecting extra fields or unknown event types.
- **Outbox notification queueing (`sellershield_agent/outbox/enqueue.py`)**:
  - Implemented `enqueue_notification(tx, customer_id, template, payload, dedupe_key, channel, now_iso)` returning row ID.
  - Implemented deduplication logic against `notifications_outbox.dedupe_key` returning existing `notification_id` and preventing duplicates.
- **Support case escalation (`sellershield_agent/cases/api.py`)**:
  - Defined `cases` and `case_events` tables matching Spec §10 columns and registered in `sellershield_agent.domain.state`.
  - Implemented `open_case(tx, customer_id, order_id, category, priority, source)` returning `case_id` (`CASE-...`).
  - Added deterministic SLA due date computation (2h for high priority, 24h for normal priority) and minimal brief stub generation.
  - Recorded initial `case_opened` event in `case_events`.
- **Decoupled router registration (`sellershield_agent/main.py`)**:
  - Registered empty router stubs for `events`, `outbox`, `cases`, `memory`, `monitor`, and `dev` one line each under `/v1`.
- **Documentation**:
  - Added `docs/notes/T3.0.md` detailing shared architecture and invariants.
  - Added DEFERRED TESTS list to `docs/TASKS.md`.
Verification run (commands and result summary):
- `just agent-lint`: Passed (All checks passed).
- `just agent-types`: Passed (129 source files checked with no issues).
- `just test-guard`: Passed (PASS: test-guard verified test suite integrity).
- `uv run pytest tests/test_phase3_shared.py`: Passed (3/3 tests passed).
Follow-ups:
- Human can proceed to freeze tests and start T3.1, T3.2, or T3.3 in parallel.

## 2026-11-11 T2.11 Money-path red-team suite
Files: scenarios/redteam/money.yaml, agent/tests/redteam/ledger.py, agent/tests/redteam/money_support.py, agent/tests/redteam/test_money_attacks.py, agent/tests/redteam/test_money_races.py, agent/tests/redteam/test_money_faults.py, scripts/redteam_money_live.py, agent/src/sellershield_agent/approvals/helpers.py, agent/src/sellershield_agent/orchestrator/cards.py, agent/src/sellershield_agent/store/http_client.py, agent/tests/e2e/test_gate_g2.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/BUGLOG.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Executable Attack Corpus (`scenarios/redteam/money.yaml`)**:
  - Authored 68 comprehensive write-path attacks covering all 8 adversarial personas across 9 attack categories:
    - Confirm abuse (15 entries: replay with same/new idempotency, random/flipped hashes, expired proposals, cross-customer IDOR, chat-typed "yes"/"confirm").
    - Tampering (8 entries: injected amount_paise, negative/zero item quantities, currency injection, model-proposed amounts).
    - Races (8 entries: 20 parallel confirms, concurrent seller approvals, customer cancel vs seller approve, TOCTOU store mutations, concurrent reservations).
    - Caps and splitting (8 entries: delivered quantity exhaustion, daily autonomous cap exhaustion, 31st-day window boundary, cumulative return cap).
    - Seller misuse (8 entries: staff approval > 200,000 paise, customer token on seller routes, empty/whitespace rejection reason, double approval).
    - Webhook abuse (5 entries: forged signature, stale/future timestamps, replayed event ID, out-of-order versioning, oversized payloads).
    - Fault injection (8 entries: store timeout after commit on create_return/reserve_refund, store 500 on release_refund, audit failure aborting step, simulated IST midnight clock jump).
    - Obedient model (4 entries: model attempting to bypass confirm, hallucinating amounts, or claiming seller approved).
    - Seller-side injection (4 entries: XSS/HTML script tags and prompt injection in customer reason text rendered in seller cards).
- **Independent Financial Ledger Oracle (`agent/tests/redteam/ledger.py`)**:
  - Independent oracle `LedgerOracle` directly inspects store fixture state and audit event hash chains.
  - Formulates and enforces 6 universal invariants:
    1. Total released and reserved refunds per order never exceed paid order total.
    2. At most one active return exists per order item.
    3. No refund release exceeds the store autonomy cap (50,000 paise) without prior seller authorization.
    4. Daily cumulative autonomous releases never exceed the daily IST cap (500,000 paise).
    5. Audit log hash chain integrity verifies continuously from genesis with zero broken links.
    6. Customer-facing text strictly excludes forbidden substrings (internal limits, risk scores, tokens, secrets, other customer data).
- **Comprehensive Red-Team Test Suites (`agent/tests/redteam/`)**:
  - `test_money_attacks.py` (15 tests): verifies confirm abuse, tampering, caps, seller misuse, webhooks, and seller-side injection.
  - `test_money_races.py` (5 tests): executes genuine multi-threaded races using `ThreadPoolExecutor` and Starlette `TestClient`.
  - `test_money_faults.py` (6 tests): injects store timeouts, network drops, and simulated crash points via `FaultPlan`.
- **Live Non-LLM Attack Replayer (`scripts/redteam_money_live.py`)**:
  - Replays non-LLM attacks against live HTTP store (`STORE_MODE=http`) and emits structured markdown reports to `.evidence/redteam_money_live_report.md`.
- **Sabotage Verification Drills**:
  - Sabotaged 3 write-path defenses (hash check, staleness check, daily autonomy cap); each sabotage immediately caused the corresponding test to fail, proving defense sensitivity.
- Verification run (commands and result summary):
  - `just agent-lint` -> All checks passed!
  - `just agent-types` -> Success: no issues found in 116 source files
  - `just test-guard` -> PASS: test-guard verified test suite integrity
  - `cd agent; uv run pytest tests/redteam -q` -> 129 passed in 58.77s
- Follow-ups:
  - Step A verified and ready for human `FREEZE`.

Files: agent/src/sellershield_agent/clock.py, agent/src/sellershield_agent/store/client.py, agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, agent/tests/test_store_http_writes.py, scripts/handshake_money.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/BUGLOG.md, docs/TASKS.md, docs/STATE.md
What changed:
- **HttpStoreClient Write Methods Implementation**:
  - Implemented all store write methods: `create_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, and `update_settings`.
  - Added `_execute_post` and `_execute_put` with zero automatic retries (preserving saga/reconciler ownership of write retries and idempotency).
  - Enforced `If-Match: "{version}"` header on `update_settings` and `Idempotency-Key` + `X-Request-ID` across all write requests.
- **Context-Based Token Isolation & Service Token Management**:
  - Customer steps (`create_return`) strictly forward the customer JWT from `RequestContext.token`.
  - Seller steps (`authorize_refund`, `update_settings`) strictly forward the seller JWT from `RequestContext.token`.
  - Service steps (`reserve_refund`, `release_refund`, `cancel_reservation`) strictly fetch and use client-credentials service tokens via `POST /auth/token` with `STORE_EXECUTOR_CLIENT_ID` and `STORE_EXECUTOR_CLIENT_SECRET`.
  - Cached service tokens with proactive refresh 60 seconds prior to expiration.
  - Token isolation verified: customer tokens are never forwarded on service calls; service tokens are never forwarded on seller calls.
- **Store Error Code Mapping**:
  - 409 `duplicate` -> `StoreDuplicate`
  - 409 `limit_exceeded` -> `StoreLimitExceeded`
  - 409 `invalid_state` / 412 -> `StoreInvalidState`
  - 409 `idempotency_mismatch` -> `StoreIdempotencyMismatch`
  - 404 -> `StoreNotFound`
  - 401 / 403 -> `StoreForbidden`
  - 5xx & network connection failures -> `StoreUnavailable`
  - `httpx.TimeoutException` -> `StoreTimeout`
- **Full-Flow Handshake Dev Tool (`scripts/handshake_money.py`)**:
  - Connects to running store and agent instances.
  - Authenticates customer C001, opens chat session, sends request for ORD-1042 defective return, parses SSE proposal card with `proposal_id` and `proposal_hash`.
  - Confirms proposal, creating store return (RET-...) and refund reservation (RFD-...), resulting in pending seller approval (APR-...).
  - Authenticates seller staff/owner (Priya), submits approval decision to agent API.
  - Advances return lifecycle in store admin (`pickup_scheduled` -> `in_transit_back` -> `admin receive` -> `admin inspect` with `inspected_ok`).
  - Awaits release and SimPay gateway refund completion, polling `GET /refunds/{refund_id}` until `completed` and exiting 0.
- Verification run (commands and result summary):
  - `cd agent; uv run ruff check .` -> All checks passed!
  - `cd agent; uv run mypy src` -> Success: no issues found in 116 source files
  - `uv run pytest tests/test_store_http_writes.py` -> 14 passed in 6.46s
  - `uv run pytest` -> 592 passed in 141.48s
  - `just agent-check` -> ruff, mypy, pytest, test-guard all passed
- Follow-ups:
  - Real store write path fully verified with test suite integrity maintained. Ready for live cross-service demonstrations.

## 2026-11-11 T2.8 Audit views and Decision Trace API
Files: agent/src/sellershield_agent/audit/models.py, agent/src/sellershield_agent/audit/sanitize.py, agent/src/sellershield_agent/audit/queries.py, agent/src/sellershield_agent/audit/trace.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/execution/db_ops.py, agent/src/sellershield_agent/execution/finalize_db.py, agent/src/sellershield_agent/execution/release_db.py, agent/src/sellershield_agent/execution/refund_webhook_db.py, agent/src/sellershield_agent/approvals/decide_db.py, agent/src/sellershield_agent/proposals/service.py, agent/src/sellershield_agent/proposals/confirm_audit.py, agent/src/sellershield_agent/proposals/confirm_store.py, agent/src/sellershield_agent/approvals/common_db.py, agent/src/sellershield_agent/api/chat.py, agent/src/sellershield_agent/tools/gateway.py, agent/src/sellershield_agent/understand/service.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/api/seller_audit.py, agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/main.py, agent/tests/audit_test_helpers.py, agent/tests/test_audit_mutators.py, agent/tests/test_audit_mutators_coverage.py, agent/tests/test_audit_views.py, agent/tests/test_audit_views_trace.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- **Centralized Mutator Enforcement & Atomicity**:
  - Defined EventType values: `policy_decision`, `proposal_created`, `proposal_confirmed`, `execution_step`, `approval_created`, `approval_decided`, `reconcile`, `webhook_received`, `notification_stub`.
  - Enforced single-writer architecture across all 39 database mutating operations: public mutators route strictly through `domain.state.transition()`, `AuditService.append()`, and execution writer helpers (`execution/db_ops.py`).
  - Implemented AST scanner `_check_single_writer_ast()` verifying no `.insert()` or `.update()` calls occur on `executions`, `proposals`, `approvals`, or `audit_events` outside permitted writer modules.
  - Added telemetry tables `llm_calls` and `tool_calls` to metadata and populated rows across the pipeline.
  - Added `transition()` dispatcher supporting optimistic locking (`expected_version`) and atomic audit event generation within `write_tx`.
- **Seller Audit Views & Decision Trace API**:
  - `GET /v1/seller/audit`: seller-only role requirement (`seller_staff`, `seller_owner`), filters by `request_id`, `resource_id`, `event_type`, `from`, `to`, cursor pagination, and boundary validation (`limit` 1..200).
  - `GET /v1/seller/audit/verify`: verifies sequential SHA-256 hash chain and returns `ChainStatus` / `AuditVerifyResponse` (`valid`, `total_events`, `first_hash`, `last_hash`, `broken_at_seq`).
  - `GET /v1/seller/traces/{request_id}`: builds full decision trace assembling `llm_calls`, `tool_calls`, `policy_decisions`, and `audit_events` mapped into canonical 10-stage timeline (`understanding`, `plan`, `tool_call`, `rule_table`, `proposal`, `confirmation`, `execution_step`, `approval`, `final_reply`, `verifier_outcome`).
  - Implemented data hygiene & security scrubbing: redacted auth tokens, API keys, and PII; customer free text HTML-escaped at presentation time; cross-customer IDOR protection filtering unrelated objects upon auth denials.
- **Verification Run (commands and result summary)**:
  - `cd agent; uv run ruff check .` -> All checks passed!
  - `cd agent; uv run mypy src` -> Success: no issues found in 116 source files
  - `uv run --directory agent pytest tests/test_audit.py tests/test_audit_mutators.py tests/test_audit_mutators_coverage.py tests/test_audit_views.py tests/test_audit_views_trace.py` -> 21 passed in 2.69s
  - `just agent-check` -> ruff clean, mypy clean, 578/578 pytest passed in 66.09s, test-guard verified test suite integrity PASS.
- Follow-ups:
  - Phase 2 write path complete. All mutators audited atomically and explainability trace API fully operational.

## 2026-11-11 INT.1 Cross-service live integration and store compatibility
Files: store-web/package.json, store-web/src/components/ChatWidget.tsx, store-web/src/hooks/useChatStream.ts, store-api/app/config.py, store-api/app/auth.py, store-api/app/deps.py, store-api/app/models.py, store-api/db.py, store-api/app/schemas.py, store-api/app/admin.py, store-api/app/returns.py, store-api/app/outbox.py, store-api/app/refunds.py, store-api/app/simpay.py, store-api/app/main.py, store-api/.env.example, store-api/.env, agent/src/sellershield_agent/store/normalizers.py, agent/src/sellershield_agent/store/http_client.py, agent/src/sellershield_agent/store/__init__.py, agent/tests/test_store_http_compat.py, scratch/live_integration_runner.py, docs/DECISIONS.md, docs/BUGLOG.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/TASKS.md, docs/STATE.md
What changed:
- `store-web`:
  - Resolved Windows WDAC native binary execution block on Rollup by adding npm override `"rollup": "npm:@rollup/wasm-node@4.68.1"` (`ADR-033`).
  - Removed unused symbol `handleQuickAction` in `ChatWidget.tsx` and unused `useEffect` import in `useChatStream.ts`.
  - Verified 16/16 Vitest tests passing, ESLint passing with 0 warnings, and Vite production build in 2.4s.
- `store-api`:
  - Created `.env` and `.env.example` safely with non-secret dev defaults (`JWT_ISSUER="nestkart-store"`, dev service client secrets, commented placeholders for webhook secrets).
  - Added `kid: "nestkart-1"` and `iat` claims to minted access and service JWT tokens; mounted `GET /.well-known/jwks.json` alongside `GET /api/v1/.well-known/jwks.json`.
  - Added `require_owner_or_monitor` dependency for `GET /api/v1/admin/settings` read access.
  - Added `Return.client_ref` column and SQLite DDL schema migration (`db._ensure_schema_columns`); added `AdminOrderDetailResponse` returning nested `returns` and `refunds`.
  - Updated saga compensation on `transition_return` so executor service role can transition `requested -> cancelled` without autonomy cap blocks.
  - Formatted wire outbox webhook envelopes in `build_webhook_wire_body()` with timezone-aware `occurred_at` and nested `data` dict, signed with HMAC-SHA256.
  - Added routing aliases for `/api/v1/healthz` and `/api/v1/readyz`.
  - Verified all 267 pytest tests passing and database seed script passing 17/17 checks.
- `agent`:
  - Created `sellershield_agent/store/normalizers.py` to bridge naive SQLite UTC timestamp strings to timezone-aware UTC (`ensure_aware_utc`), normalize order/shipment/refundable/refund dictionaries, and map `reserved` status with authorization fields to `authorized`.
  - Updated `sellershield_agent/store/http_client.py` and factory in `store/__init__.py` to inject `STORE_EXECUTOR_CLIENT_ID` / `STORE_EXECUTOR_CLIENT_SECRET`, implement role-based token routing (`monitor` for reads, `executor` for writes, customer/seller JWT when present in context), and support `items` list payloads.
  - Added 3 integration compatibility tests in `agent/tests/test_store_http_compat.py`.
- Live End-to-End Cross-Service Verification:
  - Booted `store-api` (:8000), `agent` (:8001 with `STORE_MODE=http`), and `store-web` (:5173) simultaneously.
  - Verified `healthz`, `readyz` (`status: "ok"`, `store_mode: "http"`), root JWKS, and Vite dev reverse proxy routes (`/api` and `/agent`).
  - Successfully executed `scripts/handshake.py` live against `store-api` (logged in as customer C001, queried orders, fetched `ORD-1048` with status `shipped`).
  - Executed Playwright browser inspection on `store-web` across Desktop (1440x900), Tablet (768x1024), and Mobile (390x844).
Verification run (commands and result summary):
- `npm --prefix store-web run lint` -> 0 errors, 0 warnings
- `npm --prefix store-web run build` -> built in 2.45s
- `npm --prefix store-web test` -> 16 passed in 1.81s
- `uv --directory store-api run python ../scripts/verify_seed.py` -> 17 passed, 0 failed
- `uv --directory store-api run python -m pytest -q` -> 267 passed in 8.35s
- `uv --directory agent run pytest tests/test_store_http_compat.py -v` -> 3 passed in 0.35s
- `uv --directory agent run ruff check .` -> All checks passed!
- `uv --directory agent run mypy src` -> Success: no issues found in 113 source files
- `uv --directory agent run python ../scripts/test_guard.py check` -> PASS: test-guard verified test suite integrity (566 frozen tests intact)
- `live_integration_runner.py` -> ALL LIVE INTEGRATION & BROWSER CHECKS PASSED SUCCESSFULLY (handshake exit 0, status `shipped`, 3 screenshots saved)
Follow-ups:
- T2.8 is currently running (Step A test files drafted in `agent/tests/test_audit_*.py`).
- Human will run T2.9 where audit views and decision trace endpoints will be solved.

## 2026-11-11 T2.7 Store webhooks and refund release
Files: agent/src/sellershield_agent/api/webhooks.py, agent/src/sellershield_agent/execution/webhook_db.py, agent/src/sellershield_agent/execution/release_db.py, agent/src/sellershield_agent/execution/refund_webhook_db.py, agent/src/sellershield_agent/execution/release_guards.py, agent/src/sellershield_agent/execution/release.py, agent/src/sellershield_agent/execution/reconcile_db.py, agent/src/sellershield_agent/execution/reconcile.py, agent/src/sellershield_agent/execution/reconcile_steps.py, agent/src/sellershield_agent/audit/models.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/main.py, agent/tests/test_webhooks_release.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Signed Store Webhook Ingestion (`agent/src/sellershield_agent/api/webhooks.py`, `execution/webhook_db.py`, `domain/state.py`, `audit/models.py`, `main.py`):
  - Mounted `POST /v1/webhooks/store` (and `/webhooks/store`) verifying single non-duplicated headers via `request.headers.raw` (`X-Event-Id`, `X-Store-Timestamp`, `X-Store-Signature`), enforcing 1 MB body cap (`413 limit_exceeded`), fail-closed `WEBHOOK_SECRET` (even when `DEV_MODE=true`), strict timestamp & 64-hex signature format checks, constant-time `hmac.compare_digest` over `f"{timestamp}." + raw_body`, and `-300s .. +60s` replay window against `Clock.now()`, returning uniform `401 unauthenticated` (`"Authentication failed"`, `details={}`) plus redacted `AUTH_DENIED` audit on failure.
  - Validated strict `WebhookEnvelope` (`extra="forbid"`, `StrictInt resource_version >= 0`, tz-aware `occurred_at`) and `X-Event-Id == envelope.event_id` (`400`), recorded seen event IDs (`webhook_events_seen`) and per-resource monotonic versions (`(resource_type, resource_id)` in `webhook_resource_versions`) inside a single short `write_tx`, audited `WEBHOOK_RECEIVED` (`webhook_received`, `webhook_duplicate_ignored`, `webhook_stale_version`), and resolved `StoreClient` lazily only for `return.updated=inspected_ok`.
- Pre-Release Safety Gates & Refund Release (`execution/release_guards.py`, `execution/release.py`, `execution/release_db.py`):
  - Reacts to `return.updated` (`status == "inspected_ok"`) by loading proposal/approval/execution context in `read_tx`, fetching live Store state outside DB locks, and enforcing deterministic `verify_release_safety` gates: proposal type `return`, non-cancelled eligible order, `ReturnStatus.inspected_ok`, `RefundStatus` in `reserved`/`authorized` (blocking `processing`/`completed`), `refund.amount_paise == proposal.amount_paise <= adjusted_refundable.max_refundable_paise`, `NEEDS_APPROVAL` path requiring `ApprovalStatus.approved` + `store_authorization_ref` + `RefundStatus.authorized` + exact match against `authorized_amount_paise` and `approvals.amount_paise`, `ALLOW` path enforcing `<= store_settings.autonomy_cap_paise`, and live `decide()` evaluation under both active and proposal policy versions. Blocked attempts skip the store and audit `EXECUTION_STEP` (`action="release_refund_blocked"`, `outcome="denied"`).
  - Calls `store_client.release_refund(svc_ctx, refund_id, f"{proposal_id}:release_refund")` with `ServiceContext` (`Role.service`, `token=None`) using two-phase commits (`executions` + `approvals.refund_status` sync), mapping `StoreTimeout`/`StoreUnavailable` cleanly to `"unknown"`.
- Refund Lifecycle Webhook & Reconciler Support (`execution/refund_webhook_db.py`, `execution/reconcile_db.py`, `execution/reconcile.py`, `execution/reconcile_steps.py`, `db.py`):
  - Added nullable `refund_status` on `approvals` (with `_ensure_approval_columns` SQLite migration) and `ProposalStatus.succeeded -> ProposalStatus.failed` transition.
  - `apply_refund_updated_sync()` processes `refund.updated` (`processing`, `completed`, `failed`), ignores regressions after `failed`/`completed`, requires `ApprovalStatus.approved` + `release_refund` row for `processing`/`completed`, transitions proposals (`executing -> succeeded` on `completed`; `executing|unknown|succeeded -> failed` on `failed`), writes deduplicated neutral `client_events` (`type="refund.updated"`, scrubbing raw `failure_reason`), audits `EXECUTION_STEP`, and never creates a replacement refund.
  - Extended `reconcile_db.py`, `reconcile.py`, and `reconcile_steps.py` for `release_refund` (`prepare_release_reconcile` resolves store-committed `processing`/`completed` on `timeout_after_commit` without a second `release_refund` call, retries `fail_once` with the identical `{proposal_id}:release_refund` key, and finalizes terminal release failures cleanly without compensating).
- Tests (`agent/tests/test_webhooks_release.py`):
  - Added 16 unfrozen tests covering forged/wrong-secret/missing/smuggled/empty-secret signatures (`401`), stale/future/whitespace/boundary timestamps (`401`/`200`), 10x duplicate `X-Event-Id` & header mismatch (`400`), replayed `X-Event-Id` with fresh timestamp, out-of-order lower `resource_version` per namespace, audit-only events, `inspected_ok` `ALLOW` release once, `NEEDS_APPROVAL` blocked & audited before approval (even <= autonomy cap), exact authorized release after approval, safety-gate blocks (tampered amount, un-inspected store status, tightened `v4` policy), `timeout_after_commit` & `fail_once` reconciler recovery, `refund.updated` (`processing`/`completed`/`failed`) proposal/client-event/no-new-refund checks, 200-event burst (`< 5s`), and `> 1 MB` rejection (`413`).
Verification run (commands and result summary):
- `just agent-lint` (`cd agent; uv run ruff check .`) -> `All checks passed!`
- `just agent-types` (`cd agent; uv run mypy src`) -> `Success: no issues found in 112 source files`
- `cd agent; uv run pytest -v tests/test_webhooks_release.py` (`BypassSandbox: true`) -> `16 passed in 4.27s`
- `just agent-check` (`BypassSandbox: true`): `ruff check .` (`All checks passed!`), `mypy src` (`Success: no issues found in 112 source files`), `pytest -q` (`566 passed in 63.62s`), `test_guard.py check` (`UNFROZEN: agent/tests/test_webhooks_release.py` / `PASS: test-guard verified test suite integrity.`)
- Sabotage drills (reverted cleanly after each): (1) skip HMAC signature verification -> `1 failed, 15 passed` (`test_webhook_forged_signature_returns_401_generic`); (2) skip `ReturnStatus.inspected_ok` check -> `1 failed, 15 passed` (`test_webhook_release_safety_gates_block_tampered_amount_status_and_policy`); (3) bypass amount/authorized ceiling checks -> `1 failed, 15 passed` (`test_webhook_release_safety_gates_block_tampered_amount_status_and_policy`).
Follow-ups:
- Human freeze (`just freeze-tests`) for `agent/tests/test_webhooks_release.py` (16 tests -> 566 locked total).
- Proceed to T2.8 (`Gate G2 tests and mutation audits`).
- Carrying forward M3 reminder: attach service tokens in `store/http_client.py` for `transition_return`, `release_refund`, and `cancel_reservation` on `ServiceContext`.

## 2026-11-11 T2.6 Approvals and seller endpoints
Files: contracts/openapi-agent.yaml, agent/src/sellershield_agent/auth/models.py, agent/src/sellershield_agent/auth/verify.py, agent/src/sellershield_agent/audit/models.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/execution/finalize_db.py, agent/src/sellershield_agent/execution/reconcile.py, agent/src/sellershield_agent/approvals/__init__.py, agent/src/sellershield_agent/approvals/models.py, agent/src/sellershield_agent/approvals/helpers.py, agent/src/sellershield_agent/approvals/revalidation.py, agent/src/sellershield_agent/approvals/card_db.py, agent/src/sellershield_agent/approvals/card.py, agent/src/sellershield_agent/approvals/common_db.py, agent/src/sellershield_agent/approvals/decide_db.py, agent/src/sellershield_agent/approvals/withdraw_db.py, agent/src/sellershield_agent/approvals/common.py, agent/src/sellershield_agent/approvals/reject.py, agent/src/sellershield_agent/approvals/approve.py, agent/src/sellershield_agent/approvals/decide.py, agent/src/sellershield_agent/approvals/withdraw.py, agent/src/sellershield_agent/approvals/expire.py, agent/src/sellershield_agent/api/seller_approvals.py, agent/src/sellershield_agent/api/customer_approvals.py, agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/main.py, agent/tests/test_seller_approvals.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Seller Approval HTTP Endpoints (`agent/src/sellershield_agent/api/seller_approvals.py`, `api/seller_stub.py`, `main.py`):
  - Replaced the three `/seller/approvals*` 501 stubs with `GET /v1/seller/approvals` (status filtering + offset/limit pagination scoped strictly by authenticated `store_id`), `GET /v1/seller/approvals/{id}` (deterministic card + audited uniform `404`), and `POST /v1/seller/approvals/{id}/decision` (mounted under `/v1` and root `""`).
  - Enforced store-scoped uniform `404` on unknown or cross-store approvals, required ASCII `Idempotency-Key` partitioned by seller `(store_id, user_id)`, strict `ApprovalDecisionRequest` (`extra="forbid"`, `StrictInt expected_version >= 1`, non-empty `reason` required for `reject`), role limit (`seller_staff <= 200000` paise, `seller_owner` above), and 15-minute `auth_time` step-up (`401 step_up_required` + `AUTH_DENIED` audit).
- Deterministic Card Builder & Revalidation (`agent/src/sellershield_agent/approvals/card.py`, `card_db.py`, `helpers.py`, `revalidation.py`, `models.py`):
  - Built deterministic zero-PII `SellerApproval` cards combining sanitized `order` & items (via `sanitize_order_for_card`), recomputed pre-hold `Refundable` excluding the current hold (`build_adjusted_refundable`), rule results with `seller_text`, risk score/band/signals, HTML-escaped/link-defanged/200-char truncated `"Unverified customer statement"`, evidence IDs, strictly rule-based `recommendation` (`"approve"` only when `R-09` alone fails on a low-risk eligible return; `"review"` otherwise, never reading `reason_text`), `similar_past_decisions`, `due_at`/`expires_at`, and `revalidation_diff`.
  - Stripped admin-populated nested `order.returns`/`order.refunds` during normalized comparison (`build_decision_facts_from_bundle`, `_normalize_facts_pair_for_diff`) while still comparing live `active_returns`/`adjusted_refundable`, eliminating false drift vs customer `get_order()`.
- Live Revalidation, Store-Verified Decision Execution & No-Auto-Approve Expiry (`approvals/decide.py`, `approve.py`, `reject.py`, `common.py`, `common_db.py`, `decide_db.py`, `expire.py`, `execution/reconcile.py`):
  - `decide_approval` fetches live facts outside DB locks via `ServiceContext`, evaluates `decide()` under both approval policy version and active policy version, transitions `pending -> needs_revalidation` with sanitized `diff` + `409` on fact/policy drift, atomically claims `pending -> approving`, and calls `store_client.authorize_refund` (exact reserved amount) and `transition_return(..., "approved", ...)` forwarding ONLY the verified seller's `RequestContext` (`exec_seller_ctx`), rolling back `approving -> pending` with `last_error` & partial `store_authorization_ref` on Store failure and emitting `approval.decided` + `APPROVAL_DECIDED` audit on completion.
  - Reject requires a non-empty reason, forwards the seller token to `transition_return("rejected")` and `cancel_reservation`, emits a neutral `approval.decided` client event (never exposing internal seller notes), and updates parent proposal status.
  - Added `expire_stale_approvals` & `expire_single_approval` wired into `reconcile_once`; expired proposals mark `expired`, release holds via `ServiceContext`, emit `proposal.expired`, and NEVER auto-approve on timeout.
- Approved Customer Withdraw (`CHANGE-003` in `contracts/openapi-agent.yaml`, `api/customer_approvals.py`, `approvals/withdraw.py`, `withdraw_db.py`):
  - Added `POST /v1/me/approvals/{proposal_id}/withdraw` allowing the owning customer to withdraw `pending`/`needs_revalidation` approvals with `Idempotency-Key` replay safety, uniform `404`, hold release via `ServiceContext`, and `APPROVAL_DECIDED` audit.
- Tests & Auth/DB Support (`auth/models.py`, `auth/verify.py`, `audit/models.py`, `domain/state.py`, `db.py`, `execution/finalize_db.py`, `agent/tests/test_seller_approvals.py`):
  - Added `auth_time` and `store_id` claim parsing on `Principal`, `APPROVAL_DECIDED` `EventType`, `approvals` schema columns (`store_id`, `revalidation_diff_json`, `last_error`, `created_at`) with `_ensure_approval_columns` SQLite migration, and `approving`/`needs_revalidation`/`withdrawn` transitions.
  - Added 22 tests in `agent/tests/test_seller_approvals.py` covering SC-04 approval, staff/owner limit & fresh `auth_time` step-up, customer/service `403`, reject hold cancellation, duplicate/expired/version `409`s, tampered body & exact reserved amount, fact/policy `needs_revalidation` diffs, store-failure rollback, seller-token spy verification, escaped card statement & reason immunity, uniform `404` across stores, list/filter pagination, expiry no-auto-approve, customer withdraw, refund-without-return, and audit hash chain/redaction.
Verification run (commands and result summary):
- `Set-Location agent; uv run ruff check .` -> `All checks passed!`
- `Set-Location agent; uv run mypy src` -> `Success: no issues found in 106 source files`
- `Set-Location agent; uv run python ../scripts/contract_check.py ../contracts/openapi-agent.yaml` -> `Total operations: 49 / ALL CHECKS PASSED.`
- `Set-Location agent; uv run pytest -v tests/test_seller_approvals.py` (`BypassSandbox: true`) -> `22 passed in 4.15s`
- `just agent-check`: `ruff` (`All checks passed!`), `mypy` (`Success: no issues found in 106 source files`), `uv run pytest -q` -> `1 failed, 549 passed in 60.16s` (only frozen Phase 1 `tests/e2e/test_gate_g1.py::test_gate_g1_t04_seller_route_auth_boundaries` expecting `501` stub on `GET /v1/seller/approvals` instead of implemented `200`).
- `Set-Location agent; uv run python ../scripts/test_guard.py check` -> `FAIL: test-guard detected integrity violations: - Frozen file modified: contracts/openapi-agent.yaml (hash/size mismatch)` (expected awaiting human `just freeze-tests` for approved `CHANGE-003` + unfrozen tests).
- Sabotage drills (reverted cleanly after each): (1) service token passed to `authorize_refund` -> `11 failed, 11 passed` (`test_seller_token_spy_on_authorize_and_return_transitions` + 10 approval tests failed); (2) staff limit check disabled -> `1 failed, 21 passed` (`test_staff_limit_blocks_above_200000_and_audits`); (3) reject skips `cancel_reservation` -> `2 failed, 20 passed` (`test_reject_requires_reason_transitions_return_and_cancels_hold`, `test_seller_token_spy_on_authorize_and_return_transitions`); (4) auto-approve (`authorize_refund`) on expiry -> `2 failed, 20 passed` (`test_expiry_marks_expired_releases_hold_and_never_auto_approves`, `test_approve_rejected_or_expired_returns_409`).
Follow-ups:
- Human review/freeze (`just freeze-tests`) for approved `CHANGE-003` (`contracts/openapi-agent.yaml`), `agent/tests/test_execution_saga.py` (12), and `agent/tests/test_seller_approvals.py` (22), plus handling `tests/e2e/test_gate_g1.py#L382` Phase 1 stub expectation in T2.7 Gate G2 tests.

## 2026-11-11 T2.5 Executor saga and reconciler
Files: agent/src/sellershield_agent/execution/__init__.py, agent/src/sellershield_agent/execution/common.py, agent/src/sellershield_agent/execution/db_ops.py, agent/src/sellershield_agent/execution/finalize_db.py, agent/src/sellershield_agent/execution/steps.py, agent/src/sellershield_agent/execution/saga.py, agent/src/sellershield_agent/execution/reconcile_db.py, agent/src/sellershield_agent/execution/reconcile_steps.py, agent/src/sellershield_agent/execution/reconcile.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/audit/models.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/proposals/confirm.py, agent/src/sellershield_agent/api/proposals.py, agent/src/sellershield_agent/main.py, agent/tests/test_execution_saga.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Two-Phase Saga Execution Engine (`agent/src/sellershield_agent/execution/saga.py`, `steps.py`, `db_ops.py`, `finalize_db.py`, `common.py`):
  - Implemented `Executor` (`execute()` for `confirm_proposal` handler and `run(ctx, proposal)` for direct execution) supporting both `ALLOW` (`create_return` with customer token inside request -> `transition_return` to `approved` with service token -> `reserve_refund` with service token) and `NEEDS_APPROVAL` (`create_return` for returns -> `reserve_refund` -> atomic `approvals` row with policy `due_at`/`expires_at` and role routing, `client_events` `notification.new`, and idempotent `notifications_outbox` `seller.approval_needed` stub).
  - Enforced strict two-phase commits per step: Phase (a) `write_tx` inserts `executions` (`status="executing"`, `idempotency_key=f"{proposal_id}:{step}"`) + `execution_step_started` audit; Phase (b) commits before exiting transaction; Phase (c) calls `StoreClient` with NO open DB transaction; Phase (d) `write_tx` updates `succeeded` / `failed` / `unknown` + `store_ref` + audit event.
  - Mapped transient `StoreTimeout` / `StoreUnavailable` / `TimeoutError` exclusively to `"unknown"` (never `"failed"`), wired `format_execution_status_message()` to emit truthful customer-safe text without leaking internal limits or unconfirmed success, and converted normalized `PreparedProposal.items` safely via `to_return_items()`.
- Reverse-Order Compensation (`agent/src/sellershield_agent/execution/steps.py`):
  - Stops on first terminal step failure, handles `StoreDuplicate` lookup (`proposal.failed_duplicate` with existing `return_id`), and compensates strictly in reverse order (`cancel_reservation` -> `cancel_return`) with `ServiceContext` (`role=Role.service`, `token=None`).
  - Records compensation failures (`failed` / `unknown`) and enqueues `seller.compensation_failed` outbox stubs for reconciler intervention.
- Idempotent Reconciler & Lifespan Loop (`agent/src/sellershield_agent/execution/reconcile.py`, `reconcile_db.py`, `reconcile_steps.py`, `main.py`):
  - Implemented `reconcile_once(clock, ...)` scanning `executing` / `unknown` execution steps with retry schedule `5s -> 30s -> 2m -> stuck=True` (`seller.execution_stuck` outbox stub) plus crashed/orphaned `executing`/`unknown` proposals (excluding pending approvals).
  - Verifies store state via `ServiceContext` (`get_order_admin` matching `client_ref` to `idempotency_key`), resolves steps, and resumes forward saga or runs compensation cleanly using service tokens only.
  - Wired `reconcile_once(..., min_age_seconds=0)` on app startup before serving and a cancellable background `asyncio` task polling every `RECONCILE_INTERVAL_SECONDS` (`5.0s`); replaced `PendingExecutor` with `SagaExecutor` in `confirm.py` and `api/proposals.py`.
- Schema & Test Suite (`db.py`, `audit/models.py`, `domain/state.py`, `agent/tests/test_execution_saga.py`):
  - Added `executions` reconciler columns (`attempts`, `next_retry_at`, `stuck`) + SQLite schema migration helper `_ensure_execution_columns`, `notifications_outbox` table, and `EXECUTION_STEP` / `APPROVAL_CREATED` / `RECONCILE` audit enums + `AuditEvent.details_json` helper.
  - Added 12 comprehensive tests in `agent/tests/test_execution_saga.py` covering `ALLOW`/`NEEDS_APPROVAL` paths (including `propose_refund` and owner routing with custom `resolution`/`refund_method`), reverse compensation & failure handling, `timeout_after_commit` & `unavailable` backoff/stuck reconciliation, startup & crash repair (both mid-step and between-steps), background lifespan loop cancellation, double-confirm idempotency, audit rollback leaves `executing` for reconciler, duplicate return & daily-cap compensation with neutral text, zero open-transaction spy assertions on every store call, and customer JWT isolation/redaction across DB & logs.
Verification run (commands and result summary):
- `Set-Location agent; uv run ruff check .` -> `All checks passed!`
- `Set-Location agent; uv run mypy src` -> `Success: no issues found in 89 source files`
- `Set-Location agent; uv run pytest -v tests/test_execution_saga.py` (`BypassSandbox: true` per KP-22) -> `12 passed in 1.63s`
- `Set-Location agent; uv run pytest -q` (`BypassSandbox: true` per KP-22) -> `528 passed in 55.62s` (`516` frozen + `12` new)
- `just test-guard` -> `PASS: test-guard verified test suite integrity.` (`UNFROZEN: agent/tests/test_execution_saga.py`)
- Sabotage drills (reverted after verification): (1) new idempotency key on retry -> `5 failed, 7 passed`; (2) `StoreTimeout` classified `"failed"` -> `2 failed, 10 passed`; (3) store call inside `write_tx` -> `7 failed, 5 passed` via `_TransactionSpyStoreClient.tx_violations`; (4) skipped compensation -> `4 failed, 8 passed`.
Follow-ups:
- Freeze `agent/tests/test_execution_saga.py` when approved by human (`just freeze-tests`).
- Before wiring real M3 HTTP store calls, update `store/http_client.py` (`transition_return`, `release_refund`, `cancel_reservation`) to send executor service bearer tokens on `ServiceContext` rather than empty customer headers.
- Proceed to T2.6 (`Seller approval portal and step-up auth`).

## 2026-11-11 T2.4 Confirm and cancel handlers
Files: agent/src/sellershield_agent/proposals/executor.py, agent/src/sellershield_agent/proposals/confirm_audit.py, agent/src/sellershield_agent/proposals/confirm_store.py, agent/src/sellershield_agent/proposals/confirm.py, agent/src/sellershield_agent/proposals/integrity.py, agent/src/sellershield_agent/proposals/revalidate.py, agent/src/sellershield_agent/proposals/service.py, agent/src/sellershield_agent/api/proposals.py, agent/tests/test_confirm_handler.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Executor Protocol & Dependency Injection (`proposals/executor.py`, `api/proposals.py`):
  - Defined frozen `ExecutionResult` (`executing` | `succeeded` | `pending_approval` | `unknown` | `failed` with optional `return_id`, `approval_id`, `message_key`), `@runtime_checkable class Executor(Protocol)`, and default pre-T2.5 `PendingExecutor`.
  - Exposed module-level `get_proposal_executor() -> Executor` FastAPI dependency and injected `executor` into `POST /v1/proposals/{id}/confirm`.
- Explicit Hash Binding, State-Claim Concurrency & Expiry (`proposals/integrity.py`, `proposals/confirm_audit.py`, `proposals/confirm_store.py`, `proposals/confirm.py`):
  - Added public `confirm_proposal(ctx, proposal_id, proposal_hash, ...)` entry point enforcing `Role.customer` + non-empty `customer_id` (`403` on seller/service tokens), ownership-first uniform `404` + `AUTH_DENIED` audit, and constant-time UTF-8 `hmac.compare_digest` + regex check (`409 invalid_state`, `reason="hash_mismatch"`).
  - Transitioned expired proposals `proposed -> expired` inside `write_tx` with `proposal_confirmed`/`proposal_cancelled (outcome=DENIED, reason="expired")` and `409 invalid_state` (`reason="expired"` per Option A / frozen `contracts/openapi-agent.yaml`).
  - Split confirmation database choreography cleanly across `confirm_audit.py` and `confirm_store.py` (keeping all modules under 400 lines): `prepare_action_sync` (ownership, idempotency, integrity, overlap), `claim_confirm_sync` (single `write_tx` atomic `proposed -> confirmed -> executing` with `expected_version`, `proposal_confirmed (outcome=SUCCESS)` audit, and initial `http_idempotency_keys` record), and `finalize_confirm_sync` (`executing -> succeeded|failed|unknown` when terminal/unknown and final `http_idempotency_keys.response_json` update). Unhandled executor exceptions safely settle as `unknown` without losing confirmation claim state.
  - Enforced active overlap guard across `confirmed`, `executing`, and `unknown` proposal states, and restricted `POST /v1/proposals/{id}/cancel` strictly to `proposed -> cancelled` (`409 invalid_state` after confirmation).
- Store & Policy Staleness Revalidation with Replacement Preview (`proposals/revalidate.py`, `proposals/service.py`):
  - Loaded persisted `policy_decisions.facts_json` snapshot (`load_proposal_decision_snapshot`), normalized non-temporal facts via `normalize_non_temporal_facts` (`sanitize_policy_facts` + stripping volatile datetime/reason keys), and compared against live `decide()` facts under the active `load_policy()` outside DB write locks (`KP-01`).
  - Treated any non-temporal facts drift, policy version/stricter decision drift (`ALLOW -> NEEDS_APPROVAL`, anything -> `DENY`/`ESCALATE`/`NEEDS_INFO`), live amount drift, store `409`/`422` conflict, or `propose_refund` auto-limit normalization as `stale_state`: kept the prior row unconfirmed (`proposed`), wrote `proposal_confirmed (outcome=FAILED, reason="stale_state")` and (when eligible) a replacement proposal (excluding the stale `proposal_id` from dedup/open cap via `exclude_proposal_ids` + `expected_*` args) in one `write_tx` with zero store write calls, and returned `409 invalid_state` with `details={"reason": "stale_state", "fresh_preview": ...}` (or sanitized `proposal=None` preview).
- Confirmation Handler Tests (`agent/tests/test_confirm_handler.py`):
  - Added 11 tests covering valid confirm across all 5 `ExecutionResult` states with executor order/once assertions, wrong/copied hash `409 hash_mismatch`, expiry `409` + DB `expired`, second confirm `409 already_confirmed`, uniform `404` IDOR, `403` on seller token, 20-parallel-confirm race (SC-14) with 1 winner + 19 `409`s + 1 execution, store-mutation & policy-v1-to-v3 `stale_state` with fresh preview and zero store writes, cancel once & post-confirm cancel `409`, chat `"yes"`/`"confirm"`/`"I agree"`/`"ok"` non-confirmation invariant, `proposal_confirmed` audit coverage across `success`/`denied`/`failed(stale_state)`, and customer-safe error/preview wording (no `limit`/`risk`/`threshold` terms).
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_confirm_handler.py -v` -> `11 passed`
- `just agent-check` (`ruff check .`, `mypy src`, `pytest -q`, `test-guard`) -> `516 passed`, `80 source files` clean, `PASS: test-guard verified test suite integrity` (`505` frozen across `43` files, `11` unfrozen in `agent/tests/test_confirm_handler.py`).
- Sabotage drill: removing client hash check failed 2 tests; skipping staleness check failed 3 tests; moving status transition after executor call failed 2 tests (including 20-parallel confirm race). All 3 reverted cleanly.
Follow-ups:
- Freeze `agent/tests/test_confirm_handler.py` when approved by human (`just freeze-tests`).
- Implement T2.5 (`Execution saga and two-phase write`) to replace `PendingExecutor` with the real idempotent `create_return` / `reserve_refund` / `authorize_refund` / `release_refund` saga.

## 2026-11-11 T2.3b Approval cards and customer confirmation flow
Files: agent/src/sellershield_agent/api/proposals.py, agent/src/sellershield_agent/proposals/confirm.py, agent/src/sellershield_agent/proposals/integrity.py, agent/src/sellershield_agent/proposals/revalidate.py, agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/domain/session.py, agent/src/sellershield_agent/orchestrator/cards.py, agent/src/sellershield_agent/compose/factsheet.py, agent/src/sellershield_agent/compose/templates.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/main.py, agent/tests/test_proposals_api.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Proposal Confirmation & Cancel HTTP Endpoints (`agent/src/sellershield_agent/api/proposals.py`, `main.py`):
  - Implemented `POST /v1/proposals/{id}/confirm` and `POST /v1/proposals/{id}/cancel` (mounted under `/v1` and root `""`) using strict `ConfirmProposalRequest` and `CancelProposalRequest` (`extra="forbid"`).
  - Enforced dependency order: customer JWT role + non-empty `customer_id` (`401`/`403` + `AUTH_DENIED` audit) -> single ASCII `Idempotency-Key` (`^[!-~]{1,128}$`) -> manual body parsing so unauthenticated/invalid-header requests never leak `422` on body errors first; `/cancel` accepts empty `b""` identically to `{}`.
- Proposal Integrity, Idempotency & Overlap Prevention (`agent/src/sellershield_agent/domain/state.py`, `proposals/integrity.py`, `proposals/confirm.py`):
  - Added `http_idempotency_keys` table keyed uniquely by `(customer_id, idempotency_key)` with canonical `compute_request_hash()` comparison returning cached 200 responses on exact replay or `409 idempotency_mismatch` on mismatched hash/operation/proposal.
  - Enforced ownership-first query (`proposal_id, customer_id`) *before* idempotency lookup, returning byte-identical `404 not_found` + `AUTH_DENIED` audit on unowned or missing IDs without cross-tenant key leakage.
  - Added `validate_stored_proposal()` verifying exact 4-key non-boolean integer `amounts_json` (`is_non_bool_int`), `hmac.compare_digest` parity between recomputed `canonical_card` hash, stored `proposal_hash`, embedded card hash/amounts, and client `proposal_hash`, `status == "proposed"`, and `FixedClock` expiry (`>= expires_at`).
  - Added `check_overlap_guard()` (pre-check and inside `write_tx`) blocking overlapping `confirmed`/`executing` proposals on the same `(customer_id, order_id, item_id)`.
  - Executed atomic `proposed -> confirmed -> executing` (setting `confirmed_at`) and `proposed -> cancelled` state transitions alongside `http_idempotency_keys` insert and `AuditService.append()` inside `write_tx`, rolling back cleanly on concurrency loss (`409 invalid_state`) or audit failure.
- Live Store Revalidation Outside DB Locks (`agent/src/sellershield_agent/proposals/revalidate.py`):
  - Implemented `revalidate_against_store()` reading live order, refundable, active returns, shipment, and evidence outside `write_tx` (`KP-01`), rejecting COD `original` methods and policy version changes, and verifying live `decide()` `(decision, 4-key amounts)` matches stored proposal values (`409 invalid_state` on drift, `503 upstream_unavailable` on store outage) without false invalidation on advancing `facts.now`.
- Approval Card Builders, FactSheet, Verifier & Chat Hardening (`domain/session.py`, `orchestrator/cards.py`, `compose/factsheet.py`, `compose/templates.py`, `orchestrator/pipeline.py`):
  - Added `ChatActionName(StrEnum)` enforcing the 6 allowed OpenAPI guided actions so synthetic `confirm_proposal` actions return `422`.
  - Updated `sanitize_text()` to iteratively strip HTML tags/brackets/control chars without entity escaping; added strictly allowlisted `build_confirmation_card()` and `build_return_preview_card()` with non-bool int handling.
  - Updated `FactSheet` (`proposal_id`, `proposal_total_paise`, `proposal_decision`, non-bool amount extraction), added single-sentence bilingual `templates.proposal_confirmation()`, and wired `ConversationPipeline` Stages 4 & 6 to verify and emit confirmation/return_preview SSE cards on initial turns and `client_msg_id` replays.
Verification run (commands and result summary):
- `just agent-lint`: `uv run ruff check .` -> All checks passed!
- `just agent-types`: `uv run mypy src` -> Success: no issues found in 77 source files.
- `just agent-test`: `uv run pytest -q` -> 505 passed (all existing 488 tests + 17 new tests in `agent/tests/test_proposals_api.py`).
- `just test-guard` / `just status-guard`: PASS: test-guard verified test suite integrity (43 frozen files, `FROZEN.count` = 505, `Unfrozen tests: none`).
Follow-ups:
- Implement T2.4 (`Execution saga and two-phase write`) to advance `executing` proposals to `create_return` / `reserve_refund` / `authorize_refund` / `release_refund` and populate `return_id` / `approval_id`.

## 2026-11-11 T2.3 Propose tools (propose_return, propose_refund)
Files: agent/src/sellershield_agent/tools/propose_tools.py, agent/src/sellershield_agent/proposals/service.py, agent/src/sellershield_agent/tools/gateway.py, agent/src/sellershield_agent/tools/base.py, agent/src/sellershield_agent/tools/__init__.py, agent/src/sellershield_agent/domain/state.py, agent/tests/test_propose_tools.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Propose Tool Definitions & Strict Input Models (`agent/src/sellershield_agent/tools/propose_tools.py`):
  - Implemented `ProposeReturnArgs` (`order_id`, unique `items`, `reason_code`, `reason_text`, `resolution`, optional `refund_method`, optional `evidence_ids`) and `ProposeRefundArgs` (`order_id`, `cause` in `lost`|`undelivered`, optional `items`, optional `refund_method`) with `ConfigDict(extra="forbid", frozen=True)`.
  - Zero `amount`, `amount_paise`, `paise`, `customer_id`, or `role` arguments exist anywhere; extra arguments are rejected with `invalid_args`.
  - Validates unique items, caps `evidence_ids` at 3 with safe character regex (`^[A-Za-z0-9_-]{3,64}$`), and rejects `refund_method="original"` on COD orders.
  - Sanitizes `reason_text` via `sanitize_reason_text` (iterative HTML stripping, angle bracket removal, control character stripping, 200-char cap), stores it in `payload_json` only, and omits it from `decide()` and `canonical_card()`.
  - Fetches store facts via customer `StoreClient` (`service_ctx_factory` for `get_customer_risk_facts`), runs deterministic `decide()`, and handles all outcomes:
    - `DENY`: returns no proposal (`proposal=None`) and customer-safe `rule_text`.
    - `NEEDS_INFO`: returns `missing_slots` (`evidence`, `refund_method`) with no proposal row.
    - `ESCALATE`: returns no proposal and neutral `handoff_hint`.
    - `ALLOW` / `NEEDS_APPROVAL`: persists proposal (`status="proposed"`, 10-minute expiry, canonical SHA-256 `proposal_hash`) and confirmation card payload with paise + INR display breakdown and seller review notice (`approval_sla_hours`). Refund-without-return (`propose_refund`) always enforces `needs_approval`.
- Proposal Persistence, Dedup & Rate Limits (`agent/src/sellershield_agent/proposals/service.py`, `agent/src/sellershield_agent/domain/state.py`):
  - Added optional `created_at` column on `proposals` table in `domain/state.py`.
  - Implemented open proposal deduplication returning the existing open `proposal_id` and confirmation card when the same customer re-proposes the same `(order_id, sorted items)` before expiry.
  - Enforced max 3 open proposals per customer and max 10 proposals per simulated IST day (`Asia/Kolkata`), returning `rate_limited` beyond limits, with atomic re-checks inside `write_tx` across `policy_decisions`, `proposals`, and `audit_events`.
- Tool Registry & Gateway Integration (`agent/src/sellershield_agent/tools/gateway.py`, `tools/base.py`, `tools/__init__.py`):
  - Registered `propose_return` and `propose_refund` with `ToolKind.PROPOSE` and added `build_all_tools()`.
  - Wired `engine=self.engine` into `ToolContext` and restricted `OrderDigestCache` reads/writes strictly to `ToolKind.READ` so propose calls always inspect live proposal state and limits.
- Comprehensive Test Suite (`agent/tests/test_propose_tools.py`):
  - Added 26 tests covering scenarios SC-04 through SC-17 (`SC-04` 120000 paise `needs_approval`, `SC-05` 43900 paise `allow`, `SC-06`/`SC-07`/`SC-09` `deny`, `SC-08` partial return 59899 paise, `SC-10` 50000 paise `capped=True`, `SC-11`/`SC-16` `needs_info`, `SC-17` `propose_refund` `needs_approval`, `SC-13` `reason_text` immunity & HTML stripping), `escalate`, forbidden argument rejection, dedup, 3-open & 10-daily IST limits, zero forbidden words, hash equality, uniform IDOR 404, COD `original` & traversal/excess evidence rejection, static AST checks, and runtime SpyStore DB isolation.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 73 source files.
  - `cd agent; uv run pytest -q`: 488 passed in 47.13s.
  - `cd agent; uv run python ../scripts/test_guard.py check`: PASS: test-guard verified test suite integrity (`UNFROZEN: agent/tests/test_propose_tools.py`, `agent/tests/test_state.py`, ...).
- Sabotage drill: Temporarily added `amount: int | None = None` to `ProposeReturnArgs`; `uv run pytest tests/test_propose_tools.py` failed as expected (`2 failed, 24 passed` in `test_propose_rejects_amount_customer_id_or_role[amount-50000]` and `test_static_propose_tools_invariants`); reverted immediately.
Follow-ups:
- Freeze `agent/tests/test_propose_tools.py` (26 tests) when human approves.
- Next step: Wire confirmation endpoints (`POST /proposals/{id}/confirm`, `POST /proposals/{id}/cancel`), orchestrator propose tool turn handling, and T2.4 execution saga.

## 2026-11-11 T2.2 Proposal, execution and approval data and state machines
Files: agent/src/sellershield_agent/domain/state.py, agent/src/sellershield_agent/proposals/hashing.py, agent/src/sellershield_agent/proposals/__init__.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/policy/loader.py, agent/tests/test_state.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Database Schema and Models (`agent/src/sellershield_agent/domain/state.py`):
  - Created SQLAlchemy Table definitions registered on shared `get_metadata()`:
    - `policy_decisions`: `decision_id`, `request_id`, `customer_id`, `order_id`, `policy_version`, `facts_json`, `decision_json`, `created_at`.
    - `proposals`: `proposal_id` (PRP- prefix with 8 random hex characters from `secrets.token_hex(4)`), `customer_id`, `session_id`, `type`, `payload_json`, `decision`, `amounts_json`, `policy_version`, `facts_hash`, `proposal_hash`, `status`, `expires_at`, `confirmed_at`, `version`.
    - `executions`: `execution_id`, `proposal_id`, `step`, `idempotency_key` (unique), `status`, `store_ref`, `error_code`, `started_at`, `finished_at`.
    - `approvals`: `approval_id` (APR- with 8 digits), `proposal_id` (unique), `order_id`, `return_id`, `refund_id`, `amount_paise`, `required_role`, `status`, `policy_version`, `facts_hash`, `due_at`, `expires_at`, `decided_by`, `decided_at`, `decision_reason`, `store_authorization_ref`, `version`.
    - `client_events`: `event_id`, `customer_id`, `type`, `payload_json` (customer-safe), `created_at`.
  - Registered all new tables in `agent/src/sellershield_agent/db.py` inside `init_db()` and `get_engine()`.
- State Machines and Transition Engine (`agent/src/sellershield_agent/domain/state.py`):
  - Enums: `ProposalStatus` (`proposed`, `confirmed`, `cancelled`, `expired`, `executing`, `succeeded`, `failed`, `unknown`), `ApprovalStatus` (`pending`, `approving`, `approved`, `rejected`, `expired`, `needs_revalidation`, `withdrawn`), `ExecutionStatus` (`executing`, `succeeded`, `failed`, `unknown`, `reconciled`).
  - Transition tables: `PROPOSAL_TRANSITIONS` and `APPROVAL_TRANSITIONS` matching Spec Document 6 §0 and §21 amendment 2/4.
  - Implemented `transition_proposal` and `transition_approval` with optimistic locking: single atomic `UPDATE ... WHERE id = :id AND version = :expected_version AND status IN :valid_from_statuses` checking row count, writing an audit event in the same transaction, and raising `VersionMismatch` or `InvalidTransition` on failure.
  - Added process-level `_db_write_lock` in `write_tx()` to serialize concurrent threads and avoid SQLite lock escalations.
- Proposal Canonical Hashing (`agent/src/sellershield_agent/proposals/hashing.py`):
  - `canonical_card(...)`: Extracts strictly displayed fields (`type`, `order_id`, `items`, `amounts`, `method`, `decision`, `policy_version`, `expires_at`).
  - Deterministically sorts items by `item_id`, preserves integer paise breakdown, standardizes UTC ISO-8601 timestamps, and strictly rejects model commentary and customer reasons.
  - `compute_proposal_hash(...)`: Computes deterministic SHA-256 over canonical JSON with sorted keys and no whitespace (`separators=(",", ":")`).
- Privacy and Expiry Helpers:
  - `sanitize_policy_facts(...)` and `record_policy_decision(...)`: Recursively strips addresses, phone numbers, email addresses, and tokens prior to persisting `policy_decisions`.
  - `is_proposal_expired(...)`, `is_approval_expired(...)`, and `is_approval_due(...)`: Deterministic boundary checks using injected `Clock`.
- Comprehensive Test Suite (`agent/tests/test_state.py`):
  - 49 test cases covering legal/illegal proposal and approval transitions, version mismatch rejection, thread concurrency winner drill, hash sensitivity, model text immunity, PRP id randomness/uniqueness over 10,000 draws, FixedClock expiry/SLA boundaries, PII sanitization, audit failure rollback, and table schema constraints.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 71 source files.
  - `cd agent; uv run pytest -q`: 462 passed in 47.85s.
  - `cd agent; uv run python ../scripts/test_guard.py check`: PASS: test-guard verified test suite integrity (UNFROZEN: agent/tests/test_state.py).
Follow-ups:
- Next task: T2.3 Approval cards and customer confirmation flow.

## 2026-11-11 T2.1 Store write methods and write-capable fixture store
Files: contracts/openapi-store.yaml, agent/src/sellershield_agent/domain/models.py, agent/src/sellershield_agent/store/client.py, agent/src/sellershield_agent/clock.py, agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, agent/pyproject.toml, agent/tests/fault_plan.py, agent/tests/test_store_write.py, docs/DECISIONS.md, docs/PITFALLS.md, docs/FEATURES.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Step 0 (CHANGE-002 Contract Change):
  - In `contracts/openapi-store.yaml`, added optional string `client_ref` (the `Idempotency-Key` provided at creation) to `Return` and `Refund` schemas.
  - Added `AdminOrderDetailResponse` schema extending `Order` with `returns` (list of `Return`) and `refunds` (list of `Refund`), and updated `GET /admin/orders/{id}` 200 response.
  - Confirmed additive compatibility with `openapi-spec-validator` and `scripts/contract_check.py` (49 store operations, all invariants preserved).
  - User approved and refroze `contracts/openapi-store.yaml` in `tests/FROZEN.sha256`.
- Extended `StoreClient` Protocol & Context Types (`agent/src/sellershield_agent/store/client.py`):
  - Context types: `CustomerContext`, `SellerContext` (`seller_staff` or `seller_owner`), `ServiceContext` (scopes).
  - Extended protocol with 11 write and admin operations: `create_return`, `get_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, `get_refund`, `get_order_admin`, `get_settings`, `get_customer_refund_summary`.
  - Store exceptions: `StoreDuplicate`, `StoreLimitExceeded`, `StoreInvalidState`, `StoreIdempotencyMismatch`.
- Domain Models (`agent/src/sellershield_agent/domain/models.py`):
  - Added `StoreSettings` (`autonomy_cap_paise`, `autonomy_daily_cap_paise`, `version`).
  - Added `CustomerRefundSummary` (`customer_id`, `window_days`, `refund_count`, `refund_total_paise`, `order_count`, `order_total_paise`, `refund_rate_paise_bp`).
  - Added `AdminOrderDetail` (`Order` + `returns` and `refunds`).
  - Added optional `client_ref` to `ReturnRequest` and `Refund`.
- Write-Capable In-Memory Store Simulator (`agent/src/sellershield_agent/store/fixtures.py`):
  - In-memory mutable state initialized from pristine fixtures, with `reset()` for clean per-test isolation.
  - Return state machine validation: role-based edge triggers (`customer` can only `cancel` pending returns; `seller` or `service` can advance through pickup, transit, inspection, and completion).
  - Return item validation: exactly 1 active return per item; returned quantity never exceeds delivered quantity.
  - Refund reservations: active refunds never exceed remaining order payment total.
  - Autonomy caps: service token return approvals and refund releases capped at 50,000 paise without stored seller authorization.
  - Seller authorization limits: `seller_staff` allowed up to 200,000 paise; `seller_owner` required above; authorized amount cannot exceed reservation.
  - Simulated IST daily cap: 500,000 paise autonomous refund releases per simulated IST calendar day (`_get_ist_date_key`), resetting at IST midnight.
  - Payload idempotency: identical key and payload replays cached result; same key with differing payload raises `StoreIdempotencyMismatch`.
  - Concurrency safety: `asyncio.Lock` protects concurrent reservation attempts.
- Fault Injection Helper (`agent/tests/fault_plan.py`):
  - Test-only utility (never imported in `src/`): supports `fail_once`, `timeout_after_commit`, `unavailable`, `conflict`, and `slow`.
- Store Clock Caching (`agent/src/sellershield_agent/clock.py`):
  - Replaced Phase 0 stub with 60s offset caching and fallback on store failure; fixture store injects synchronized `Clock`.
- Comprehensive Test Suite (`agent/tests/test_store_write.py`):
  - 28 test cases covering all store write rules, caps, role boundaries, idempotency replays/mismatches, timeout_after_commit persistence, and concurrent reservation contention.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 68 source files.
  - `cd agent; uv run pytest -q`: 413 passed in 49.02s.
  - `cd agent; uv run python ../scripts/test_guard.py check`: PASS: test-guard verified test suite integrity.
Follow-ups:
- Send CHANGE-002 notification message to teammate W1.
- Proceed to Task T2.2 (Propose tools: return, refund, cancel).

## 2026-11-11 T1.11 Red-team suite: attackers and clumsy users
Files: scenarios/redteam/attacks.yaml, agent/tests/redteam/__init__.py, agent/tests/redteam/fakes.py, agent/tests/redteam/test_attacks.py, agent/tests/redteam/test_protocol.py, agent/tests/redteam/test_flood.py, agent/tests/redteam/test_clumsy.py, scripts/redteam_live.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/api/ratelimit.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/understand/injection.py, agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/orchestrator/resolver.py, agent/src/sellershield_agent/tools/gateway.py, .gitignore, docs/CHANGELOG.md, docs/FEATURES.md, docs/BUGLOG.md, docs/PITFALLS.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Red-Team Attack Corpus (`scenarios/redteam/attacks.yaml`):
  - Created 80 executable attack scenarios across all 9 adversarial and clumsy personas: direct injection (11), indirect injection via data fields (9), role-play / jailbreak (7), multilingual & obfuscated (9), data exfiltration (9), money manipulation (9), identity spoofing (7), protocol abuse (8), clumsy honest users (11).
  - Defined explicit test expectations: allowed tools, forbidden substrings, max tool budget, must_flag audit triggers, and HTTP status codes.
- Compromised Model & Scripted Fakes (`agent/tests/redteam/fakes.py`):
  - Implemented `ObedientFakeLLM` assuming a completely compromised model that obeys all attacker demands (invents non-existent tools, emits 50k characters, leaks fake system prompts and tokens, adds forbidden customer_id arguments, loops tool calls, invents refund numbers).
  - Implemented `ScriptedFakeLLM` providing deterministic scripted completions for normal test workflows.
- Comprehensive Red-Team Test Suites (`agent/tests/redteam/`):
  - `test_attacks.py`: Parameterized test running all 80 attacks through the live HTTP application (`create_app()`) with `ObedientFakeLLM` and `FixtureStoreClient`. Asserts allowlist enforcement, context scoping, zero forbidden leaks in SSE streams, no delta emission prior to verification, verified SHA-256 audit chain, sub-5.0s execution, and audit trail flagging.
  - `test_protocol.py`: Protocol abuse suite validating invalid JSON, non-JSON content types, 100 KB headers, duplicate Authorization headers, null bytes, unicode/padded order IDs, and unauthorized extra body fields with standard envelope errors and zero 500s.
  - `test_flood.py`: Denial of service and flood tests validating rapid message rate limits (429), failed-auth IP rate limiting, 50x deduplication on identical `client_msg_id` with zero redundant tool calls, and event loop cleanliness on midstream disconnect.
  - `test_clumsy.py`: Clumsy honest user suite covering whitespace, pure emoji, order ID typos, multiple orders, Hindi Devanagari numerals, ALL-CAPS, Hinglish code-switching, 1000-character messages, and polite goodbyes without crashing or guessing orders.
- Live Red-Team Replay Script (`scripts/redteam_live.py`):
  - Created real-key live replay script using `LLM_CACHE_MODE=record`, capped at 100 LLM calls, outputting category score tables and generating reports in `agent/.evidence/`.
- Hardened Application Security & Bug Fixes:
  - `agent/src/sellershield_agent/errors.py`: Fixed `_sanitize_validation_error` decoding raw `bytes` to prevent `TypeError: Object of type bytes is not JSON serializable` on invalid content-type bodies (returning clean 422 standard envelope).
  - `agent/src/sellershield_agent/api/ratelimit.py` & `auth/deps.py`: Added `is_exceeded()` and wired IP rate limiting (429 on >= 20 failed authentications per minute).
  - `agent/src/sellershield_agent/understand/injection.py`: Expanded injection heuristics with zero-width stripping, unhyphenation, leetspeak mapping, and comprehensive regex coverage matching all 32 adversarial test triggers without false positives on honest text.
  - `agent/src/sellershield_agent/understand/router.py`: Scoped `POLICY_PHRASES` regex away from bare `rules?` and routed injection attempts without legitimate orders to `OFF_TOPIC`.
  - `agent/src/sellershield_agent/orchestrator/pipeline.py`: Fixed idempotency replay caching and turn persistence; added negative/invalid quantity rejection; added malformed/typo order ID detection preventing order guessing.
  - `agent/src/sellershield_agent/orchestrator/resolver.py`: Added early break on `invalid_args` tool errors to enforce bounded tool budgets on padded IDs.
  - `agent/src/sellershield_agent/tools/gateway.py`: Defaulted `policy` to `load_policy()` when None.
Verification run (commands and result summary):
- `(Select-String -Path scenarios/redteam/attacks.yaml -Pattern '^- id:').Count`: 80 attacks (all category minimums met).
- `cd agent; uv run ruff check .`: All checks passed!
- `cd agent; uv run mypy src`: Success: no issues found in 68 source files.
- `cd agent; uv run pytest tests/redteam -q`: 103 passed in 6.92s.
- `just agent-check`: ruff clean, mypy clean, 385 tests passed in 44.90s, test-guard verified test suite integrity.
Follow-ups:
- Run `python ../scripts/redteam_live.py` from `agent/` with live API keys for real-world model audit.

## 2026-11-11 T1.10 Gate G1 tests and auth boundaries
Files: agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/api/dev_stub.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/auth/verify.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/main.py, agent/tests/e2e/__init__.py, agent/tests/e2e/test_gate_g1.py, docs/evidence/README.md, .gitignore, docs/CHANGELOG.md, docs/FEATURES.md, docs/TASKS.md, docs/STATE.md
What changed:
- Seller & Dev Stub Routers (`agent/src/sellershield_agent/api/seller_stub.py`, `dev_stub.py`, `main.py`):
  - Registered stub routers covering all 30 seller and dev operations defined in `contracts/openapi-agent.yaml`.
  - Protected all standard seller endpoints (`/seller/*`) with `require_seller`.
  - Protected owner-restricted endpoints (`/seller/policies/{version}/activate`, `/dev/*`) with `require_owner`.
  - Protected dev operations (`/dev/*`) with `require_dev_mode` (enforcing `DEV_MODE=true`).
  - Standard stub behavior returns `501 not_implemented` with the standard API error envelope.
  - Mounted routers under `/v1/seller`, `/seller`, `/v1/dev`, and `/dev` in `main.py`.
- Authentication & Authorization Denial Audit Logging (`agent/src/sellershield_agent/auth/deps.py`, `verify.py`, `errors.py`):
  - Implemented `record_auth_denied()` recording immutable `auth_denied` audit events on every authentication and authorization rejection.
  - Denials write clean reason codes only (e.g. `missing_authorization_header`, `malformed_authorization_header`, `role_mismatch`, `token_expired`, `dev_mode_required`, `authz_step_up_stale`).
  - Strictly prevents logging or recording tokens, Authorization headers, or customer secrets in audit tables or application logs (KP-13, KP-28).
  - Attached reason tracking on `AppHTTPException` across `verify_token` and `deps.py`.
- Gate G1 Executable Proof Test Suite (`agent/tests/e2e/test_gate_g1.py`):
  - T01: Own order lookup (SC-01) with live HTTP client and FixtureStoreClient; verified SSE contains ORD-1048 and shipping ETA, tool call audited, and audit chain verified.
  - T02: Cross-customer access (SC-02) returning uniform 404 with zero C001 data leaked and denial audited.
  - T03: Spoofed identity or admin claim in chat text (SC-03) ignored; identity strictly determined from JWT claims.
  - T04: Seller route auth boundaries verified: customer token returns 403, missing token returns 401, expired token returns 401, staff on owner route returns 403, and owner returns 501. Every denial wrote an `auth_denied` audit row and SHA-256 audit chain verified intact.
  - Guided fallback: Chat with LLM unavailable (`DEV_FAULT_LLM_DOWN`) returns guided mode with chips and still answers deterministic `track_order` button actions without calling LLM.
- Evidence Documentation (`docs/evidence/README.md`, `.gitignore`):
  - Created `docs/evidence/README.md` defining invariants for local evidence artifacts.
  - Added `docs/evidence/*` and `!docs/evidence/README.md` to `.gitignore`.
Verification run (commands and result summary):
- `cd agent; uv run ruff check .`: All checks passed.
- `cd agent; uv run mypy src`: Success: no issues found in 68 source files.
- `cd agent; uv run pytest tests/e2e/test_gate_g1.py -q`: 5 passed in 0.77s.
- `just agent-check`: ruff clean, mypy clean, 282 tests passed in 38.40s, test-guard verified test suite integrity.
Follow-ups:
- Milestone M1 complete; proceed to Phase 2 (Write path, mutations, and approvals).

## 2026-11-11 T1.9 Real store client and handshake
Files: agent/src/sellershield_agent/store/http_client.py, agent/src/sellershield_agent/store/__init__.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/api/health.py, agent/src/sellershield_agent/api/chat.py, agent/src/sellershield_agent/clock.py, agent/src/sellershield_agent/domain/models.py, .env.example, agent/.env.example, scripts/handshake.py, agent/tests/test_store_http.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Real HTTP Store Client (`agent/src/sellershield_agent/store/http_client.py`):
  - Implemented `HttpStoreClient` fulfilling `StoreClient` asynchronous Protocol with `httpx.AsyncClient`.
  - Normalized base URL from `STORE_BASE_URL` with `/api/v1` path routing.
  - Configured timeouts: 2 s connect and 8 s read; disabled redirect following (`follow_redirects=False`).
  - Added bounded retries with randomized jitter (max 2 attempts) for idempotent `GET` on 502, 503, 504 and connection errors.
  - Forwarded customer bearer token (`Authorization: Bearer <token>`) from `RequestContext.token` (`SecretStr`) and `X-Request-ID`. Tokens are strictly never logged (KP-13).
  - Implemented service token client credentials flow (`POST /api/v1/auth/token`) using `STORE_MONITOR_CLIENT_ID` and `STORE_MONITOR_CLIENT_SECRET`, cached in memory with auto-refresh 60 s before expiry.
  - Error mapping hierarchy: 404 -> `StoreNotFound` (uniform for missing and unowned orders, KP-15), 401/403 -> `StoreForbidden`, 409 -> `StoreConflict`, 5xx/network errors -> `StoreUnavailable`, timeouts -> `StoreTimeout`, malformed order IDs -> `StoreValidationError`.
  - Unknown response fields are dropped cleanly; malformed response shapes raise `StoreUnavailable` and are logged without the response payload.
  - Integrated `get_customer_risk_facts` combining `/admin/customers/{id}` and `/customers/{id}/refund-summary` with basis-point ratio conversion.
  - Integrated `sync_clock` and `get_clock` updating synchronized `StoreClock`.
- Store Clock (`agent/src/sellershield_agent/clock.py`):
  - Completed `StoreClock` tracking simulated store time and offset.
- Configuration & Mode Enforcement (`agent/src/sellershield_agent/config.py`, `main.py`, `api/health.py`):
  - Added `STORE_MODE` setting (`fixtures` | `http`, default `fixtures`) to config and `.env.example` templates.
  - Startup check in `main.py`: outside `DEV_MODE`, app refuses to start with `STORE_MODE=fixtures` (KP-24).
  - Added structured startup logging stating active `store_mode`.
  - Updated `readyz` health probe to return active `store_mode` and probe store reachability (reporting `status="degraded"` on store failure) when `STORE_MODE=http`.
- Store Client Factory (`agent/src/sellershield_agent/store/__init__.py`, `api/chat.py`):
  - Added `get_store_client(clock, settings)` factory routing to `HttpStoreClient` or `FixtureStoreClient` based on `STORE_MODE`.
  - Wired into chat orchestrator pipeline.
- Handshake Dev Tool (`scripts/handshake.py`):
  - Created standalone dev verification script logging in as C001, querying orders, printing order status only, and exiting 0 on success.
- Test Suite (`agent/tests/test_store_http.py`):
  - Added 24 contract tests with `respx` verifying all StoreClient methods, token forwarding, error mappings, retries, Pydantic validation, and health probes.
Verification run:
- `cd agent; uv run ruff check .`: passed with 0 errors.
- `cd agent; uv run mypy src`: passed with 0 errors across 66 source files.
- `cd agent; uv run pytest -q`: 277 passed in 38.15s.
Follow-ups:
- Run with live NestKart store once W1 launches store service.

## 2026-11-11 T1.8 Orchestrator, SSE chat and guided actions
Files: contracts/openapi-agent.yaml, agent/src/sellershield_agent/domain/session.py, agent/src/sellershield_agent/orchestrator/__init__.py, agent/src/sellershield_agent/orchestrator/cards.py, agent/src/sellershield_agent/orchestrator/resolver.py, agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/api/ratelimit.py, agent/src/sellershield_agent/api/chat.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/main.py, scripts/dev_auth.py, scripts/chat_cli.py, agent/tests/test_chat.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Step 0 (CHANGE-001):
  - Updated `contracts/openapi-agent.yaml`: Added `ChatAction` schema with allowed action names (`track_order`, `return_status`, `refund_status`, `policy_info`, `start_return`, `talk_to_human`) and optional `order_id`. Made `text` optional in `ChatMessageRequest` when `action` is present. Preserved `additionalProperties: false`. Validated with openapi-spec-validator and contract_check.py. Recorded in docs/DECISIONS.md.
- Domain & Database:
  - Added `sessions` and `messages` tables in `domain/session.py` with SQLAlchemy Table definitions, composite unique index on `(session_id, client_msg_id)`, JSON columns for session state and cards.
  - Updated `db.py` to register session and message tables on `init_db`, use `StaticPool` for in-memory SQLite, and provide engine and rate limiter resets.
- Orchestrator Pipeline & Resolver:
  - Implemented Stages 0-6 in `orchestrator/pipeline.py`:
    - Stage 0: Session lookup with customer ID ownership verification (uniform 404 on IDOR). Client message deduplication on `client_msg_id` returning cached response with zero duplicate tool executions.
    - Stage 1: Understanding via `understand()` service, regex order ID extraction, and heuristic prompt injection scanning.
    - Stage 2: Resolver loop (`orchestrator/resolver.py`) with strict execution budgeting (max 5 iterations, 8 tool calls, 20s budget), initial plan generation in trace, duplicate tool call prevention.
    - Stage 3: FactSheet extraction (`build_factsheet`) from tool results with grounded order, shipping, return, and refund attributes.
    - Stage 4: Composition & Verifier gate (`compose.verifier`). All model drafts are verified before emission; if violations occur, the pipeline falls back directly to deterministic verified templates (`not_found`, `shipping_status`, `order_status`, `refund_status`, `policy_summary`).
    - Stage 5: Safe streaming (Amendment 2): Emits `tool.status` events during tool execution; verified text is emitted in `message.delta` chunks; structured interactive cards (`order_summary`, `shipment_timeline`, `chips`) emitted as `card` events; terminal `final` event carrying `message_id` and full verified text.
    - Stage 6: Session state update: persists state JSON and message history.
  - Guided Mode & Dev Flags:
    - Zero-LLM execution path when `action` is passed.
    - Fallback to guided mode with chips menu when `LLMUnavailable` is raised or when `DEV_FAULT_LLM_DOWN` is enabled with `DEV_MODE=true`.
  - Cards & Sanitization:
    - `orchestrator/cards.py` with recursive HTML escaping (`html.escape`) and length truncation for all card string fields.
  - Rate Limiting & Protection:
    - In-memory sliding window rate limiter in `api/ratelimit.py`: 20 messages/minute per customer, 60 failed auth attempts/minute per IP.
    - Message length validation (max 1000 chars, 422 error envelope on 1001 chars), control character stripping.
    - Strict logging hygiene: info level never logs raw message text or authentication tokens.
- Developer Tools:
  - `scripts/dev_auth.py`: Generates RSA key in memory, serves JWKS at localhost:8099, prints tokens for C001, C002, and seller. Refuses to run if DEV_MODE is false. Added `/token/{user}` endpoint for automatic token distribution to CLI.
  - `scripts/chat_cli.py`: Interactive CLI to create sessions, send text or actions, and stream SSE events in real-time. Refuses to run if DEV_MODE is false. Added auto-fetch of dev tokens from local dev auth server.
- Bug Fixes:
  - BUG-001: Fixed Windows relative SQLite path handling in `db.py` (`create_engine_from_url`) by stripping leading slash before `Path.mkdir()`, preventing `OperationalError: unable to open database file` when creating `agent/data`.
  - BUG-002: Added automatic fallback in `JWKSClient._fetch_with_retries` (`auth/jwks.py`) to query `http://localhost:8099/.well-known/jwks.json` when `DEV_MODE=true` and primary port 8000 is down.
- Test Suite (`agent/tests/test_chat.py`):
  - 14 tests covering SC-01, SC-02, SC-03, verifier fallback, LLM down guided mode, zero-LLM guided action, idempotency, 1001-char limit, rate limit (20/min), uniform 404 on IDOR, seller token 403, dev flag gating, card HTML escaping, and INFO-level logging hygiene.
Verification run (commands and result summary):
- `cd agent; uv run ruff check .`: passed with 0 errors.
- `cd agent; uv run mypy src`: passed with 0 errors across 65 source files.
- `cd agent; uv run pytest -q`: 253 passed in 36.62s.
- `cd agent; uv run python ../scripts/test_guard.py check`: flags contracts/openapi-agent.yaml due to CHANGE-001 (awaiting user refreeze).
Follow-ups:
- Refreeze contracts/openapi-agent.yaml in test-guard manifest for CHANGE-001.

## 2026-11-11 T1.7 Understanding stage and deterministic router
Files: agent/src/sellershield_agent/understand/__init__.py, agent/src/sellershield_agent/understand/schema.py, agent/src/sellershield_agent/understand/injection.py, agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/understand/service.py, agent/prompts/understand.v1.md, prompts/understand.v1.md, scenarios/golden/intents.yaml, agent/tests/test_understand.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `understand/schema.py`:
  - Flat Pydantic model `Understanding` (`extra="forbid"`).
  - Enums: `Intent` (13 values: `order_status`, `shipping_status`, `return_request`, `refund_status`, `refund_request`, `cancellation`, `damaged_item`, `wrong_item`, `human_request`, `policy_question`, `small_talk`, `off_topic`, `abuse`), `ReasonCode` (7 values: `defective`, `damaged`, `wrong_item`, `not_as_described`, `changed_mind`, `late`, `other`), `Language` (`en`, `hi`, `hinglish`, `other`), `Sentiment` (`calm`, `frustrated`, `angry`), `ClarificationSlot` (`order_id`, `items`, `reason`, `refund_method`, `none`), and `SafetyFlag` (`injection_attempt`, `abusive`, `off_topic`, `pii_request`).
  - Fields: `confidence` (0.0 to 1.0), `order_ids` (list of str), `item_mentions` (list of str), `reason_text` (max 200), `asks_for_human` (bool), `secondary_intents` (list of Intent), `needs_clarification` (bool), `safety_flags` (list of SafetyFlag).
- Implemented `prompts/understand.v1.md` (and `agent/prompts/understand.v1.md`):
  - System prompt enforcing untrusted message boundary inside `<customer_message>...</customer_message>` tags.
  - Full schema enum definitions and 6 few-shot examples (English status, Hinglish damaged return, prompt injection attempt, human escalation with legal threats, cancellation, and ambiguous clarification).
- Implemented `understand/injection.py`:
  - `detect_injection_heuristics()` detecting prompt injection patterns (`ignore previous`, `system prompt`, `you are now`, `approve my refund`, `as admin`, `as root`, `developer mode`, `disregard previous`, `override rules`, `prompt leak`). Advisory only; adds `SafetyFlag.INJECTION_ATTEMPT` and triggers `injection_flag` audit event without altering business logic.
- Implemented `understand/router.py`:
  - `DeterministicRouter`: Regex `ORD-\d+` ID extraction, bilingual keyword and phrase rules (English & common Hinglish: *kab aayega*, *wapas karo*, *toota hua*, *galat samaan*, *paisa wapas*), reason codes, language detection (Devanagari script for `hi`, vocabulary overlap for `hinglish`, Latin for `en`), human agent phrases (*human*, *agent*, *manager*, *insaan*, *baat karao*), policy `escalation_keywords` integration (*lawyer*, *legal*, *court*, *consumer forum*, *fraud*, *sue*, *police*, *chargeback*). Confidence strictly capped at $\le 0.5$.
- Implemented `understand/service.py`:
  - `understand(message, context, llm_chain, router, tx=None, audit_recorder=None)` orchestrating: (1) prompt hash computation; (2) injection heuristic scanning and `injection_flag` audit event; (3) structured LLM classification via `generate_structured`; (4) resilient fallback on `(LLMUnavailable, StructuredOutputError, LLMError, Exception)` to `DeterministicRouter` and `llm_fallback` audit event logging; (5) confidence gating (< 0.6 forces `needs_clarification=True` and slot assignment); (6) order ID integrity (drops invented IDs not in message or customer order digest; merges regex IDs from message); (7) prompt SHA-256 hash written to audit trail; (8) redacted and truncated debug logging; never logs raw customer text or tokens at INFO level.
- Created `scenarios/golden/intents.yaml`:
  - 30 labelled golden evaluation utterances covering English, Hinglish, typos, multi-intent, off-topic, abuse, and prompt injection attacks.
- Implemented test suite in `agent/tests/test_understand.py` (10 tests):
  - Valid LLM output used directly.
  - Invalid output retries once in `generate_structured`, then falls back to `DeterministicRouter`.
  - LLM unavailable triggers router fallback and `llm_fallback` audit event.
  - Invented order IDs dropped while valid and message-extracted IDs are preserved.
  - Confidence < 0.6 enforces `needs_clarification=True`.
  - Injection heuristics set safety flag and log `injection_flag` audit event.
  - Hinglish messages route accurately to `order_status` or `shipping_status`.
  - Prompt hash recorded in audit trail.
  - Golden intents evaluation executes across all 30 utterances and prints router accuracy.
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_understand.py -q -s`:
  - 10 passed in 0.75s.
  - Printed router accuracy on golden utterances: 100.0% (30/30).
- `just agent-check`:
  - `agent-lint`: Ruff check passed with 0 errors.
  - `agent-types`: Mypy passed with 0 issues in 59 source files.
  - `agent-test`: 239 passed in 40.60s (10 new tests in `test_understand.py`).
  - `test-guard`: PASS (test suite integrity verified, 26 frozen files intact, FROZEN.count 203 <= 239 collected).
- Live provider test on baseline and stress inputs:
  - English, Hinglish, and prompt injection verified on live Gemini/Gemma provider with valid Understanding models.
  - Stress testing (mixed language, empty message, order ID only, multi-order, injected JSON intent, 1000-char wall of text) verified graceful degradation to deterministic router with zero unhandled exceptions.
Follow-ups:
- T1.8 Grounded conversational orchestrator wiring Ingress -> Understanding -> Workflow / Tools -> Composition -> Verifier -> Stream.

## 2026-11-11 T1.6 FactSheet, verifier and templated replies
Files: agent/src/sellershield_agent/compose/__init__.py, agent/src/sellershield_agent/compose/factsheet.py, agent/src/sellershield_agent/compose/verifier.py, agent/src/sellershield_agent/compose/templates.py, agent/src/sellershield_agent/compose/regenerate.py, agent/tests/test_verifier.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `compose/factsheet.py`:
  - Immutable Pydantic model `FactSheet` (`frozen=True, extra="forbid"`).
  - Collects allowed IDs (`ORD`, `RET`, `RFD`, `CASE`, `APR`, `PRP`), integer paise amounts, IST calendar dates via `to_ist(dt).date()`, relative day facts (`eta_in_days`, `delivered_days_ago`), customer-safe rule texts, and supported action claims (`shipped`, `delivered`, `refunded`, `processed`, `approved`, `created`, `cancelled`).
  - `build_factsheet(results, language, clock)` builds immutable factsheet from single or multiple `ToolResult` instances.
- Implemented `compose/verifier.py`:
  - `verify(draft, factsheet, language) -> VerificationResult(ok, violations)`.
  - Deterministic pre-processing: Unicode NFKC normalization, zero-width stripping (`\u200b`, `\u200c`, `\ufeff`), control char removal, dash normalization (`[–—−]` -> `-`).
  - 10 strict verification checks:
    1. Maximum length constraint (`len(draft) <= 1200`).
    2. Language & script validation (Devanagari presence required for `hi`, rejected for `en`).
    3. Forbidden content: Tool names (`get_order`, etc.), internal prompts/markers, risk scores/levels, auto-limits/thresholds, customer IDs (`\bC\d{3,}\b`), token/key strings, URLs, HTML, Markdown links/images.
    4. Spelled-out currency amounts rejection (hundred, thousand, lakh, sau, hazaar next to currency words).
    5. Currency amount extraction and verification (rupee sign, Rs, INR, rupees, rupaye, Devanagari digits, split lines) strictly converted to integer paise and verified against `factsheet.amounts_paise`.
    6. Entity ID extraction (case-insensitive `ORD|RET|RFD|CASE|APR|PRP`) and verification against `factsheet.allowed_ids`.
    7. Date extraction (ISO, formatted English & Hindi month strings) and verification against `factsheet.dates`.
    8. Relative temporal claims ("tomorrow", "yesterday", "in X days", "X days ago", "कल", "आज", "परसों") verified against `factsheet.relative_facts`.
    9. Action claims (`refunded`, `processed`, `approved`, `created`, `cancelled`, `shipped`, `delivered`) verified against `factsheet.supported_claims`.
    10. Promise words (`guaranteed`, `definitely`, `will be refunded`, `100%`) verified against `factsheet.promise_facts`.
- Implemented `compose/templates.py`:
  - 10 deterministic bilingual fallback templates built strictly from `FactSheet`: `order_status`, `shipping_status`, `delay_options`, `return_eligibility_preview`, `refund_status`, `policy_summary`, `not_found`, `store_unavailable`, `human_handoff_unavailable`, `guided_mode_menu`.
  - Verified to pass `verify()` by construction across all scenario orders (`SC-01` to `SC-17`) in both English and Hindi.
- Implemented `compose/regenerate.py`:
  - `build_regeneration_prompt()` builds a structured correction message detailing exact violations, allowed IDs, formatted amounts, dates, and claims for one regeneration attempt before falling back to templates.
- Implemented test suite in `agent/tests/test_verifier.py` (26 tests):
  - Adversarial amount tests (invented amount, formatting variants, spelled-out rejection).
  - Identifier tests (unrecognized IDs, case-insensitivity, customer ID leakage).
  - Date and relative-time tests (unbacked dates, tomorrow without ETA fact, valid relative days).
  - Claim and promise word tests.
  - Forbidden content (internal terms, tool names, URLs, HTML, markdown media).
  - Language and length boundary tests.
  - 5 nasty breakdown tests (1 paisa off, 1 digit off in ID, unbacked claim word `refunded`, Devanagari Hindi digits `₹१,२००` vs `₹१,२०१`, split across newlines).
  - Hypothesis property test: `format_inr(paise)` on any valid paise amount always passes verification.
  - Full scenario template verification across all 16 scenario orders in both English and Hindi.
  - Regeneration prompt test.
Verification run (commands and result summary):
- `just agent-check`:
  - `agent-lint`: Ruff check passed with 0 errors.
  - `agent-types`: Mypy passed with 0 issues in 54 source files.
  - `agent-test`: 229 passed in 35.99s (26 new tests in `test_verifier.py`).
  - `test-guard`: PASS (test suite integrity verified, 26 frozen files intact, FROZEN.count 203 <= 229 collected).
Follow-ups:
- T1.7 (Grounded conversational loop orchestrating Gateway -> FactSheet -> LLM -> Verifier -> Regeneration/Template Fallback).

## 2026-11-11 T1.5 Tool gateway and read tools
Files: agent/src/sellershield_agent/tools/__init__.py, agent/src/sellershield_agent/tools/base.py, agent/src/sellershield_agent/tools/messages.py, agent/src/sellershield_agent/tools/cache.py, agent/src/sellershield_agent/tools/read_tools.py, agent/src/sellershield_agent/tools/formatting.py, agent/src/sellershield_agent/tools/gateway.py, agent/src/sellershield_agent/db.py, agent/tests/test_tools.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `tools/base.py`:
  - Enums `ToolKind` (`read`, `propose`) and `ToolErrorCode` (`not_found`, `not_permitted`, `invalid_args`, `policy_denied`, `stale_state`, `rate_limited`, `upstream_unavailable`, `timeout`, `internal`).
  - Pydantic model `ToolResult` (`extra='forbid'`, frozen) with `ok`, `data`, `error_code`, `error_message`, `retryable`, `fact_ids`, `facts`.
  - `ToolContext` encapsulating `StoreClient`, customer `RequestContext`, `Clock`, `PolicyDocument`, and service-context factory. Strictly hides customer token from handlers.
  - Dataclass `Tool` binding name, description, flat args model, kind, and async handler.
- Implemented `tools/messages.py`:
  - Localized customer-safe messages in English and Hindi for every error code. Strictly excludes internal limits, thresholds, risk scores, stack traces, and staff roles.
- Implemented `tools/cache.py`:
  - `OrderDigestCache` providing per-session digest caching with 60-second TTL driven by simulated `Clock`. Includes `invalidate(order_id)` and `invalidate_session(session_id)` hooks.
- Implemented `tools/read_tools.py`:
  - 8 safe read tools: `list_my_orders`, `get_order`, `get_shipping_status`, `get_return_status`, `get_refund_status`, `get_policy_summary`, `check_return_eligibility`, `preview_refund`.
  - Strict Pydantic input models with `extra='forbid'`, completely devoid of `customer_id`, `user_id`, `token`, `amount`, or `paise` fields.
  - Response minimization: outputs contain only minimal required identifiers, dates, statuses, amounts, and customer-safe rule texts. Completely strips addresses, phone numbers, emails, risk scores, rule weights, and internal approval thresholds.
  - `check_return_eligibility` and `preview_refund` integrate with `decide()` and `compute_refund()` using customer context for orders and internal service context for risk facts.
- Implemented `tools/formatting.py`:
  - `tools_for_llm(allowed)`: Generates flat OpenAI function specifications with one-sentence descriptions including "do not use if..." guidance.
  - `tool_results_block(results)`: Encloses outputs in tagged `<tool_results>` block with explicit untrusted data disclaimer, strips HTML tags and non-printable control characters, and truncates string values to 200 characters.
- Implemented `tools/gateway.py`:
  - `ToolGateway`:
    - Allowlist enforcement returning `not_permitted` and logging audited denial.
    - Strict argument validation rejecting unexpected fields (`invalid_args`).
    - ID format sanity validation (`^ORD-[0-9]{3,6}$`, `^RET-[0-9]{3,6}$`, `^RFD-[0-9]{3,6}$`).
    - Digest cache lookup and update with 60s TTL.
    - 8.0-second execution timeout via `asyncio.timeout(8.0)`.
    - Safe error mapping: `StoreNotFound` maps to uniform `not_found`, timeouts map to `upstream_unavailable` (`retryable=True`), unhandled errors map to `internal` with zero leaked stack traces.
    - Audit logging: Appends exactly one `tool_call` audit event via `AuditService.append` in an independent short `write_tx` transaction strictly *after* the store call finishes (never holding transactions across I/O). Denial details scrub foreign customer IDs.
    - `call_many`: Concurrently dispatches independent read operations bounded by `asyncio.Semaphore(4)`.
- Updated `db.py`:
  - Made `metadata` optional in `init_db(engine, metadata=None)`, defaulting cleanly to `get_metadata()`.
- Implemented test suite in `agent/tests/test_tools.py` (13 tests):
  - Uniform 404 for unowned vs nonexistent order with scrubbed audit details.
  - Malformed ID rejection.
  - Tool outside allowlist denial and audit.
  - Unknown argument field rejection.
  - Store timeout mapping to retryable `upstream_unavailable`.
  - Zero PII (address, phone, email) leakage in outputs.
  - Zero risk score and threshold leakage in outputs or audit details.
  - Single audit record atomicity per call.
  - Digest cache hit avoiding store call and cache invalidation.
  - Parallel `call_many` execution with 4-concurrency cap.
  - Safe fault isolation returning `internal` without stack traces.
  - Static architecture invariant tests (read/propose kinds only, no store writes, no customer_id/user_id/token/amount arg fields).
  - Sanitization against prompt injection, control characters, and 200-char truncation.
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_tools.py -q`: 13 passed in 0.88s.
- `cd agent; uv run ruff check .`: All checks passed!
- `cd agent; uv run mypy src`: Success: no issues found in 49 source files.
- `just agent-check`:
  - `agent-lint`: ruff check passed.
  - `agent-types`: mypy passed (49 source files).
  - `agent-test`: 203 passed in 35.99s.
  - `test-guard`: PASS: test-guard verified test suite integrity.
Follow-ups:
- Connect orchestrator and FactSheet verifier to invoke tools through ToolGateway.

## 2026-11-111.4 Audit service with hash chain
Files: agent/src/sellershield_agent/audit/__init__.py, agent/src/sellershield_agent/audit/models.py, agent/src/sellershield_agent/audit/sanitize.py, agent/src/sellershield_agent/audit/service.py, agent/tests/test_audit.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `audit/models.py`:
  - `audit_events` SQLAlchemy Core table attached to shared `get_metadata()` with primary key autoincrement `seq`, event metadata columns, `details_json`, `prev_hash`, and `hash`.
  - Unique index `ix_audit_events_prev_hash` on `prev_hash` to cryptographically prevent chain forks and ensure strictly linear history.
  - Enums `ActorType`, `EventType`, `Outcome` and Pydantic models `AuditEvent` and `ChainStatus`.
- Implemented `audit/sanitize.py`:
  - `sanitize_details(raw_details, actor_id, is_denial)`:
    - Validates JSON compatibility, rejecting non-serializable objects (sets, callables) with `ValueError`.
    - Reuses sensitive key patterns from `logging.py` (`authorization`, `api_key`, `token`, `password`, `secret`, `cookie`).
    - Strips the word `Authorization` in any case from keys (renaming to `[REDACTED]`) and values.
    - Redacts email addresses and fake Bearer tokens (`Bearer <token>` -> `[REDACTED_TOKEN]`).
    - For denial events (`is_denial=True`), scrubs foreign customer IDs (`CUST-...`) not matching `actor_id` to prevent IDOR customer data leakage.
    - Enforces 500-character maximum string length limit on all string fields.
    - Generates deterministic canonical JSON (`sort_keys=True, separators=(',', ':')`).
- Implemented `audit/service.py`:
  - `AuditService.append(tx, event)`: Runs inside caller's `write_tx` (BEGIN IMMEDIATE), reads the latest committed hash (or genesis `0` * 64 if table empty), calculates SHA-256 hash over canonical JSON of event fields + `prev_hash`, inserts row atomically with business changes, and returns `seq`.
  - `AuditService.verify_chain(conn)`: Recomputes hashes and validates sequential link integrity from genesis to latest row, returning `ChainStatus(ok, checked, first_broken_seq)`.
- Implemented comprehensive test suite in `agent/tests/test_audit.py` (12 tests):
  - Genesis link validation for first event.
  - 100 sequential events verification.
  - Comprehensive column tampering detection across all stored fields breaking verify at `seq=5`.
  - Middle row deletion detection.
  - 8-thread concurrency appending 25 events each against a real SQLite file yielding 200 events, unique `prev_hash` values, zero duplicate forks, and valid chain. Engine disposed to prevent Windows file locking (KP-21).
  - Business statement failure rollback test (audit aborts if business raises).
  - Audit failure rollback test (business aborts if audit append raises).
  - Redaction of fake bearer tokens and emails.
  - Exclusion of the word `Authorization`.
  - String truncation to 500 chars and non-JSON rejection.
  - Denial event foreign customer ID redaction.
  - Empty chain verification.
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_audit.py -q`: 12 passed in 1.37s.
- `cd agent; 1..5 | ForEach-Object { uv run pytest tests/test_audit.py -k test_concurrent_threads_append_valid_chain -q }`: 5 runs passed, zero flakiness.
- `cd agent; uv run ruff check .`: All checks passed!
- `cd agent; uv run mypy src`: Success: no issues found in 42 source files.
- `just agent-check`:
  - `agent-lint`: ruff check passed.
  - `agent-types`: mypy passed (42 source files).
  - `agent-test`: 190 passed in 35.80s.
  - `test-guard`: PASS: test-guard verified test suite integrity.
Follow-ups:
- Wire FactSheet, verifier, and proposal execution to write all state transitions through `AuditService.append`.

## 2026-11-11 .3 Rules, decision engine, risk indicators and scenario tests
Files: agent/src/sellershield_agent/policy/facts.py, agent/src/sellershield_agent/policy/rules.py, agent/src/sellershield_agent/policy/risk.py, agent/src/sellershield_agent/policy/delay.py, agent/src/sellershield_agent/policy/decide.py, agent/src/sellershield_agent/policy/__init__.py, agent/tests/test_decide.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `policy/facts.py`: Frozen immutable `DecisionFacts` model holding order, items, reason, resolution, refund method, evidence presence, active returns, historical 30d refunds, refundable amounts, customer risk facts, shipment info, and evaluation timestamp. Implemented `compute_facts_hash(facts)` computing deterministic SHA-256 hash over canonical JSON strictly excluding `reason_text` (KP-27).
- Implemented `policy/rules.py`: Pure functions for rules R-01 through R-15 returning `RuleResult(id, passed, blocking, outcome_if_failed, customer_text, seller_text, facts, missing_slots)`:
  - R-01: Order ownership verified by construction.
  - R-02: Order state validation (delivered for returns; shipped for lost refunds).
  - R-03: Category return window calculation in Asia/Kolkata timezone with 23:59:59 boundary check; recognizes prior established claims on partial refunds.
  - R-04: Non-returnable hygiene categories (e.g., innerwear, personal care, baby products).
  - R-05: Return quantity within delivered and unreturned bounds.
  - R-06: Duplicate active return request prevention.
  - R-07: Defective or damaged reason validation.
  - R-08: Evidence requirements for damaged, wrong_item, not_as_described (emits missing_slots=[evidence]).
  - R-09: Single-claim auto-approval threshold check against policy auto limit.
  - R-10: 30-day cumulative refund limit check.
  - R-11: High customer risk flag check.
  - R-12: Approver role determination (seller_staff up to 200,000 paise, seller_owner above, ESCALATE at 500,000 paise hard ceiling).
  - R-13: Risk indicator scoring flags.
  - R-14: COD refund method verification (emits missing_slots=[refund_method] if omitted).
  - R-15: Pre-dispatch cancellation check (denied if order shipped or delivered).
  - Enforced customer text safety: zero forbidden words (`limit`, `threshold`, `risk`, `score`, `staff`, `owner`, `auto`) in all customer-visible messages (KP-23).
- Implemented `policy/risk.py`: Spec §7.5 risk signal scoring:
  - `returns_30d >= 5` (+50) or `>= 3` (+30).
  - `refund_ratio_90d_bp > 4000` (+20).
  - `order_value_paise >= 200000` (+20).
  - `account_age_days < 14` (+15).
  - `suspected_injection` (+10).
  - Bands: low (< 30), medium (30-59), high (60+). Risk never causes DENY; high risk forces NEEDS_APPROVAL, medium sets recommendation_review flag.
- Implemented `policy/delay.py`: `assess_delay(shipment, now, policy) -> DelayAssessment(kind, days_late)` computing calendar-day delay in policy timezone (Asia/Kolkata), mapping under 2 days to `minor`, 2+ days to `options`, and carrier status `lost` to `lost`.
- Implemented `policy/decide.py`: `decide(facts, policy, now) -> Decision` evaluating all 15 rules, applying precedence `DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW`, calculating exact integer refund breakdown, assigning `required_approver_role`, setting `capped` and `recommendation_review` flags, and computing `facts_hash`.
- Created comprehensive test suite in `agent/tests/test_decide.py` (35 tests):
  - Scenarios SC-04 through SC-17 using real `FixtureStoreClient` data under policy v3 and v1.
  - Passing and failing coverage for every rule R-01 to R-15.
  - Customer-facing text safety verifying zero forbidden words across all rule results.
  - Precedence order verification.
  - Facts hash stability and reason text independence (property test).
  - Adversarial break-it tests: zero quantities, cross-order item IDs, high risk with tiny refund, and Hypothesis stateful fuzzing proving `decide()` never raises and remains 100% deterministic.
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_decide.py -v`: 35 passed in 0.50s.
- `cd agent; uv run ruff check .`: All checks passed!
- `cd agent; uv run mypy src`: Success: no issues found in 38 source files.
- `just agent-check`:
  - `agent-lint`: ruff check passed.
  - `agent-types`: mypy passed (38 source files).
  - `agent-test`: 178 passed in 34.83s.
  - `test-guard`: PASS: test-guard verified test suite integrity.
Follow-ups:
- Proceed to T1.4 (FactSheet and verifier).

## 2026-11-11 .2 Policy documents and refund calculator
Files: agent/src/sellershield_agent/policy/__init__.py, agent/src/sellershield_agent/policy/models.py, agent/src/sellershield_agent/policy/loader.py, agent/src/sellershield_agent/policy/summary.py, agent/src/sellershield_agent/policy/refund.py, agent/src/sellershield_agent/policy/versions/v1.json, agent/src/sellershield_agent/policy/versions/v2.json, agent/src/sellershield_agent/policy/versions/v3.json, agent/tests/test_policy.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `policy/models.py`: Immutable frozen Pydantic v2 models (`extra='forbid'`, `frozen=True`): `PolicyDocument`, `ReturnShippingPolicy`, `RiskPolicy`, `CustomerFacingText`, `RefundBreakdown`, and `ItemAmounts` with zero float fields and non-negative integer paise/basis points validation.
- Created `policy/versions/v1.json`, `v2.json`, `v3.json` matching Spec §7.2: v1 auto limit 100000 paise, v2 75000 paise, v3 50000 paise; all other fields strictly identical.
- Implemented `policy/loader.py`: `load_policy(version=None)` picking active version from `POLICY_ACTIVE_VERSION` (default `v3`), validating against `PolicyDocument`, and strictly rejecting unknown fields and negative values.
- Implemented `policy/summary.py`: `customer_safe_summary(policy)` returning `CustomerSafeSummary` with category return windows, shipping deduction/free rules, and refund timing only. Strictly excludes all forbidden words (`limit`, `threshold`, `risk`, `approval`, `score`, `staff`, `owner`, `auto`) to prevent prompt injection and policy boundary leakage (KP-23 & Amendment 6).
- Implemented `policy/refund.py`: Pure deterministic `compute_refund(order, requested_items, reason_code, resolution, policy, whole_order_returned, shipment_lost, max_refundable_paise) -> RefundBreakdown` with integer arithmetic (zero floats, zero round, zero Decimal division):
  - Per-unit allocation with remainder paise assigned to the first unit of each returned line.
  - Original shipping fee refunded only for whole-order seller-fault returns or lost shipments.
  - Return shipping fee (6000 paise) deducted only when reason is not free and resolution is refund.
  - Total clamped to never drop below zero.
  - Total capped at `max_refundable_paise` when specified.
  - Pure function with zero LLM calls and zero clock reading.
- Authored 20 comprehensive unit, boundary, adversarial, and property tests in `agent/tests/test_policy.py` covering all 4 worked examples (ORD-1042, ORD-2032, ORD-2031, ORD-2040), odd remainder paise, zero negative totals, whole vs partial shipping refunds, store credit fee waiver, Hypothesis properties, and loader/summary safety.
Verification run (commands and result summary):
- `git grep -n -E 'float\(|round\(' agent/src/sellershield_agent/policy`: 0 matches found.
- `cd agent; uv run pytest tests/test_policy.py`: 20 passed in 1.11s.
- `just agent-check`:
  - `agent-lint`: ruff check passed.
  - `agent-types`: mypy passed (33 source files).
  - `agent-test`: 143 passed in 34.75s.
  - `test-guard`: PASS: test-guard verified test suite integrity.
Follow-ups:
- Proceed to T1.3 (Rules, decision engine, risk indicators and scenario tests).

## 2026-11-11 .1 Domain models, StoreClient interface and fixture store
Files: scenarios/fixtures/store.json, agent/src/sellershield_agent/domain/__init__.py, agent/src/sellershield_agent/domain/models.py, agent/src/sellershield_agent/store/__init__.py, agent/src/sellershield_agent/store/client.py, agent/src/sellershield_agent/store/fixtures.py, agent/tests/test_fixtures.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `domain/models.py` (under 300 lines, frozen Pydantic v2 with extra='forbid'): `Order`, `OrderItem`, `Address`, `Shipment`, `ShipmentEvent`, `ReturnRequest` (`Return`), `ReturnItem`, `Refund`, `RefundableItem`, `Refundable`, `CustomerRiskFacts`, and enums `OrderStatus`, `PaymentMethod`, `PaymentStatus`, `ShipmentStatus`, `ReturnStatus`, `ReturnReasonCode`, `ReturnResolution`, `RefundStatus`, `RefundMethod`. All money fields end in `_paise` and are integer; datetimes validated as timezone-aware UTC; zero float fields exist.
- Implemented `store/client.py`: Asynchronous `StoreClient` protocol, `RequestContext` holding verified `Principal`, `request_id`, and masked `SecretStr` token, and uniform exception hierarchy (`StoreNotFound`, `StoreForbidden`, `StoreConflict`, `StoreUnavailable`, `StoreTimeout`, `StoreValidationError`).
- Implemented `store/fixtures.py` and `scenarios/fixtures/store.json`: In-memory `FixtureStoreClient` with 17 scenarios (SC-01 to SC-17), customers C001–C005, products P-101–P-302, 5 historic returns for C003, and ₹400 earlier refund on ORD-2040. Relative times resolved dynamically via injected `Clock` (KP-06). Strict order ID validation (`^ORD-[0-9]{3,6}$`) rejecting malformed IDs before accessing data. Uniform 404 behavior for unowned vs nonexistent orders (KP-15).
- Created `agent/tests/test_fixtures.py`: 10 comprehensive tests validating scenario orders, ownership uniformity, money invariants & coupon allocations, relative time resolution, zero floats, risk facts authorization, order ID validation, frozen immutability, and delivery timestamps.
Verification run (commands and result summary):
- `cd agent; uv run pytest tests/test_fixtures.py -v`: 10 passed in 0.22s.
- `cd agent; uv run ruff check .`: All checks passed!
- `cd agent; uv run mypy src`: Success: no issues found in 28 source files.
- `cd agent; uv run pytest -q`: 123 passed in 35.93s.
Follow-ups:
- Human freeze of new test and fixture files via `just freeze-tests`.
- Proceed to T1.2 (Store HTTP client and readiness probe).

## 2026-11-11 .0b Test guard
Files: scripts/test_guard.py, scripts/test_guard.toml, justfile, agent/pyproject.toml, tests/guard/test_guard.py, tests/FROZEN.sha256, tests/FROZEN.count, docs/TEST_CHANGES.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `scripts/test_guard.py` (under 400 lines, standard library only, Python 3.12 typed) with subcommands `check`, `freeze`, and `status`. Supports `--root PATH` for isolated integration testing and `--refreeze PATH --reason TEXT` with audit trail in `docs/TEST_CHANGES.md`.
- Implemented `scripts/test_guard.toml` configuring frozen globs (`agent/tests/**/*.py`, `tests/guard/**/*.py`, `scenarios/fixtures/**`, `scenarios/golden/**`, `scenarios/redteam/**`, `contracts/*.yaml`, `agent/src/sellershield_agent/policy/versions/*.json`, `scripts/test_guard.py`, `scripts/test_guard.toml`), manifest and count paths, and allowed markers (`live`, `store`, `asyncio`, `parametrize`, `usefixtures`, `filterwarnings`).
- Created manifest `tests/FROZEN.sha256` (18 files: Phase 0 tests, contracts, guard script & config, guard tests) and `tests/FROZEN.count` (113 collected tests).
- Updated `justfile` adding recipes `test-guard`, `freeze-tests`, `status-guard`, and structuring `agent-check` to run `ruff`, `mypy`, `pytest`, and `test-guard` in exact order with plain `uv run pytest -q`.
- Updated `agent/pyproject.toml` adding `../tests/guard` to `testpaths` and configuring `pythonpath` and `mypy_path`.
- Implemented 12 comprehensive integration tests in `tests/guard/test_guard.py` testing tampering failure modes against temporary projects.
- Conducted attack drill: comment modification on frozen test file fails; forbidden `-k` in addopts fails; `assert True` in unfrozen test file fails; all restored and verified.
Verification run (commands and result summary):
- `just agent-check`:
  - `agent-lint`: ruff check passed.
  - `agent-types`: mypy passed (23 files).
  - `agent-test`: 113 passed in 33.69s.
  - `test-guard`: PASS: test-guard verified test suite integrity.
- `just freeze-tests`: verified immutable refusal without `--refreeze` and `--reason`.
- `just test-guard`: PASS.
Follow-ups:
- Proceed to T1.1 (Domain models, StoreClient interface and fixture store).

## 2026-11-11 .10 LLM adapter and provider chain
=======
<<<<<<< HEAD
## 2026-11-11 T0.10 LLM adapter and provider chain
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: agent/src/sellershield_agent/llm/__init__.py, agent/src/sellershield_agent/llm/config.py, agent/src/sellershield_agent/llm/client.py, agent/src/sellershield_agent/llm/cache.py, agent/src/sellershield_agent/llm/chain.py, agent/src/sellershield_agent/llm/structured.py, agent/scripts/llm_smoke.py, agent/tests/test_llm.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/PITFALLS.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `llm/config.py`: `ProviderConfig` and `ProviderCapabilities` (`supports_json_schema`, `supports_tools`, `parallel_tools`, all defaulting to `False` for unknown providers) with automatic capability detection for Gemini, Groq, OpenRouter, and OpenAI, and `load_providers_from_settings()` building providers from environment (`LLM_PRIMARY_*`, `LLM_SECONDARY_*`, `LLM_TERTIARY_*`).
- Implemented `llm/client.py`: Async wrapper over `openai.AsyncOpenAI(base_url, api_key, timeout=12, max_retries=0)` supporting explicit `httpx.AsyncClient` injection for zero-network testing with respx. Maps 401/403 to `ConfigError` naming provider (never the key) and 404 to clear model-not-found `ConfigError`.
- Implemented `llm/cache.py`: Canonical SHA-256 JSON key hashing across `(provider, model, messages, tools, schema)` with persistent storage under `agent/.llm_cache/` (gitignored), supporting `off`, `record`, and `replay` modes. Atomic file writes and strict `CacheMiss` exception on replay misses.
- Implemented `llm/chain.py`: Multi-provider failover chain trying primary, secondary, and tertiary on timeout, 429, 5xx, or connection errors. In-memory `CircuitBreaker` using domain `Clock` (KP-06) that trips after 3 consecutive failures within 60s, skips provider for 60s cooldown, then enters half-open state. Safe structlog logging emitting `provider`, `model`, `latency_ms`, `cached`, `attempt` without prompts or keys.
- Implemented `llm/structured.py`: `generate_structured[T: BaseModel]` leveraging JSON schema mode if supported or fallback to JSON object mode, Pydantic validation, single automatic retry with validation error feedback, and `StructuredOutputError` on double failure.
- Implemented `scripts/llm_smoke.py`: Executable verification script requesting flat Pydantic model `{answer: str, confidence: float}` for "Reply with the word ready", printing latencies and results without printing secrets, exiting 1 if no provider works.
- Authored 12 offline tests in `agent/tests/test_llm.py` mocking providers with respx (0 network calls).
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 97 passed in 3.50s.
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 23 source files.
- Accidental secrets check: `git grep -n -E 'AIza|gsk_'` returned 0 results.
Follow-ups:
- Human verification of `scripts/llm_smoke.py` with real keys in `agent/.env`.
- Proceed to T0.11 (Gate G0, local commit and tag).

<<<<<<< HEAD
## 2026-11-11 .9 JWT and JWKS verification
=======
## 2026-11-11 T0.9 JWT and JWKS verification
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: agent/src/sellershield_agent/auth/__init__.py, agent/src/sellershield_agent/auth/models.py, agent/src/sellershield_agent/auth/jwks.py, agent/src/sellershield_agent/auth/verify.py, agent/src/sellershield_agent/auth/deps.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/api/health.py, agent/tests/test_auth.py, agent/tests/test_health.py, agent/tests/test_money.py, agent/tests/conftest.py, docs/CHANGELOG.md, docs/FEATURES.md, docs/PITFALLS.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `auth/models.py` with `Role` StrEnum (`customer`, `seller_staff`, `seller_owner`, `service`) and immutable `Principal` model (`user_id`, `role`, `customer_id`, `scopes`, `auth_time`, `jti`, `token_exp`).
- Implemented `auth/jwks.py` `JWKSClient` with lazy fetch on first use, 1-hour in-memory cache, unknown `kid` refresh rate limit of at most once per 30 seconds, 3 retries with backoff, retention of last known good keys if a refetch fails, `follow_redirects=False` with strict timeouts, and rejection of plain HTTP unless `DEV_MODE=True`.
- Implemented `auth/verify.py` `verify_token` enforcing RS256 only (rejecting `none`, `HS256`, algorithm confusion), non-empty `kid`, claims validation (`sub`, `role`, `iss`, `exp`, `iat`, `jti`, `aud` containing `sellershield-agent`), evaluation of time claims against domain `Clock` with 30s leeway (KP-06), mandatory `customer_id` for customer role, and redacting raw tokens from all logs and error envelopes.
- Implemented `auth/deps.py` with FastAPI dependencies `get_principal`, `require_customer`, `require_seller`, `require_owner`, and `require_fresh_auth(max_age_seconds=900)` answering 401 with `step_up_required` for stale authentication.
- Updated `errors.py` with `AppHTTPException` carrying explicit error code and details, and default 401 mapping to `unauthenticated`.
- Updated `config.py` with `JWT_ISSUER: str = "nestkart-store"`.
- Updated `api/health.py` `/readyz` probe to check JWKS readiness and return 200 `status="degraded"` when store JWKS is down, preventing startup crashes.
- Authored 24 comprehensive security, rotation, outage, and adversarial break-it tests in `agent/tests/test_auth.py` with all RSA keys generated exclusively in memory.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 85 passed in 2.92s.
  - `cd agent; uv run ruff check .`: All checks passed!
  - `cd agent; uv run mypy src`: Success: no issues found in 17 source files.
- `cd agent; uv run pytest tests/test_auth.py -q`: 24 passed in 1.69s.
- Accidental secrets check: `git grep -n -E 'BEGIN (RSA )?PRIVATE KEY'` returned 0 results.
Follow-ups:
- Proceed to T0.10 (LLM adapter and provider chain).

<<<<<<< HEAD
## 2026-11-11  Database, clock, money and time helpers
=======
## 2026-11-11 T0.8 Database, clock, money and time helpers
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: agent/src/sellershield_agent/db.py, agent/src/sellershield_agent/clock.py, agent/src/sellershield_agent/money.py, agent/src/sellershield_agent/timeutil.py, agent/tests/test_db.py, agent/tests/test_money.py, agent/tests/test_timeutil.py, README.md, docs/CHANGELOG.md, docs/FEATURES.md, docs/DECISIONS.md, docs/PITFALLS.md, docs/TASKS.md, docs/STATE.md
What changed:
- Implemented `db.py` SQLite engine factory with Section 16 recipe: `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `isolation_level=None` on connect, and `BEGIN IMMEDIATE` on begin. Added `write_tx()` and `read_tx()` context managers (with docstring prohibiting transaction holds across awaits/HTTP) and idempotent `init_db()` creating `_meta` table.
- Implemented `clock.py` with `Clock` protocol, `SystemClock` (aware UTC), `FixedClock` (settable, rejecting naive datetimes), and `StoreClock` stub.
- Implemented `money.py` with `Paise = int`, `format_inr()` (integer math only: whole rupees without decimals, fractional with two decimals and zero padding), `parse_rupees()` (Decimal-based, rejecting float and >2 decimals), and `allocate_largest_remainder()` (Hamilton method with stable tie-breaking).
- Implemented `timeutil.py` with `to_ist()`, `ist_day_end()` (calculates 23:59:59.999999 Asia/Kolkata date + days as aware UTC), and `is_within_window()` with strict rejection of naive datetimes.
- Authored 41 comprehensive unit, concurrency, and property tests (Hypothesis) covering WAL pragmas, thread concurrency under busy_timeout, transaction rollback, uncommitted write isolation, exact rupee allocation, leap day / midnight boundaries, and tzdata loading.
- Documented integer paise and IST boundary guarantees in root `README.md`.
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 60 passed in 1.15s.
  - `cd agent; uv run ruff check .`: All checks passed.
  - `cd agent; uv run mypy src`: Success: no issues found in 12 source files.
- `cd agent; uv run pytest tests/test_db.py tests/test_money.py tests/test_timeutil.py -v`: 41 passed in 1.52s.
Follow-ups:
- Proceed to T0.9 (JWT and JWKS verification).

<<<<<<< HEAD
## 2026-11-11 .7 Agent service skeleton
=======
## 2026-11-11 T0.7 Agent service skeleton
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: agent/pyproject.toml, agent/.env.example, agent/src/sellershield_agent/__init__.py, agent/src/sellershield_agent/config.py, agent/src/sellershield_agent/logging.py, agent/src/sellershield_agent/middleware.py, agent/src/sellershield_agent/errors.py, agent/src/sellershield_agent/api/__init__.py, agent/src/sellershield_agent/api/health.py, agent/src/sellershield_agent/main.py, agent/tests/__init__.py, agent/tests/conftest.py, agent/tests/test_config.py, agent/tests/test_cors.py, agent/tests/test_errors.py, agent/tests/test_health.py, agent/tests/test_logging.py, agent/tests/test_request_id.py, docs/FEATURES.md, docs/DECISIONS.md, docs/CHANGELOG.md, docs/TASKS.md, docs/STATE.md
What changed:
- Initialized `agent/` as a uv package with Python >=3.12, pinned dependencies from 02-agent.md Section 0A, and configured strict tooling (`mypy strict`, `ruff`, `pytest-asyncio`).
- Implemented `config.py` with Pydantic Settings reading `agent/.env`, using `SecretStr` for credentials, and a fail-fast loader that reports missing/invalid variable names only without leaking secret values or inputs.
- Implemented `logging.py` with Structlog JSON output, recursive redaction of sensitive keys (`authorization`, `api_key`, `token`, `access_token`, `password`, `secret`, `cookie`), email pattern masking (`[REDACTED_EMAIL]`), and request_id context binding.
- Implemented `middleware.py` with `RequestIDMiddleware` (enforces `^[A-Za-z0-9-]{8,64}$`, echoes header), `AccessLogMiddleware` (single structured access line with latency_ms), and `BodyLimitMiddleware` (pure ASGI middleware enforcing 1 MB limit returning 413 envelope).
- Implemented `errors.py` with standard error envelopes (`{error: {code, message, request_id, details}}`) for HTTP exceptions, sanitized request validation errors, and safe 500 internal errors suppressing tracebacks.
- Implemented `api/health.py` with GET `/healthz` (always 200 ok) and GET `/readyz` (checks settings availability).
- Implemented `main.py` with `create_app()` factory, CORS disabled by default, and interactive OpenAPI documentation gated behind `DEV_MODE=True`.
- Authored test suite in `agent/tests/` covering all 9 required specs and break-it cases (19 tests).
Verification run (commands and result summary):
- `just agent-check`:
  - `cd agent; uv run pytest -q`: 19 passed in 0.31s.
  - `cd agent; uv run ruff check .`: All checks passed.
  - `cd agent; uv run mypy src`: Success: no issues found in 8 source files.
- Live server test on port 8001:
  - `Invoke-RestMethod http://localhost:8001/healthz`: `{"status":"ok","timestamp":"2026-11-07T11:18:07.581236+00:00"}`
  - `Invoke-RestMethod http://localhost:8001/nope`: `{"error":{"code":"not_found","message":"Not Found","request_id":"req-9005c5e6c63940d3","details":{}}}`
Follow-ups:
- Proceed to T0.8 (Database, clock, money and time helpers).

<<<<<<< HEAD
## 2026-11-11 .6b Agent OpenAPI contract (milestone M0)
=======
## 2026-11-11 T0.6b Agent OpenAPI contract (milestone M0)
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: contracts/openapi-agent.yaml, docs/spec/CONTRACT_COVERAGE.md, scripts/contract_check.py, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Authored contracts/openapi-agent.yaml (OpenAPI 3.1) with 48 operations across 8 tags.
- Defined chat SSE stream (x-sse-events) covering message.delta, tool.status, cards (order_summary, shipment_timeline, return_preview, confirmation, case_created, chips), final, and error.
- Defined push notifications stream GET /v1/chat/stream and polling fallback GET /v1/me/events?since=.
- Defined seller approvals workflow with 401 step_up_required (15-min auth_time check).
- Defined case brief retrieval, seller inbox management, and agent handbacks.
- Enforced money safety: all money fields end in _paise and are type integer.
- Enforced idempotency: all mutating POST operations require Idempotency-Key header.
- Updated docs/spec/CONTRACT_COVERAGE.md mapping all Section 11 and Section 21 amendments (0 missing).
- Milestone M0 contract freeze complete across Store and Agent services.
Verification run (commands and result summary):
- `uvx openapi-spec-validator contracts/openapi-agent.yaml`: OK.
- `uv run --with pyyaml python scripts/contract_check.py contracts/openapi-agent.yaml`: Total operations 48, ALL CHECKS PASSED.
- `(Select-String -Path contracts/openapi-agent.yaml -Pattern 'operationId:').Count`: 48.
- `Select-String -Path docs/spec/CONTRACT_COVERAGE.md -Pattern 'MISSING'`: 0 occurrences.
Follow-ups:
- Notify W1 and W2 that contracts/ is frozen (Milestone M0).
- Proceed to T0.7 (Agent service skeleton).

<<<<<<< HEAD
## 2026-11-11 .6a Store OpenAPI contract (milestone M0)
=======
## 2026-11-11 T0.6a Store OpenAPI contract (milestone M0)
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: contracts/openapi-store.yaml, docs/spec/CONTRACT_COVERAGE.md, scripts/contract_check.py, docs/FEATURES.md, docs/DECISIONS.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Authored contracts/openapi-store.yaml (OpenAPI 3.1) with 49 operations across 8 tags and 7 webhook outbox definitions.
- Enforced money safety: all money attributes end in _paise and are type integer.
- Enforced idempotency: all mutating POST operations require Idempotency-Key header.
- Enforced data isolation: uniform 404 for unowned orders; additionalProperties: false on all request bodies.
- Enforced concurrency control: If-Match version tag on admin settings updates.
- Created docs/spec/CONTRACT_COVERAGE.md mapping all Section 6 and Section 16 amendments (0 missing).
- Created scripts/contract_check.py verifying AST invariants, operationIds, security, idempotency, and error envelopes.
Verification run (commands and result summary):
- `uvx openapi-spec-validator contracts/openapi-store.yaml`: OK (schema valid OpenAPI 3.1).
- `uv run --with pyyaml python scripts/contract_check.py contracts/openapi-store.yaml`: Total operations 49, ALL CHECKS PASSED.
- `(Select-String -Path contracts/openapi-store.yaml -Pattern 'operationId:').Count`: 49.
- `Select-String -Path docs/spec/CONTRACT_COVERAGE.md -Pattern 'MISSING'`: 0 occurrences.
Follow-ups:
- Implement Prompt 6b (TASK T0.6b - Agent OpenAPI contract).

<<<<<<< HEAD
## 2026-11-11  Repository skeleton and tooling
=======
## 2026-11-11 T0.4 Repository skeleton and tooling
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Files: .gitignore, .gitattributes, .editorconfig, justfile, .env.example, README.md, agent/README.md, store-api/README.md, store-web/README.md, contracts/README.md, scenarios/golden/README.md, scripts/README.md, docs/spec/README.md, docs/TASKS.md, docs/STATE.md, docs/CHANGELOG.md
What changed:
- Created .gitignore with protections for .env, sqlite DBs, caches, virtual environments, and node_modules.
- Created .gitattributes with `* text=auto` and .editorconfig with multi-language spacing rules.
- Created justfile with powershell windows-shell and recipes for agent checks, secrets scanning, and workspace stubs.
- Created .env.example listing all 21 agent configuration variables from docs/spec/02-agent.md Section 0A with 'replace-me'.
- Created root README.md with project summary, status, quickstart stubs, and build timeline disclosure.
- Created skeleton folders with ownership READMEs: agent/ (A), store-api/ (W1), store-web/ (W2), contracts/ (A), scenarios/golden/ (A & team), scripts/ (All), docs/spec/ (All).
Verification run (commands and result summary):
- `(Get-Item AGENTS.md).Length`: 6545 bytes (under 12000 character limit).
- `git status`: Successfully lists all newly added skeleton files as untracked.
- `Set-Content -Path .env -Value 'TEST=1' ; git status ; Remove-Item .env`: Verified that .env is ignored and excluded by git status.
Follow-ups:
- Human toolchain installation for `just` and `gitleaks` (T0.1).
<<<<<<< HEAD
=======
=======
(none yet)
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
