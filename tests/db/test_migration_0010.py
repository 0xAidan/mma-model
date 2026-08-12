"""Migration 0010 exact result-version provenance and reversal-proxy correction."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from tests.history.helpers import alembic_config

UTC = timezone.utc
NOW = datetime(2026, 8, 12, 16, 0, 0, tzinfo=UTC).isoformat()
NIGHT = datetime(2017, 7, 11, 19, 0, 0, tzinfo=UTC).isoformat()
PROXY = datetime(2017, 7, 12, 19, 0, 0, tzinfo=UTC).isoformat()


def _insert_graph(conn: sqlite3.Connection, *, bout_id: str, fighter_a: str, fighter_b: str) -> None:
    conn.execute(
        "INSERT INTO canonical_fighters(id, display_name, created_at, updated_at) "
        "VALUES (?, 'A', ?, ?), (?, 'B', ?, ?)",
        (fighter_a, NOW, NOW, fighter_b, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_events(id, name, series, status, created_at, updated_at) "
        "VALUES (?, 'Card', 'dwcs', 'completed', ?, ?)",
        (f"event-{bout_id}", NOW, NOW),
    )
    conn.execute(
        "INSERT INTO canonical_bouts("
        "id, event_id, fighter_a_id, fighter_b_id, scheduled_rounds, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 3, 'completed', ?, ?)",
        (bout_id, f"event-{bout_id}", fighter_a, fighter_b, NOW, NOW),
    )


def _insert_run(conn: sqlite3.Connection, run_id: str) -> None:
    conn.execute(
        "INSERT INTO ingest_runs("
        "id, source, stream, scope, status, started_at, observation_count, created_at) "
        "VALUES (?, 'dwcs_manifest', 'history', 'test', 'succeeded', ?, 0, ?)",
        (run_id, NOW, NOW),
    )


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    external_id: str,
    subject_id: str,
    version_kind: str,
    result_type: str,
    source: str = "dwcs_manifest",
    observed_at: str = NOW,
    effective_at: str = NIGHT,
    proxy_published_at: str | None = PROXY,
    timestamp_quality: str = "publication_proxy",
    version_state: str = "event_night_equals_current",
) -> int:
    attrs = json.dumps(
        {
            "result_type": result_type,
            "version_state": version_state,
            "fighter_a_id": f"fa-{subject_id}",
            "fighter_b_id": f"fb-{subject_id}",
        }
    )
    cur = conn.execute(
        "INSERT INTO raw_observations("
        "ingest_run_id, source, stream, scope, checkpoint_version, external_id, "
        "entity_kind, observed_at, effective_at, source_updated_at, payload_hash, "
        "raw_ref, detail_level, version_kind, schema_version, subject_id, created_at, "
        "proxy_published_at, timestamp_quality, quality_tier, attributes_json) "
        "VALUES (?, ?, 'history', 'test', 'v1', ?, 'bout_result', ?, ?, NULL, ?, "
        "NULL, 'summary', ?, '1', ?, ?, ?, ?, 'silver', ?)",
        (
            run_id,
            source,
            external_id,
            observed_at,
            effective_at,
            "a" * 64,
            version_kind,
            subject_id,
            NOW,
            proxy_published_at,
            timestamp_quality,
            attrs,
        ),
    )
    return int(cur.lastrowid)


def _insert_version(
    conn: sqlite3.Connection,
    *,
    bout_id: str,
    fighter_a: str,
    fighter_b: str,
    version_kind: str,
    result_type: str,
    revision: int = 1,
    observed_at: str = NOW,
    effective_at: str = NIGHT,
) -> int:
    cur = conn.execute(
        "INSERT INTO bout_result_versions("
        "bout_id, version_kind, revision, fighter_a_id, fighter_b_id, "
        "winner_fighter_id, result_type, effective_at, observed_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            bout_id,
            version_kind,
            revision,
            fighter_a,
            fighter_b,
            result_type,
            effective_at,
            observed_at,
            NOW,
        ),
    )
    return int(cur.lastrowid)


def test_0010_backfill_unique_ambiguous_missing_and_reversal_proxy(tmp_path: Path) -> None:
    db_path = tmp_path / "mig0010.db"
    cfg = alembic_config(db_path)
    command.upgrade(cfg, "0009_history_constraints")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_run(conn, "run-1")
        _insert_graph(conn, bout_id="bout-unique", fighter_a="fa-u", fighter_b="fb-u")
        _insert_graph(conn, bout_id="bout-amb", fighter_a="fa-a", fighter_b="fb-a")
        _insert_graph(conn, bout_id="bout-miss", fighter_a="fa-m", fighter_b="fb-m")
        _insert_graph(conn, bout_id="bout-rev", fighter_a="fa-r", fighter_b="fb-r")
        _insert_graph(conn, bout_id="bout-nc", fighter_a="fa-n", fighter_b="fb-n")

        unique_obs = _insert_observation(
            conn,
            run_id="run-1",
            external_id="unique-current",
            subject_id="bout-unique",
            version_kind="current",
            result_type="decisive",
        )
        unique_ver = _insert_version(
            conn,
            bout_id="bout-unique",
            fighter_a="fa-u",
            fighter_b="fb-u",
            version_kind="current",
            result_type="decisive",
        )

        _insert_observation(
            conn,
            run_id="run-1",
            external_id="amb-dwcs",
            subject_id="bout-amb",
            version_kind="current",
            result_type="decisive",
            source="dwcs_manifest",
        )
        _insert_observation(
            conn,
            run_id="run-1",
            external_id="amb-ufc",
            subject_id="bout-amb",
            version_kind="current",
            result_type="decisive",
            source="ufcstats_public",
        )
        amb_ver = _insert_version(
            conn,
            bout_id="bout-amb",
            fighter_a="fa-a",
            fighter_b="fb-a",
            version_kind="current",
            result_type="decisive",
        )

        miss_ver = _insert_version(
            conn,
            bout_id="bout-miss",
            fighter_a="fa-m",
            fighter_b="fb-m",
            version_kind="current",
            result_type="decisive",
            observed_at=datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        )

        _insert_observation(
            conn,
            run_id="run-1",
            external_id="rev-night",
            subject_id="bout-rev",
            version_kind="event_night",
            result_type="decisive",
            version_state="reversed_to_no_contest",
        )
        rev_current_obs = _insert_observation(
            conn,
            run_id="run-1",
            external_id="rev-current",
            subject_id="bout-rev",
            version_kind="current",
            result_type="no_contest",
            version_state="reversed_to_no_contest",
        )
        _insert_version(
            conn,
            bout_id="bout-rev",
            fighter_a="fa-r",
            fighter_b="fb-r",
            version_kind="event_night",
            result_type="decisive",
        )
        rev_current_ver = _insert_version(
            conn,
            bout_id="bout-rev",
            fighter_a="fa-r",
            fighter_b="fb-r",
            version_kind="current",
            result_type="no_contest",
        )

        nc_obs = _insert_observation(
            conn,
            run_id="run-1",
            external_id="nc-current",
            subject_id="bout-nc",
            version_kind="current",
            result_type="no_contest",
            version_state="event_night_equals_current",
        )
        _insert_version(
            conn,
            bout_id="bout-nc",
            fighter_a="fa-n",
            fighter_b="fb-n",
            version_kind="current",
            result_type="no_contest",
        )
        conn.commit()
        before_versions = conn.execute(
            "SELECT COUNT(*) FROM bout_result_versions"
        ).fetchone()[0]
        before_obs = conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0]
        before_fighters = conn.execute(
            "SELECT COUNT(*) FROM canonical_fighters"
        ).fetchone()[0]
    finally:
        conn.close()

    command.upgrade(cfg, "0010_result_version_provenance")
    engine = create_engine(f"sqlite:///{db_path}")
    cols = {row[1] for row in engine.connect().execute(text("PRAGMA table_info(bout_result_versions)"))}
    assert "raw_observation_id" in cols
    assert "provenance_status" in cols
    with engine.connect() as db:
        unique_row = db.execute(
            text("SELECT raw_observation_id, provenance_status FROM bout_result_versions WHERE id = :id"),
            {"id": unique_ver},
        ).one()
        assert unique_row[0] == unique_obs
        assert unique_row[1] == "linked"
        amb_row = db.execute(
            text("SELECT raw_observation_id, provenance_status FROM bout_result_versions WHERE id = :id"),
            {"id": amb_ver},
        ).one()
        assert amb_row[0] is None
        assert amb_row[1] == "ambiguous"
        miss_row = db.execute(
            text("SELECT raw_observation_id, provenance_status FROM bout_result_versions WHERE id = :id"),
            {"id": miss_ver},
        ).one()
        assert miss_row[0] is None
        assert miss_row[1] == "unknown"
        rev_obs = db.execute(
            text(
                "SELECT proxy_published_at, timestamp_quality, attributes_json "
                "FROM raw_observations WHERE id = :id"
            ),
            {"id": rev_current_obs},
        ).one()
        assert rev_obs[0] is None
        assert rev_obs[1] == "unknown"
        attrs = json.loads(str(rev_obs[2]))
        assert attrs["correction_publication_unknown"] is True
        assert attrs["cleared_proxy_published_at"]
        assert attrs["cleared_timestamp_quality"] == "publication_proxy"
        rev_ver = db.execute(
            text("SELECT COUNT(*) FROM bout_result_versions WHERE id = :id"),
            {"id": rev_current_ver},
        ).scalar_one()
        assert rev_ver == 1
        nc_row = db.execute(
            text(
                "SELECT proxy_published_at, timestamp_quality FROM raw_observations WHERE id = :id"
            ),
            {"id": nc_obs},
        ).one()
        assert nc_row[0] is not None
        assert nc_row[1] == "publication_proxy"
        assert db.execute(text("SELECT COUNT(*) FROM bout_result_versions")).scalar_one() == before_versions
        assert db.execute(text("SELECT COUNT(*) FROM raw_observations")).scalar_one() == before_obs
        assert db.execute(text("SELECT COUNT(*) FROM canonical_fighters")).scalar_one() == before_fighters

    command.downgrade(cfg, "0009_history_constraints")
    engine2 = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine2).get_table_names())
    assert "bout_result_versions" in names
    with engine2.connect() as db:
        cols_down = {row[1] for row in db.execute(text("PRAGMA table_info(bout_result_versions)"))}
        assert "raw_observation_id" not in cols_down
        assert "provenance_status" not in cols_down
        assert db.execute(text("SELECT COUNT(*) FROM bout_result_versions")).scalar_one() == before_versions
        assert db.execute(text("SELECT COUNT(*) FROM raw_observations")).scalar_one() == before_obs
        restored = db.execute(
            text(
                "SELECT proxy_published_at, timestamp_quality, attributes_json "
                "FROM raw_observations WHERE id = :id"
            ),
            {"id": rev_current_obs},
        ).one()
        assert restored[0] is not None
        assert restored[1] == "publication_proxy"
        restored_attrs = json.loads(str(restored[2]))
        assert "correction_publication_unknown" not in restored_attrs
        assert db.execute(text("SELECT COUNT(*) FROM canonical_fighters")).scalar_one() == before_fighters
