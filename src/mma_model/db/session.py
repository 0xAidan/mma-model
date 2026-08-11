"""Database engine and session factory."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.config import get_settings
from mma_model.db.models import Base


def apply_sqlite_pragmas(connection: Connection) -> None:
    """Enable foreign keys (and WAL for file-backed DBs) on a SQLAlchemy connection."""
    if connection.dialect.name != "sqlite":
        return
    connection.execute(text("PRAGMA foreign_keys=ON"))
    # WAL is unsupported / meaningless for pure in-memory URLs.
    db_api = connection.connection.dbapi_connection
    raw_path = ""
    if hasattr(db_api, "execute"):
        row = db_api.execute("PRAGMA database_list").fetchone()
        if row is not None and len(row) >= 3:
            raw_path = row[2] or ""
    if raw_path and raw_path != ":memory:":
        connection.execute(text("PRAGMA journal_mode=WAL"))


def sqlite_connect_pragmas(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    row = dbapi_conn.execute("PRAGMA database_list").fetchone()
    raw_path = row[2] if row is not None and len(row) >= 3 else ""
    if raw_path and raw_path != ":memory:":
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def _attach_sqlite_listeners(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    # Avoid duplicate listeners when get_engine is called more than once in tests.
    if getattr(engine, "_mma_sqlite_pragmas", False):
        return
    event.listen(engine, "connect", sqlite_connect_pragmas)
    engine._mma_sqlite_pragmas = True  # type: ignore[attr-defined]


def get_engine() -> Engine:
    settings = get_settings()
    url = settings.mma_database_url
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=False, future=True)
    _attach_sqlite_listeners(engine)
    return engine


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _alembic_config(url: str | None = None) -> Config:
    settings = get_settings()
    root = settings.project_root
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or settings.mma_database_url)
    return cfg


def init_db() -> None:
    """Apply Alembic migrations to head (compatible replacement for create_all-only init)."""
    # Ensure metadata modules are imported before upgrade (env.py also imports them).
    import mma_model.db.tables.core  # noqa: F401

    command.upgrade(_alembic_config(), "head")


def create_all_for_tests(bind: Engine | None = None) -> None:
    """Create all tables via metadata (unit tests that do not run Alembic)."""
    target = bind or engine
    _attach_sqlite_listeners(target)
    Base.metadata.create_all(bind=target)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
