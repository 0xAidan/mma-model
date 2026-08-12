"""Identity migration 0007 up/down with populated DWCS-103-like store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.resolver import resolve_fighter

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 20, 0, 0, tzinfo=UTC)
IDENTITY_TABLES = {
    "identity_review_queue",
    "identity_match_evidence",
    "identity_scoring_blocks",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def test_identity_migration_up_down_preserves_canonical(tmp_path: Path) -> None:
    db_path = tmp_path / "mig104.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "0006_observation_pit_metadata")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    fid = canonical_fighter_id("77001")
    with Session() as session:
        session.add(CanonicalFighter(id=fid, display_name="Preserve Me"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="77001"))
        session.commit()
    engine.dispose()

    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert IDENTITY_TABLES.issubset(names)

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session() as session:
        resolve_fighter(
            session,
            source="tapology_public",
            external_id="mig-1",
            display_name="Preserve Me",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    engine.dispose()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("SELECT COUNT(*) FROM canonical_fighters").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_source_ids WHERE source='espn'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM identity_review_queue").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM identity_match_evidence").fetchone()[0] >= 1
    finally:
        conn.close()

    command.downgrade(cfg, "0006_observation_pit_metadata")
    names_after = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert IDENTITY_TABLES.isdisjoint(names_after)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM canonical_fighters").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT external_id FROM fighter_source_ids WHERE source='espn'"
            ).fetchone()[0]
            == "77001"
        )
        # No DWCS-104 owned rows remain.
        for table in IDENTITY_TABLES:
            assert table not in {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    finally:
        conn.close()


def test_identity_unique_constraints_enforced(tmp_path: Path) -> None:
    db_path = tmp_path / "uq.db"
    command.upgrade(_alembic_config(db_path), "head")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO canonical_fighters(id, display_name, created_at, updated_at) "
            "VALUES ('f1','A',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO identity_review_queue("
            "id, status, version, source, external_id, display_name, normalized_name, "
            "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
            "created_at, updated_at, reversible) "
            "VALUES ('r1','pending',1,'tapology_public','x','A','a','[]','{}',"
            "'manual_enqueue','1',?,?,1)",
            (now, now),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO identity_review_queue("
                "id, status, version, source, external_id, display_name, normalized_name, "
                "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
                "created_at, updated_at, reversible) "
                "VALUES ('r2','pending',1,'tapology_public','x','A','a','[]','{}',"
                "'manual_enqueue','1',?,?,1)",
                (now, now),
            )
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
            conn.rollback()
        assert raised

        conn.execute(
            "UPDATE identity_review_queue SET status='reversed' WHERE id='r1'"
        )
        conn.commit()
        conn.execute(
            "INSERT INTO identity_review_queue("
            "id, status, version, source, external_id, display_name, normalized_name, "
            "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
            "created_at, updated_at, reversible) "
            "VALUES ('r-rev-2','reversed',1,'tapology_public','x','A','a','[]','{}',"
            "'manual_enqueue','1',?,?,1)",
            (now, now),
        )
        conn.commit()
        reversed_count = conn.execute(
            "SELECT COUNT(*) FROM identity_review_queue "
            "WHERE source='tapology_public' AND external_id='x' AND status='reversed'"
        ).fetchone()[0]
        assert reversed_count == 2

        conn.execute(
            "INSERT INTO identity_review_queue("
            "id, status, version, source, external_id, display_name, normalized_name, "
            "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
            "created_at, updated_at, reversible) "
            "VALUES ('r-open','pending',1,'tapology_public','x','A','a','[]','{}',"
            "'manual_enqueue','1',?,?,1)",
            (now, now),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO identity_review_queue("
                "id, status, version, source, external_id, display_name, normalized_name, "
                "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
                "created_at, updated_at, reversible) "
                "VALUES ('r-open-2','approved',1,'tapology_public','x','A','a','[]','{}',"
                "'manual_enqueue','1',?,?,1)",
                (now, now),
            )
            conn.commit()
            open_raised = False
        except sqlite3.IntegrityError:
            open_raised = True
            conn.rollback()
        assert open_raised

        indexes = {
            r[1]
            for r in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name LIKE 'uq_identity_review%'"
            ).fetchall()
        }
        assert "uq_identity_review_open_source_external" in indexes
        assert "uq_identity_review_source_external_status" not in indexes
    finally:
        conn.close()
