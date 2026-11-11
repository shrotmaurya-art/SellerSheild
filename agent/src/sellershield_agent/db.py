"""SQLite database layer using the BEGIN IMMEDIATE recipe from spec §16.

Shared recipe: BEGIN IMMEDIATE on every transaction serializes writes,
which is fine at demo scale.  WAL mode and busy_timeout let readers and
writers coexist.  See docs/spec/01-playground.md section 16.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Integer,
    MetaData,
    Table,
    create_engine,
    event,
    text,
)

# ── module-level metadata with the _meta table ────────────────────

_metadata = MetaData()

_meta_table = Table(
    "_meta",
    _metadata,
    Column("schema_version", Integer, nullable=False),
)


def get_metadata() -> MetaData:
    """Return the shared MetaData that includes the ``_meta`` table."""
    return _metadata


# ── engine factory ────────────────────────────────────────────────


def create_engine_from_url(url: str) -> Engine:
    """Create a SQLAlchemy engine with the SQLite safety recipe.

    The parent directory of the database file is created automatically.
    In-memory URLs (``sqlite:///:memory:`` or ``sqlite://``) skip
    directory creation.
    """
    # Create parent directory for file-based databases
    parsed = urlparse(url)
    db_path_str = parsed.path
    # urlparse gives "/path" on Windows for sqlite:///C:/... → strip leading /
    if db_path_str and db_path_str not in ("/", "/:memory:"):
        # On Windows urlparse("sqlite:///C:/foo/bar.db").path == "/C:/foo/bar.db"
        if len(db_path_str) >= 3 and db_path_str[0] == "/" and db_path_str[2] == ":":
            db_path_str = db_path_str[1:]
        db_file = Path(db_path_str)
        db_file.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, connection_record: Any) -> None:
        # We control BEGIN ourselves — disable pysqlite's implicit transactions
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


# ── transaction helpers ───────────────────────────────────────────


@contextmanager
def write_tx(engine: Engine) -> Generator[Connection, None, None]:
    """Context manager for a write transaction.

    Commits on clean exit, rolls back on exception.

    **Never hold this across an await or an HTTP call.**
    Long-held write transactions block all other writers because
    BEGIN IMMEDIATE acquires an exclusive lock.  See KP-01.
    """
    with engine.connect() as conn:
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


@contextmanager
def read_tx(engine: Engine) -> Generator[Connection, None, None]:
    """Context manager for a read-only transaction.

    Uses BEGIN IMMEDIATE at demo scale (per spec §16).
    Rolls back at exit — no commit needed for reads.
    """
    with engine.connect() as conn:
        try:
            yield conn
        finally:
            conn.rollback()


# ── schema initialisation ────────────────────────────────────────


def init_db(engine: Engine, metadata: MetaData) -> None:
    """Create all tables and seed the ``_meta`` row (idempotent).

    Safe to call multiple times — uses IF NOT EXISTS for tables and
    checks for an existing seed row before inserting.
    """
    metadata.create_all(engine)

    with write_tx(engine) as conn:
        count = conn.execute(
            text("SELECT count(*) FROM _meta")
        ).scalar()
        if count == 0:
            conn.execute(
                _meta_table.insert().values(schema_version=1)
            )

