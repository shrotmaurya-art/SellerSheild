"""Tests for the SQLite database layer (sellershield_agent.db)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from sellershield_agent.db import (
    create_engine_from_url,
    get_metadata,
    init_db,
    read_tx,
    write_tx,
)


def _file_url(path: Path) -> str:
    """Build a sqlite:/// URL with forward slashes (Windows-safe)."""
    return f"sqlite:///{str(path).replace(chr(92), '/')}"


# ── pragma tests ──────────────────────────────────────────────────


def test_pragma_wal(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text("PRAGMA journal_mode")).scalar()
            assert row == "wal"
    finally:
        engine.dispose()


def test_pragma_foreign_keys(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text("PRAGMA foreign_keys")).scalar()
            assert row == 1
    finally:
        engine.dispose()


def test_busy_timeout(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text("PRAGMA busy_timeout")).scalar()
            assert row == 5000
    finally:
        engine.dispose()


# ── transactional behaviour ───────────────────────────────────────


def test_concurrent_writers_no_locked_error(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        # Create a real table for the test
        with engine.connect() as conn:
            conn.execute(
                sa.text("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
            )
            conn.commit()

        ready = threading.Event()
        errors: list[Exception] = []

        def writer1() -> None:
            try:
                with write_tx(engine) as conn:
                    conn.execute(
                        sa.text("INSERT INTO items (id, val) VALUES (1, 'a')")
                    )
                    ready.set()
                    time.sleep(0.2)
            except Exception as exc:
                errors.append(exc)

        def writer2() -> None:
            try:
                ready.wait(timeout=5.0)
                with write_tx(engine) as conn:
                    conn.execute(
                        sa.text("INSERT INTO items (id, val) VALUES (2, 'b')")
                    )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=writer1)
        t2 = threading.Thread(target=writer2)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        assert errors == [], f"Unexpected errors: {errors}"

        with read_tx(engine) as conn:
            count = conn.execute(sa.text("SELECT count(*) FROM items")).scalar()
            assert count == 2
    finally:
        engine.dispose()


def test_rollback_on_exception(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        with engine.connect() as conn:
            conn.execute(
                sa.text("CREATE TABLE things (id INTEGER PRIMARY KEY)")
            )
            conn.commit()

        with pytest.raises(RuntimeError):
            with write_tx(engine) as conn:
                conn.execute(sa.text("INSERT INTO things (id) VALUES (1)"))
                raise RuntimeError("boom")

        with read_tx(engine) as conn:
            count = conn.execute(sa.text("SELECT count(*) FROM things")).scalar()
            assert count == 0
    finally:
        engine.dispose()


def test_uncommitted_write_invisible(tmp_path: Path) -> None:
    """An uncommitted write in connection A is invisible to a WAL reader B.

    We use a raw DBAPI connection for the reader to avoid BEGIN IMMEDIATE
    (which would block while A holds the write lock).  In WAL mode,
    ordinary ``BEGIN`` (deferred) reads see the last committed snapshot.
    """
    db_path = tmp_path / "test.db"
    url = _file_url(db_path)
    engine_a = create_engine_from_url(url)
    engine_b = create_engine_from_url(url)
    try:
        with engine_a.connect() as conn:
            conn.execute(
                sa.text("CREATE TABLE rows (id INTEGER PRIMARY KEY)")
            )
            conn.commit()

        # A starts a write but does not commit yet
        ctx = write_tx(engine_a)
        conn_a = ctx.__enter__()
        conn_a.execute(sa.text("INSERT INTO rows (id) VALUES (1)"))

        # B reads using a deferred transaction (WAL reader) — must NOT see the row
        raw_b = engine_b.raw_connection()
        try:
            cur = raw_b.cursor()
            cur.execute("BEGIN")
            cur.execute("SELECT count(*) FROM rows")
            count = cur.fetchone()[0]  # type: ignore[index]
            assert count == 0
            cur.execute("ROLLBACK")
            cur.close()
        finally:
            raw_b.close()

        # Now let A commit
        ctx.__exit__(None, None, None)

        # B reads again — should see it now
        with read_tx(engine_b) as conn_b:
            count = conn_b.execute(sa.text("SELECT count(*) FROM rows")).scalar()
            assert count == 1
    finally:
        engine_a.dispose()
        engine_b.dispose()


# ── init_db ───────────────────────────────────────────────────────


def test_init_db_creates_meta_table(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        init_db(engine, get_metadata())

        with read_tx(engine) as conn:
            row = conn.execute(
                sa.text("SELECT schema_version FROM _meta")
            ).scalar()
            assert row == 1
    finally:
        engine.dispose()


def test_init_db_idempotent(tmp_path: Path) -> None:
    url = _file_url(tmp_path / "test.db")
    engine = create_engine_from_url(url)
    try:
        init_db(engine, get_metadata())
        init_db(engine, get_metadata())  # second call must not raise

        with read_tx(engine) as conn:
            count = conn.execute(
                sa.text("SELECT count(*) FROM _meta")
            ).scalar()
            assert count == 1
    finally:
        engine.dispose()


# ── filesystem behaviour ─────────────────────────────────────────


def test_engine_creates_parent_dir(tmp_path: Path) -> None:
    deep = tmp_path / "sub" / "deep" / "test.db"
    url = _file_url(deep)
    engine = create_engine_from_url(url)
    try:
        # Trigger file creation by connecting
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        assert deep.parent.exists()
    finally:
        engine.dispose()


def test_engine_dispose_releases_file(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    url = _file_url(db_path)
    engine = create_engine_from_url(url)

    with write_tx(engine) as conn:
        conn.execute(
            sa.text("CREATE TABLE x (id INTEGER PRIMARY KEY)")
        )

    engine.dispose()

    # On Windows, deletion fails if a handle is still open
    db_path.unlink()  # must not raise
