"""Harden manual price integrity: selection CHECKs + attempted_provider (DWCS-202).

Revision ID: 0014_odds_manual_integrity
Revises: 0013_odds_manual_prices
Create Date: 2026-08-12

SQLite cannot ADD CHECK constraints in place; recreate
``odds_manual_price_observations`` with full DWCS-200 selection semantics and
structured ``attempted_provider`` for entitlement failures.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0014_odds_manual_integrity"
down_revision: Union[str, Sequence[str], None] = "0013_odds_manual_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FAMILY_OUTCOME_LINE_SQL = (
    "("
    "market_family = 'moneyline' AND outcome_key IN ('fighter_a', 'fighter_b') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'totals' AND outcome_key IN ('over', 'under') "
    "AND line_point IN (1.5, 2.5)"
    ") OR ("
    "market_family = 'goes_distance' "
    "AND outcome_key IN ('goes_distance', 'inside_distance') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'method' "
    "AND outcome_key IN ('ko_tko', 'submission', 'decision', 'other_stoppage') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'fighter_by_method' AND outcome_key IN ("
    "'a_ko_tko', 'a_submission', 'a_other_stoppage', 'a_decision', "
    "'b_ko_tko', 'b_submission', 'b_other_stoppage', 'b_decision'"
    ") AND line_point IS NULL"
    ") OR ("
    "market_family = 'exact_round' "
    "AND outcome_key IN ('round_1', 'round_2', 'round_3', 'round_4', 'round_5') "
    "AND line_point IS NULL"
    ")"
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_tables()
    if "odds_manual_price_observations" not in existing:
        return

    drop_odds_sqlite_guards(op.get_bind())
    cols = _column_names("odds_manual_price_observations")
    # Idempotent: already rebuilt with attempted_provider + new CHECKs.
    if "attempted_provider" in cols:
        install_odds_sqlite_guards(op.get_bind())
        return

    op.rename_table(
        "odds_manual_price_observations",
        "odds_manual_price_observations_old",
    )
    op.create_table(
        "odds_manual_price_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("automated", sa.Integer(), nullable=False),
        sa.Column("bookmaker_key", sa.String(length=64), nullable=False),
        sa.Column("bookmaker_title", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("market_family", sa.String(length=64), nullable=False),
        sa.Column("outcome_key", sa.String(length=64), nullable=False),
        sa.Column("line_point", sa.Float(), nullable=True),
        sa.Column("price_decimal", sa.Float(), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("attempted_provider", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_external_id", sa.String(length=128), nullable=True),
        sa.Column("settlement_identity", sa.String(length=200), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('user_observed')",
            name="ck_odds_manual_source_kind",
        ),
        sa.CheckConstraint("automated IN (0, 1)", name="ck_odds_manual_automated"),
        sa.CheckConstraint("automated = 0", name="ck_odds_manual_non_automated"),
        sa.CheckConstraint(
            "lifecycle IN ('available', 'unknown', 'suspended', "
            "'locked', 'removed', 'entitlement_failed')",
            name="ck_odds_manual_lifecycle",
        ),
        sa.CheckConstraint(
            "("
            "lifecycle = 'available' AND price_decimal IS NOT NULL "
            "AND price_decimal > 1.0"
            ") OR ("
            "lifecycle != 'available' AND price_decimal IS NULL"
            ")",
            name="ck_odds_manual_price_provenance",
        ),
        sa.CheckConstraint(
            "("
            "lifecycle = 'entitlement_failed' "
            "AND attempted_provider IS NOT NULL "
            "AND length(trim(attempted_provider)) > 0"
            ") OR ("
            "lifecycle != 'entitlement_failed' AND attempted_provider IS NULL"
            ")",
            name="ck_odds_manual_attempted_provider",
        ),
        sa.CheckConstraint(
            _FAMILY_OUTCOME_LINE_SQL,
            name="ck_odds_manual_family_outcome_line",
        ),
        sa.CheckConstraint(
            "length(trim(bookmaker_key)) > 0",
            name="ck_odds_manual_bookmaker_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(region)) > 0",
            name="ck_odds_manual_region_nonempty",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_odds_manual_price_dedupe_key"),
    )
    op.execute(
        text(
            """
            INSERT INTO odds_manual_price_observations (
              id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
              region, market_family, outcome_key, line_point, price_decimal, lifecycle,
              attempted_provider, observed_at, source_updated_at, event_external_id,
              settlement_identity, detail, created_at
            )
            SELECT
              id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
              region, market_family, outcome_key, line_point, price_decimal, lifecycle,
              NULL, observed_at, source_updated_at, event_external_id,
              settlement_identity, detail, created_at
            FROM odds_manual_price_observations_old
            WHERE lifecycle != 'entitlement_failed'
            """
        )
    )
    op.drop_table("odds_manual_price_observations_old")
    op.create_index(
        "ix_odds_manual_price_observations_dedupe_key",
        "odds_manual_price_observations",
        ["dedupe_key"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_source_kind",
        "odds_manual_price_observations",
        ["source_kind"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_bookmaker_key",
        "odds_manual_price_observations",
        ["bookmaker_key"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_region",
        "odds_manual_price_observations",
        ["region"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_market_family",
        "odds_manual_price_observations",
        ["market_family"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_outcome_key",
        "odds_manual_price_observations",
        ["outcome_key"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_lifecycle",
        "odds_manual_price_observations",
        ["lifecycle"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_attempted_provider",
        "odds_manual_price_observations",
        ["attempted_provider"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_observed_at",
        "odds_manual_price_observations",
        ["observed_at"],
    )
    op.create_index(
        "ix_odds_manual_price_observations_event_external_id",
        "odds_manual_price_observations",
        ["event_external_id"],
    )
    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    existing = _existing_tables()
    if "odds_manual_price_observations" not in existing:
        return
    drop_odds_sqlite_guards(op.get_bind())
    cols = _column_names("odds_manual_price_observations")
    if "attempted_provider" not in cols:
        install_odds_sqlite_guards(op.get_bind())
        return

    op.rename_table(
        "odds_manual_price_observations",
        "odds_manual_price_observations_new",
    )
    op.create_table(
        "odds_manual_price_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("automated", sa.Integer(), nullable=False),
        sa.Column("bookmaker_key", sa.String(length=64), nullable=False),
        sa.Column("bookmaker_title", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("market_family", sa.String(length=64), nullable=False),
        sa.Column("outcome_key", sa.String(length=64), nullable=False),
        sa.Column("line_point", sa.Float(), nullable=True),
        sa.Column("price_decimal", sa.Float(), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_external_id", sa.String(length=128), nullable=True),
        sa.Column("settlement_identity", sa.String(length=200), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('user_observed')",
            name="ck_odds_manual_source_kind",
        ),
        sa.CheckConstraint("automated IN (0, 1)", name="ck_odds_manual_automated"),
        sa.CheckConstraint("automated = 0", name="ck_odds_manual_non_automated"),
        sa.CheckConstraint(
            "lifecycle IN ('available', 'unknown', 'suspended', "
            "'locked', 'removed', 'entitlement_failed')",
            name="ck_odds_manual_lifecycle",
        ),
        sa.CheckConstraint(
            "("
            "lifecycle = 'available' AND price_decimal IS NOT NULL "
            "AND price_decimal > 1.0"
            ") OR ("
            "lifecycle != 'available' AND price_decimal IS NULL"
            ")",
            name="ck_odds_manual_price_provenance",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_odds_manual_price_dedupe_key"),
    )
    op.execute(
        text(
            """
            INSERT INTO odds_manual_price_observations (
              id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
              region, market_family, outcome_key, line_point, price_decimal, lifecycle,
              observed_at, source_updated_at, event_external_id,
              settlement_identity, detail, created_at
            )
            SELECT
              id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
              region, market_family, outcome_key, line_point, price_decimal, lifecycle,
              observed_at, source_updated_at, event_external_id,
              settlement_identity, detail, created_at
            FROM odds_manual_price_observations_new
            """
        )
    )
    op.drop_table("odds_manual_price_observations_new")
    install_odds_sqlite_guards(op.get_bind())
