"""Add append-only manual price observations (DWCS-202).

Revision ID: 0013_odds_manual_prices
Revises: 0012_odds_availability
Create Date: 2026-08-12

Upgrade adds ``odds_manual_price_observations`` and append-only SQLite guards.
Downgrade drops that table/triggers only.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0013_odds_manual_prices"
down_revision: Union[str, Sequence[str], None] = "0012_odds_availability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    if "odds_manual_price_observations" not in existing:
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
            sa.CheckConstraint(
                "automated IN (0, 1)",
                name="ck_odds_manual_automated",
            ),
            sa.CheckConstraint(
                "automated = 0",
                name="ck_odds_manual_non_automated",
            ),
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
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()
    if "odds_manual_price_observations" in existing:
        op.drop_table("odds_manual_price_observations")
    install_odds_sqlite_guards(op.get_bind())
