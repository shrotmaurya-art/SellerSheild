# DECISIONS (choices and reasons; do not reverse without a new entry)
ADR-001 SQLite files for store and agent (no Supabase or Firebase): transactions, zero setup, offline demo.
ADR-002 Hand-written orchestrator, no LangChain: control of loops, allowlists, audit and tests.
ADR-003 OpenAI-compatible client for all LLM providers: one adapter, config switches provider.
ADR-004 LLM tools are read and propose only; writes live in the execution layer after a human click.
ADR-005 uv for Python and just for tasks on Windows (PowerShell shell); no global pip.
ADR-006 Store is the identity provider (RS256 and JWKS); agent only verifies.
<<<<<<< HEAD
ADR-007 No GitHub remote until 11 Nov; temporary private sync remote only if the team needs it.
ADR-008 Store OpenAPI 3.1 contract design and loophole closures.
ADR-009 Agent OpenAPI 3.1 contract design and security guardrails.
ADR-010 Agent service skeleton, config fail-fast, and redacted logging.
ADR-011 Database engine safety recipe, clock abstraction, money arithmetic, and IST time utilities.
ADR-012 JWT RS256 token verification, JWKS caching and resilience, and role-based FastAPI dependencies.
ADR-013 LLM provider chain, failover, circuit breaker, and safe caching.
ADR-014 Test guard architecture, hash manifest, and test-integrity invariants.
ADR-015 Domain models, StoreClient protocol, and in-memory fixture store.
ADR-016 Versioned policy documents, loader validation, and integer refund calculator.
ADR-017 Deterministic decision engine, rule catalog, risk bands, and slot collection.
ADR-018 Tamper-evident audit service, hash chain, and details sanitization.
ADR-019 Centralized Tool Gateway, read tools, order digest caching, and formatting boundaries.
ADR-020 Grounded FactSheet, deterministic verifier, regeneration protocol, and templated fallbacks.
ADR-021 Deterministic router confidence cap, prompt hash audit logging, and LLM fallback resilience.
ADR-022 Conversational orchestrator, safe SSE streaming, and zero-LLM guided actions.
ADR-023 Real HTTP store client, service token caching, and bounded GET retries.
ADR-024 Adversarial Red-Team Suite, Compromised-Model Defense, and Protocol Abuse Hardening.
ADR-025 Store write methods, in-memory simulator, and fault injection.
ADR-026 Proposal, execution and approval state machines, canonical hashing, and atomic transitions.
ADR-027 Propose tools isolation, atomic limit enforcement, and confirmation card hashing.
ADR-028 Approval cards, customer confirmation flow, and live store revalidation.
ADR-029 Confirm/cancel handlers, Executor protocol, and stale-state fresh preview semantics.
ADR-030 Two-phase Executor saga, ServiceContext compensation, and startup/background reconciler.
ADR-031 Store-verified seller approvals, live revalidation, and non-approving expiry.
ADR-032 Signed webhook ingestion, monotonic versioning, and pre-release safety gates.
ADR-033 Store-Web pure-WASM Rollup override and live store integration fixes.
ADR-034 Centralized mutator architecture and tamper-evident decision trace API.
ADR-035 Real store writes, token isolation, and full-flow handshake.
ADR-036 Money-path adversarial red-team suite, independent financial ledger oracle, and fault injection attacks.
ADR-037 Support case deterministic brief and atomic handler model.
ADR-038 Structured customer memory: IDs/enums only, live chip lookups, and prompt data boundaries.
ADR-039 Push SSE stream, post-commit cross-thread notifier, and hardened email outbox worker.
ADR-040 Seller Overview, Dev Controls, and Repeatable Demo Run Architecture.
ADR-041 Proactive Delay Monitor Architecture, Concurrency Lock, and Deduplication Key Design.
CHANGE-001 Agent OpenAPI Contract: Optional text and guided chat actions.
CHANGE-002 Store OpenAPI Contract: client_ref on Return/Refund and AdminOrderDetailResponse.
CHANGE-003 Agent OpenAPI Contract: Customer approval withdrawal (POST /me/approvals/{proposal_id}/withdraw).
=======
<<<<<<< HEAD
ADR-007 Store OpenAPI 3.1 contract design and loophole closures.
ADR-008 Agent OpenAPI 3.1 contract design and security guardrails.
ADR-09 Agent service skeleton, config fail-fast, and redacted logging.
ADR-010 Database engine safety recipe, clock abstraction, money arithmetic, and IST time utilities.
ADR-011 JWT RS256 token verification, JWKS caching and resilience, and role-based FastAPI dependencies.
ADR-012 LLM provider chain, failover, circuit breaker, and safe caching.
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be

Entry format:
## ADR-0xx Title  Date  Status
Context / Options considered / Decision / Consequences

<<<<<<< HEAD
## ADR-008 Store OpenAPI 3.1 Contract and Loophole Closures  2026-11-11 ccepted
=======
## ADR-008 Store OpenAPI 3.1 Contract and Loophole Closures  2026-11-11  Accepted
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Context:
T0.6a requires freezing the Store API contract for milestone M0 across W1, W2, and A, accounting for all Section 16 strict-review amendments.
Options considered:
1. Allow flexible payload shapes and float money amounts.
2. Strict OpenAPI 3.1 with additionalProperties: false, integer _paise types, uniform 404s, and Idempotency-Key headers on all mutating POST endpoints.
Decision:
Option 2 chosen.
- Loophole closure 1: Integer paise and minimum: 0 on all money amounts prevent fractional paise drift.
- Loophole closure 2: Uniform 404 for nonexistent and unowned orders prevents enumeration attacks.
- Loophole closure 3: additionalProperties: false on all request bodies prevents injection of unauthorized fields.
- Loophole closure 4: Idempotency-Key header enforced on all mutating POST requests prevents double charges or duplicate returns under network retries.
- Loophole closure 5: If-Match version tag enforced on admin settings updates prevents concurrent overwrite of autonomy caps.
Consequences:
Contracts pass openapi-spec-validator and scripts/contract_check.py. All three developers build against a single source of truth.

