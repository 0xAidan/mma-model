"""Append-only odds events, quotes, and quota observations (DWCS-201).

Revision ID: 0011_odds_quotes
Revises: 0010_result_version_provenance
Create Date: 2026-08-12

Upgrade adds DWCS-201-owned odds tables and SQLite append-only guards.
Downgrade drops those tables/triggers only.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0011_odds_quotes"
down_revision: Union[str, Sequence[str], None] = "0010_result_version_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ODDS_TABLES = (
    "odds_quotes",
    "odds_quota_observations",
    "odds_events",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "odds_events" not in existing:
        op.create_table(
            "odds_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("sport_key", sa.String(length=80), nullable=False),
            sa.Column("home_team", sa.String(length=200), nullable=False),
            sa.Column("away_team", sa.String(length=200), nullable=False),
            sa.Column("commence_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "provider",
                "external_event_id",
                name="uq_odds_events_provider_ext",
            ),
        )
        op.create_index("ix_odds_events_provider", "odds_events", ["provider"])
        op.create_index(
            "ix_odds_events_external_event_id",
            "odds_events",
            ["external_event_id"],
        )
        op.create_index(
            "ix_odds_events_commence_time",
            "odds_events",
            ["commence_time"],
        )

    if "odds_quota_observations" not in existing:
        op.create_table(
            "odds_quota_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("endpoint", sa.String(length=128), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("requests_remaining", sa.Integer(), nullable=True),
            sa.Column("requests_used", sa.Integer(), nullable=True),
            sa.Column("requests_last", sa.Integer(), nullable=True),
            sa.Column("requests_last_inferred", sa.Integer(), nullable=True),
            sa.Column("requests_last_source", sa.String(length=32), nullable=False),
            sa.Column("empty_response", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "requests_last_source IN "
                "('inferred_empty_zero', 'missing', 'provider')",
                name="ck_odds_quota_requests_last_source",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_odds_quota_observations_provider",
            "odds_quota_observations",
            ["provider"],
        )
        op.create_index(
            "ix_odds_quota_observations_endpoint",
            "odds_quota_observations",
            ["endpoint"],
        )
        op.create_index(
            "ix_odds_quota_observations_observed_at",
            "odds_quota_observations",
            ["observed_at"],
        )

    if "odds_quotes" not in existing:
        op.create_table(
            "odds_quotes",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("dedupe_key", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("bookmaker_key", sa.String(length=64), nullable=False),
            sa.Column("bookmaker_title", sa.String(length=128), nullable=False),
            sa.Column("region", sa.String(length=32), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("market_family", sa.String(length=64), nullable=False),
            sa.Column("provider_market_key", sa.String(length=64), nullable=False),
            sa.Column("outcome_key", sa.String(length=64), nullable=False),
            sa.Column("outcome_label", sa.String(length=200), nullable=False),
            sa.Column("line_point", sa.Float(), nullable=True),
            sa.Column("price_decimal", sa.Float(), nullable=False),
            sa.Column("availability", sa.String(length=32), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("commence_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_ref", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["odds_events.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key", name="uq_odds_quotes_dedupe_key"),
        )
        op.create_index("ix_odds_quotes_dedupe_key", "odds_quotes", ["dedupe_key"])
        op.create_index("ix_odds_quotes_provider", "odds_quotes", ["provider"])
        op.create_index("ix_odds_quotes_bookmaker_key", "odds_quotes", ["bookmaker_key"])
        op.create_index("ix_odds_quotes_region", "odds_quotes", ["region"])
        op.create_index("ix_odds_quotes_event_id", "odds_quotes", ["event_id"])
        op.create_index(
            "ix_odds_quotes_external_event_id",
            "odds_quotes",
            ["external_event_id"],
        )
        op.create_index("ix_odds_quotes_market_family", "odds_quotes", ["market_family"])
        op.create_index("ix_odds_quotes_outcome_key", "odds_quotes", ["outcome_key"])
        op.create_index("ix_odds_quotes_availability", "odds_quotes", ["availability"])
        op.create_index("ix_odds_quotes_observed_at", "odds_quotes", ["observed_at"])
        op.create_index("ix_odds_quotes_snapshot_at", "odds_quotes", ["snapshot_at"])

    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()
    for table in ODDS_TABLES:
        if table in existing:
            op.drop_table(table)
