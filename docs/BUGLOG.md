# BUGLOG (debug record; search it before fixing anything)

Entry format:
## BUG-001 Short title   [OPEN | FIXED]   Date: YYYY-MM-DD   Task: T0.x
Symptom (what the human saw, exact error text):
Reproduction (command or test):
Root cause (evidence, not guesses):
Fix (what changed, file and function):
Regression test (path::name):
Do NOT (what must never be reintroduced):
Recurrences: 0
Files touched: path1, path2   <- agents search by file name

Entries (newest first):

## BUG-019 Pre-dispatch order cancellation stubbed in pipeline as unavailable   [OPEN]   Date: 2026-11-11   Task: BT-1
Symptom (what the human saw, exact error text):
Customer typing `"cancel ORD-4021"` received text: `"Order cancellation is not available in this build. If your order has arrived, return options may apply."` instead of cancellation proposal.
Reproduction (command or test):
Browser acceptance test 3.8 as Meera Iyer (`ORD-4021`).
Root cause (evidence, not guesses):
In `agent/src/sellershield_agent/orchestrator/pipeline.py:660-690`, `Intent.CANCELLATION` was placed inside the unsupported scope block which returned a static unavailable template and denied tool resolution.
Fix (what changed, file and function):
Pending implementation in Phase C.
Regression test (path::name):
Pending.
Do NOT (what must never be reintroduced):
Do not stub `Intent.CANCELLATION` for placed orders that have not yet been dispatched.
Recurrences: 0
Files touched: agent/src/sellershield_agent/orchestrator/pipeline.py

## BUG-018 Return proposal defaults to line total quantity instead of requested partial quantity   [OPEN]   Date: 2026-11-11   Task: BT-1
Symptom (what the human saw, exact error text):
Customer typing `"I want to return one kurta in ORD-2031"` received a proposal card for `Cotton Kurta (×2)` for `₹1,257.98` instead of 1 kurta (~`₹598.99`).
Reproduction (command or test):
Browser acceptance test 3.4 as Diya Nair (`ORD-2031`).
Root cause (evidence, not guesses):
Quantity extraction in `router.py` and `resolver.py` does not parse word numerals ("one", "single", "1") from message text when building item selections for `propose_return`, falling back to full line item quantity `oi.qty`.
Fix (what changed, file and function):
Pending implementation in Phase C.
Regression test (path::name):
Pending.
Do NOT (what must never be reintroduced):
Do not default to full line item quantity when customer explicitly specifies a partial quantity.
Recurrences: 0
Files touched: agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/orchestrator/resolver.py

## BUG-017 Saga transition_return fails with 403 Forbidden during auto-approval execution   [FIXED]   Date: 2026-11-11   Task: BT-1
Symptom (what the human saw, exact error text):
Proposal `PRP-f640cda5` (`ORD-2032`) had `decision='allow'` and customer confirmed, but return claim remained in `requested` status (`Waiting for seller review`) in the seller's manual queue. Execution audit log recorded `execution_step_failed: error_code="forbidden", step="transition_return"`.
Reproduction (command or test):
Browser acceptance test 3.1 as Diya Nair (`ORD-2032`).
Root cause (evidence, not guesses):
When `store.transition_return` is invoked by `saga.py` with `svc_ctx`, `HttpStoreClient._get_service_token("executor")` is called. In `agent/src/sellershield_agent/config.py`, `STORE_EXECUTOR_CLIENT_ID`, `STORE_EXECUTOR_CLIENT_SECRET`, `STORE_MONITOR_CLIENT_ID`, `STORE_MONITOR_CLIENT_SECRET`, and `WEBHOOK_SECRET` defaulted to `None`. In HTTP mode, `_get_service_token` failed to authenticate with `store-api`, which requires client_id `agent-executor` with `returns:transition` scope.
Fix (what changed, file and function):
In `agent/src/sellershield_agent/config.py`: configured default values matching store-api dev seed: `STORE_EXECUTOR_CLIENT_ID="agent-executor"`, `STORE_EXECUTOR_CLIENT_SECRET=SecretStr("fake_executor_secret_key_12345")`, `STORE_MONITOR_CLIENT_ID="agent-monitor"`, `STORE_MONITOR_CLIENT_SECRET=SecretStr("fake_monitor_secret_key_67890")`, and `WEBHOOK_SECRET=SecretStr("fake_dev_webhook_secret_do_not_use_in_prod")`.
Regression test (path::name):
agent/tests/test_store_http_phase3.py::test_executor_service_token_transition_return
agent/tests/test_config.py::test_default_service_credentials
Do NOT (what must never be reintroduced):
Do not leave executor or monitor service credentials unset in Settings defaults.
Recurrences: 0
Files touched: agent/src/sellershield_agent/config.py

