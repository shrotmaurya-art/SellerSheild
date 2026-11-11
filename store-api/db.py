from pathlib import Path

from sqlalchemy import create_engine, event

# Automatically ensure store-api/data directory exists
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
db_path = DATA_DIR / "store.db"

# Exact SQLite recipe from spec section 16
engine = create_engine(
    f"sqlite:///{db_path.as_posix()}",
    connect_args={"check_same_thread": False, "timeout": 30.0},
)


@event.listens_for(engine, "connect")
def on_connect(dbapi_conn, record):
    dbapi_conn.isolation_level = None  # we control BEGIN ourselves
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


@event.listens_for(engine, "begin")
def on_begin(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


# Automatically ensure store-api/data/store.db is created
with engine.connect() as _conn:
    pass


def _ensure_schema_columns() -> None:
    """Apply additive schema migrations for existing SQLite databases."""
    with engine.begin() as conn:
        has_returns = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='returns'"
        ).first()
        if has_returns is not None:
            cols = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(returns)").fetchall()
            }
            if "client_ref" not in cols:
                conn.exec_driver_sql("ALTER TABLE returns ADD COLUMN client_ref VARCHAR(128)")


_ensure_schema_columns()
