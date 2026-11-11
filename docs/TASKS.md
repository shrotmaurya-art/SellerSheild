# TASKS
Status legend: TODO, DOING, DONE, BLOCKED. Frozen tests are listed at the bottom.

## Phase 0 - Foundations (agent path)
<<<<<<< HEAD
=======
<<<<<<< HEAD
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
T0.1  Install toolchain (human)                         DONE
T0.2  Accounts and keys (human)                         TODO
T0.3  Antigravity install and safety settings (human)   DONE
T0.4  Repository skeleton, rules and memory files       DONE
T0.5  Specs into the repo and rules smoke test          TODO
T0.6  Store and agent OpenAPI contracts (milestone M0)  DONE
T0.7  Agent service skeleton                            DONE
T0.8  Database, clock, money and time helpers           DONE
T0.9  JWT and JWKS verification                         DONE
T0.10 LLM adapter and provider chain                    DONE
<<<<<<< HEAD
T0.11 Gate G0, local commit and tag (human)             DONE

## Phase 1 - Walking Skeleton (read-only path)
T1.0b Test guard                                        DONE
T1.1  Domain models, StoreClient interface and fixtures DONE
T1.2  Policy documents and refund calculator            DONE
T1.3  Rules, decision engine, risk and scenario tests   DONE
T1.4  Audit service with hash chain                     DONE
T1.5  Tool gateway and read tools                       DONE
T1.6  FactSheet, verifier and templated replies         DONE
T1.7  Understanding stage and deterministic router      DONE
T1.8  Orchestrator, SSE chat and guided actions         DONE
T1.9  Real store client and handshake                   DONE
T1.10 Gate G1 tests and auth boundaries                 DONE
T1.11 Red-team suite: attackers and clumsy users      DONE

## Phase 2 - Write Path, Mutations and Approvals
T2.1  Store write methods and write-capable fixture store DONE
T2.2  Proposal, execution, approval state and hashing     DONE
T2.3  Propose tools (propose_return, propose_refund)      DONE
T2.3b Approval cards and customer confirmation flow       DONE
T2.4  Confirm and cancel handlers                         DONE
T2.5  Execution saga and two-phase write                  DONE
T2.6  Seller approval portal and step-up auth             DONE
T2.7  Store webhooks and refund release                   DONE
T2.8  Gate G2 tests, mutation audits and Decision Trace   DONE
T2.9  Real store writes and full-flow handshake           DONE
T2.11 Money-path red-team suite                           DONE
T2.12 Gate G2 tests and flow script                       DONE
INT.1 Store API, Store Web, and Agent live integration    DONE

## Phase 3 - Autonomous Operations & Seller Tools
T3.0  Shared stubs and setup (interfaces for lean Phase 3)  DONE
T3.1  Cases, escalation triggers and handler model          DONE
T3.2  Push stream, notification outbox and notifications    DONE
T3.3  Structured memory                                     DONE
T3.4  Proactive delay monitor                               DONE
T3.5  Overview, dev controls and demo run script            DONE
T3.6  Gate G3 tests and red team                            DONE
T3.7  Real store: mail and shipments                        DONE

## Phase 4 - Hardening, Security & Evaluation
L4.3  demo_check: the demo beats as one command             DONE
L4.4  Security minimum: route sweep, DEV gating, log redaction DONE

## DEFERRED TESTS
- stream cursor edge cases
- retention
- slow-consumer resync
- daily notice caps
- outbox lease recovery
- SLA overdue
- language 3-of-5 rule
- multi-intent
- backwards clock
- large-data performance
- evidence flow (SC-11)
- Policy Studio
- analytics

FROZEN tests:
- agent/tests/test_auth.py (24 tests)
- agent/tests/test_config.py (2 tests)
- agent/tests/test_cors.py (3 tests)
- agent/tests/test_db.py (10 tests)
- agent/tests/test_errors.py (6 tests)
- agent/tests/test_fixtures.py (10 tests)
- agent/tests/test_health.py (3 tests)
- agent/tests/test_llm.py (17 tests)
- agent/tests/test_logging.py (3 tests)
- agent/tests/test_money.py (20 tests)
- agent/tests/test_request_id.py (3 tests)
- agent/tests/test_timeutil.py (10 tests)
- agent/tests/test_store_write.py (28 tests)
- agent/tests/test_policy.py (20 tests)
- agent/tests/test_decide.py (35 tests)
- agent/tests/test_audit.py (12 tests)
- agent/tests/test_tools.py (13 tests)
- agent/tests/test_verifier.py (26 tests)
- agent/tests/test_understand.py (10 tests)
- agent/tests/test_chat.py (14 tests)
- agent/tests/test_store_http.py (24 tests)
- agent/tests/e2e/test_gate_g1.py (5 tests)
- agent/tests/redteam/test_attacks.py (80 tests)
- agent/tests/redteam/test_protocol.py (9 tests)
- agent/tests/redteam/test_flood.py (4 tests)
- agent/tests/redteam/test_clumsy.py (10 tests)
- agent/tests/test_state.py (49 tests)
- agent/tests/test_propose_tools.py (26 tests)
- agent/tests/test_proposals_api.py (17 tests)
- agent/tests/test_confirm_handler.py (11 tests)
- agent/tests/test_execution_saga.py (12 tests)
- agent/tests/test_seller_approvals.py (22 tests)
- tests/guard/test_guard.py (12 tests)
UNFROZEN tests (pending human freeze):
- agent/tests/audit_test_helpers.py
- agent/tests/e2e/test_gate_g2.py (18 tests)
- agent/tests/e2e/test_gate_g3.py (9 tests)
- agent/tests/redteam/ledger.py
- agent/tests/redteam/money_support.py
- agent/tests/redteam/test_money_attacks.py
- agent/tests/redteam/test_money_faults.py
- agent/tests/redteam/test_money_races.py
- agent/tests/redteam/test_phase3.py (30 tests)
- agent/tests/test_audit_mutators.py
- agent/tests/test_audit_mutators_coverage.py
- agent/tests/test_audit_views.py
- agent/tests/test_audit_views_trace.py
- agent/tests/test_cases_escalation.py (10 tests)
- agent/tests/test_memory.py (6 tests)
- agent/tests/test_monitor.py (6 tests)
- agent/tests/test_phase3_shared.py (3 tests)
- agent/tests/test_push_outbox.py (14 tests)
- agent/tests/test_store_http_compat.py
- agent/tests/test_store_http_phase3.py (15 tests)
- agent/tests/test_store_http_writes.py (14 tests)
- agent/tests/test_t35_overview_dev_demo.py (6 tests)
Total: 566 locked in tests/FROZEN.sha256 across 46 frozen manifest entries; 169 passing unfrozen integration/audit/write/phase-3 tests (total 735 suite tests passing)
=======
=======
T0.1  Install toolchain (human)                         TODO
T0.2  Accounts and keys (human)                         TODO
T0.3  Antigravity install and safety settings (human)   TODO
T0.4  Repository skeleton, rules and memory files       TODO
T0.5  Specs into the repo and rules smoke test          TODO
T0.6  Store and agent OpenAPI contracts (milestone M0)  TODO
T0.7  Agent service skeleton                            TODO
T0.8  Database, clock, money and time helpers           TODO
T0.9  JWT and JWKS verification                         TODO
T0.10 LLM adapter and provider chain                    TODO
>>>>>>> 464b2531d34564b356d74ce0a20c885038d9957b
T0.11 Gate G0, local commit and tag (human)             TODO

FROZEN tests: none yet
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
