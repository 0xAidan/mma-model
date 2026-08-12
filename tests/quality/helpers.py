"""Shared helpers for DWCS-106 quality tests. Disposable temp DBs only."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.provenance import IngestRun, RawObservation
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


def add_ingest_run(session, *, source: str = "ufcstats_public", status: str = "succeeded"):
    run = IngestRun(
        source=source,
        stream="history",
        scope="quality-test",
        status=status,
    )
    session.add(run)
    session.flush()
    return run


def add_observation(session, run_id: str, **overrides) -> RawObservation:
    now = overrides.pop("observed_at", FIXED_NOW)
    row = RawObservation(
        ingest_run_id=run_id,
        source=overrides.pop("source", "ufcstats_public"),
        stream=overrides.pop("stream", "history"),
        scope=overrides.pop("scope", "quality-test"),
        checkpoint_version=overrides.pop("checkpoint_version", "v1"),
        external_id=overrides.pop("external_id", "obs-1"),
        entity_kind=overrides.pop("entity_kind", "bout_result"),
        observed_at=now,
        effective_at=overrides.pop("effective_at", now),
        source_published_at=overrides.pop("source_published_at", None),
        source_updated_at=overrides.pop("source_updated_at", None),
        proxy_published_at=overrides.pop("proxy_published_at", None),
        timestamp_quality=overrides.pop("timestamp_quality", "publication_proxy"),
        quality_tier=overrides.pop("quality_tier", "silver"),
        payload_hash=overrides.pop("payload_hash", "e" * 64),
        raw_ref=overrides.pop("raw_ref", None),
        subject_id=overrides.pop("subject_id"),
        version_kind=overrides.pop("version_kind", "event_night"),
        attributes_json=overrides.pop(
            "attributes_json",
            '{"result_type":"decisive","winner_fighter_id":"aaa"}',
        ),
    )
    if overrides:
        raise TypeError(f"unexpected observation fields: {sorted(overrides)}")
    session.add(row)
    session.flush()
    return row
