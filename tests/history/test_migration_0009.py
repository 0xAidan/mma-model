"""Migration 0009 up/down preserves 0008 history rows and pre-105 data."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.history.helpers import alembic_config

HISTORY_TABLES = {
    "history_source_bouts",
    "history_conflicts",
    "history_source_failures",
    "history_frontier",
    "history_reconstructions",
    "history_explicit_records",
}


def test_0009_upgrade_downgrade_preserves_0008_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "mig109.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, "0008_regional_history")
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
            "INSERT INTO history_source_bouts("
            "id, source, stream, external_bout_id, fighter_source, fighter_external_id, "
            "fighter_name, fighter_canonical_id, opponent_name, classification, "
            "regulated_us, result, left_truncated, version_kind, revision, bout_status, "
            "quality_tier, timestamp_quality, observed_at, effective_at, payload_hash, "
            "identity_status, is_current_record, created_at) VALUES ("
            "'b-keep','tapology_public','fighter_history','tb-keep','tapology_public',"
            "'tap-keep','Keep Me','f-keep','Opp','professional','unknown','win',0,"
            "'event_night',1,'completed','bronze','unknown',?,?,?, 'linked', 0, ?)",
            (now, now, "a" * 64, now),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "0009_history_constraints")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine).get_table_names())
    assert HISTORY_TABLES.issubset(names)
    with engine.connect() as db:
        cols = {row[1] for row in db.execute(text("PRAGMA table_info(history_source_bouts)"))}
        assert "event_time_precision" in cols
        assert "observation_origin" in cols
        count = db.execute(text("SELECT COUNT(*) FROM history_source_bouts")).scalar_one()
        assert count == 1
        fighter = db.execute(text("SELECT COUNT(*) FROM canonical_fighters")).scalar_one()
        assert fighter == 1

    command.downgrade(cfg, "0008_regional_history")
    engine2 = create_engine(f"sqlite:///{db_path}")
    with engine2.connect() as db:
        cols = {row[1] for row in db.execute(text("PRAGMA table_info(history_source_bouts)"))}
        assert "event_time_precision" not in cols
        count = db.execute(text("SELECT COUNT(*) FROM history_source_bouts")).scalar_one()
        assert count == 1
        fighter = db.execute(text("SELECT COUNT(*) FROM canonical_fighters")).scalar_one()
        assert fighter == 1
