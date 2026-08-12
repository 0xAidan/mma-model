"""Shared helpers for DWCS-105 history tests."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 15, 0, 0, tzinfo=UTC)
TAPOLOGY_FIXTURES = REPO_ROOT / "tests/fixtures/sources/tapology"
SHERDOG_FIXTURES = REPO_ROOT / "tests/fixtures/sources/sherdog"
COMBAT_FIXTURES = REPO_ROOT / "tests/fixtures/sources/combat_registry"


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def make_history_db(tmp_path: Path) -> dict:
    db_path = tmp_path / "history.db"
    command.upgrade(alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    store = ContentAddressedRawStore(tmp_path / "raw")
    repo = IngestRepository(session_factory=Session, raw_store=store)
    return {
        "db_path": db_path,
        "db_url": f"sqlite:///{db_path}",
        "engine": engine,
        "Session": Session,
        "store": store,
        "repo": repo,
    }


def stage_sync_fixtures(tmp_path: Path) -> Path:
    root = tmp_path / "regional_fixtures"
    tap = root / "tapology_public" / "fighters"
    sh = root / "sherdog_public" / "fighters"
    cr = root / "combat_registry" / "results"
    tap.mkdir(parents=True)
    sh.mkdir(parents=True)
    cr.mkdir(parents=True)
    shutil.copy(TAPOLOGY_FIXTURES / "fighter_public_sample.html", tap / "tap-100.html")
    shutil.copy(TAPOLOGY_FIXTURES / "fighter_tap-100-p2.html", tap / "tap-100-p2.html")
    shutil.copy(TAPOLOGY_FIXTURES / "fighter_tap-jose.html", tap / "tap-jose.html")
    shutil.copy(SHERDOG_FIXTURES / "fighter_public_sample.html", sh / "sh-100.html")
    shutil.copy(COMBAT_FIXTURES / "results_sample.html", cr / "cr-100.html")
    return root
