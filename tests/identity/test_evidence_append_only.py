"""Append-only identity evidence at the SQLite layer (DWCS-104)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.db.tables.identity import IdentityMatchEvidence
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.resolver import resolve_fighter

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 20, 30, 0, tzinfo=UTC)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def test_evidence_orm_update_and_delete_are_rejected(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ev.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session() as session:
        fid = canonical_fighter_id("40001")
        session.add(CanonicalFighter(id=fid, display_name="Append Only"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="40001"))
        session.commit()
        resolve_fighter(
            session,
            source="tapology_public",
            external_id="ao-1",
            display_name="Append Only",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        row = session.scalar(select(IdentityMatchEvidence))
        assert row is not None
        row.status = "inactive"
        with pytest.raises((OperationalError, IntegrityError, Exception), match="append-only"):
            session.commit()
        session.rollback()
        victim = session.scalar(select(IdentityMatchEvidence))
        assert victim is not None
        session.delete(victim)
        with pytest.raises((OperationalError, IntegrityError, Exception), match="append-only"):
            session.commit()
        session.rollback()
    engine.dispose()


def test_evidence_raw_sql_update_and_delete_are_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "evraw.db"
    command.upgrade(_alembic_config(db_path), "head")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO canonical_fighters(id, display_name, created_at, updated_at) "
            "VALUES ('f-ev','A',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO identity_review_queue("
            "id, status, version, source, external_id, display_name, normalized_name, "
            "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
            "created_at, updated_at, reversible) "
            "VALUES ('r-ev','pending',1,'tapology_public','x','A','a','[]','{}',"
            "'manual_enqueue','1',?,?,1)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO identity_match_evidence("
            "id, created_at, resolver_version, rule_id, action, source, external_id, "
            "display_name, normalized_name, actor, evidence_json, reversible, status) "
            "VALUES ('e-ev',?,'1','manual_enqueue','queued','tapology_public','x',"
            "'A','a','system','{}',1,'active')",
            (now,),
        )
        conn.commit()
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute("UPDATE identity_match_evidence SET status='inactive' WHERE id='e-ev'")
            conn.commit()
        conn.rollback()
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute("DELETE FROM identity_match_evidence WHERE id='e-ev'")
            conn.commit()
        conn.rollback()
        count = conn.execute("SELECT COUNT(*) FROM identity_match_evidence").fetchone()[0]
        assert count == 1
        status = conn.execute(
            "SELECT status FROM identity_match_evidence WHERE id='e-ev'"
        ).fetchone()[0]
        assert status == "active"
    finally:
        conn.close()


def test_downgrade_drops_owned_triggers(tmp_path: Path) -> None:
    db_path = tmp_path / "evdown.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "identity_match_evidence_no_update" in names
        assert "identity_match_evidence_no_delete" in names
    finally:
        conn.close()
    command.downgrade(cfg, "0006_observation_pit_metadata")
    conn = sqlite3.connect(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "identity_match_evidence_no_update" not in names
        assert "identity_match_evidence_no_delete" not in names
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "identity_match_evidence" not in tables
        assert "canonical_fighters" in tables
    finally:
        conn.close()
