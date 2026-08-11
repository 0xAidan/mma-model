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
                    "(bout_id, version_kind, fighter_a_id, fighter_b_id, winner_fighter_id, "
                    "result_type, method, ending_round, time_str, effective_at, observed_at, "
                    "created_at) "
                    "VALUES ('b1', 'event_night', 'fa', 'fb', 'fc', 'win', 'KO', 1, '1:00', "
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


def test_downgrade_upgrade_preserves_legacy_fixture_counts(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    _seed_legacy_schema(db_path)
    before = {
        "fighters": _count_rows(db_path, "fighters"),
        "events": _count_rows(db_path, "events"),
        "fights": _count_rows(db_path, "fights"),
    }
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    # Downgrade through canonical+import only; baseline drop would erase fixtures.
    command.downgrade(cfg, "0001_baseline")
    command.upgrade(cfg, "head")
    after = {
        "fighters": _count_rows(db_path, "fighters"),
        "events": _count_rows(db_path, "events"),
        "fights": _count_rows(db_path, "fights"),
    }
    assert after == before
    # Source IDs remain resolvable after round-trip upgrade.
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


def test_alembic_cli_defaults_to_disposable_url() -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    url = cfg.get_main_option("sqlalchemy.url")
    assert url is not None
    assert url.endswith("mma_alembic_disposable.db")
    assert "mma.db" not in url


def test_mma_database_url_env_is_respected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disposable = tmp_path / "from_env.db"
    monkeypatch.setenv("MMA_DATABASE_URL", f"sqlite:///{disposable}")
    from mma_model.config import get_settings

    get_settings.cache_clear()
    # Stock Config still points at disposable placeholder; env must win in env.py.
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    assert disposable.exists()
    assert CANONICAL_TABLES.issubset(_table_names(disposable))
    get_settings.cache_clear()