## BUG-016 Order listing and unspecific where-is-order queries fall back to arbitrary ORD-1051   [OPEN]   Date: 2026-11-11   Task: BT-1
Symptom (what the human saw, exact error text):
`"show my orders"` or `"where is my order"` (with no ID) outputs `"Order ORD-1051 status is placed."` instead of rendering a list/chips of orders.
Reproduction (command or test):
Browser acceptance tests 1.2 and 1.3 as Aarav Mehta.
Root cause (evidence, not guesses):
In `pipeline.py`, when `list_my_orders` runs, `sheet.orders` is populated, but `draft_text = template_orders_list(sheet, "en")` fallback handling in `pipeline.py:951` falls back to `template_shipping_status(sheet, "en")`, which takes `next(iter(sheet.allowed_ids))` (`ORD-1051`). Furthermore, unspecific `"where is my order"` query does not provide chips prompting the user to select an order.
Fix (what changed, file and function):
Pending implementation in Phase C.
Regression test (path::name):
Pending.
Do NOT (what must never be reintroduced):
Do not use `next(iter(sheet.allowed_ids))` as a fallback order ID when multiple orders exist and none was specified.
Recurrences: 0
Files touched: agent/src/sellershield_agent/orchestrator/pipeline.py, agent/src/sellershield_agent/compose/templates.py

## BUG-015 Router regex misses typos and pipeline loses active order context across conversation turns   [OPEN]   Date: 2026-11-11   Task: BT-1
Symptom (what the human saw, exact error text):
1. `"wher is ordr 1048 plz"` resulted in `"Order ORD-1051 status is placed."`
2. Follow-up query `"when will it arrive?"` immediately after asking about `ORD-1048` lost context and responded with `ORD-1051`.
Reproduction (command or test):
Browser acceptance tests 1.5 and 1.6 as Aarav Mehta.
Root cause (evidence, not guesses):
1. In `understand/router.py`, `ORD_FLEXIBLE_REGEX` only matches `(?:ORD|order)` with strict space/hyphen/hash, failing on typos like `ordr`, `odr`.
2. In `pipeline.py`, session state does not retrieve previously referenced active `order_id` from preceding turns when the user asks an elliptical follow-up question.
Fix (what changed, file and function):
Pending implementation in Phase C.
Regression test (path::name):
Pending.
Do NOT (what must never be reintroduced):
Do not discard previous turn order context in multi-turn conversations.
Recurrences: 0
Files touched: agent/src/sellershield_agent/understand/router.py, agent/src/sellershield_agent/orchestrator/pipeline.py

