"""Migration and canonical core schema tests (DWCS-100).

All database work uses disposable copies under tmp_path — never the live data/mma.db.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_TABLES = {
    "fighters",
    "events",
    "fights",
    "fight_fighter_stats",
    "ingest_cursors",
    "odds_snapshots",
    "fighter_composites",
}
CANONICAL_TABLES = {
    "canonical_fighters",
    "canonical_events",
    "canonical_bouts",
    "fighter_source_ids",
    "event_source_ids",
    "bout_source_ids",
    "fighter_aliases",
    "bout_participants",
    "bout_result_versions",
    "fighter_profile_observations",
    "fighter_stat_observations",
}
PROVENANCE_TABLES = {
    "ingest_runs",
    "raw_observations",
    "source_checkpoints",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _table_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_clean_upgrade_creates_baseline_and_canonical_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "clean.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    names = _table_names(db_path)
    assert LEGACY_TABLES.issubset(names)
    assert CANONICAL_TABLES.issubset(names)
    assert PROVENANCE_TABLES.issubset(names)


def _seed_legacy_schema(db_path: Path) -> None:
    """Create the pre-Alembic seven-table schema and insert fixture rows."""
    from datetime import date

    from mma_model.db.models import Base, Event, Fight, Fighter
    from mma_model.db.session import _attach_sqlite_listeners

    engine = create_engine(f"sqlite:///{db_path}")
    _attach_sqlite_listeners(engine)
    # Only legacy tables — simulate a DB created before canonical migrations.
    legacy_tables = [
        Base.metadata.tables[name]
        for name in (
            "fighters",
            "events",
            "fights",
            "fight_fighter_stats",
            "ingest_cursors",
            "odds_snapshots",
            "fighter_composites",
        )
    ]
    Base.metadata.create_all(bind=engine, tables=legacy_tables)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        session.add_all(
            [
                Fighter(id="f1", name="Alice Alpha"),
                Fighter(id="f2", name="Bob Bravo"),
                Event(id="e1", name="UFC Test 1", event_date=date(2024, 1, 1)),
                Fight(
                    id="fight1",
                    event_id="e1",
                    fighter_a_id="f1",
                    fighter_b_id="f2",
                    winner_id="f1",
                    method="KO/TKO",
                    fight_round=1,
                    time_str="2:30",
                    detail_ingested=True,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()


def test_legacy_upgrade_imports_ufcstats_source_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _seed_legacy_schema(db_path)
    assert _count_rows(db_path, "fighters") == 2
    assert _count_rows(db_path, "events") == 1
    assert _count_rows(db_path, "fights") == 1

    command.upgrade(_alembic_config(db_path), "head")

    names = _table_names(db_path)
    assert CANONICAL_TABLES.issubset(names)
    # Legacy CLI rows untouched.
    assert _count_rows(db_path, "fighters") == 2
    assert _count_rows(db_path, "fights") == 1

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        fighter_ext = {
            row[0]
            for row in conn.execute(
                "SELECT external_id FROM fighter_source_ids WHERE source='ufcstats'"
            )
        }
        event_ext = {
            row[0]
            for row in conn.execute(
                "SELECT external_id FROM event_source_ids WHERE source='ufcstats'"
            )
        }
        bout_ext = {
            row[0]
            for row in conn.execute(
                "SELECT external_id FROM bout_source_ids WHERE source='ufcstats'"
            )
        }
        assert fighter_ext == {"f1", "f2"}
        assert event_ext == {"e1"}
        assert bout_ext == {"fight1"}
    finally:
        conn.close()


def test_foreign_key_violations_fail_on_sqlite_connections(tmp_path: Path) -> None:
    db_path = tmp_path / "fk.db"
    command.upgrade(_alembic_config(db_path), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    from mma_model.db.session import _attach_sqlite_listeners

    _attach_sqlite_listeners(engine)
    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO fighter_source_ids "
                    "(fighter_id, source, external_id, created_at) "
                    "VALUES ('missing-fighter', 'ufcstats', 'x', '2024-01-01T00:00:00+00:00')"
                )
            )
            conn.commit()
    engine.dispose()


def test_distinct_fighters_and_winner_checks(tmp_path: Path) -> None:
    db_path = tmp_path / "checks.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    from mma_model.db.session import _attach_sqlite_listeners

    _attach_sqlite_listeners(engine)
    now = "2024-01-01T00:00:00+00:00"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO canonical_fighters (id, display_name, created_at, updated_at) "
                "VALUES ('fa', 'A', :now, :now), ('fb', 'B', :now, :now), ('fc', 'C', :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO canonical_events "
                "(id, name, series, status, scheduled_start_at, event_date, location, "
                "created_at, updated_at) "
                "VALUES ('ev1', 'Event', 'dwcs', 'scheduled', NULL, NULL, NULL, :now, :now)"
            ),
            {"now": now},
        )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO canonical_bouts "
                    "(id, event_id, fighter_a_id, fighter_b_id, scheduled_rounds, "
                    "weight_class, status, created_at, updated_at) "
                    "VALUES ('b_bad', 'ev1', 'fa', 'fa', 3, NULL, 'scheduled', :now, :now)"
                ),
                {"now": now},
            )
        conn.execute(
            text(
                "INSERT INTO canonical_bouts "
                "(id, event_id, fighter_a_id, fighter_b_id, scheduled_rounds, "
                "weight_class, status, created_at, updated_at) "
                "VALUES ('b1', 'ev1', 'fa', 'fb', 3, NULL, 'scheduled', :now, :now)"
            ),
            {"now": now},
        )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO bout_result_versions "
                    "(bout_id, version_kind, revision, fighter_a_id, fighter_b_id, "
                    "winner_fighter_id, result_type, method, ending_round, time_str, "
                    "effective_at, observed_at, created_at) "
                    "VALUES ('b1', 'event_night', 1, 'fa', 'fb', 'fc', 'win', 'KO', 1, '1:00', "
                    ":now, :now, :now)"
                ),
                {"now": now},
            )
    engine.dispose()


def test_clean_downgrade_base_and_reupgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "clean_roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    names = _table_names(db_path)
    assert LEGACY_TABLES.issubset(names)
    assert CANONICAL_TABLES.issubset(names)


def test_downgrade_base_preserves_preexisting_legacy_fixture_counts(tmp_path: Path) -> None:
    """Legacy rows must survive upgrade → downgrade base → upgrade head."""
    db_path = tmp_path / "roundtrip.db"
    _seed_legacy_schema(db_path)
    before = {
        "fighters": _count_rows(db_path, "fighters"),
        "events": _count_rows(db_path, "events"),
        "fights": _count_rows(db_path, "fights"),
    }
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    # Baseline downgrade must not destroy pre-existing legacy data.
    assert _count_rows(db_path, "fighters") == before["fighters"]
    assert _count_rows(db_path, "events") == before["events"]
    assert _count_rows(db_path, "fights") == before["fights"]
    command.upgrade(cfg, "head")
    after = {
        "fighters": _count_rows(db_path, "fighters"),
        "events": _count_rows(db_path, "events"),
        "fights": _count_rows(db_path, "fights"),
    }
    assert after == before
    conn = sqlite3.connect(db_path)
    try:
        resolved = conn.execute(
            "SELECT fighter_id FROM fighter_source_ids "
            "WHERE source='ufcstats' AND external_id='f1'"
        ).fetchone()
        assert resolved is not None
    finally:
        conn.close()


def test_sqlite_wal_and_foreign_keys_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "pragmas.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    from mma_model.db.session import _attach_sqlite_listeners

    _attach_sqlite_listeners(engine)
    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
    engine.dispose()
    assert int(fk) == 1
    assert str(journal).lower() == "wal"


def test_orphan_fight_import_fails_closed(tmp_path: Path) -> None:
    """Dirty legacy fights with missing event/fighter refs must not be skipped."""
    db_path = tmp_path / "orphan.db"
    _seed_legacy_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Insert a fight pointing at a non-existent event (FK off to simulate dirty legacy).
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO fights "
            "(id, event_id, fighter_a_id, fighter_b_id, winner_id, weight_class, "
            "method, fight_round, time_str, detail_ingested) "
            "VALUES ('orphan1', 'missing_event', 'f1', 'f2', NULL, NULL, NULL, NULL, NULL, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(Exception, match="orphan1|missing_event|unresolved|fail"):
        command.upgrade(_alembic_config(db_path), "head")


def test_downgrade_import_with_null_bout_stat_observations(tmp_path: Path) -> None:
    """0003 downgrade must remove UFCStats fighter stats even when bout_id is NULL."""
    db_path = tmp_path / "null_bout_stats.db"
    _seed_legacy_schema(db_path)
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    from mma_model.db.session import _attach_sqlite_listeners

    _attach_sqlite_listeners(engine)
    now = "2024-01-01T00:00:00+00:00"
    with engine.begin() as conn:
        fighter_id = conn.execute(
            text(
                "SELECT fighter_id FROM fighter_source_ids "
                "WHERE source='ufcstats' AND external_id='f1'"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO fighter_stat_observations "
                "(fighter_id, bout_id, stat_key, value_num, value_text, source, "
                "effective_at, observed_at, created_at) "
                "VALUES (:fighter_id, NULL, 'career_sig_str', 1.0, NULL, 'ufcstats', "
                ":now, :now, :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )
    engine.dispose()

    # Must not raise FK errors when rolling import back.
    command.downgrade(cfg, "0002_canonical_core")
    assert "canonical_fighters" in _table_names(db_path)
    conn = sqlite3.connect(db_path)
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM fighter_source_ids WHERE source='ufcstats'"
        ).fetchone()[0]
        assert remaining == 0
        stats = conn.execute("SELECT COUNT(*) FROM fighter_stat_observations").fetchone()[0]
        assert stats == 0
    finally:
        conn.close()


def test_downgrade_preserves_shared_canonical_entities_and_other_source_rows(
    tmp_path: Path,
) -> None:
    """Shared fighter/event/bout entities and non-UFCStats provenance must survive 0003 down."""
    db_path = tmp_path / "shared_entities.db"
    _seed_legacy_schema(db_path)
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    from mma_model.db.session import _attach_sqlite_listeners

    _attach_sqlite_listeners(engine)
    now = "2024-06-01T00:00:00+00:00"
    with engine.begin() as conn:
        fighter_id = conn.execute(
            text(
                "SELECT fighter_id FROM fighter_source_ids "
                "WHERE source='ufcstats' AND external_id='f1'"
            )
        ).scalar_one()
        event_id = conn.execute(
            text(
                "SELECT event_id FROM event_source_ids "
                "WHERE source='ufcstats' AND external_id='e1'"
            )
        ).scalar_one()
        bout_id = conn.execute(
            text(
                "SELECT bout_id FROM bout_source_ids "
                "WHERE source='ufcstats' AND external_id='fight1'"
            )
        ).scalar_one()

        # Second source retains the same canonical entities.
        conn.execute(
            text(
                "INSERT INTO fighter_source_ids "
                "(fighter_id, source, external_id, created_at) "
                "VALUES (:fighter_id, 'balldontlie', 'bdl-f1', :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO event_source_ids "
                "(event_id, source, external_id, created_at) "
                "VALUES (:event_id, 'balldontlie', 'bdl-e1', :now)"
            ),
            {"event_id": event_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO bout_source_ids "
                "(bout_id, source, external_id, created_at) "
                "VALUES (:bout_id, 'balldontlie', 'bdl-fight1', :now)"
            ),
            {"bout_id": bout_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO fighter_aliases (fighter_id, alias, source, created_at) "
                "VALUES (:fighter_id, 'BDL Alias', 'balldontlie', :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO fighter_profile_observations "
                "(fighter_id, attribute, value_text, value_num, value_date, source, "
                "effective_at, observed_at, created_at) "
                "VALUES (:fighter_id, 'stance', 'Orthodox', NULL, NULL, 'balldontlie', "
                ":now, :now, :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO fighter_stat_observations "
                "(fighter_id, bout_id, stat_key, value_num, value_text, source, "
                "effective_at, observed_at, created_at) "
                "VALUES (:fighter_id, NULL, 'bdl_rating', 9.5, NULL, 'balldontlie', "
                ":now, :now, :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )
        # UFCStats-owned observation should still be removable.
        conn.execute(
            text(
                "INSERT INTO fighter_stat_observations "
                "(fighter_id, bout_id, stat_key, value_num, value_text, source, "
                "effective_at, observed_at, created_at) "
                "VALUES (:fighter_id, NULL, 'ufc_only', 1.0, NULL, 'ufcstats', "
                ":now, :now, :now)"
            ),
            {"fighter_id": fighter_id, "now": now},
        )

    engine.dispose()

    command.downgrade(cfg, "0002_canonical_core")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_source_ids WHERE source='ufcstats'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_source_ids WHERE source='ufcstats'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bout_source_ids WHERE source='ufcstats'"
            ).fetchone()[0]
            == 0
        )

        # Shared canonical entities retained by balldontlie.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_fighters WHERE id=?",
                (fighter_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_events WHERE id=?",
                (event_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM canonical_bouts WHERE id=?",
                (bout_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bout_source_ids "
                "WHERE source='balldontlie' AND bout_id=?",
                (bout_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM event_source_ids "
                "WHERE source='balldontlie' AND event_id=?",
                (event_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_source_ids "
                "WHERE source='balldontlie' AND fighter_id=?",
                (fighter_id,),
            ).fetchone()[0]
            == 1
        )

        # Non-UFCStats provenance/observations preserved.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_aliases "
                "WHERE source='balldontlie' AND alias='BDL Alias'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_profile_observations "
                "WHERE source='balldontlie' AND attribute='stance'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_stat_observations "
                "WHERE source='balldontlie' AND stat_key='bdl_rating'"
            ).fetchone()[0]
            == 1
        )
        # UFCStats-owned observation removed.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_stat_observations WHERE source='ufcstats'"
            ).fetchone()[0]
            == 0
        )
        # UFCStats aliases removed; other-source alias kept.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM fighter_aliases WHERE source='ufcstats'"
            ).fetchone()[0]
            == 0
        )
        # Shared bout dependents remain (participants/results for retained bout).
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bout_participants WHERE bout_id=?",
                (bout_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM bout_result_versions WHERE bout_id=?",
                (bout_id,),
            ).fetchone()[0]
            >= 1
        )
    finally:
        conn.close()


def test_plain_alembic_fails_without_explicit_database_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stock alembic.ini must not silently target a disposable or live DB."""
    monkeypatch.delenv("MMA_DATABASE_URL", raising=False)
    from mma_model.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    with pytest.raises(RuntimeError, match="explicit|MMA_DATABASE_URL|database url"):
        command.upgrade(cfg, "head")
    get_settings.cache_clear()


def test_mma_database_url_env_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disposable = tmp_path / "from_env.db"
    monkeypatch.setenv("MMA_DATABASE_URL", f"sqlite:///{disposable}")
    from mma_model.config import get_settings

    get_settings.cache_clear()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    assert disposable.exists()
    assert CANONICAL_TABLES.issubset(_table_names(disposable))
    get_settings.cache_clear()
