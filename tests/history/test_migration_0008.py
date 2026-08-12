"""Migration 0008 up/down preserves identity and canonical rows."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from tests.history.helpers import alembic_config

HISTORY_TABLES = {
    "history_source_bouts",
    "history_conflicts",
    "history_source_failures",
    "history_frontier",
    "history_reconstructions",
    "history_explicit_records",
}
IDENTITY_TABLES = {
    "identity_review_queue",
    "identity_match_evidence",
    "identity_scoring_blocks",
}


def test_history_migration_up_down_preserves_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "mig105.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, "0007_identity_review_queue")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO canonical_fighters(id, display_name, created_at, updated_at) "
            "VALUES ('f-keep','Keep Me',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO identity_review_queue("
            "id, status, version, source, external_id, display_name, normalized_name, "
            "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
            "created_at, updated_at, reversible) "
            "VALUES ('r-keep','pending',1,'tapology_public','x','Keep Me','keep me',"
            "'[]','{}','manual_enqueue','1',?,?,1)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert HISTORY_TABLES.issubset(names)
    assert IDENTITY_TABLES.issubset(names)

    command.downgrade(cfg, "0007_identity_review_queue")
    names_after = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert HISTORY_TABLES.isdisjoint(names_after)
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM canonical_fighters").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM identity_review_queue").fetchone()[0] == 1
    finally:
        conn.close()