## BUG-014 Endpoint readyz un-injected JWKS client causing 16s timeout and outbox race condition   [FIXED]   Date: 2026-11-11   Task: T3.6
Symptom (what the human saw, exact error text):
`AssertionError: assert 'sending' == 'failed'` in `test_audit_fail_blocks_mutation_t20_mail_down_and_clears_on_reset` during full suite runs, accompanied by multiple 16.4-second JWKS fetch timeout warnings: `[WinError 10061] No connection could be made because the target machine actively refused it`.
Reproduction (command or test):
`uv run pytest tests/test_t35_overview_dev_demo.py -k test_audit_fail_blocks_mutation_t20_mail_down_and_clears_on_reset`
Root cause (evidence, not guesses):
`readyz` route in `api/health.py` called `deps.get_jwks_client()` directly instead of declaring `Depends(deps.get_jwks_client)`. Consequently, test dependency overrides (`app.dependency_overrides[deps.get_jwks_client]`) did not apply to `/readyz`. The route invoked the default JWKS client targeting `http://localhost:8000`, which retried 3 times with 16.4s latency. During this 16.4s delay, the background lifespan outbox worker loop woke up (2s sleep), claimed the newly enqueued outbox notification into `sending` state, and caused a race condition with the test's synchronous `process_outbox_batch` call.
Fix (what changed, file and function):
In `agent/src/sellershield_agent/api/health.py:readyz`, injected `jwks_client: Annotated[Any, Depends(deps.get_jwks_client)] = None`. When dependency overrides are active in tests, FastAPI supplies the test's `_FakeJWKSClient` instantly (0.45s runtime); when un-overridden in production, it falls back to `deps.get_jwks_client()`.
Regression test (path::name):
agent/tests/test_t35_overview_dev_demo.py::test_audit_fail_blocks_mutation_t20_mail_down_and_clears_on_reset
agent/tests/test_health.py::test_readyz_endpoint
Do NOT (what must never be reintroduced):
Do not call dependency provider functions directly within route handler bodies when they should be resolved via FastAPI `Depends()`.
Recurrences: 0
Files touched: agent/src/sellershield_agent/api/health.py

## BUG-013 Fixture store shipment ETA offset resolution and monitor duplicate handling   [FIXED]   Date: 2026-11-11   Task: T3.4
Symptom (what the human saw, exact error text):
1. `AssertionError: assert len(obx_2037) == 1 (0 == 1)` because `dedupe_key` had current date instead of fixture ETA date `2026-11-04`.
2. `TypeError: HttpStoreClient._get_service_token() got an unexpected keyword argument 'service_role'` during HTTP shipment listing.
3. `KeyError: 'to'` when verifying `store.mailbox` after outbox batch processing in `test_ord1070_lost_shipment_creates_single_case_and_brief`.
4. `AssertionError: assert 0 >= 1 where 0 = dup_res.skipped_duplicates` when duplicate shipment rows exist in the same store response.
Reproduction (command or test):
`uv run pytest tests/test_monitor.py`
Root cause (evidence, not guesses):
1. `scenarios/fixtures/store.json` shipments specify `eta_at_offset` (e.g. `"T0-4d"`) rather than `eta_at`. `list_shipments_for_monitor` only checked `s.get("eta_at")`, evaluating to `None` and defaulting to today's date. Furthermore, fast-forwarding the simulated clock re-evaluated relative offsets against the fast-forwarded time rather than the simulation baseline `T0`.
2. `HttpStoreClient._get_service_token` declared parameter `role: str = "monitor"`, but caller passed `service_role="monitor"`.
3. `FixtureStoreClient.send_mail` stored `{"recipient": recipient}` in mailbox records; test asserted on `mail["to"]`.
4. Duplicate shipments were discarded before entering the scan loop, preventing `skipped_duplicates` from incrementing on duplicate records within a page.
Fix (what changed, file and function):
1. In `store/fixtures.py`, recorded baseline `self._t0 = self._clock.now()` on initialization/reset; resolved `s.get("eta_at") or s.get("eta_at_offset")` relative to `self._t0`.
2. In `store/http_client.py:list_shipments_for_monitor`, passed `role="monitor"`.
3. In `store/fixtures.py:send_mail`, added `"to": recipient` alongside `"recipient": recipient`.
4. In `monitor/service.py:run_monitor`, tracked `processed_keys_in_run` inside `write_tx` across the full batch up to 200 shipments, properly recording `skipped_duplicates += 1`.
Regression test (path::name):
agent/tests/test_monitor.py::test_t18_ord2037_flagged_once_and_second_run_creates_nothing
agent/tests/test_monitor.py::test_ord1070_lost_shipment_creates_single_case_and_brief
agent/tests/test_monitor.py::test_ontime_minor_delays_and_no_customer_token_enforced
agent/tests/test_monitor.py::test_seven_day_fast_forward_flags_newly_late_and_opens_exception_case_without_duplicates
Do NOT (what must never be reintroduced):
Do not re-evaluate fixture `T0` relative offsets against a moving simulated clock during time-travel runs; anchor offsets to the baseline `T0`.
Recurrences: 0
Files touched: agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/store/http_client.py, agent/src/sellershield_agent/monitor/service.py

