"""Shared helpers for DWCS-106 quality tests. Disposable temp DBs only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 20, 0, 0, tzinfo=UTC)


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def make_empty_db(tmp_path: Path, *, migrate: bool = False) -> dict:
    db_path = tmp_path / "coverage.db"
    url = f"sqlite:///{db_path}"
    if migrate:
        command.upgrade(alembic_config(db_path), "head")
        engine = create_engine(url, future=True)
        _attach_sqlite_listeners(engine)
    else:
        engine = create_engine(url, future=True)
        _attach_sqlite_listeners(engine)
        create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    store = ContentAddressedRawStore(tmp_path / "raw")
    repo = IngestRepository(session_factory=Session, raw_store=store)
    return {
        "db_path": db_path,
        "db_url": url,
        "engine": engine,
        "Session": Session,
        "store": store,
        "repo": repo,
    }
