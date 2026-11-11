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

<<<<<<< HEAD
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

Registry (planned):
F-A00 Contracts (done)                    F-A11 Proposals and confirm
F-A01 Config and logging (done)           F-A12 Executor saga
F-A02 Error envelope and request ids (done) F-A13 Approvals
F-A03 Database layer (done)               F-A14 Audit chain
F-A04 Money and time helpers (done)       F-A15 Cases and handler model
F-A05 JWT and JWKS verification (done)     F-A16 Memory
F-A06 LLM provider chain (done)            F-A17 Notification outbox
F-A07 Policy engine                       F-A18 Proactive monitor
F-A08 FactSheet and verifier              F-A19 Push stream
F-A09 Tool gateway                        F-A20 Evaluation harness
F-A10 Orchestrator
=======
Registry (planned):
F-A01 Config and logging              F-A11 Proposals and confirm
F-A02 Error envelope and request ids  F-A12 Executor saga
F-A03 Database layer                  F-A13 Approvals
F-A04 Money and time helpers          F-A14 Audit chain
F-A05 JWT and JWKS verification       F-A15 Cases and handler model
F-A06 LLM provider chain              F-A16 Memory
F-A07 Policy engine                   F-A17 Notification outbox
F-A08 FactSheet and verifier          F-A18 Proactive monitor
F-A09 Tool gateway                    F-A19 Push stream
F-A10 Orchestrator                    F-A20 Evaluation harness
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