## BUG-012 Fixture shipment customer resolution and dev JWKS signing key async mismatch   [FIXED]   Date: 2026-11-11   Task: T3.5
Symptom (what the human saw, exact error text):
1. `ValidationError: 1 validation error for MonitorShipment customer_id Field required [type=missing]` during proactive delay monitor scans on fixture store data.
2. `RuntimeWarning: coroutine '_FakeJWKSClient.get_signing_key' was never awaited` and `AttributeError: 'coroutine' object has no attribute 'key'` resulting in 401 unauthenticated on dev endpoints in standalone scripts.
Reproduction (command or test):
`uv run pytest tests/test_t35_overview_dev_demo.py`
Root cause (evidence, not guesses):
1. `scenarios/seed/shipments.json` does not store `customer_id` directly on shipment records; customer ownership lives on the corresponding order records in `scenarios/seed/orders.json`.
2. PyJWT's `jwt.decode(..., key=jwks_client.get_signing_key(kid).key)` expects a synchronous `PyJWK` object return rather than an unawaited async coroutine.
Fix (what changed, file and function):
1. In `sellershield_agent/store/fixtures.py:list_shipments_for_monitor`, resolved `customer_id` from orders mapping before creating `MonitorShipment` models, and made `customer_id: str = ""` default in `MonitorShipment` (`domain/models.py`).
2. In `scripts/demo_run.py`, implemented synchronous `get_signing_key` on `_FakeJWKSClient` returning `jwt.PyJWK.from_dict(jwk)`.
Regression test (path::name):
agent/tests/test_t35_overview_dev_demo.py::test_end_to_end_four_beats_on_fixture_store
Do NOT (what must never be reintroduced):
Do not assume `shipments.json` contains `customer_id`, and do not make `_FakeJWKSClient.get_signing_key` an async coroutine.
Recurrences: 0
Files touched: agent/src/sellershield_agent/store/fixtures.py, agent/src/sellershield_agent/domain/models.py, scripts/demo_run.py

## BUG-011 Shadowed seller notification stub routes and ASGI SSE test hang   [FIXED]   Date: 2026-11-11   Task: T3.2
Symptom (what the human saw, exact error text):
1. `GET /v1/seller/notifications` and `POST /v1/seller/notifications/{id}/retry` returned HTTP `501 Not Implemented` even after registering `outbox_router`.
2. SSE streaming tests using `httpx.ASGITransport(app=app)` blocked indefinitely or failed socketpairs inside the Windows sandbox (`KP-48`), and initial `test_every_template_passes_forbidden_words` / `test_proposal_expired_and_refund_updated_emit_events` fixture setup checks failed on concatenated subject characters (`CASE-1Bccevilattackertest`) and hash-invalidated `expires_at` SQL updates.
Reproduction (command or test):
`uv --directory agent run pytest tests/test_push_outbox.py -v`
Root cause (evidence, not guesses):
1. `api/seller_stub.py` still defined `GET /seller/notifications` and `POST /seller/notifications/{id}/retry` 501 stubs while registered alongside `outbox_router`.
2. `httpx.ASGITransport` buffers streaming async body responses without yielding partial SSE chunks properly across teardown; additionally, `_sanitize_subject_id` stripped punctuation rather than truncating injection suffixes after CR/LF/spaces, and mutating `proposals.expires_at` in SQL without advancing `Clock` broke `proposal_hash` before expiry checks ran.
Fix (what changed, file and function):
1. Removed the `/notifications*` 501 stubs from `agent/src/sellershield_agent/api/seller_stub.py` and mounted `events_router` / `outbox_router` ahead of `seller_router` in `agent/src/sellershield_agent/main.py`.
2. Replaced `ASGITransport` SSE listeners in `agent/tests/test_push_outbox.py` with an ephemeral loopback `uvicorn` `live_url` fixture (`BypassSandbox: true`), updated `_sanitize_subject_id` in `agent/src/sellershield_agent/outbox/templates.py` to stop at CR/LF and non-ID delimiters, and advanced `FixedClock` in Test 14 to trigger real expiry.
Regression test (path::name):
agent/tests/test_push_outbox.py::test_seller_approval_reaches_customer_stream_within_2_seconds
Do NOT (what must never be reintroduced):
Do not keep shadowed 501 routes in `seller_stub.py` or test infinite SSE streams over `httpx.ASGITransport`.
Recurrences: 0
Files touched: agent/src/sellershield_agent/api/seller_stub.py, agent/src/sellershield_agent/main.py, agent/src/sellershield_agent/outbox/templates.py, agent/tests/test_push_outbox.py

