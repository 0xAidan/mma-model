"""Migration coverage for DWCS-201 odds quote/availability tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
ODDS_TABLES = {
    "odds_events",
    "odds_quotes",
    "odds_quota_observations",
    "odds_availability_observations",
}


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _seed_event(conn) -> None:  # noqa: ANN001
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
        assert "odds_availability_observations_no_update" in triggers
        assert "odds_availability_observations_no_delete" in triggers

        _seed_event(conn)
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
        conn.execute(
            text(
                """
                INSERT INTO odds_availability_observations (
                  dedupe_key, provider, region, event_id, external_event_id,
                  bookmaker_key, bookmaker_title, provider_market_key, market_family,
                  availability, observed_at, commence_time, snapshot_at, created_at
                ) VALUES (
                  'unk1', 'the_odds_api', 'us', 'e1', 'ext-1',
                  'draftkings', 'DraftKings', 'totals', 'totals',
                  'unknown', '2026-08-11T21:00:00+00:00',
                  '2026-08-12T00:00:00+00:00', NULL, '2026-08-11T21:00:00+00:00'
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO odds_quota_observations (
                  provider, endpoint, observed_at, requests_remaining, requests_used,
                  requests_last, requests_last_inferred, requests_last_source,
                  empty_response, created_at
                ) VALUES (
                  'the_odds_api', 'current_odds', '2026-08-11T21:00:00+00:00',
                  10, 1, 0, NULL, 'provider', 1, '2026-08-11T21:00:00+00:00'
                )
                """
            )
        )
    engine.dispose()


def test_availability_rejects_null_unknown_key_and_mismatched_pair(tmp_path: Path) -> None:
    db_path = tmp_path / "odds-check.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _seed_event(conn)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_availability_observations (
                      dedupe_key, provider, region, event_id, external_event_id,
                      bookmaker_key, bookmaker_title, provider_market_key, market_family,
                      availability, observed_at, commence_time, snapshot_at, created_at
                    ) VALUES (
                      'null-family', 'the_odds_api', 'us', 'e1', 'ext-1',
                      'draftkings', 'DraftKings', 'totals', NULL,
                      'unknown', '2026-08-11T21:00:00+00:00',
                      '2026-08-12T00:00:00+00:00', NULL, '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_availability_observations (
                      dedupe_key, provider, region, event_id, external_event_id,
                      bookmaker_key, bookmaker_title, provider_market_key, market_family,
                      availability, observed_at, commence_time, snapshot_at, created_at
                    ) VALUES (
                      'bad-key', 'the_odds_api', 'us', 'e1', 'ext-1',
                      'draftkings', 'DraftKings', 'method_of_victory', 'method',
                      'unknown', '2026-08-11T21:00:00+00:00',
                      '2026-08-12T00:00:00+00:00', NULL, '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_availability_observations (
                      dedupe_key, provider, region, event_id, external_event_id,
                      bookmaker_key, bookmaker_title, provider_market_key, market_family,
                      availability, observed_at, commence_time, snapshot_at, created_at
                    ) VALUES (
                      'mismatch', 'the_odds_api', 'us', 'e1', 'ext-1',
                      'draftkings', 'DraftKings', 'h2h', 'method',
                      'unknown', '2026-08-11T21:00:00+00:00',
                      '2026-08-12T00:00:00+00:00', NULL, '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )
    engine.dispose()


def test_quota_provenance_and_quote_integrity_checks(tmp_path: Path) -> None:
    db_path = tmp_path / "odds-quota-check.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        _seed_event(conn)

    # inferred_empty_zero without empty_response=1
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_quota_observations (
                      provider, endpoint, observed_at, requests_remaining, requests_used,
                      requests_last, requests_last_inferred, requests_last_source,
                      empty_response, created_at
                    ) VALUES (
                      'the_odds_api', 'current_odds', '2026-08-11T21:00:00+00:00',
                      10, 1, NULL, 0, 'inferred_empty_zero', 0,
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # provider source with null requests_last
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_quota_observations (
                      provider, endpoint, observed_at, requests_remaining, requests_used,
                      requests_last, requests_last_inferred, requests_last_source,
                      empty_response, created_at
                    ) VALUES (
                      'the_odds_api', 'current_odds', '2026-08-11T21:00:00+00:00',
                      10, 1, NULL, NULL, 'provider', 0,
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # missing source with inferred set
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_quota_observations (
                      provider, endpoint, observed_at, requests_remaining, requests_used,
                      requests_last, requests_last_inferred, requests_last_source,
                      empty_response, created_at
                    ) VALUES (
                      'the_odds_api', 'current_odds', '2026-08-11T21:00:00+00:00',
                      10, 1, NULL, 0, 'missing', 0,
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # negative counter
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO odds_quota_observations (
                      provider, endpoint, observed_at, requests_remaining, requests_used,
                      requests_last, requests_last_inferred, requests_last_source,
                      empty_response, created_at
                    ) VALUES (
                      'the_odds_api', 'current_odds', '2026-08-11T21:00:00+00:00',
                      -1, 1, 1, NULL, 'provider', 0,
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # quote price_decimal <= 1.0
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
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
                      'bad-price', 'the_odds_api', 'fanduel', 'FanDuel', 'us',
                      'e1', 'ext-1', 'moneyline', 'h2h',
                      'fighter_a', 'A', NULL, 1.0, 'available',
                      '2026-08-11T21:00:00+00:00', NULL,
                      '2026-08-12T00:00:00+00:00', NULL, 'raw',
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # quote arbitrary availability
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
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
                      'bad-avail', 'the_odds_api', 'fanduel', 'FanDuel', 'us',
                      'e1', 'ext-1', 'moneyline', 'h2h',
                      'fighter_a', 'A', NULL, 1.8, 'maybe',
                      '2026-08-11T21:00:00+00:00', NULL,
                      '2026-08-12T00:00:00+00:00', NULL, 'raw',
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )

    # quote mismatched pair
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
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
                      'bad-pair', 'the_odds_api', 'fanduel', 'FanDuel', 'us',
                      'e1', 'ext-1', 'method', 'h2h',
                      'fighter_a', 'A', NULL, 1.8, 'available',
                      '2026-08-11T21:00:00+00:00', NULL,
                      '2026-08-12T00:00:00+00:00', NULL, 'raw',
                      '2026-08-11T21:00:00+00:00'
                    )
                    """
                )
            )
    engine.dispose()


def test_downgrade_removes_availability_then_odds_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "odds-down.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0011_odds_quotes")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine).get_table_names())
    assert "odds_availability_observations" not in names
    assert "odds_quotes" in names
    engine.dispose()

    command.downgrade(cfg, "0010_result_version_provenance")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(inspect(engine).get_table_names())
    assert ODDS_TABLES.isdisjoint(names)
    assert "raw_observations" in names
    engine.dispose()
