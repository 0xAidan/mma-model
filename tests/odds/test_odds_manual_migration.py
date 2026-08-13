"""Manual-price migration creates the final 0013 schema (DWCS-202)."""

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


def test_fresh_head_creates_final_manual_table(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(odds_manual_price_observations)"))
        }
        assert "attempted_provider" in cols
        assert "selection_identity" in cols
        assert "settlement_identity" not in cols
        conn.execute(
            text(
                """
                INSERT INTO odds_manual_price_observations (
                  dedupe_key, source_kind, automated, bookmaker_key, region,
                  market_family, outcome_key, line_point, price_decimal, lifecycle,
                  attempted_provider, selection_identity, observed_at, created_at
                ) VALUES (
                  'ok-ent', 'user_observed', 0, 'bk', 'us',
                  'moneyline', 'fighter_a', NULL, NULL, 'entitlement_failed',
                  'sportsgameodds', 'moneyline:fighter_a',
                  '2026-08-12T18:00:00+00:00', '2026-08-12T18:00:00+00:00'
                )
                """
            )
        )
        count = conn.execute(
            text("SELECT COUNT(*) FROM odds_manual_price_observations")
        ).scalar_one()
        assert count == 1
        identity = conn.execute(
            text(
                "SELECT selection_identity FROM odds_manual_price_observations "
                "WHERE dedupe_key = 'ok-ent'"
            )
        ).scalar_one()
        assert identity == "moneyline:fighter_a"