## BUG-010 T2.11 Step A red-team test fixture schema and loopback socket alignment   [FIXED]   Date: 2026-11-11   Task: T2.11
Symptom (what the human saw, exact error text):
1. `CompileError: Unconsumed column names: customer_id, customer_reason` when inserting test fixtures into `approvals` table.
2. `AssertionError: assert res.data.get("type") == "confirmation"` due to `OrderItemInput` requiring `qty`, not `quantity`.
3. Multi-threaded test runner socketpair hang on Windows standard sandbox.
Reproduction (command or test):
`uv run pytest tests/redteam/test_money_attacks.py tests/redteam/test_money_races.py`
Root cause (evidence, not guesses):
1. The `approvals` table in `sellershield_agent.domain.state` stores customer reason text within the associated `proposals.payload_json["reason_text"]` rather than a direct column on `approvals`.
2. `OrderItemInput` uses Pydantic V2 `extra="forbid"` and requires `qty: int`.
3. Windows sandboxed loopback socketpairs hang during concurrent `TestClient` thread execution (KP-48).
Fix (what changed, file and function):
1. Aligned test helpers in `agent/tests/redteam/money_support.py` and `test_money_attacks.py` to insert valid `approvals` columns (`approval_id`, `proposal_id`, `order_id`, `amount_paise`, `status`, `policy_version`, `required_role`, `store_id`, `facts_hash`, `due_at`, `expires_at`, `version`) and store customer text in `proposals`.
2. Passed `qty` in `OrderItemInput` arguments.
3. Executed pytest commands with BypassSandbox where concurrent Starlette TestClient loopback sockets are opened.
Regression test (path::name):
agent/tests/redteam/test_money_attacks.py::test_seller_staff_cannot_approve_above_200000_paise
Do NOT (what must never be reintroduced):
Do not bypass schema definitions in test fixtures or use non-existent column names.
Recurrences: 0
Files touched: agent/tests/redteam/money_support.py, agent/tests/redteam/test_money_attacks.py, agent/tests/redteam/test_money_races.py

