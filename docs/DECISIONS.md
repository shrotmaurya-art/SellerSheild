# DECISIONS (choices and reasons; do not reverse without a new entry)
ADR-001 SQLite files for store and agent (no Supabase or Firebase): transactions, zero setup, offline demo.
ADR-002 Hand-written orchestrator, no LangChain: control of loops, allowlists, audit and tests.
ADR-003 OpenAI-compatible client for all LLM providers: one adapter, config switches provider.
ADR-004 LLM tools are read and propose only; writes live in the execution layer after a human click.
ADR-005 uv for Python and just for tasks on Windows (PowerShell shell); no global pip.
ADR-006 Store is the identity provider (RS256 and JWKS); agent only verifies.
<<<<<<< HEAD
ADR-007 Store OpenAPI 3.1 contract design and loophole closures.
ADR-008 Agent OpenAPI 3.1 contract design and security guardrails.
ADR-09 Agent service skeleton, config fail-fast, and redacted logging.
ADR-010 Database engine safety recipe, clock abstraction, money arithmetic, and IST time utilities.
ADR-011 JWT RS256 token verification, JWKS caching and resilience, and role-based FastAPI dependencies.
ADR-012 LLM provider chain, failover, circuit breaker, and safe caching.

Entry format:
## ADR-0xx Title  Date  Status
Context / Options considered / Decision / Consequences

## ADR-008 Store OpenAPI 3.1 Contract and Loophole Closures  2026-11-11  Accepted
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

## ADR-009 Agent OpenAPI 3.1 Contract and Security Guardrails  2026-11-11  Accepted
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
=======


Entry format:
## ADR-0xx Title  Date  Status
Context / Options considered / Decision / Consequences
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
