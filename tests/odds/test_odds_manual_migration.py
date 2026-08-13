"""Manual-price migration preserves audit rows (DWCS-202)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_manual_price_upgrade_preserves_available_and_entitlement_rows(
    tmp_path: Path,
) -> None:
    """Seed a legacy draft table, upgrade 0013, prove zero row loss + backfill."""
    db_path = tmp_path / "manual-mig.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "0012_odds_availability")

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE odds_manual_price_observations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  dedupe_key VARCHAR(64) NOT NULL UNIQUE,
                  source_kind VARCHAR(32) NOT NULL,
                  automated INTEGER NOT NULL,
                  bookmaker_key VARCHAR(64) NOT NULL,
                  bookmaker_title VARCHAR(128),
                  region VARCHAR(32) NOT NULL,
                  market_family VARCHAR(64) NOT NULL,
                  outcome_key VARCHAR(64) NOT NULL,
                  line_point FLOAT,
                  price_decimal FLOAT,
                  lifecycle VARCHAR(32) NOT NULL,
                  observed_at DATETIME NOT NULL,
                  source_updated_at DATETIME,
                  event_external_id VARCHAR(128),
                  settlement_identity VARCHAR(200),
                  detail VARCHAR(500),
                  created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO odds_manual_price_observations (
                  dedupe_key, source_kind, automated, bookmaker_key, region,
                  market_family, outcome_key, line_point, price_decimal, lifecycle,
                  observed_at, detail, created_at
                ) VALUES
                (
                  'avail-1', 'user_observed', 0, 'fanduel', 'us',
                  'moneyline', 'fighter_a', NULL, 1.91, 'available',
                  '2026-08-12T18:00:00+00:00', NULL, '2026-08-12T18:00:00+00:00'
                ),
                (
                  'ent-1', 'user_observed', 0, 'bet365', 'uk',
                  'moneyline', 'fighter_b', NULL, NULL, 'entitlement_failed',
                  '2026-08-12T18:05:00+00:00',
                  'provider=opticodds: Phase 0 unauthorized',
                  '2026-08-12T18:05:00+00:00'
                ),
                (
                  'ent-2', 'user_observed', 0, 'draftkings', 'us',
                  'totals', 'over', 2.5, NULL, 'entitlement_failed',
                  '2026-08-12T18:06:00+00:00',
                  'no parseable provider prefix',
                  '2026-08-12T18:06:00+00:00'
                )
                """
            )
        )
        before = conn.execute(
            text("SELECT COUNT(*) FROM odds_manual_price_observations")
        ).scalar_one()
        assert before == 3

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT COUNT(*) FROM odds_manual_price_observations")
        ).scalar_one()
        assert after == 3
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(odds_manual_price_observations)"))
        }
        assert "attempted_provider" in cols
        rows = {
            row.dedupe_key: row
            for row in conn.execute(
                text(
                    "SELECT dedupe_key, lifecycle, attempted_provider, price_decimal "
                    "FROM odds_manual_price_observations"
                )
            ).mappings()
        }
        assert rows["avail-1"].lifecycle == "available"
        assert rows["avail-1"].attempted_provider is None
        assert rows["avail-1"].price_decimal == 1.91
        assert rows["ent-1"].lifecycle == "entitlement_failed"
        assert rows["ent-1"].attempted_provider == "opticodds"
        assert rows["ent-1"].price_decimal is None
        assert rows["ent-2"].attempted_provider == "legacy_unspecified"


def test_fresh_head_creates_final_manual_table(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO odds_manual_price_observations (
                  dedupe_key, source_kind, automated, bookmaker_key, region,
                  market_family, outcome_key, line_point, price_decimal, lifecycle,
                  attempted_provider, observed_at, created_at
                ) VALUES (
                  'ok-ent', 'user_observed', 0, 'bk', 'us',
                  'moneyline', 'fighter_a', NULL, NULL, 'entitlement_failed',
                  'sportsgameodds', '2026-08-12T18:00:00+00:00',
                  '2026-08-12T18:00:00+00:00'
                )
                """
            )
        )
        count = conn.execute(
            text("SELECT COUNT(*) FROM odds_manual_price_observations")
        ).scalar_one()
        assert count == 1