## BUG-009 Windows Smart App Control (SAC) blocking unsigned .pyd files in agent .venv   [FIXED]   Date: 2026-11-11   Task: T2.9
Symptom (what the human saw, exact error text):
ImportError: DLL load failed while importing _util_cy / _native: An operation did not complete successfully because the file contains a virus or potentially unwanted software (Windows SAC block on unsigned third-party C-extensions).
Reproduction (command or test):
uv run pytest in agent on Windows with Smart App Control enabled.
Root cause (evidence, not guesses):
Windows 11 Smart App Control blocks unsigned binary DLLs/native extensions (*.pyd) from non-whitelisted paths. SQLAlchemy and Hypothesis include optional native accelerators that triggered SAC.
Fix (what changed, file and function):
Disabled unsigned C-extensions by renaming them to *.pyd.disabled. SQLAlchemy automatically falls back to pure-Python implementations. For hypothesis._native, implemented pure-Python IEEE-754 conversion fallbacks and renamed the .pyd. Set UV_NO_SYNC=1 in test commands to prevent uv from overwriting pure-Python fallbacks.
Regression test (path::name):
agent/tests/test_store_http_writes.py::test_create_return_contract_headers_and_payload
Do NOT (what must never be reintroduced):
Do not re-enable or rely on unsigned compiled .pyd binaries on Windows with SAC.
Recurrences: 0
Files touched: agent/.venv/Lib/site-packages/sqlalchemy/*, agent/.venv/Lib/site-packages/hypothesis/_native/*

## BUG-008 T2.8 Step A Trace and Audit View Test Mismatches   [FIXED]   Date: 2026-11-11   Task: T2.8
Symptom (what the human saw, exact error text):
8 test failures in unfrozen agent/tests/test_audit_*.py: ValueError: 'policy_decision' is not a valid EventType and assert 501 == 200 on GET /v1/seller/traces/{req_id}.
Reproduction (command or test):
uv --directory agent run pytest tests/test_audit_mutators.py tests/test_audit_views.py tests/test_audit_views_trace.py
Root cause (evidence, not guesses):
Step A tests for T2.8 were drafted in advance per AGENTS.md §8 (test integrity). The corresponding endpoints (traces, audit log filters) and EventType values had not yet been implemented in the domain/router layer.
Fix (what changed, file and function):
Implemented EventType enum values, unified mutator architecture, GET /v1/seller/audit, GET /v1/seller/audit/verify, and GET /v1/seller/traces/{request_id}. All 21 tests passing.
Regression test (path::name):
agent/tests/test_audit_views_trace.py::test_decision_trace_redacts_secrets_escapes_text_and_isolates_customers
Do NOT (what must never be reintroduced):
Do not weaken or delete Step A tests; implement endpoints in T2.9 to satisfy the drafted contract.
Recurrences: 0
Files touched: agent/src/sellershield_agent/audit/*, agent/src/sellershield_agent/api/seller_audit.py

## BUG-007 Subprocess Winsock WinError 10106 on Windows stripped environment   [FIXED]   Date: 2026-11-11   Task: INT.1
Symptom (what the human saw, exact error text):
OSError: [WinError 10106] The requested service provider could not be loaded or initialized in asyncio.windows_events when running scripts/handshake.py via child process.
Reproduction (command or test):
subprocess.run([..., "handshake.py"], env={"STORE_BASE_URL": "http://127.0.0.1:8000"})
Root cause (evidence, not guesses):
On Windows, passing an explicit `env={...}` dictionary without copying `os.environ` omitted `SYSTEMROOT` (`C:\Windows`). Without `SYSTEMROOT`, the C runtime cannot locate the Winsock network provider DLLs needed by Python's `_overlapped` C extension module.
Fix (what changed, file and function):
In subprocess calls, used `env = os.environ.copy(); env["STORE_BASE_URL"] = ...` to preserve system environment paths.
Regression test (path::name):
scratch/live_integration_runner.py::main
Do NOT (what must never be reintroduced):
Never pass a bare dictionary as `env` to subprocess on Windows without copying `os.environ`.
Recurrences: 0
Files touched: scripts/handshake.py, scratch/live_integration_runner.py

## BUG-006 Store API naive UTC timestamps failing Agent timezone-aware validation   [FIXED]   Date: 2026-11-11  Task: INT.1
Symptom (what the human saw, exact error text):
pydantic_core._pydantic_core.ValidationError: 1 validation error for Order: created_at: Input should have valid timezone info [type=timezone_aware, input_value='2026-11-09T10:00:00', input_type=str]
Reproduction (command or test):
agent/tests/test_store_http_compat.py::test_http_store_list_orders_and_admin_detail_normalize_store_api_shapes
Root cause (evidence, not guesses):
`store-api` uses SQLite datatypes which persist ISO datetimes without timezone offsets (`+00:00` or `Z`). `agent` models enforce strict timezone-aware UTC (`_validate_aware_utc`).
Fix (what changed, file and function):
Implemented `sellershield_agent/store/normalizers.py` (`ensure_aware_utc`, `clean_order_dict`, `clean_refund_dict`, `clean_shipment_dict`) to append `+00:00` to naive ISO timestamp strings and parse timestamps before model instantiation.
Regression test (path::name):
agent/tests/test_store_http_compat.py::test_http_store_list_orders_and_admin_detail_normalize_store_api_shapes
Do NOT (what must never be reintroduced):
Never allow naive datetimes across domain model boundaries.
Recurrences: 0
Files touched: agent/src/sellershield_agent/store/normalizers.py, agent/src/sellershield_agent/store/http_client.py

## BUG-005 Vite Rollup native binary blocked on Windows Application Control (WDAC)   [FIXED]   Date: 2026-11-11   Task: INT.1
Symptom (what the human saw, exact error text):
Error: Cannot find module '@rollup/rollup-win32-x64-msvc' ... Error: ERR_DLOPEN_FAILED
Reproduction (command or test):
npm --prefix store-web run build
Root cause (evidence, not guesses):
Windows Application Control policy blocked execution of unsigned/native DLLs within `node_modules/@rollup/rollup-win32-x64-msvc`.
Fix (what changed, file and function):
Added npm override `"overrides": { "rollup": "npm:@rollup/wasm-node@4.68.1" }` in `store-web/package.json` to run the WebAssembly version of Rollup. Recorded under `ADR-033`.
Regression test (path::name):
store-web/package.json
Do NOT (what must never be reintroduced):
Do not revert to native MSVC rollup DLL on this Windows environment.
Recurrences: 0
Files touched: store-web/package.json, docs/DECISIONS.md

## BUG-004 SQLite UNIQUE constraint failure and missing tool status on client_msg_id replay   [FIXED]   Date: 2026-11-11   Task: T1.11
Symptom (what the human saw, exact error text):
sqlite3.IntegrityError: UNIQUE constraint failed: messages.session_id, messages.client_msg_id when replaying a request with the same client_msg_id 50 times.
Reproduction (command or test):
agent/tests/redteam/test_flood.py::test_flood_same_client_msg_id_deduplicated
Root cause (evidence, not guesses):
In agent/src/sellershield_agent/orchestrator/pipeline.py (_persist_turn), customer and agent messages were inserted without checking if records for the client_msg_id were already present in the database. Furthermore, idempotency replay lookup only queried for f"{client_msg_id}_reply", and did not restore cached tool status events, causing redundant execution or mismatch in emitted event sequences.
Fix (what changed, file and function):
In pipeline.py, added existence checks before inserting customer and agent records in _persist_turn. Enhanced replay query to check client_msg_id.in_([client_msg_id, f"{client_msg_id}_reply"]) with role == "agent", and persisted tool status events inside cards_json so replayed turns emit identical SSE event streams without executing duplicate tool calls.
Regression test (path::name):
agent/tests/redteam/test_flood.py::test_flood_same_client_msg_id_deduplicated
Do NOT (what must never be reintroduced):
Do not insert un-checked message records during replayed turns; always persist and replay tool status events for cached replies.
Recurrences: 0
Files touched: agent/src/sellershield_agent/orchestrator/pipeline.py

## BUG-003 TypeError on non-JSON raw bytes payload in validation error handler   [FIXED]   Date: 2026-11-11   Task: T1.11
Symptom (what the human saw, exact error text):
HTTP 500 Internal Server Error when non-JSON body sent with XML content-type. Server logged: TypeError: Object of type bytes is not JSON serializable in Starlette JSONResponse.
Reproduction (command or test):
agent/tests/redteam/test_protocol.py::test_protocol_wrong_content_type
Root cause (evidence, not guesses):
When request payload parsing failed for non-JSON content types, Pydantic captured the raw body as bytes in err["input"]. In _sanitize_validation_error (agent/src/sellershield_agent/errors.py), err["input"] was passed directly to the response dictionary. Starlette JSONResponse uses json.dumps which cannot serialize raw bytes, resulting in an unhandled 500 error instead of a clean 422 validation envelope.
Fix (what changed, file and function):
In agent/src/sellershield_agent/errors.py (_sanitize_validation_error), added a check for isinstance(item["input"], (bytes, bytearray)) and decoded it to UTF-8 with errors="replace" before adding it to the error envelope.
Regression test (path::name):
agent/tests/redteam/test_protocol.py::test_protocol_wrong_content_type
Do NOT (what must never be reintroduced):
Never pass un-decoded raw bytes in error detail envelopes passed to JSONResponse.
Recurrences: 0
Files touched: agent/src/sellershield_agent/errors.py

## BUG-002 JWKS port 8000 connection failure in dev mode   [FIXED]   Date: 2026-11-11   Task: T1.8
Symptom (what the human saw, exact error text):
Create session failed with 401 unauthenticated: {"error":{"code":"unauthenticated","message":"Authentication required","request_id":"...","details":{}}}
Reproduction (command or test):
Run scripts/dev_auth.py on port 8099, run uvicorn on port 8001 with default STORE_JWKS_URL (http://localhost:8000/.well-known/jwks.json), and call POST /v1/chat/sessions with a dev auth token.
Root cause (evidence, not guesses):
STORE_JWKS_URL in agent/.env points to http://localhost:8000/.well-known/jwks.json (where W1's real store will run). When testing locally before W1 is deployed, dev_auth.py serves JWKS on port 8099. When verifying tokens with kid "dev-key-1", JWKSClient queried port 8000, received [WinError 10061] No connection could be made because target machine actively refused it, and failed to retrieve the public key.
Fix (what changed, file and function):
Updated agent/src/sellershield_agent/auth/jwks.py (JWKSClient._fetch_with_retries) to automatically fall back to http://localhost:8099/.well-known/jwks.json when DEV_MODE is true and primary port 8000 is unreachable. Also updated scripts/dev_auth.py to serve tokens at /token/{user} and scripts/chat_cli.py to auto-fetch tokens so developers never need to manually copy-paste tokens.
Regression test (path::name):
Verified via live token verification probe: jwks_dev_fallback_fetched fetched dev keys and verified usr_c001 successfully. All 253 tests pass.
Do NOT (what must never be reintroduced):
Do not require manual env var exports for local dev auth verification when dev_mode is true.
Recurrences: 0
Files touched: agent/src/sellershield_agent/auth/jwks.py, scripts/dev_auth.py, scripts/chat_cli.py

## BUG-001 Windows SQLite relative path parent directory creation   [FIXED]   Date: 2026-11-11   Task: T1.8
Symptom (what the human saw, exact error text):
Create session failed with HTTP 500: {"error":{"code":"internal","message":"Internal server error","request_id":"...","details":{}}}. Server logged: sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) unable to open database file.
Reproduction (command or test):
Run uvicorn with DATABASE_URL=sqlite:///data/agent.db on Windows before agent/data directory exists.
Root cause (evidence, not guesses):
urllib.parse.urlparse("sqlite:///data/agent.db").path returned "/data/agent.db". On Windows, Path("/data/agent.db") interprets the leading slash as drive root C:\data\agent.db, so parent.mkdir() attempted to create C:\data rather than the project-local agent/data folder. When SQLite attempted to open data/agent.db relative to cwd, the directory did not exist, raising sqlite3.OperationalError: unable to open database file.
Fix (what changed, file and function):
In agent/src/sellershield_agent/db.py (create_engine_from_url), stripped the leading slash from relative SQLite URLs (not starting with sqlite:////) before passing to Path(), ensuring db_file.parent.mkdir() correctly creates agent/data.
Regression test (path::name):
Verified create_engine_from_url("sqlite:///data/agent.db") automatically creates agent/data and initializes database tables cleanly.
Do NOT (what must never be reintroduced):
Do not pass raw urlparse(url).path directly to Path() on Windows without stripping leading slashes for relative SQLite paths.
Recurrences: 0
Files touched: agent/src/sellershield_agent/db.py