<<<<<<< HEAD
## ADR-009 Agent OpenAPI 3.1 Contract and Security Guardrails  2026-11-11 Acepted
=======
## ADR-009 Agent OpenAPI 3.1 Contract and Security Guardrails  2026-11-11  Accepted
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
Context:
T0.6b requires freezing the Agent API contract for milestone M0, implementing chat SSE streams, push notifications, seller approvals, and policy simulation.
Options considered:
1. Traditional polling only without typed SSE cards.
2. OpenAPI 3.1 with server-sent events (x-sse-events), card payloads, push events stream, step-up authorization, and IDOR isolation.
Decision:
Option 2 chosen.
- Loophole closure 1: x-required-role and IDOR checks prevent customers or unauthorized staff from accessing other customers' cases or seller tools.
- Loophole closure 2: Step-up authorization requires auth_time within 15 minutes for high-risk actions (policy activation, approvals above staff limit), returning 401 step_up_required.
- Loophole closure 3: Deterministic proposal_hash check on confirm ensures what the customer approved matches the calculated proposal exactly.
- Loophole closure 4: Evidence capped at 3 items and strictly referenced by store ID without direct external image loading by the agent.
- Loophole closure 5: Dev routes (/dev/*) explicitly tagged with x-dev-only: true to guarantee separation from production runtime.
Consequences:
Contracts pass openapi-spec-validator and scripts/contract_check.py. Milestone M0 contract freeze complete.

## ADR-010 Agent Service Skeleton, Config Fail-Fast, and Redacted Logging  2026-11-11  Accepted
Context:
T0.7 requires building the FastAPI agent service skeleton in agent/ that safely loads settings, enforces correlation tracking with X-Request-ID, redacts logs, enforces 1 MB body limits, and guarantees standard error envelopes.
Options considered:
1. Default FastAPI handlers, printing logs to stdout without recursive redaction, and permissive CORS.
2. Hardened skeleton: pydantic-settings failing fast with variable names only; structlog JSON processor recursively masking sensitive keys and email patterns; pure ASGI body limit middleware; uniform OpenAPI error envelopes suppressing tracebacks; CORS disabled by default.
Decision:
Option 2 chosen.
- Loophole closure 1: Settings initialization error sanitization prevents leaking secret values, defaults, or inputs when required variables are missing.
- Loophole closure 2: Structlog recursive dictionary and list sanitization prevents header or payload credential leakage (authorization, token, password, api_key, cookie) and redacts email addresses (KP-13).
- Loophole closure 3: Request validation error handler scrubs user inputs on sensitive fields so malformed passwords/tokens are never reflected in 422 envelopes.
- Loophole closure 4: X-Request-ID regex validation (`^[A-Za-z0-9-]{8,64}$`) prevents log/header injection attacks by regenerating IDs on invalid input.
- Loophole closure 5: Pure ASGI body limit middleware terminates requests exceeding 1 MB with uniform 413 envelopes without deadlock.
- Loophole closure 6: API documentation (/docs, /redoc, /openapi.json) is disabled unless DEV_MODE=true.
Consequences:
Agent service passes 19 comprehensive tests, mypy strict, and ruff. Server starts reliably on port 8001 and passes live REST probes.

## ADR-011 Database Safety Recipe, Clock, Money, and Time Utilities  2026-11-11  Accepted
Context:
T0.8 establishes core foundation components used across all agent features: safe SQLite transaction handling, aware clock abstraction, integer money calculations, and IST calendar-window operations.
Options considered:
1. Ad-hoc datetime.now() calls, float money math, and standard SQLite auto-transactions.
2. Hardened deterministic foundations:
   - SQLite recipe with WAL, busy_timeout=5000, and BEGIN IMMEDIATE for write transactions.
   - Clock protocol returning aware UTC datetimes, preventing domain datetime.now() drift.
   - Pure integer Paise arithmetic with Decimal-based parsing and Hamilton largest-remainder allocation.
   - Time utilities rejecting naive datetimes and enforcing 23:59:59.999999 Asia/Kolkata day end boundaries.
Decision:
Option 2 chosen.
- Loophole closure 1: Pure integer paise allocation with index-stable tie-breaking eliminates fractional paise drift across line-item discounts.
- Loophole closure 2: parse_rupees validates maximum 2 decimal places to avoid silent precision truncation.
- Loophole closure 3: SQLite write_tx ensures rollback on exception and documents prohibition of awaits across transactions.
- Loophole closure 4: All time utilities strictly reject naive datetimes and compute exact IST boundaries irrespective of system local timezone.
Consequences:
All 41 foundation unit/property tests pass. Total agent test suite reaches 60 passing tests.

## ADR-012 JWT Verification, JWKS Caching, and Role Authorization  2026-11-11  Accepted
Context:
T0.9 requires verifying store-issued access tokens safely, even when the store identity provider is down or rotating keys, preventing algorithm attacks, IDOR, and token leakage.
Options considered:
1. Accept symmetric HS256 tokens for local testing, bypass flags, or crash agent on store JWKS downtime.
2. Hardened deterministic verification:
   - Allow RS256 algorithm only; reject `none`, `HS256`, and algorithm confusion attacks using public key as secret.
   - Strictly require `sub`, `role`, `iss`, `exp`, `iat`, `jti`, and `aud` containing `sellershield-agent`.
   - Evaluate all time claims (`exp`, `iat`, `nbf`) against domain `Clock` rather than system time.
   - Enforce customer tokens must contain non-empty `customer_id`.
   - Rate limit JWKS unknown `kid` refresh attempts to at most once per 30 seconds; cache keys for 1 hour; 3 retries with backoff; keep last good keys on refetch failure; disable redirects (`follow_redirects=False`).
   - Lazy fetch keys on first use so agent startup never crashes when store is down; `/readyz` reports degraded instead of 503.
   - Return 401 `unauthenticated` and 403 `forbidden` envelopes without leaking internal failure reasons to clients; never log tokens.
   - Require step-up authorization (`require_fresh_auth`) with 401 `step_up_required` if `auth_time` > 15 minutes.
Decision:
Option 2 chosen.
- Loophole closure 1: Pre-decoding inspection of unverified header rejects `alg != "RS256"`, thwarting both `alg: none` and `HS256` key confusion attacks.
- Loophole closure 2: Validating claims against `Clock` enables simulated time acceleration during demos and testing without false expiries.
- Loophole closure 3: Throttling unknown `kid` fetches to once per 30s defends the JWKS client against DoS floods of fake key identifiers.
- Loophole closure 4: Customer tokens without `customer_id` are rejected immediately, preventing customer IDOR bypasses.
- Loophole closure 5: All private keys for tests are generated exclusively in memory and never written to disk or logged.
Consequences:
Agent test suite increases to 85 passing tests. Verification commands and linting pass with zero warnings.

## ADR-013 LLM Provider Chain, Failover, Circuit Breaker, and Safe Caching  2026-11-11  Accepted
Context:
T0.10 requires building a resilient, multi-provider LLM adapter chain (Gemini Flash primary, Groq secondary, OpenRouter tertiary) with automatic fallback, circuit breaking via Clock, flat Pydantic structured output with retry, replay caching, and zero secret leakage.
Options considered:
1. Vendor-specific SDKs, direct calls without circuit breaking, unstructured outputs, or logging raw model inputs/outputs.
2. Standardized OpenAI-compatible adapter:
   - AsyncOpenAI wrapper with explicit httpx.AsyncClient injection enabling 100% offline respx mocking.
   - Circuit breaker tracking consecutive failures within 60s window using the domain Clock, skipping unhealthy providers for 60s before half-opening.
   - Sequential fallback on timeout (12s), 429 rate limit, 5xx server errors, or connection errors.
   - 401, 403, and 404 raise ConfigError naming the provider and model without echoing API keys.
   - Flat Pydantic structured output using JSON schema when supported or JSON object fallback, with a single self-healing retry on validation failure.
   - Canonical SHA-256 JSON caching in agent/.llm_cache/ (gitignored) with off, record, and replay modes.
Decision:
Option 2 chosen.
- Loophole closure 1: Redaction guarantee: API keys are SecretStr, never printed, and never echoed in ConfigError messages, logs, or exception traces.
- Loophole closure 2: Unknown provider capabilities default strictly to False for JSON schema and tool calling, falling back to JSON object mode.
- Loophole closure 3: Domain Clock dependency ensures simulated clock time controls circuit breaker intervals deterministically during tests.
- Loophole closure 4: Atomic temporary-file replacement for cache writes prevents read corruptions under concurrent calls.
- Loophole closure 5: Prompts and messages are never logged, ensuring customer data and PII never leak into telemetry.
Consequences:
12 offline tests pass with respx. Agent check suite reaches 97 passing tests, ruff clean, mypy strict clean. Ready for live human smoke testing.
<<<<<<< HEAD

## ADR-014 Test Guard Architecture, Hash Manifest, and Test-Integrity Invariants  2026-11-11  Accepted
Context:
T1.0b requires making it mechanically impossible for autonomous coding agents to pass checks by weakening, skipping, deleting, or loosening tests, conftest fixtures, or configuration options.
Options considered:
1. Relying on git commit reviews and manual diff inspection only.
2. Mechanical pre-execution test guard enforcing an immutable SHA-256 manifest, AST assertion counting and semantic checks, collected test count monotonically increasing threshold, forbidden pytest config options, and plain justfile recipe validation.
Decision:
Option 2 chosen.
- Loophole closure 1: Frozen files manifest tests/FROZEN.sha256 locks contracts, tests, fixtures, and guard code; modifications without --refreeze PATH --reason TEXT are strictly rejected.
- Loophole closure 2: AST traversal forbids assert True, assert 1, self-comparison expressions, and empty/pass-only test bodies across both frozen and newly added test files.
- Loophole closure 3: AST traversal forbids @pytest.mark.skip, @pytest.mark.xfail, pytest.skip(), and unapproved markers (only live and store allowed for test categorization).
- Loophole closure 4: Inspection of config files rejects -k, --deselect, --ignore, -x, --maxfail, --lf, --co in addopts.
- Loophole closure 5: Verification of justfile enforces that agent-check executes ruff, mypy, pytest (as plain uv run pytest -q), and test-guard in exact order.
- Loophole closure 6: Monotonically increasing tests/FROZEN.count ensures deleted or uncollected tests immediately fail the guard check.
Consequences:
just agent-check runs test-guard on every cycle. Test tampering is mechanically blocked. 12 integration tests in tests/guard/test_guard.py verify all sabotage modes fail as intended.

## ADR-015 Domain Models, StoreClient Protocol, and In-Memory Fixture Store  2026-11-11  Accepted
Context:
T1.1 requires the agent's strongly-typed view of the Store API contract, the asynchronous StoreClient Protocol and RequestContext, and an in-memory FixtureStoreClient with seed data covering scenarios SC-01 through SC-17.
Options considered:
1. Dynamic dictionary access and float money representations.
2. Rigid typed domain:
   - Frozen Pydantic v2 models (`extra='forbid'`, frozen=True) mirroring `contracts/openapi-store.yaml`.
   - Money strictly integer paise (`_paise`) and risk ratios in integer basis points (`_bp`).
   - Zero float fields on any domain model (KP-05).
   - Datetimes validated as aware UTC.
   - Strict order ID validation (`^ORD-[0-9]{3,6}$`) before touching data.
   - Uniform 404 behavior for unowned vs nonexistent orders (KP-15).
   - Injected domain Clock for resolving relative fixture offsets (KP-06).
   - RequestContext holding SecretStr customer token (never logged, KP-13).
Decision:
Option 2 chosen.
- Loophole closure 1: Integer paise and basis points guarantee arithmetic precision with zero float drift.
- Loophole closure 2: Regex validation on order IDs prevents malformed, padded, lowercase, or unicode lookalike strings from probing storage or returning false 404s.
- Loophole closure 3: Order lookup returns identical StoreNotFound message and exception type whether an order is missing or owned by another customer, eliminating customer enumeration attacks.
- Loophole closure 4: Customer risk facts endpoint explicitly forbids customer role contexts, enforcing backoffice / service privilege separation.
- Loophole closure 5: Relative fixture offsets dynamically resolved against injected Clock ensure test repeatability across time-shifted test cases.
Consequences:
10 comprehensive integration and property tests pass in `agent/tests/test_fixtures.py`. Fixture store provides deterministic behavior across all 17 hackathon scenarios.

## ADR-016 Versioned Policy Documents, Loader Validation, and Integer Refund Calculator  2026-11-11  Accepted
Context:
T1.2 requires implementing versioned policy documents (`v1`, `v2`, `v3`), active version resolution via environment, customer-safe policy summarization without leakage of risk or limits, and an exact integer-paise refund calculator adhering strictly to Spec §7.1, §7.2, and §7.4.
Options considered:
1. Floating-point division with rounding, relaxed schema models, and single global policy values.
2. Hardened deterministic policy and refund architecture:
   - Frozen Pydantic v2 models (`extra='forbid'`, `frozen=True`) with non-negative constraints (`ge=0`) on all paise amounts, basis points, and day counts.
   - Distinct canonical JSON version files (`v1.json`, `v2.json`, `v3.json`) differing strictly in `auto_limit_paise` (₹1,000, ₹750, ₹500).
   - Dynamic `POLICY_ACTIVE_VERSION` environment selection with safe fallback to `v3`.
   - String-subclassed `CustomerSafeSummary` providing structured attributes while stripping out all forbidden internal terms (`limit`, `threshold`, `risk`, `approval`, `score`, `staff`, `owner`, `auto`).
   - Exact integer math refund formula: remainder paise assigned to the first unit; shipping refunded only for whole-order seller-fault or lost shipments; fee deducted only on customer-fault refund resolutions; total floor clamped at 0; and store `max_refundable_paise` capping.
Decision:
Option 2 chosen.
- Loophole closure 1: Integer paise allocation (`base = line_paid // qty`, `rem = line_paid % qty`) guarantees exact sum equality across units without float drift (KP-05).
- Loophole closure 2: Clamping total to `max(0, ...)` guarantees customer never incurs a negative balance or owes fees beyond item amount.
- Loophole closure 3: Strict `extra='forbid'` rejects tampered JSON files or injected unauthorized keys.
- Loophole closure 4: Automated forbidden-word inspection blocks leakage of internal risk metrics, thresholds, or staff limits to customers (KP-23).
- Loophole closure 5: Zero I/O, zero LLM calls, and zero Clock access within `compute_refund` guarantees pure deterministic execution.
Consequences:
20 tests pass in `agent/tests/test_policy.py`. Total test suite reaches 143 passing tests. Mypy and ruff pass with 0 errors.

## ADR-017 Deterministic Decision Engine, Rule Catalog, and Risk Assessment  2026-11-11  Accepted
Context:
T1.3 requires implementing the deterministic decision engine `decide(facts, policy, now) -> Decision` evaluating pure rules R-01 through R-15, computing additive risk indicators, assessing delivery delay, and synthesizing decisions with strict precedence (`DENY > ESCALATE > NEEDS_INFO > NEEDS_APPROVAL > ALLOW`) without floating point math, LLM calls, I/O, or clock reads.
Options considered:
1. LLM-based or fuzzy decision making with floating-point calculations and unstructured string outputs.
2. Pure deterministic engine in Python standard library and Pydantic v2:
   - Frozen immutable `DecisionFacts` and SHA-256 canonical `facts_hash` strictly excluding `reason_text`.
   - Discrete, pure rule functions (R-01 to R-15) returning typed `RuleResult` with neutral, customer-safe text free of forbidden words (`limit`, `threshold`, `risk`, `score`, `staff`, `owner`, `auto`).
   - Additive customer risk scoring per Spec §7.5 into low/medium/high bands, where risk never triggers `DENY` and high risk forces `NEEDS_APPROVAL`.
   - Calendar-day delay assessment in policy timezone (Asia/Kolkata) with minor (< 2d), options (>= 2d), and lost statuses.
   - Precedence-driven outcome synthesis with approver role routing (`seller_staff` up to ₹2,000, `seller_owner` above, `ESCALATE` at ₹5,000 hard ceiling).
   - Missing slot detection for unselected refund methods (COD returns) and missing damage evidence (`damaged`, `wrong_item`, `not_as_described`) returning `NEEDS_INFO`.
Decision:
Option 2 chosen.
- Loophole closure 1: SHA-256 `facts_hash` explicitly omits `reason_text`, making the evaluation hash immune to adversarial prompt injection, whitespace manipulation, or customer commentary (KP-27, SC-13).
- Loophole closure 2: Customer-facing text across all rules passes an automated forbidden words filter to prevent leakage of internal risk metrics, thresholds, or approval hierarchies (KP-23).
- Loophole closure 3: Strict rule precedence guarantees safety: a hard `DENY` or `ESCALATE` takes priority over informational or approval requests.
- Loophole closure 4: High risk never auto-denies a customer; it routes requests to seller review (`NEEDS_APPROVAL`), protecting good customers from algorithmic lockout (Spec §7.5, SC-15).
- Loophole closure 5: Lost shipments by carrier are handled as an explicit exception in R-02 and routed to `seller_staff` approval with full refunds including shipping fees (Spec §7.6, SC-17).
- Loophole closure 6: Partial prior refunds within 2 days of delivery establish timely claim status for subsequent claims on the same order, preventing unfair window denials on split claims (SC-10).
Consequences:
35 comprehensive tests pass in `agent/tests/test_decide.py`. Total test suite reaches 178 passing tests. Zero type errors in mypy strict mode and zero ruff lint errors.

## ADR-018 Tamper-Evident Audit Service, Hash Chain, and Details Sanitization  2026-11-11  Accepted
Context:
T1.4 requires a tamper-evident audit service (`AuditService.append`, `AuditService.verify_chain`) with a SHA-256 hash chain and details sanitization (`sanitize_details`) providing the auditable backbone for all subsequent actions, tool calls, and state transitions.
Options considered:
1. Asynchronous fire-and-forget audit logger writing into a separate database or file.
2. Synchronous in-transaction audit service:
   - `audit_events` table attached to shared `get_metadata()` with unique index on `prev_hash` to prevent chain forks.
   - `append(tx, event)` runs inside caller's `write_tx` (BEGIN IMMEDIATE) so the audit row and business modification commit together atomically.
   - `hash` computed via SHA-256 over canonical JSON of all event fields plus `prev_hash`; first event links to genesis `"0" * 64`.
   - `sanitize_details` rejecting non-JSON types, redacting Bearer tokens and email addresses, stripping the word `Authorization` in any case, enforcing 500-char string limits, and scrubbing foreign customer IDs from denial events.
   - `verify_chain(conn)` recomputing hashes from genesis to verify cryptographic continuity and identify broken sequence numbers.
Decision:
Option 2 chosen.
- Loophole closure 1: Atomic transaction binding: audit append must occur in the caller's transaction. If audit insert fails, the business transaction aborts; if the business statement fails, no orphaned audit row is written (Invariant 6).
- Loophole closure 2: Unique index on `prev_hash`: prevents concurrent transactions from generating branching hash chains. Serialized under BEGIN IMMEDIATE.
- Loophole closure 3: Strict sanitization: redaction of tokens and keys (KP-13), truncation to 500 characters, scrubbing foreign customer IDs from denial events to prevent IDOR leaks, and complete elimination of the word `Authorization` in any case.
- Loophole closure 4: Windows SQLite file lock safety: temp test databases explicitly dispose engines before fixture unlinking (KP-21).
Consequences:
12 comprehensive tests pass in `agent/tests/test_audit.py`. Full test suite reaches 190 passing tests. Full `just agent-check` passes with zero lint, zero type, and zero test-guard integrity errors.

## ADR-019 Centralized Tool Gateway, Read Tools, Order Digest Caching, and Formatting Boundaries  2026-11-11  Accepted
Context:
T1.5 requires establishing the Tool Gateway as the sole funnel through which agent tools execute, implementing the 8 safe read tools, 60s per-session digest caching, response minimization, LLM function formatting, untrusted data block wrapping, and post-execution tamper-evident audit logging.
Options considered:
1. Allow LLM or handlers to call store client methods directly without a gateway layer, holding open database transactions during remote calls.
2. Centralized `ToolGateway`:
   - Allowlist checking per state and workflow.
   - Strict Pydantic argument validation with `extra='forbid'`, rejecting forbidden fields (`customer_id`, `user_id`, `token`, `amount`, `paise`).
   - ID format sanity checks (`^ORD-[0-9]{3,6}$`, `^RET-[0-9]{3,6}$`, `^RFD-[0-9]{3,6}$`).
   - Context isolation: Handlers receive `ToolContext` and cannot inspect customer bearer tokens directly.
   - 8.0-second execution timeout via `asyncio.timeout(8.0)`.
   - Bounded parallel reads: `call_many` runs with concurrency capped at 4 (`asyncio.Semaphore(4)`).
   - Uniform 404s: Cross-customer order lookups look identical to nonexistent orders (KP-15).
   - Response minimization: Outputs stripped of addresses, phone numbers, emails, risk scores, rule weights, and internal limits.
   - Tagged untrusted data block: `tool_results_block` strips HTML, control characters, truncates strings to 200 chars, and tags content as untrusted data (KP-25).
   - Audit logging: Exactly one `tool_call` audit row written through `AuditService.append` in an independent short `write_tx` strictly after the store call finishes (KP-01).
Decision:
Option 2 chosen.
- Loophole closure 1: Prohibiting write tools from being exposed to the LLM (read and propose only).
- Loophole closure 2: Never holding a SQLite transaction across network calls prevents database deadlocks.
- Loophole closure 3: Uniform 404 and scrubbed denial audit details prevent cross-customer order enumeration.
- Loophole closure 4: 200-char truncation and HTML/control character stripping neutralize prompt injection via store data.
Consequences:
13 comprehensive tests pass in `agent/tests/test_tools.py`. Total test suite reaches 203 passing tests. Zero type errors and zero lint errors.

## ADR-020 Grounded FactSheet, Deterministic Verifier, Regeneration Protocol, and Templated Fallbacks  2026-11-11  Accepted
Context:
T1.6 requires guaranteeing that nothing the model writes reaches a customer unless every claim, amount, identifier, date, and status is grounded in deterministic tool execution results.
Options considered:
1. Allow LLM to output free text and stream tokens directly to the customer, relying on prompt instructions to avoid hallucinating amounts or claims.
2. Two-stage deterministic verification and templated fallback pipeline:
   - `FactSheet`: Immutable Pydantic model (`frozen=True, extra="forbid"`) extracted deterministically from `ToolResult` data and IDs.
   - `Verifier`: Pure deterministic Python code checking 10 distinct invariants:
     (1) All currency amounts must equal a FactSheet amount in integer paise (KP-05).
     (2) Spelled-out currency amounts are strictly rejected.
     (3) All entity IDs must exist in allowed IDs.
     (4) All dates must match FactSheet IST dates.
     (5) Relative temporal claims must match relative facts (`eta_in_days`, `delivered_days_ago`).
     (6) Action claims must be supported by active claim tokens.
     (7) Promise words must be backed by explicit promise facts.
     (8) Internal terms (tool names, prompts, risk scores, auto-limits, thresholds, customer IDs, JWT/API keys, URLs, HTML, Markdown images/links) are strictly forbidden.
     (9) Language matching (Devanagari present for `hi`, Latin for `en`).
     (10) Length boundary (`len(draft) <= 1200`).
   - One regeneration attempt with structured violation feedback; on second failure, fall back to deterministic templates.
   - Deterministic bilingual fallback templates passing verification by construction.
Decision:
Option 2 chosen.
- Loophole closure 1: Unicode NFKC normalization and zero-width character stripping prevent evasion via invisible characters or full-width digits.
- Loophole closure 2: Case-insensitive ID extraction and dash normalization prevent bypass via casing tricks or typographic dashes.
- Loophole closure 3: Converting all currency occurrences to integer paise prevents floating point drift or off-by-one paisa fabrication.
- Loophole closure 4: Forbidding URLs, HTML, and Markdown media neutralizes data exfiltration and prompt injection channels.
- Loophole closure 5: Never streaming unverified model text (KP-30) ensures that an unverified hallucination can never be viewed by a customer.
Consequences:
26 comprehensive tests pass in `agent/tests/test_verifier.py`. Total test suite reaches 229 passing tests. Full `just agent-check` passes with zero lint, zero type, and zero test-guard integrity errors.

## ADR-021 Deterministic Router Confidence Cap, Prompt Hash Audit Logging, and LLM Fallback Resilience  2026-11-11  Accepted
Context:
T1.7 requires turning customer messages into validated `Understanding` objects with a zero-downtime deterministic fallback, prompt injection heuristics, prompt version SHA-256 tracking, and hallucinated order ID pruning.
Options considered:
1. LLM-only classification without deterministic fallback or with arbitrary router confidence scores.
2. Architecture with schema-constrained LLM generation, offline keyword/regex fallback router, confidence thresholding, prompt SHA-256 audit tracking, and order ID integrity gates:
   - Flat Pydantic v2 model `Understanding` (`extra="forbid"`).
   - System prompt isolating untrusted message within `<customer_message>` delimiters.
   - Advisory prompt injection heuristics logging `injection_flag` audit events without changing business logic.
   - `DeterministicRouter` with confidence strictly capped at $\le 0.5$, ensuring deterministic fallbacks always enforce `needs_clarification = True`.
   - Any LLM error (provider down, rate limit, schema failure, config error) falls back to router with an `llm_fallback` audit event.
   - Dropping any LLM-invented order ID that is not present in either the customer message or customer order digest.
   - SHA-256 hash of prompt version recorded in audit trail for full reproducibility.
Decision:
Option 2 chosen.
- Loophole closure 1: Capping router confidence at $\le 0.5$ guarantees that offline keyword matches never bypass clarification chips when human oversight or disambiguation is warranted.
- Loophole closure 2: Regex word boundary matching (`\b`) prevents substring collisions (e.g. `calculate` triggering `late`).
- Loophole closure 3: Filtering model-generated order IDs against the union of message-extracted regex IDs and customer order digest strictly eliminates hallucinated entity IDs.
- Loophole closure 4: Catching all provider exceptions during the understanding phase guarantees customer support continuity even when third-party LLM APIs face complete outages or rate limits.
- Loophole closure 5: Advisory injection heuristics isolate injection detection from core decision code; detection produces safety flags and audit events only, maintaining invariant that policy and safety do not depend on untrusted prompt filtering.
Consequences:
Router achieves 100% accuracy on 30 golden evaluation cases. 10 comprehensive tests pass in `agent/tests/test_understand.py`. Live LLM baseline and stress tests pass cleanly. Total test suite reaches 239 passing tests. Full `just agent-check` passes cleanly.

## CHANGE-001 Agent OpenAPI Contract: Optional Text and Guided Chat Action  2026-11-09  Accepted
Context:
T1.8 introduces guided actions (chips and buttons) allowing customers to interact with deterministic workflows without requiring free-form text input or LLM generation. When an action is selected, the request carries an action object and text may be omitted.
Options considered:
1. Require text even when action is present (e.g. sending empty string or placeholder).
2. Add an optional action object {name: ..., order_id: ...} to ChatMessageRequest, making text optional when action is present, with additionalProperties: false.
Decision:
Option 2 chosen.
- Added schema `ChatAction` with required property `name` (enum: `track_order`, `return_status`, `refund_status`, `policy_info`, `start_return`, `talk_to_human`) and optional property `order_id` (string), `additionalProperties: false`.
- Updated `ChatMessageRequest`: `action` references `#/components/schemas/ChatAction`; `required: [client_msg_id]`; `anyOf: [{required: [text]}, {required: [action]}]`; `additionalProperties: false`.
- Additive change preserving backwards compatibility: existing text-only requests remain fully valid.
Consequences:
Both `openapi-spec-validator` and `scripts/contract_check.py` pass. W2 front-end can now trigger zero-LLM guided workflows directly from action cards/chips.

## ADR-023 Real HTTP Store Client, Service Token Caching, and Bounded Retries  2026-11-09  Accepted
Context:
T1.9 requires implementing the production `HttpStoreClient` fulfilling the `StoreClient` protocol to connect to W1's real NestKart store backend, enabling hot-swapping between in-memory fixtures and real HTTP endpoints via `STORE_MODE` without touching any policy, tool, or composition code.
Options considered:
1. Hardcode HTTP endpoints with direct requests and synchronous timeouts.
2. Production `HttpStoreClient` using `httpx.AsyncClient` with base URL normalization (`/api/v1`), token forwarding from `RequestContext` (`SecretStr`), client-credentials service token caching with pre-expiry refresh, bounded retries on idempotent GET requests, domain error mapping (uniform 404), and startup validation refusing `STORE_MODE=fixtures` outside `DEV_MODE`.
Decision:
Option 2 chosen.
- Loophole closure 1 (KP-13): Customer tokens are forwarded strictly per-request and never logged or persisted. Invalid shapes are logged without the response payload.
- Loophole closure 2 (KP-15): Uniform 404 mapping for unowned and nonexistent orders prevents order ID enumeration attacks.
- Loophole closure 3 (KP-24): The application strictly refuses to start with `STORE_MODE=fixtures` when `DEV_MODE=False`, preventing simulated data from masking live integration during judging.
- Loophole closure 4: Bounded retries with randomized jitter (2 tries total) apply strictly to idempotent GET requests encountering 502/503/504 or connection failures. Mutations and client errors fail fast without retry.
- Contract mismatch observations communicated to W1:
  - In `contracts/openapi-store.yaml`, endpoints are defined relative to `servers: [url: http://localhost:8000/api/v1]`. `HttpStoreClient` normalizes base URLs whether configured with or without the `/api/v1` suffix.
  - Store status and payment method enums (e.g. `shipped` for order in transit, `prepaid` for digital payments) are normalized to domain enums while dropping unrecognized additional fields.
Consequences:
All 24 contract tests pass in `agent/tests/test_store_http.py`. Entire test suite passes (277 tests). Full ruff linting and mypy strict typing pass with 0 errors.

Context:
T1.8 requires implementing the customer conversational chat endpoint streaming verified answers over SSE (`POST /v1/chat/sessions/{id}/messages`), enforcing safe streaming invariants (Amendment 2: never stream raw tokens before verification passes), zero-LLM guided actions and offline fallback mode, IDOR session isolation, idempotency, and sliding-window rate limits.
Options considered:
1. Stream tokens directly from LLM as they are generated, then redact or recall violations.
2. Complete resolution, extract FactSheet, verify draft (with one regeneration attempt), and only then stream verified text in delta chunks over SSE; support zero-LLM guided workflows and fallback with chips.
Decision:
Option 2 chosen.
- Loophole closure 1: Strict adherence to Amendment 2 prevents hallucinated amounts, fabricated return dates, or data leakage from ever reaching the customer's browser.
- Loophole closure 2: Composite unique constraint `(session_id, client_msg_id)` ensures idempotent retries return identical cached answers with zero redundant tool executions.
- Loophole closure 3: In-memory sliding-window rate limiters (20 msgs/min per user, 60 failed auths/min per IP) protect agent resources from denial-of-service and brute-force abuse.
- Loophole closure 4: Uniform 404 responses on cross-customer session access prevent session ID enumeration or cross-tenant visibility.
- Loophole closure 5: Recursive HTML escaping and length truncation on interactive card payloads prevent XSS vulnerabilities in frontend rendering.
Consequences:
All 14 comprehensive unit and integration tests pass in `agent/tests/test_chat.py`. Total test suite passes (253 tests). Full ruff linting and mypy strict typing pass with 0 errors.

## ADR-024 Adversarial Red-Team Suite, Compromised-Model Defense, and Protocol Abuse Hardening  2026-11-09  Accepted
Context:
TASK T1.11 requires building an executable adversarial attack and clumsy-user red-team test suite (80 scenarios) assuming the model is compromised (`ObedientFakeLLM`), while verifying deterministic boundaries (tool allowlists, customer scoping, FactSheet verifier, Pydantic inputs, sliding-window rate limits, and SHA-256 audit hash chains).
Options considered:
1. Rely primarily on system prompt instructions and LLM alignment to resist injection and protect confidential parameters.
2. Multi-tier deterministic defense:
   - Tool Gateway allowlist & context scoping: Reject unauthorized tools and strictly bind customer_id to authenticated JWT.
   - Grounded FactSheet Verifier: Inspect all final agent text claims against deterministic facts before streaming any delta.
   - Pre-resolution heuristic injection & money manipulation scanners: Normalize zero-width, unhyphenated, and leet characters; flag injection patterns in audit trail; reject non-positive/negative refund quantities before tool execution.
   - Pydantic strict schemas & safe error serialization: Reject extra fields (422) and decode non-JSON input bytes to string.
   - Rate limiters: Enforce IP rate limits on failed auth (429) and user message quotas (429).
   - Replay deduplication: Cache tool status in cards_json and replay cached responses without extra tool calls.
Decision:
Option 2 chosen.
- Loophole closure 1: Assumption that the LLM is compromised means prompt injection cannot force unauthorized tool calls or emit unverified facts; the deterministic verifier and gateway enforce security independently of model compliance.
- Loophole closure 2: Decoding raw bytes in Starlette error handler eliminates 500 crashes on non-JSON content types.
- Loophole closure 3: Negative lookbehind in quantity scanner avoids false-positive rejections of hyphenated order identifiers (e.g. `ORD-1048`).
- Loophole closure 4: 50x request replays with identical `client_msg_id` return cached SSE streams without duplicate tool invocations or SQLite constraint errors.
Consequences:
All 103 red-team tests pass (`agent/tests/redteam/`). Total test suite passes (385 tests) in 44.90s. `just agent-check` passes with zero ruff or mypy errors.

## CHANGE-002 Store OpenAPI Contract: client_ref on Return/Refund and AdminOrderDetailResponse  2026-11-09  Accepted
Context:
TASK T2.1 requires store write methods and a write-capable fixture store. For the agent executor saga and reconciler (Spec §6.3, Spec §21), the store needs to associate returned and refunded entities with the `Idempotency-Key` provided at creation time (`client_ref`), and provide an administrative view of order details including associated returns and refunds (`GET /admin/orders/{id}`).
Options considered:
1. Modify `Order` directly with optional fields and rely on untyped response payloads.
2. Additive contract change:
   - Add optional string `client_ref` (the creation `Idempotency-Key`) to `Return` and `Refund` schemas.
   - Define `AdminOrderDetailResponse` with all `Order` attributes plus `returns` and `refunds` arrays, and update `GET /admin/orders/{id}` 200 response schema from `Order` to `AdminOrderDetailResponse`.
Decision:
Option 2 chosen.
- Loophole closure 1: Adding `client_ref` to `Return` and `Refund` allows the agent executor saga to reconcile half-done mutations without guessing or scanning timestamps.
- Loophole closure 2: Explicit `AdminOrderDetailResponse` gives seller and service callers direct visibility into active returns and refunds for an order, enabling deterministic validation of refund totals against delivered quantities and payments.
- Additive compatibility: All existing fields and endpoints remain strictly backwards-compatible. Customer `GET /orders/{id}` continues returning standard `Order`.
Consequences:
Validated with `openapi-spec-validator` and `scripts/contract_check.py` (49 operations, all invariants preserved). Contract approved and refrozen by human. Ready to dispatch change notification to W1.

## ADR-025 Store Write Methods, In-Memory Simulator, and Fault Injection  2026-11-09  Accepted
Context:
The money and mutation paths require StoreClient write methods (`create_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, `get_return`, `get_refund`, `get_order_admin`, `get_settings`, `get_customer_refund_summary`) and an in-memory fixture simulator enforcing store-level invariants for testing edge cases, timeouts, and multi-step sagas without depending on live network infrastructure.
Options considered:
1. Stub write methods with mock return values in tests.
2. Implement an in-memory mutable simulator (`FixtureStoreClient`) enforcing full store state machines, single-return-per-item, refund reservation caps, service autonomy caps (50,000 paise), seller authorization role limits (staff 200,000 paise, owner above), simulated IST daily caps (500,000 paise), strict payload idempotency, and opt-in test fault injection (`FaultPlan`).
Decision:
Option 2 chosen.
- Enforces strict store invariants in the simulator so agent tests encounter real store constraints.
- Idempotency records track `(payload_key, result_obj)`: identical replays return the cached object, while differing payloads raise `StoreIdempotencyMismatch`.
- Concurrency serialization via `asyncio.Lock` ensures parallel reservation attempts for the same item cannot both succeed.
- Test-only fault plan isolates transient failures (`timeout_after_commit`, `fail_once`, etc.) without polluting production runtime.
Consequences:
All 28 store write tests pass. `just agent-check` passes cleanly (413 tests). Simulator serves as the trusted test oracle for Phase 2 executor sagas.

## ADR-026 Proposal, Execution and Approval State Machines, Canonical Hashing, and Atomic Transitions  2026-11-10  Accepted
Context:
Task T2.2 requires durable, tested state for proposals, saga executions, seller approvals, and policy decisions, along with canonical proposal hashing to guarantee "what you saw is what you approved" (Spec §2, §6.3, §9.1, §10, §21 amendment 4).
Options considered:
1. In-memory dictionaries or mutable ORM objects with naive JSON hashing and separate audit writes.
2. SQLAlchemy tables attached to shared metadata, cryptographically random non-sequential IDs (`secrets.token_hex(4)` for PRP-, random 8 digits for APR-), canonical card extraction sorting items and strictly isolating model commentary, atomic `UPDATE ... WHERE id = :id AND version = :expected_version AND status IN :valid_from_statuses` checking row count, audit event appended inside the same write transaction, and thread serialization via `_db_write_lock`.
Decision:
Option 2 chosen.
- Loophole closure 1 (What you saw is what you approved): `canonical_card(...)` deterministically extracts only displayed elements (type, order_id, items sorted by item_id, integer paise breakdown, method, decision, policy_version, ISO-8601 UTC expiry). Reason text and model commentary are strictly excluded, preventing prompt injection or commentary changes from altering approval hashes.
- Loophole closure 2 (Sabotage Drill): Amount breakdown is mandatory; omitting amounts raises a TypeError and fails signature generation.
- Loophole closure 3 (Race Conditions & Deadlocks): Using one atomic `UPDATE` with status and version in the WHERE clause, combined with `_db_write_lock` in `write_tx`, prevents SQLite read-to-write lock escalation deadlocks and guarantees that exactly one thread wins a race while the other receives `VersionMismatch`.
- Loophole closure 4 (Audit Atomicity): The transition and audit event commit together in the caller's write transaction; if audit append fails, the transaction rolls back completely, leaving status and version untouched.
- Loophole closure 5 (PII Scrubbing): `sanitize_policy_facts` recursively scrubs customer addresses, phone numbers, email addresses, and tokens before JSON persistence in `policy_decisions`.
Consequences:
All 49 state tests pass (`agent/tests/test_state.py`). Total test suite passes (462 tests) in 47.85s. `just agent-check` passes with zero ruff or mypy errors. Table schemas initialized idempotently via `init_db`.

## ADR-027 Propose Tools Isolation, Atomic Limit Enforcement, and Confirmation Card Hashing  2026-11-10  Accepted
Context:
Task T2.3 requires adding `propose_return` and `propose_refund` as the sole mechanism for the LLM to initiate returns or refunds without write capability against the store (Spec §5 W3/W4, §6.2, §7, §21).
Options considered:
1. Allow propose handlers inside `propose_tools.py` directly or rely on gateway session cache for duplicate calls.
2. Separate validation/orchestration (`tools/propose_tools.py`) from durable limit checking, card building, and persistence (`proposals/service.py`), paired with gateway `ToolKind.READ`-only caching and TOCTOU atomic re-validation.
Decision:
Option 2 chosen.
- Loophole closure 1 (Zero Model Control over Identity or Money): Both `ProposeReturnArgs` and `ProposeRefundArgs` use `extra="forbid"`, `frozen=True`, and expose zero `amount`, `customer_id`, or `role` fields. Identity binds strictly to `ctx.customer_ctx.principal.customer_id`, asserting `order.customer_id == customer_id` (uniform `StoreNotFound` on mismatch).
- Loophole closure 2 (Prompt-Injection Immunity): `sanitize_reason_text` iteratively strips nested HTML tags and control characters and caps at 200 chars, while `reason_text` is excluded from `decide()` and `canonical_card()`, keeping hashes and amounts invariant under adversarial injections (SC-13).
- Loophole closure 3 (Dedup-Before-Limit & TOCTOU Closing): `find_open_or_check_limits` checks existing open proposals for `(customer_id, order_id, canonical sorted items)` before applying the 3-open / 10-IST-day quota, and re-checks inside `write_tx` (`BEGIN IMMEDIATE`) immediately prior to inserting `policy_decisions`, `proposals`, and `audit_events` atomically.
- Loophole closure 4 (Cache Bypass & Hash Parity): `ToolGateway` bypasses `OrderDigestCache` for `ToolKind.PROPOSE` so Clock expiry and DB state stay authoritative, and `proposals/service.py` stores `expires_at` in `%Y-%m-%dT%H:%M:%SZ` UTC format matching `canonical_card` normalization.
- Loophole closure 5 (COD & Refund-Without-Return Guardrails): Rejects `refund_method="original"` on COD orders with `invalid_args`, validates max 3 `evidence_ids` against traversal patterns, and always forces `needs_approval` on `propose_refund` even below the per-refund auto-limit.
Consequences:
All 26 tests in `agent/tests/test_propose_tools.py` pass; total agent test suite reaches 488 passing tests with zero ruff/mypy errors and clean `test-guard` verification. Both modules remain cleanly under 320 lines with single responsibilities.

## ADR-028 Approval cards, customer confirmation flow, and live store revalidation  2026-11-10  Accepted
Context:
Task T2.3b requires wiring the customer confirmation flow (`POST /proposals/{id}/confirm` and `/cancel`), strict allowlisted approval/preview cards (`confirmation`, `return_preview`), FactSheet/Verifier compliance for proposal turns, and hardening against cross-tenant idempotency probing, typed-chat confirmation bypasses (T05), hash tampering, clock advance staleness, overlapping confirmations, and DB lock retention across HTTP calls (KP-01).
Options considered:
1. Handle confirmation directly inside chat text/actions, compare `facts_hash` during store revalidation, and rely on global `idempotency_key` uniqueness.
2. Enforce click-only HTTP endpoints with `ChatActionName(StrEnum)` restriction, per-customer `http_idempotency_keys` (`customer_id, idempotency_key`), ownership-first uniform 404 lookup before idempotency checks, body parsing after auth/idempotency dependencies, live store revalidation via `decide()` outside `write_tx`, overlap guards, and strict allowlisted XSS-stripped card builders.
Decision:
Option 2 chosen.
- Loophole closure 1 (Tenant Isolation & No Idempotency Oracle): `http_idempotency_keys` scopes keys uniquely per `(customer_id, idempotency_key)`, and `_prepare_action_sync` queries `(proposal_id, customer_id)` *before* checking idempotency so foreign or missing proposals always return byte-identical `404 not_found` + `AUTH_DENIED` audit with zero tenant leakage.
- Loophole closure 2 (Pre-Body Dependency Order): Moving JSON body parsing inside `confirm_proposal` and `cancel_proposal` (while preserving `openapi_extra` schema) guarantees unauthenticated or role-rejected callers with empty/malformed bodies receive `401`/`403` + `AUTH_DENIED` audit instead of early FastAPI `422` validation errors; `/cancel` accepts empty `b""` identically to `{}`.
- Loophole closure 3 (Store Revalidation Without False `facts.now` Staleness): `revalidate_against_store` fetches live store facts outside `write_tx` (`KP-01`) and compares deterministic `decide()` `(decision, canonical 4-key non-bool amounts)` rather than recomputing `facts_hash`, preventing normal clock progression inside the 10-minute validity window from falsely failing confirmation.
- Loophole closure 4 (What You Saw Integrity & Overlap Prevention): `validate_stored_proposal` verifies exact 4-key non-bool int amounts (`is_non_bool_int`), `hmac.compare_digest` across recomputed `canonical_card` hash, stored hash, embedded card hash/amounts, and request `proposal_hash`, plus expiry against `Clock`. `check_overlap_guard` blocks confirming overlapping `confirmed`/`executing` proposals on the same `(customer_id, order_id, item_id)` inside both pre-check and `write_tx`.
- Loophole closure 5 (Card Allowlists & T05 Chat Bypass Prevention): `ChatAction.name` uses `ChatActionName(StrEnum)` (6 OpenAPI actions only), `build_confirmation_card` / `build_return_preview_card` strip HTML tags/brackets cleanly via `sanitize_text()` without entity noise, and `FactSheet` + `templates.proposal_confirmation` keep SSE replies grounded and single-sentence.
Consequences:
All 17 tests in `agent/tests/test_proposals_api.py` and all 505 tests in the frozen suite pass with zero `ruff` or `mypy --strict` errors and clean `test-guard` integrity (`43` frozen files, `FROZEN.count` = `505`). T2.3b leaves confirmed proposals atomically in `status="executing"` ready for T2.4 execution saga.

## ADR-029 Confirm/Cancel Handlers, Executor Protocol, and Stale-State Fresh Preview Semantics  2026-11-10  Accepted
Context:
T2.4 requires completing `confirm_proposal` and `cancel_proposal` with: (a) clean decoupling from T2.5's multi-step execution saga via an `Executor` protocol (`get_proposal_executor()` FastAPI dependency), (b) atomic `proposed -> confirmed -> executing` claim *before* executor invocation plus settlement (`succeeded` / `failed` / `unknown` / `executing` / `pending_approval`) *after* executor return, (c) explicit staleness detection (`facts_hash`/snapshot drift, policy version/decision strictness drift, store `409`/`422` state change) returning a `fresh_preview` without calling store writes, and (d) strict preservation of the 43 frozen test files (`tests/FROZEN.sha256`, including `test_proposals_api.py` and `test_state.py`) and `contracts/openapi-agent.yaml`.
Options considered:
1. Return HTTP 410 on expired proposals, transition stale proposals to `status="failed"`, and compare raw `facts_hash` directly (breaking frozen `test_proposals_api.py:672`, `test_state.py:149`, `test_proposals_api.py:821`, and `test_proposals_api.py:720`).
2. Adopt Option A (human-approved):
   - Expired confirms/cancels return `409 invalid_state` with `details={"reason": "expired"}` (matching `contracts/openapi-agent.yaml` and frozen `test_proposals_api.py`) while transitioning `proposed -> expired` and writing `proposal_confirmed`/`proposal_cancelled (DENIED, reason="expired")` in `write_tx`.
   - `stale_state` keeps the original stale proposal row unconfirmed (`status="proposed"`, `confirmed_at=None`, matching frozen `test_state.py`/`test_proposals_api.py`), records `proposal_confirmed (FAILED, reason="stale_state")` alongside any newly persisted replacement proposal inside a single `write_tx`, and returns `409 invalid_state` with `details={"reason": "stale_state", "fresh_preview": ...}`.
   - `revalidate_against_store()` compares non-temporal facts normalized through `normalize_non_temporal_facts()` (`sanitize_policy_facts` + stripping volatile datetime/`reason_text` keys), policy version, decision strictness, and live amounts instead of raw `facts_hash` so benign clock ticks inside the 10-minute window never false-fail.
   - `claim_confirm_sync()` commits `proposed -> confirmed -> executing` + initial `http_idempotency_keys` row + `proposal_confirmed (SUCCESS)` inside `write_tx`; `executor.execute()` runs outside DB locks; `finalize_confirm_sync()` transitions `executing -> succeeded|failed|unknown` and updates `http_idempotency_keys.response_json`.
   - Replacement preview creation passes `exclude_proposal_ids={old_proposal_id}` and `expected_*` matching to `persist_proposal_and_build_card()` so stale proposals never self-deduplicate or falsely trigger the 3-open cap against themselves.
Decision:
Option 2 (Option A) chosen.
Consequences:
All 505 frozen tests and 11 new tests in `agent/tests/test_confirm_handler.py` (516 total) pass unchanged with zero frozen file modifications, `ruff check .`, `mypy src` (`80 source files`), and `test-guard` clean. `PendingExecutor` cleanly bridges confirmations until T2.5 replaces it with the real execution saga.

## ADR-030 Two-Phase Executor Saga, ServiceContext Compensation, and Startup/Background Reconciler  2026-11-10  Accepted
Context:
T2.5 requires executing confirmed `ALLOW` and `NEEDS_APPROVAL` proposals across `agent.db` and the store API without ever holding DB locks across I/O (`KP-01`), duplicating store mutations (`KP-16`), leaking or reusing expired customer JWTs in background/compensation paths (Spec §21 amendment 1), or reporting premature success on store timeouts/crashes (Spec §6.3, §14.7).
Options considered:
1. Run all store steps (`create_return`, `transition_return`, `reserve_refund`) inside a single `write_tx` or pass the customer token through to background retry queues.
2. Decompose `execution/` into focused modules (`common.py`, `db_ops.py`, `finalize_db.py`, `steps.py`, `saga.py`, `reconcile_db.py`, `reconcile_steps.py`, `reconcile.py`) enforcing strict 2-phase commits per step, deterministic `f"{proposal_id}:{step}"` idempotency keys, `"unknown"` state on transient errors, reverse-order `ServiceContext` compensation, startup (`min_age_seconds=0`) + 5s periodic reconciler (`5s -> 30s -> 2m -> stuck`) covering both stalled steps and orphaned crashed proposals, and zero customer token persistence.
Decision:
Option 2 chosen.
- Loophole closure 1 (No Transactions Held Across Store Calls): Every step commits `executions(status="executing")` + audit before invoking `StoreClient`, and opens a second short `write_tx` afterward to record `succeeded`/`failed`/`unknown` + audit (`KP-01`). Verified at runtime by `_TransactionSpyStoreClient` hooking SQLAlchemy `begin`/`commit`/`rollback` and `_db_write_lock`.
- Loophole closure 2 (Customer Token Request Isolation): Only in-request `create_return` receives `customer_ctx`; `transition_return`, `reserve_refund`, `cancel_reservation`, `cancel_return`, and `reconcile_once` (`get_order_admin` matched via `client_ref`) always construct fresh `ServiceContext` (`role=Role.service`, `token=None`). Canaries confirm zero JWT persistence across DB tables or `structlog` output.
- Loophole closure 3 (Between-Step Crash & Audit Rollback Recovery): `scan_reconcile_candidates_sync` detects both `executing`/`unknown` steps and crashed in-flight `executing` proposals that have zero pending execution rows (e.g., crash or audit failure right before step 2), excluding proposals already holding an `approvals` row (`NEEDS_APPROVAL` awaiting seller click).
- Loophole closure 4 (Never Fake Success & Neutral Failure Messages): `StoreTimeout`/`StoreUnavailable`/`TimeoutError` always transition to `"unknown"` (`"We are verifying this request with the store..."`), while terminal failures (`StoreDuplicate`, `StoreLimitExceeded`, `StoreConflict`) run reverse compensation (`cancel_reservation` -> `cancel_return`), record `seller.compensation_failed` on failure, and emit neutral messages devoid of internal limits/caps.
Consequences:
All 528 tests pass (`516` frozen + `12` in `agent/tests/test_execution_saga.py`), `ruff check .` and `mypy src` (`89` source files) pass, `test-guard` passes cleanly, and all 4 sabotage drills fail tests as expected before revert. `PendingExecutor` is replaced by `SagaExecutor` across `confirm_proposal` and `api/proposals.py`.

## CHANGE-003 Customer Approval Withdrawal Endpoint  2026-11-10  Accepted
Context:
T2.6 requires allowing a customer to withdraw a pending seller approval request (`POST /v1/me/approvals/{proposal_id}/withdraw`) before the seller decides, releasing reserved store holds and transitioning the approval to `withdrawn`. Inspecting frozen `contracts/openapi-agent.yaml` showed no `/me/approvals/{proposal_id}/withdraw` path or `WithdrawApprovalResponse` schema existed.
Options considered:
1. Omit customer withdrawal or reuse `/proposals/{id}/cancel` (which is restricted strictly to pre-confirm `proposed` rows).
2. Human-approved contract amendment adding `POST /me/approvals/{proposal_id}/withdraw` (`operationId: withdrawApproval`, `x-required-role: customer`, required `Idempotency-Key`, optional `{}` body, `WithdrawApprovalResponse` `{approval_id, proposal_id, status: "withdrawn"}`) plus `withdrawn` status on `SellerApproval.status`.
Decision:
Option 2 chosen and approved by human before implementation.
Consequences:
`scripts/contract_check.py ../contracts/openapi-agent.yaml` passes (`Total operations: 49 / ALL CHECKS PASSED`). `contracts/openapi-agent.yaml` hash differs from `tests/FROZEN.sha256` until human runs `just freeze-tests`.

## ADR-031 Store-Verified Seller Approvals, Live Revalidation, and Non-Approving Expiry  2026-11-10  Accepted
Context:
T2.6 requires replacing `/seller/approvals*` 501 stubs with deterministic seller approval cards, store-bound approve/reject execution, 15-minute `auth_time` step-up above `staff_approval_limit_paise`, live fact/policy revalidation (`needs_revalidation`), non-approving expiry sweeps, and strict isolation of untrusted customer text from model prompts or recommendations (Spec 02 §§9.1, 9.2, 11, 13, 21 amendments 2 & 9, 23, 24).
Options considered:
1. Trust agent-only role checks and reuse `ServiceContext` for `authorize_refund`, compare naive raw snapshots (which include `order.returns`/`refunds` populated only on admin GETs), and run role/step-up checks after idempotency replay.
2. Decompose `approvals/` into focused <= 385-line modules enforcing:
   - Store-scoped `store_id` isolation (`404` uniform across unknown and foreign-store IDs + `AUTH_DENIED` audit).
   - Seller role limit + 15-minute `auth_time` step-up (`401 step_up_required`) checked BEFORE seller-scoped `(store_id, user_id)` `Idempotency-Key` replay so stale/downgraded credentials never replay prior privileged decisions.
   - Live revalidation outside DB write locks via `ServiceContext` using `build_adjusted_refundable` (excluding own return/refund hold) and normalizing away admin-populated `order.returns`/`order.refunds` to match proposal-time `get_order()`, checking both approval policy and active policy versions.
   - Atomic `pending -> approving` claim before calling Store with ONLY the verified seller's `RequestContext` (`exec_seller_ctx`), exact `reserved_amount`, rollback to `pending` with `last_error` & partial `store_authorization_ref` on Store failure, seller-token `transition_return("rejected")` + `cancel_reservation` on reject, and `ServiceContext` cancellation-only expiry in `expire_stale_approvals`.
   - Rule-based `recommendation` (`approve` only when `R-09` alone fails with low risk on an undrifted eligible return), with customer statement isolated inside HTML-escaped, link-defanged, 200-char truncated `"Unverified customer statement"`.
Decision:
Option 2 chosen.
Consequences:
All 22 tests in `agent/tests/test_seller_approvals.py` and 549/550 full-suite tests pass (`ruff check .`, `mypy --strict`, `contract_check.py`, and all 4 sabotage drills verified). Only frozen Phase 1 `test_gate_g1.py#L382` (asserting 501 stub on `GET /v1/seller/approvals`) fails due to T2.6 implementing that endpoint; test remains unmodified pending human freeze/update in T2.7.

## ADR-032 Signed Webhook Ingestion, Monotonic Versioning, and Pre-Release Safety Gates  2026-11-10  Accepted
Context:
T2.7 requires ingesting signed Store webhooks (`POST /v1/webhooks/store`), guarding against signature forgery, header smuggling, replay attacks, out-of-order deliveries, and 200-event bursts, automatically releasing refunds with `ServiceContext` only when `return.updated=inspected_ok` passes all live store/approval/policy checks, reconciling release faults idempotently, and applying `refund.updated` (`processing`/`completed`/`failed`) lifecycle transitions safely (Spec 01 §§6, 7.4; Spec 02 §§6.3, 9, 12, 21 finding 14, 24).
Options considered:
1. Trust webhook payload status flags directly, use Starlette merged header lookups (`request.headers.get`), track resource versions globally by ID only, inject `get_proposal_store_client` unconditionally on the webhook route, and auto-retry or create new refunds on `refund.updated=failed`.
2. Harden ingestion and split execution across focused <=398-line modules:
   - Fail-closed HMAC verification (`WEBHOOK_SECRET` non-empty even in `DEV_MODE=true`), single raw-header checks via `request.headers.raw`, constant-time `hmac.compare_digest` over `f"{ts}.{raw_body}"`, `-300s .. +60s` replay window, generic `401` responses with `AUTH_DENIED` audit, `413` over 1 MB, and `X-Event-Id == body.event_id` (`400`).
   - Atomic `webhook_events_seen` deduplication + `(resource_type, resource_id)` namespaced monotonic `webhook_resource_versions` inside one short `write_tx`, paired with lazy `_resolve_webhook_store_client` so audit-only event bursts avoid eager Store/JWKS setup (`<5s` for 200 events).
   - Pure `verify_release_safety` pre-checks outside DB locks (`ReturnStatus.inspected_ok`, `RefundStatus.reserved|authorized` blocking `processing|completed`, exact match with proposal & `adjusted_refundable`, `NEEDS_APPROVAL` seller `ApprovalStatus.approved` + `store_authorization_ref` + `RefundStatus.authorized` + exact `authorized_amount_paise`, `ALLOW` autonomy cap check, and live `decide()` under active & proposal policies) prior to `ServiceContext` two-phase `release_refund` (`{pid}:release_refund`).
   - Reconciler `release_refund` step delegating safety checks to `prepare_release_reconcile` (resolving committed `processing`/`completed` on `timeout_after_commit` with zero duplicate store release calls; retrying `fail_once` with identical idempotency key) and `apply_refund_updated_sync` supporting `ProposalStatus.succeeded -> ProposalStatus.failed` with monotonic regression guards and sanitized customer `client_events` (zero replacement refund creation).
Decision:
Option 2 chosen.
Consequences:
All 16 tests in `agent/tests/test_webhooks_release.py`, all 566 full-suite tests, `ruff check .`, `mypy src` (`112 source files`), `test-guard`, and all 3 sabotage drills (skip signature check, skip `inspected_ok` gate, ignore amount/authorized ceiling) pass cleanly. `test_webhooks_release.py` awaits human `just freeze-tests`.

## ADR-033 Store-Web Pure-WASM Rollup Override and Live Store Integration Fixes  2026-11-10  Accepted
Context:
Running the full project locally hit three blockers: (1) Windows Application Control (`ERR_DLOPEN_FAILED`) blocks the native `rollup-win32-x64-msvc.node` binary needed by `vite` / `vitest` in `store-web` alongside two TypeScript/ESLint unused-symbol errors (`ChatWidget.tsx`, `useChatStream.ts`); (2) cross-service integration gaps between `store-api` (`:8000`) and `agent` (`:8001`) across JWT issuer/headers (`nestkart-store`, `kid`, `iat`, `/.well-known/jwks.json`), `GET /api/v1/admin/settings` monitor read access, `agent-executor` credential wiring + saga return cancellation cap bypass, `AdminOrderDetail` + `client_ref` parity, `Refund` `"authorized"` mapping, naive UTC datetimes, DTO field mismatches (`items`, `name_snapshot`, `flags_json`, partial `ship_to_json`, `max_refundable_paise`, `total_refunds_paise`), `refund.updated` `return_id`, and `WebhookEnvelope` wire serialization; (3) missing `store-api/.env` / `.env.example`.
Options considered:
1. Disable lockfile pinning in `store-web` or weaken strict schemas (`extra="ignore"`) in `agent/domain/models.py`.
2. Add an explicit npm `"overrides": { "rollup": "npm:@rollup/wasm-node@4.68.1" }` in `store-web/package.json` with updated `store-web/package-lock.json` (preserving `test_sec_11_pinned_dependencies`), fix unused TS symbols cleanly, keep `agent` strict domain models intact by normalizing store responses in a dedicated `store/normalizers.py` module, preserve `store-api` DB `EventsOutbox.payload_json` flat format while wrapping HTTP webhook POSTs in signed `WebhookEnvelope`, add `client_ref` / `AdminOrderDetailResponse` in `store-api`, and create non-secret `store-api/.env` + `.env.example`.
Decision:
Option 2 chosen (human-approved: `"1 both ,2 fix all, 3 there is no env i found in store api create one if not there"`).
Consequences:
`store-web` lint/build/unit tests work under Windows WDAC with pinned `package-lock.json`; `store-api` and `agent` interoperate end-to-end with zero relaxed Pydantic/verifier/auth invariants and zero modified frozen test files.

## ADR-034 Centralized Mutator Architecture and Tamper-Evident Decision Trace API  2026-11-11  Accepted
Context:
T2.8 requires ensuring every mutation is recorded atomically in the SHA-256 audit log, centralizing public mutators so no database write bypasses audit logging, adding telemetry tables (`llm_calls`, `tool_calls`), exposing seller-facing audit querying (`GET /v1/seller/audit`) and chain verification (`GET /v1/seller/audit/verify`), and assembling comprehensive decision traces (`GET /v1/seller/traces/{request_id}`) explaining the lifecycle of any customer request across 10 canonical stages with complete PII/token redaction and IDOR protection.
Options considered:
1. Scatter ad-hoc audit writes across callers, allow direct table queries in endpoints, and return unfiltered audit rows to callers.
2. Enforce a single-writer AST architecture where mutations occur strictly through `domain.state.transition()`, `AuditService.append()`, and `execution/db_ops.py` writer functions; persist `llm_calls` and `tool_calls` in the orchestrator/pipeline; build a canonical 10-stage decision trace with presentation-time HTML escaping, regex-based PII scrubbing, and fail-safe IDOR isolation.
Decision:
Option 2 chosen.
Consequences:
All 39 mutators are audited atomically within their write transactions; AST scanner `_check_single_writer_ast()` guarantees compliance; seller audit and trace endpoints pass all security, pagination, and scrubbing tests without altering frozen test suites.

## ADR-035 Real Store Writes, Token Isolation, and Full-Flow Handshake (T2.9)  2026-11-11  Accepted
Context:
Task T2.9 requires extending `HttpStoreClient` with all mutating write endpoints (`create_return`, `transition_return`, `reserve_refund`, `authorize_refund`, `release_refund`, `cancel_reservation`, `update_settings`), enforcing strict context-based token routing and isolation (customer token on customer steps, seller token on seller steps, service token via client-credentials for service steps cached until 60s before expiry), guaranteeing zero automatic retries on writes, comprehensive 4xx/5xx/network/timeout error mapping, and creating `scripts/handshake_money.py` verifying full end-to-end money flow against live services.
Options considered:
1. Rely on mock/fixture write client, permit token forwarding across distinct roles, or add automatic retries on mutating endpoints.
2. Extend `HttpStoreClient` with dedicated `_execute_post` and `_execute_put` without retry logic; enforce token isolation (customer tokens rejected on service calls, service tokens rejected on seller calls); issue service tokens via `POST /auth/token` with 60s pre-expiration caching; map error codes explicitly (`StoreDuplicate`, `StoreLimitExceeded`, `StoreInvalidState`, `StoreIdempotencyMismatch`, `StoreNotFound`, `StoreForbidden`, `StoreUnavailable`, `StoreTimeout`); create automated dev tool `scripts/handshake_money.py` driving customer chat -> proposal -> confirm -> seller approve -> admin return advancement -> webhook/SimPay completion.
Decision:
Option 2 chosen.
Consequences:
Sagas and reconcilers maintain single ownership of write retries and idempotency; no cross-role token leakage occurs; `just agent-check` passes with test integrity verified; end-to-end money lifecycle is verified against the real store API.

## ADR-036 Money-Path Adversarial Red-Team Suite and Independent Ledger Oracle (T2.11)  2026-11-11  Accepted
Context:
Task T2.11 requires a comprehensive adversarial test suite targeting the money-movement write path across 8 personas (Replay & tamper customer, Racer, Splitter, Greedy seller, Webhook attacker, Fault injector, Obedient model, Seller-side injector) with at least 60 attacks, an independent ledger oracle asserting financial and item invariants directly from store fixtures and audit records, real multi-threaded race execution, fault injection attacks (store timeouts, 500s, audit failures, clock jumps), live replay runner (`scripts/redteam_money_live.py`), and sabotage verification drills.
Options considered:
1. Re-run previous T1.11 test suite with superficial money checks or inline assertions inside handlers.
2. Develop an independent 68-scenario adversarial corpus in `scenarios/redteam/money.yaml`, build an independent financial `LedgerOracle` in `agent/tests/redteam/ledger.py` verifying cumulative releases, item returns, authorization boundaries, daily caps, and audit chain integrity, execute attacks via `TestClient` with `ObedientFakeLLM` across `test_money_attacks.py`, `test_money_races.py` (20 parallel confirms and concurrent approvals), and `test_money_faults.py` (FaultPlan injection), implement `scripts/redteam_money_live.py` for live store execution, and verify failure sensitivity through 3 defense sabotage drills.
Decision:
Option 2 chosen.
Consequences:
The money-movement write path is provably defended against adversarial replay, race conditions, limit splitting, privilege escalation, and store faults; financial invariants hold without exception; sabotage drills confirm all tests fail if defenses are removed.

## ADR-037 Support Case Deterministic Brief and Atomic Handler Model (T3.1)  2026-11-11  Accepted
Context:
Task T3.1 requires persistent support case creation, deterministic briefs, escalation triggers, customer/seller case endpoints, and an atomic handler model (`agent` <-> `human`) ensuring the seller and autonomous agent never talk over each other.
Options considered:
1. Use LLM to summarize case history and generate recommended next steps, and let agent continue suggesting actions while human handled.
2. Build a completely deterministic brief builder (`cases/brief.py`) that sanitizes untrusted customer input (HTML escaping, token stripping, tagged `verified: False`), derives recommendations from hardcoded category rule tables (immune to prompt injection), atomically flips the handler to `human` upon seller reply with optimistic concurrency, and enforces strict agent silence and zero propose tools while `handler == "human"`.
Decision:
Option 2 chosen.
Consequences:
Model hallucination and prompt injection cannot steer seller recommendations; sellers and the autonomous agent never talk over each other; all 10 T3.1 unit and integration tests pass cleanly; test suite integrity is preserved.

## ADR-038 Structured Customer Memory: IDs/Enums Only, Live Chip Lookups, and Prompt Data Boundaries  2026-11-11  Accepted
Context:
Task T3.3 requires persistent, visible, safe customer memory across sessions (`customer_memory`, `memory_events`), domain event writer hooks, snapshot greeting chips in `POST /v1/chat/sessions`, sanitized injection into `understand()`, and `DELETE /v1/me/memory` without ever storing free text or reading memory in policy or authorization code (Spec 02 §8, §24).
Options considered:
1. Store conversational summaries or cached order/return status labels as free text in memory rows and trust snapshot dictionaries directly in prompt construction.
2. Enforce structural schema-by-construction (`CustomerProfile` with `extra="forbid"`, `<= 64` char limit per string, max 20 `open_item_ids`, validated enums/IDs/dates/counts only), record domain events deterministically inside caller `write_tx` transactions (`memory/writer.py`), omit mutable status words from memory by routing `"Check return RET-xxxx"` chip taps directly to live read tools/views (`orchestrator/pipeline.py`), derive prompt `customer_id` exclusively from verified `ctx.customer_id` wrapped inside `<data><memory>`, and honor `app.dependency_overrides[get_store_client]` in `api/chat.py` while returning strict `{"erased": true}` on `DELETE /v1/me/memory`.
Decision:
Option 2 chosen.
- Loophole closure 1 (Strict Prompt Boundary): `understand/service.py` ignores `memory_snapshot["customer_id"]`, validates `ctx.customer_id` via `_MEMORY_CUSTOMER_ID_RE`, and parses profile fields through `CustomerProfile` (`extra="forbid"`) inside `<data><memory>`.
- Loophole closure 2 (OpenAPI Erasure Parity): `memory/router.py` returns `{"erased": True}` strictly matching `contracts/openapi-agent.yaml` (`additionalProperties: false`).
- Loophole closure 3 (Live Status Chip Dependency Parity): `api/chat.py` resolves `app.dependency_overrides[get_store_client]` in `get_pipeline` so greeting chip taps query live mutated store state in both production and isolated tests.
Consequences:
All 6 tests in `agent/tests/test_memory.py` (SC-04 snapshot + live chip tap, cross-customer isolation, Hypothesis 64-char/no-free-text property test, stored injection resistance across all writer fields, mid-session erasure + audit, AST isolation check) and `test-guard` pass cleanly.

## ADR-040 Seller Overview, Dev Controls, and Repeatable Demo Run Architecture (T3.5)  2026-11-11  Accepted
Context:
Task T3.5 requires the seller operational overview (`GET /v1/seller/overview`), dev fault injection controls (`POST /v1/dev/faults`, `GET /v1/dev/faults`), state reset (`POST /v1/dev/reset`), and a standalone dev demo script (`scripts/demo_run.py`) executing 4 core beats with cryptographic audit verification (Spec 02 §11, §14.7, §24).
Options considered:
1. Expose dev routes unconditionally or under simple headers, use persistent files for dev faults, and write demo scripts with hardcoded timing sleeps.
2. Architecture invariants and safeguards:
   - Guard `/v1/dev/*` and `/dev/*` routes with `require_dev_mode` as router-level dependency, returning `404` (`code="not_found"`) before any auth or JWKS calls when `DEV_MODE=false`.
   - Keep dev faults (`llm_down`, `audit_fail`, `mail_down`, `slow_llm`) strictly in memory, never persisting to disk or surviving process restarts; display them in `/readyz` only when `DEV_MODE=true`.
   - Implement `POST /v1/dev/reset` to wipe 19 state tables, reset faults/cooldowns, reload policies `v1`..`v3` (`v3` active), and restart the SHA-256 audit chain with a verified genesis event (`seq=1`, `prev_hash="0"*64`) via `AuditService.reset_chain` (preserving AST single-writer invariant).
   - Implement `GET /v1/seller/overview` returning overdue approvals by Clock comparison, open cases by priority, failed notification IDs, last monitor run summary, and today's metrics using the IST (`Asia/Kolkata`) calendar date, exposing IDs only with zero customer PII and zero forbidden internal words.
   - Implement `scripts/demo_run.py` executing system reset followed by 4 distinct beats through the real HTTP API pipeline, printing PASS/FAIL per beat, and exiting non-zero on any failure.
Decision:
Option 2 chosen.
Consequences:
All 6 tests in `test_t35_overview_dev_demo.py` pass, `scripts/demo_run.py` passes consecutively twice on all 4 beats, and `ruff check src/` and `mypy src` are completely clean.

## ADR-041 Proactive Delay Monitor Architecture, Concurrency Lock, and Deduplication Key Design (T3.4)  2026-11-11  Accepted
Context:
Task T3.4 requires an autonomous proactive delay monitor that detects late shipments, alerts affected customers once, opens cases for lost shipments or carrier exceptions of 5 days or more, and provides a manual seller run button with concurrency protection (Spec 02 §§5 W2/W9, 9.5, 13, 21).
Options considered:
1. Scan on every customer message or poll without locks, notify without structured deduplication keys, and allow parallel overlapping store scans.
2. Architecture invariants and safeguards:
   - Autonomous background service `run_monitor(service_ctx, clock)` querying `list_shipments_for_monitor` via service context only (`Role.service` with zero caller token). Customer tokens or tainted tokens are blocked (`StoreForbidden`).
   - Hard cap of 200 shipments per run with cursor pagination and loop guard detecting repeated cursors (`seen_cursors`).
   - Strict deduplication keys for outbox notifications (`delay:{order_id}:{eta_date}:{kind}`) and `monitor_events` (`{order_id}:{eta_date}:{kind}`). Repeat runs create zero duplicate notifications and zero duplicate cases (T18).
   - Async run lock `_run_lock`: a concurrent request immediately receives the currently active `run_id` without executing duplicate store scans.
   - Seller endpoint `POST /v1/seller/monitor/run` guarded by `require_seller` and a 10s cooldown per Clock returning 429 `rate_limited`.
   - Separation of case triggers: in-transit delays offer guided action chips ("Wait", "Cancel if not delivered", "Talk to support") without opening cases; high-priority cases with deterministic briefs are opened strictly for lost shipments or carrier exceptions >= 5 days.
Decision:
Option 2 chosen.
Consequences:
All 6 tests in `agent/tests/test_monitor.py` pass cleanly, full suite passes, and sabotage drills verify dedupe key and run lock protections.






=======
=======


Entry format:
## ADR-0xx Title  Date  Status
Context / Options considered / Decision / Consequences
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
