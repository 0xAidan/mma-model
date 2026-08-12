"""Add append-only odds availability observations (DWCS-201 review).

Revision ID: 0012_odds_availability
Revises: 0011_odds_quotes
Create Date: 2026-08-12

Upgrade adds ``odds_availability_observations`` and append-only SQLite guards.
Downgrade drops that table/triggers only.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards
from mma_model.domain.markets import MarketFamily

revision: str = "0012_odds_availability"
down_revision: Union[str, Sequence[str], None] = "0011_odds_quotes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MARKET_FAMILY_SQL = ", ".join(repr(member.value) for member in MarketFamily)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    if "odds_availability_observations" not in existing:
        op.create_table(
            "odds_availability_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("dedupe_key", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("region", sa.String(length=32), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("bookmaker_key", sa.String(length=64), nullable=True),
            sa.Column("bookmaker_title", sa.String(length=128), nullable=True),
            sa.Column("provider_market_key", sa.String(length=64), nullable=False),
            sa.Column("market_family", sa.String(length=64), nullable=False),
            sa.Column("availability", sa.String(length=32), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("commence_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                f"market_family IN ({_MARKET_FAMILY_SQL})",
                name="ck_odds_availability_market_family",
            ),
            sa.ForeignKeyConstraint(["event_id"], ["odds_events.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key", name="uq_odds_availability_dedupe_key"),
        )
        op.create_index(
            "ix_odds_availability_observations_dedupe_key",
            "odds_availability_observations",
            ["dedupe_key"],
        )
        op.create_index(
            "ix_odds_availability_observations_market_family",
            "odds_availability_observations",
            ["market_family"],
        )
        op.create_index(
            "ix_odds_availability_observations_provider",
            "odds_availability_observations",
            ["provider"],
        )
        op.create_index(
            "ix_odds_availability_observations_region",
            "odds_availability_observations",
            ["region"],
        )
        op.create_index(
            "ix_odds_availability_observations_event_id",
            "odds_availability_observations",
            ["event_id"],
        )
        op.create_index(
            "ix_odds_availability_observations_external_event_id",
            "odds_availability_observations",
            ["external_event_id"],
        )
        op.create_index(
            "ix_odds_availability_observations_bookmaker_key",
            "odds_availability_observations",
            ["bookmaker_key"],
        )
        op.create_index(
            "ix_odds_availability_observations_provider_market_key",
            "odds_availability_observations",
            ["provider_market_key"],
        )
        op.create_index(
            "ix_odds_availability_observations_availability",
            "odds_availability_observations",
            ["availability"],
        )
        op.create_index(
            "ix_odds_availability_observations_observed_at",
            "odds_availability_observations",
            ["observed_at"],
        )
        op.create_index(
            "ix_odds_availability_observations_snapshot_at",
            "odds_availability_observations",
            ["snapshot_at"],
        )
    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    if "odds_availability_observations" in _existing_tables():
        op.drop_table("odds_availability_observations")
    # Reinstall quote-only guards after availability drop.
    install_odds_sqlite_guards(op.get_bind())
