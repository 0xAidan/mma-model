"""Add odds matching alias/lifecycle tables (DWCS-203).

Revision ID: 0014_odds_matching
Revises: 0013_odds_manual_prices
Create Date: 2026-08-12

Upgrade adds versioned provider-event aliases, append-only match decisions, and
append-only bout lifecycle observations. Downgrade drops those tables/triggers.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0014_odds_matching"
down_revision: Union[str, Sequence[str], None] = "0013_odds_manual_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MATCH_TABLES = (
    "odds_bout_lifecycle_observations",
    "odds_match_observations",
    "odds_provider_event_aliases",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    drop_odds_sqlite_guards(op.get_bind())

    if "odds_provider_event_aliases" not in existing:
        op.create_table(
            "odds_provider_event_aliases",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("alias_version", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("match_rule", sa.String(length=64), nullable=False),
            sa.Column("evidence_json", sa.String(length=2000), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="ck_odds_provider_event_alias_status",
            ),
            sa.CheckConstraint(
                "match_rule IN ('provider_id', 'participant_pair')",
                name="ck_odds_provider_event_alias_match_rule",
            ),
            sa.CheckConstraint(
                "alias_version >= 1",
                name="ck_odds_provider_event_alias_version",
            ),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "provider",
                "external_event_id",
                "alias_version",
                name="uq_odds_provider_event_alias_version",
            ),
        )
        op.create_index(
            "ix_odds_provider_event_aliases_provider",
            "odds_provider_event_aliases",
            ["provider"],
        )
        op.create_index(
            "ix_odds_provider_event_aliases_external_event_id",
            "odds_provider_event_aliases",
            ["external_event_id"],
        )
        op.create_index(
            "ix_odds_provider_event_aliases_bout_id",
            "odds_provider_event_aliases",
            ["bout_id"],
        )
        op.create_index(
            "ix_odds_provider_event_aliases_status",
            "odds_provider_event_aliases",
            ["status"],
        )

    if "odds_match_observations" not in existing:
        op.create_table(
            "odds_match_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("dedupe_key", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=True),
            sa.Column("match_status", sa.String(length=32), nullable=False),
            sa.Column("match_rule", sa.String(length=64), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=False),
            sa.Column("review_id", sa.String(length=36), nullable=True),
            sa.Column("eligible_for_value", sa.Integer(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "match_status IN ('matched', 'unmatched', 'ambiguous_blocked')",
                name="ck_odds_match_observations_status",
            ),
            sa.CheckConstraint(
                "("
                "match_rule IS NULL OR "
                "match_rule IN ('provider_id', 'participant_pair')"
                ")",
                name="ck_odds_match_observations_rule",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "dedupe_key", name="uq_odds_match_observations_dedupe_key"
            ),
        )
        for name, col in (
            ("ix_odds_match_observations_dedupe_key", "dedupe_key"),
            ("ix_odds_match_observations_provider", "provider"),
            ("ix_odds_match_observations_external_event_id", "external_event_id"),
            ("ix_odds_match_observations_bout_id", "bout_id"),
            ("ix_odds_match_observations_match_status", "match_status"),
            ("ix_odds_match_observations_review_id", "review_id"),
            ("ix_odds_match_observations_observed_at", "observed_at"),
        ):
            op.create_index(name, "odds_match_observations", [col])

    if "odds_bout_lifecycle_observations" not in existing:
        op.create_table(
            "odds_bout_lifecycle_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("dedupe_key", sa.String(length=64), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("external_event_id", sa.String(length=128), nullable=True),
            sa.Column("lifecycle", sa.String(length=32), nullable=False),
            sa.Column("evidence_kind", sa.String(length=128), nullable=False),
            sa.Column("detail", sa.String(length=500), nullable=True),
            sa.Column("price_decimal", sa.Float(), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "lifecycle IN ("
                "'active', 'stale', 'missing_unknown', 'locked', "
                "'cancelled', 'replaced', 'review_blocked'"
                ")",
                name="ck_odds_bout_lifecycle_lifecycle",
            ),
            sa.CheckConstraint(
                "price_decimal IS NULL",
                name="ck_odds_bout_lifecycle_no_price",
            ),
            sa.CheckConstraint(
                "length(trim(evidence_kind)) > 0",
                name="ck_odds_bout_lifecycle_evidence_kind",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "dedupe_key", name="uq_odds_bout_lifecycle_dedupe_key"
            ),
        )
        for name, col in (
            ("ix_odds_bout_lifecycle_observations_dedupe_key", "dedupe_key"),
            ("ix_odds_bout_lifecycle_observations_bout_id", "bout_id"),
            ("ix_odds_bout_lifecycle_observations_provider", "provider"),
            ("ix_odds_bout_lifecycle_observations_external_event_id", "external_event_id"),
            ("ix_odds_bout_lifecycle_observations_lifecycle", "lifecycle"),
            ("ix_odds_bout_lifecycle_observations_observed_at", "observed_at"),
        ):
            op.create_index(name, "odds_bout_lifecycle_observations", [col])

    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()
    for table in MATCH_TABLES:
        if table in existing:
            op.drop_table(table)
    install_odds_sqlite_guards(op.get_bind())
