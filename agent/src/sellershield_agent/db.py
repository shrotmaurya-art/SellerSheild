"""SQLite database layer using the BEGIN IMMEDIATE recipe from spec §16.

Shared recipe: BEGIN IMMEDIATE on every transaction serializes writes,
which is fine at demo scale.  WAL mode and busy_timeout let readers and
writers coexist.  See docs/spec/01-playground.md section 16.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
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
        elif db_path_str.startswith("/") and not url.startswith("sqlite:////"):
            db_path_str = db_path_str.lstrip("/")
        db_file = Path(db_path_str)
        if db_file.parent and str(db_file.parent) != ".":
            db_file.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if url in ("sqlite:///:memory:", "sqlite://", "sqlite:"):
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool

    engine = create_engine(
        url,
        **kwargs,
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
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception:
            pass

    @event.listens_for(engine, "before_execute", retval=True)
    def _on_before_execute(
        conn: Connection,
        clause: Any,
        multiparams: Any,
        params: Any,
        execution_options: Any,
    ) -> tuple[Any, Any, Any]:
        from sqlalchemy.sql.elements import TextClause

        if (
            isinstance(clause, TextClause)
            and ":true" in clause.text
            and (not params or "true" not in params)
        ):
            return text(clause.text.replace(":true", "\\:true")), multiparams, params
        return clause, multiparams, params

    return engine


# ── transaction helpers ───────────────────────────────────────────

_db_write_lock = threading.Lock()


@contextmanager
def write_tx(engine: Engine) -> Generator[Connection, None, None]:
    """Context manager for a write transaction.

    Commits on clean exit, rolls back on exception.
    Serializes concurrent thread writes to prevent SQLite lock races.

    **Never hold this across an await or an HTTP call.**
    Long-held write transactions block all other writers because
    BEGIN IMMEDIATE acquires an exclusive lock.  See KP-01.
    """
    after_commit: list[Callable[[], None]] = []
    with _db_write_lock:
        with engine.connect() as conn:
            conn.info["_after_commit"] = after_commit
            try:
                yield conn
                conn.commit()
            except BaseException:
                after_commit.clear()
                conn.rollback()
                raise
            finally:
                conn.info.pop("_after_commit", None)
    # Dispatch registered post-commit callbacks after WAL commit & lock release
    for cb in after_commit:
        cb()


@contextmanager
def read_tx(engine: Engine) -> Generator[Connection, None, None]:
    """Context manager for a read-only transaction.

    Uses BEGIN IMMEDIATE at demo scale (per spec §16).
    Rolls back at exit — no commit needed for reads.
    """
    with _db_write_lock:
        with engine.connect() as conn:
            try:
                yield conn
            finally:
                conn.rollback()


# ── schema initialisation ────────────────────────────────────────


def _ensure_execution_columns(conn: Connection) -> None:
    """Ensure T2.5 reconciler columns exist on pre-existing executions tables."""
    cols = {str(row[1]) for row in conn.execute(text("PRAGMA table_info(executions)")).fetchall()}
    if not cols:
        return
    if "attempts" not in cols:
        conn.execute(text("ALTER TABLE executions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"))
    if "next_retry_at" not in cols:
        conn.execute(text("ALTER TABLE executions ADD COLUMN next_retry_at VARCHAR(32)"))
    if "stuck" not in cols:
        conn.execute(text("ALTER TABLE executions ADD COLUMN stuck BOOLEAN NOT NULL DEFAULT 0"))


def _ensure_approval_columns(conn: Connection) -> None:
    """Ensure T2.6 approval columns exist on pre-existing approvals tables."""
    cols = {str(row[1]) for row in conn.execute(text("PRAGMA table_info(approvals)")).fetchall()}
    if not cols:
        return
    if "store_id" not in cols:
        conn.execute(
            text(
                "ALTER TABLE approvals ADD COLUMN store_id VARCHAR(64) NOT NULL DEFAULT 'nestkart'"
            )
        )
    if "store_authorization_ref" not in cols:
        conn.execute(text("ALTER TABLE approvals ADD COLUMN store_authorization_ref VARCHAR(64)"))
    if "refund_status" not in cols:
        conn.execute(text("ALTER TABLE approvals ADD COLUMN refund_status VARCHAR(32)"))
    if "last_error" not in cols:
        conn.execute(text("ALTER TABLE approvals ADD COLUMN last_error TEXT"))
    if "revalidation_diff_json" not in cols:
        conn.execute(text("ALTER TABLE approvals ADD COLUMN revalidation_diff_json TEXT"))
    if "created_at" not in cols:
        conn.execute(text("ALTER TABLE approvals ADD COLUMN created_at VARCHAR(32)"))


def _ensure_cases_columns(conn: Connection) -> None:
    """Ensure T3.1 cases columns exist on pre-existing cases tables."""
    cols = {str(row[1]) for row in conn.execute(text("PRAGMA table_info(cases)")).fetchall()}
    if not cols:
        return
    if "session_id" not in cols:
        conn.execute(text("ALTER TABLE cases ADD COLUMN session_id VARCHAR(64)"))
    if "handler" not in cols:
        conn.execute(
            text("ALTER TABLE cases ADD COLUMN handler VARCHAR(32) NOT NULL DEFAULT 'agent'")
        )


def seed_policies(conn: Connection) -> None:
    """Seed policy versions v1..v3 into policies table (v3 active)."""
    from pathlib import Path

    from sellershield_agent.domain.state import policies

    versions_dir = Path(__file__).resolve().parent / "policy" / "versions"
    existing = {row[0] for row in conn.execute(text("SELECT version FROM policies")).fetchall()}
    specs = [
        ("v1", "retired", "2026-11-01T00:00:00Z", None),
        ("v2", "retired", "2026-11-05T00:00:00Z", None),
        ("v3", "active", "2026-11-08T00:00:00Z", "2026-11-08T12:00:00Z"),
    ]
    for ver, status, created_at, activated_at in specs:
        if ver not in existing:
            fpath = versions_dir / f"{ver}.json"
            doc_json = fpath.read_text(encoding="utf-8") if fpath.is_file() else "{}"
            conn.execute(
                policies.insert().values(
                    version=ver,
                    status=status,
                    document_json=doc_json,
                    created_by="system",
                    created_at=created_at,
                    activated_at=activated_at,
                )
            )


def init_db(engine: Engine, metadata: MetaData | None = None) -> None:
    """Create all tables and seed the ``_meta`` row (idempotent).

    Safe to call multiple times — uses IF NOT EXISTS for tables and
    checks for an existing seed row before inserting.
    """
    import sellershield_agent.audit.models  # noqa: F401
    import sellershield_agent.domain.session  # noqa: F401
    import sellershield_agent.domain.state  # noqa: F401

    md = metadata if metadata is not None else get_metadata()
    md.create_all(engine)

    with write_tx(engine) as conn:
        _ensure_execution_columns(conn)
        _ensure_approval_columns(conn)
        _ensure_cases_columns(conn)
        seed_policies(conn)
        count = conn.execute(text("SELECT count(*) FROM _meta")).scalar()
        if count == 0:
            conn.execute(_meta_table.insert().values(schema_version=1))


# ── global engine singleton ───────────────────────────────────────

_default_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the global database engine for the active settings, initialized."""
    global _default_engine
    if _default_engine is None:
        # Import tables to ensure they register on MetaData
        import sellershield_agent.audit.models  # noqa: F401
        import sellershield_agent.domain.session  # noqa: F401
        import sellershield_agent.domain.state  # noqa: F401
        from sellershield_agent.config import get_settings

        settings = get_settings()
        _default_engine = create_engine_from_url(settings.DATABASE_URL)
        init_db(_default_engine)
    return _default_engine


def reset_engine() -> None:
    """Reset the global database engine (useful in test teardown)."""
    global _default_engine
    if _default_engine is not None:
        _default_engine.dispose()
        _default_engine = None
    try:
        from sellershield_agent.dev.faults import clear_faults

        clear_faults()
    except ImportError:
        pass
    try:
        from sellershield_agent.api.chat import reset_chat_pipeline
        from sellershield_agent.api.ratelimit import user_message_limiter

        reset_chat_pipeline()
        user_message_limiter.reset()
    except ImportError:
        pass
