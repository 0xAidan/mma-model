"""Migration coverage for DWCS-201 odds quote tables."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
ODDS_TABLES = {
    "odds_events",
    "odds_quotes",
    "odds_quota_observations",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def test_upgrade_head_creates_odds_tables_and_append_only_triggers(tmp_path: Path) -> None:
    db_path = tmp_path / "odds-mig.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine).get_table_names())
    assert ODDS_TABLES.issubset(names)

    with engine.begin() as conn:
        triggers = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        assert "odds_quotes_no_update" in triggers
        assert "odds_quotes_no_delete" in triggers

        conn.execute(
            text(
                """
                INSERT INTO odds_events (
                  id, provider, external_event_id, sport_key, home_team, away_team,
                  commence_time, created_at, updated_at
                ) VALUES (
                  'e1', 'the_odds_api', 'ext-1', 'mma_mixed_martial_arts',
                  'A', 'B', '2026-08-12T00:00:00+00:00',
                  '2026-08-11T00:00:00+00:00', '2026-08-11T00:00:00+00:00'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO odds_quotes (
                  dedupe_key, provider, bookmaker_key, bookmaker_title, region,
                  event_id, external_event_id, market_family, provider_market_key,
                  outcome_key, outcome_label, line_point, price_decimal, availability,
                  observed_at, source_updated_at, commence_time, snapshot_at, raw_ref,
                  created_at
                ) VALUES (
                  'abc', 'the_odds_api', 'fanduel', 'FanDuel', 'us',
                  'e1', 'ext-1', 'moneyline', 'h2h',
                  'fighter_a', 'A', NULL, 1.8, 'available',
                  '2026-08-11T21:00:00+00:00', '2026-08-11T21:00:00+00:00',
                  '2026-08-12T00:00:00+00:00', NULL, 'raw',
                  '2026-08-11T21:00:00+00:00'
                )
                """
            )
        )
    engine.dispose()


def test_downgrade_removes_odds_tables_only(tmp_path: Path) -> None:
    db_path = tmp_path / "odds-down.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0010_result_version_provenance")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine).get_table_names())
    assert ODDS_TABLES.isdisjoint(names)
    assert "raw_observations" in names
    engine.dispose()
