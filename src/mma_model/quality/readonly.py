"""Read-only SQLite engine for DWCS-106 coverage (no writes, no migrations)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from mma_model.quality.constants import LIVE_DB_URLS


class CoverageDatabaseError(ValueError):
    """Raised for empty, malformed, live, or unusable coverage database URLs."""


def sqlite_path_from_url(db_url: str) -> Path:
    raw = str(db_url or "").strip()
    if not raw:
        raise CoverageDatabaseError("empty database url")
    if raw in LIVE_DB_URLS or raw.endswith("/data/mma.db") or raw.endswith("data/mma.db"):
        raise CoverageDatabaseError("refusing live data/mma.db")
    if raw.startswith("sqlite:////"):
        path = Path(unquote(raw[len("sqlite:////") :]))
        if not str(path).startswith("/"):
            path = Path("/") / path
        return path
    if raw.startswith("sqlite:///"):
        rest = unquote(raw[len("sqlite:///") :])
        if rest.startswith("file:"):
            raise CoverageDatabaseError("malformed database url")
        if not rest or rest == ":memory:":
            raise CoverageDatabaseError("malformed database url")
        return Path(rest)
    parsed = urlparse(raw)
    if parsed.scheme != "sqlite":
        raise CoverageDatabaseError("malformed database url")
    raise CoverageDatabaseError("malformed database url")


def is_prohibited_live_url(db_url: str, *, default_url: str | None = None) -> bool:
    raw = str(db_url or "").strip()
    if raw in LIVE_DB_URLS:
        return True
    if default_url is not None and raw == str(default_url).strip():
        return True
    try:
        path = sqlite_path_from_url(raw)
    except CoverageDatabaseError:
        return False
    return path.name == "mma.db" and path.parent.name == "data"


def open_readonly_sqlite_engine(db_url: str) -> Engine:
    path = sqlite_path_from_url(db_url)
    if not path.is_file():
        raise CoverageDatabaseError(f"database file missing: {path}")

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    engine = create_engine("sqlite://", creator=_connect, future=True)

    @event.listens_for(engine, "connect")
    def _query_only(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with engine.connect() as connection:
        connection.execute(text("PRAGMA query_only=ON"))
    return engine


def readonly_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
