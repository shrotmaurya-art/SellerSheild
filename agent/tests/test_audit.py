"""Tests for the tamper-evident audit service with hash chain.

Covers:
- Genesis link for the first event
- Sequential verification across 100 events
- Tamper detection on any stored column
- Breakage detection on middle row deletion
- 8-thread concurrency against a real SQLite file (no duplicate prev_hash, 200 events)
- Atomic rollback if business statement fails
- Atomic rollback if audit append fails
- Redaction of fake bearer token and email addresses
- Complete exclusion of the word 'Authorization'
- 500-character string truncation and non-JSON rejection
- Foreign customer ID scrub in denial events
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from sellershield_agent.audit import (
    ActorType,
    AuditEvent,
    AuditService,
    ChainStatus,
    EventType,
    Outcome,
    sanitize_details,
)
from sellershield_agent.db import (
    create_engine_from_url,
    get_metadata,
    init_db,
    read_tx,
    write_tx,
)


def _make_sample_event(
    event_id: str = "evt-001",
    action: str = "read_order",
    details: dict[str, Any] | None = None,
    outcome: Outcome = Outcome.SUCCESS,
    actor_id: str | None = "CUST-001",
    event_type: EventType = EventType.MESSAGE_RECEIVED,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        ts_utc="2026-11-08T14:00:00Z",
        request_id="req-123",
        session_id="sess-456",
        actor_type=ActorType.CUSTOMER,
        actor_id=actor_id,
        event_type=event_type,
        resource_type="order",
        resource_id="ORD-999",
        action=action,
        outcome=outcome,
        policy_version="v1",
        details=details if details is not None else {"note": "sample"},
    )


def test_empty_chain_verifies() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    with read_tx(engine) as conn:
        status: ChainStatus = AuditService.verify_chain(conn)

    assert status.ok is True
    assert status.checked == 0
    assert status.first_broken_seq is None
    engine.dispose()


def test_first_event_links_to_genesis() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    sample = _make_sample_event(event_id="evt-genesis")
    with write_tx(engine) as tx:
        seq = AuditService.append(tx, sample)

    assert seq == 1

    with read_tx(engine) as conn:
        row = conn.execute(
            text("SELECT prev_hash, hash, seq FROM audit_events WHERE seq = 1")
        ).fetchone()
        assert row is not None
        assert row[0] == "0" * 64
        assert len(row[1]) == 64

        status = AuditService.verify_chain(conn)
        assert status.ok is True
        assert status.checked == 1
        assert status.first_broken_seq is None

    engine.dispose()


def test_100_sequential_events_verify() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    for i in range(1, 101):
        evt = _make_sample_event(
            event_id=f"evt-{i:03d}",
            action=f"action_{i}",
            details={"step": i},
        )
        with write_tx(engine) as tx:
            seq = AuditService.append(tx, evt)
            assert seq == i

    with read_tx(engine) as conn:
        rows = conn.execute(
            text("SELECT seq, prev_hash, hash FROM audit_events ORDER BY seq ASC")
        ).fetchall()
        assert len(rows) == 100

        # Verify cryptographic link from row i to row i-1
        assert rows[0][1] == "0" * 64
        for idx in range(1, 100):
            assert rows[idx][1] == rows[idx - 1][2]

        status = AuditService.verify_chain(conn)
        assert status.ok is True
        assert status.checked == 100
        assert status.first_broken_seq is None

    engine.dispose()


def test_changing_any_stored_field_breaks_verify() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    for i in range(1, 11):
        evt = _make_sample_event(
            event_id=f"evt-{i:03d}",
            action=f"action_{i}",
            details={"step": i},
        )
        with write_tx(engine) as tx:
            AuditService.append(tx, evt)

    # Initial check passes
    with read_tx(engine) as conn:
        status_clean = AuditService.verify_chain(conn)
        assert status_clean.ok is True
        assert status_clean.checked == 10

    # Every stored column (excluding primary key seq itself) must invalidate hash check
    columns_to_tamper = [
        ("event_id", "'tampered-event'"),
        ("ts_utc", "'2099-01-01T00:00:00Z'"),
        ("request_id", "'tampered-req'"),
        ("session_id", "'tampered-sess'"),
        ("actor_type", "'seller'"),
        ("actor_id", "'tampered-actor'"),
        ("event_type", "'auth_denied'"),
        ("resource_type", "'tampered-res'"),
        ("resource_id", "'ORD-TAMPERED'"),
        ("action", "'tampered-action'"),
        ("outcome", "'failed'"),
        ("policy_version", "'v99'"),
        ("details_json", "'{\"tampered\": true}'"),
        ("prev_hash", "'f' * 64"),
        ("hash", "'0' * 64"),
    ]

    target_seq = 5

    for col, new_val in columns_to_tamper:
        with write_tx(engine) as tx:
            orig = tx.execute(
                text(f"SELECT {col} FROM audit_events WHERE seq = :seq"),
                {"seq": target_seq},
            ).scalar()

            if col == "prev_hash":
                val_expr = "f" * 64
            elif col == "hash":
                val_expr = "0" * 64
            else:
                val_expr = new_val.strip("'")

            tx.execute(
                text(f"UPDATE audit_events SET {col} = :val WHERE seq = :seq"),
                {"val": val_expr, "seq": target_seq},
            )

        with read_tx(engine) as conn:
            status_tampered = AuditService.verify_chain(conn)
            assert status_tampered.ok is False
            assert status_tampered.first_broken_seq == target_seq

        # Revert change before next column test
        with write_tx(engine) as tx:
            tx.execute(
                text(f"UPDATE audit_events SET {col} = :orig WHERE seq = :seq"),
                {"orig": orig, "seq": target_seq},
            )

    engine.dispose()


def test_deleting_middle_row_breaks_verify() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    for i in range(1, 11):
        evt = _make_sample_event(event_id=f"evt-{i:03d}")
        with write_tx(engine) as tx:
            AuditService.append(tx, evt)

    # Delete row 5
    with write_tx(engine) as tx:
        tx.execute(text("DELETE FROM audit_events WHERE seq = 5"))

    with read_tx(engine) as conn:
        status = AuditService.verify_chain(conn)
        assert status.ok is False
        # The chain breaks because row 6's prev_hash points to deleted row 5's hash
        assert status.first_broken_seq in (5, 6)

    engine.dispose()


def test_concurrent_threads_append_valid_chain(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent_audit.db"
    engine = create_engine_from_url(f"sqlite:///{db_file}")
    init_db(engine, get_metadata())

    num_threads = 8
    events_per_thread = 25
    total_events = num_threads * events_per_thread

    def worker(thread_idx: int) -> list[int]:
        inserted_seqs: list[int] = []
        for j in range(events_per_thread):
            event = _make_sample_event(
                event_id=f"t{thread_idx}-evt-{j:02d}",
                action=f"thread_action_{thread_idx}_{j}",
                details={"thread": thread_idx, "iter": j},
            )
            with write_tx(engine) as tx:
                s = AuditService.append(tx, event)
                inserted_seqs.append(s)
        return inserted_seqs

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, t) for t in range(num_threads)]
        for f in as_completed(futures):
            res = f.result()
            assert len(res) == events_per_thread

    with read_tx(engine) as conn:
        count = conn.execute(text("SELECT count(*) FROM audit_events")).scalar()
        assert count == total_events

        prev_hashes = conn.execute(
            text("SELECT prev_hash FROM audit_events")
        ).scalars().all()
        # No duplicate prev_hash exists
        assert len(set(prev_hashes)) == total_events

        status = AuditService.verify_chain(conn)
        assert status.ok is True
        assert status.checked == total_events
        assert status.first_broken_seq is None

    # KP-21: Dispose engine to release Windows file locks
    engine.dispose()


def test_business_statement_failure_rolls_back_audit() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    event = _make_sample_event(event_id="evt-rollback-biz")

    with pytest.raises(RuntimeError, match="business failed"):
        with write_tx(engine) as tx:
            AuditService.append(tx, event)
            # Business statement in the same transaction fails
            raise RuntimeError("business failed")

    with read_tx(engine) as conn:
        count = conn.execute(text("SELECT count(*) FROM audit_events")).scalar()
        assert count == 0

    engine.dispose()


def test_audit_failure_rolls_back_business_statement() -> None:
    engine = create_engine_from_url("sqlite:///:memory:")
    init_db(engine, get_metadata())

    # Create dummy business table
    with write_tx(engine) as tx:
        tx.execute(text("CREATE TABLE business_orders (id TEXT PRIMARY KEY, amount INTEGER)"))

    # Attempt transaction: insert business order, but audit append raises due to non-JSON details
    with pytest.raises(ValueError, match="not JSON"):
        with write_tx(engine) as tx:
            tx.execute(
                text("INSERT INTO business_orders (id, amount) VALUES ('ORD-1', 500)")
            )
            # Non-JSON details object (a function) causes append to raise
            bad_event = AuditEvent(
                event_id="evt-bad",
                ts_utc="2026-11-08T14:00:00Z",
                request_id="req-1",
                session_id=None,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                event_type=EventType.TOOL_CALL,
                resource_type=None,
                resource_id=None,
                action="call_tool",
                outcome=Outcome.FAILED,
                policy_version=None,
                details={"bad_func": lambda: None},
            )
            AuditService.append(tx, bad_event)

    # Verify that business change was rolled back
    with read_tx(engine) as conn:
        biz_count = conn.execute(text("SELECT count(*) FROM business_orders")).scalar()
        assert biz_count == 0
        audit_count = conn.execute(text("SELECT count(*) FROM audit_events")).scalar()
        assert audit_count == 0

    engine.dispose()


def test_sanitize_removes_fake_bearer_token_and_email() -> None:
    raw_details = {
        "header": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.sig",
        "email": "customer.secret@example.com",
        "nested": {
            "token": "secret_token_12345",
            "contact": "support.desk+tag@sellershield.internal",
        },
    }

    sanitized_str = sanitize_details(raw_details)
    parsed = json.loads(sanitized_str)

    assert "customer.secret@example.com" not in sanitized_str
    assert "support.desk+tag@sellershield.internal" not in sanitized_str
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in sanitized_str
    assert "secret_token_12345" not in sanitized_str

    assert parsed["email"] == "[REDACTED_EMAIL]"
    assert parsed["nested"]["token"] == "[REDACTED]"
    assert parsed["nested"]["contact"] == "[REDACTED_EMAIL]"


def test_details_never_contain_authorization() -> None:
    raw_details = {
        "Authorization": "Bearer fake_token",
        "authorization": "basic dXNlcjpwYXNz",
        "headers": {
            "Proxy-Authorization": "secret",
            "Authorization": "token",
        },
        "message": "Header Authorization was missing from payload",
    }

    sanitized_str = sanitize_details(raw_details)

    # The word 'authorization' must never appear in any case
    assert "authorization" not in sanitized_str.lower()


def test_sanitize_truncates_long_strings_and_rejects_non_json() -> None:
    # 600 character string must be truncated to 500 characters
    long_str = "x" * 600
    details = {"payload": long_str}
    sanitized_str = sanitize_details(details)
    parsed = json.loads(sanitized_str)
    assert len(parsed["payload"]) == 500

    # Non-JSON objects must raise ValueError
    with pytest.raises(ValueError, match="not JSON"):
        sanitize_details(set([1, 2, 3]))

    with pytest.raises(ValueError, match="not JSON"):
        sanitize_details("{not valid json syntax")


def test_denial_event_redacts_other_customer_ids() -> None:
    details = {
        "reason": "Forbidden access to CUST-999 by CUST-001",
        "target_customer": "CUST-999",
        "self": "CUST-001",
    }

    sanitized_str = sanitize_details(details, actor_id="CUST-001", is_denial=True)

    assert "CUST-999" not in sanitized_str
    assert "CUST-001" in sanitized_str

