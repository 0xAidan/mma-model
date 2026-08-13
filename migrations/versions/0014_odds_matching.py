"""Add odds matching, review queue, and lifecycle tables (DWCS-203).

Revision ID: 0014_odds_matching
Revises: 0013_odds_manual_prices
Create Date: 2026-08-12

Final unshipped schema: versioned provider-event aliases, dedicated bout-match
reviews, append-only match/lifecycle observations, FKs/CHECKs, and partial
unique active-alias index. Downgrade drops the DWCS-203 tables/indexes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0014_odds_matching"
down_revision: Union[str, Sequence[str], None] = "0013_odds_manual_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MATCH_TABLES = (
    "odds_match_observations",
    "odds_bout_lifecycle_observations",
    "odds_provider_event_aliases",
    "odds_bout_match_reviews",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    drop_odds_sqlite_guards(op.get_bind())

    if "odds_bout_match_reviews" not in existing:
        op.create_table(
            "odds_bout_match_reviews",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("external_event_id", sa.String(length=128), nullable=False),
            sa.Column("home_team", sa.String(length=200), nullable=False),
            sa.Column("away_team", sa.String(length=200), nullable=False),
            sa.Column("commence_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("candidate_bout_ids_json", sa.String(length=2000), nullable=False),
            sa.Column("reason", sa.String(length=500), nullable=False),
            sa.Column("rule_id", sa.String(length=128), nullable=False),
            sa.Column("evidence_json", sa.String(length=2000), nullable=False),
            sa.Column("decision_bout_id", sa.String(length=36), nullable=True),
            sa.Column("activated_alias_id", sa.String(length=36), nullable=True),
            sa.Column("activated_alias_version", sa.Integer(), nullable=True),
            sa.Column("decided_by", sa.String(length=128), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'approved', 'rejected', 'reversed')",
                name="ck_odds_bout_match_reviews_status",
            ),
            sa.CheckConstraint(
                "version >= 1",
                name="ck_odds_bout_match_reviews_version",
            ),
            sa.CheckConstraint(
                "("
                "status = 'approved' AND decision_bout_id IS NOT NULL"
                ") OR ("
                "status != 'approved'"
                ")",
                name="ck_odds_bout_match_reviews_decision",
            ),
            sa.ForeignKeyConstraint(["decision_bout_id"], ["canonical_bouts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        for name, col in (
            ("ix_odds_bout_match_reviews_status", "status"),
            ("ix_odds_bout_match_reviews_provider", "provider"),
            ("ix_odds_bout_match_reviews_external_event_id", "external_event_id"),
            ("ix_odds_bout_match_reviews_commence_time", "commence_time"),
            ("ix_odds_bout_match_reviews_decision_bout_id", "decision_bout_id"),
            ("ix_odds_bout_match_reviews_activated_alias_id", "activated_alias_id"),
        ):
            op.create_index(name, "odds_bout_match_reviews", [col])
        op.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_odds_bout_match_reviews_pending_provider_ext "
                "ON odds_bout_match_reviews (provider, external_event_id) "
                "WHERE status = 'pending'"
            )
        )

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
                "match_rule IN ('provider_id', 'participant_pair', 'manual_review')",
                name="ck_odds_provider_event_alias_match_rule",
            ),
            sa.CheckConstraint(
                "alias_version >= 1",
                name="ck_odds_provider_event_alias_version",
            ),
            sa.CheckConstraint(
                "("
                "status = 'active' AND superseded_at IS NULL"
                ") OR ("
                "status = 'superseded' AND superseded_at IS NOT NULL"
                ")",
                name="ck_odds_provider_event_alias_superseded_at",
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
        for name, col in (
            ("ix_odds_provider_event_aliases_provider", "provider"),
            ("ix_odds_provider_event_aliases_external_event_id", "external_event_id"),
            ("ix_odds_provider_event_aliases_bout_id", "bout_id"),
            ("ix_odds_provider_event_aliases_status", "status"),
        ):
            op.create_index(name, "odds_provider_event_aliases", [col])
        op.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_odds_provider_event_alias_active "
                "ON odds_provider_event_aliases (provider, external_event_id) "
                "WHERE status = 'active'"
            )
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
                "match_rule IN ('provider_id', 'participant_pair', 'manual_review')"
                ")",
                name="ck_odds_match_observations_rule",
            ),
            sa.CheckConstraint(
                "eligible_for_value IN (0, 1)",
                name="ck_odds_match_observations_eligible",
            ),
            sa.CheckConstraint(
                "("
                "match_status = 'matched' AND bout_id IS NOT NULL "
                "AND match_rule IS NOT NULL AND eligible_for_value IN (0, 1)"
                ") OR ("
                "match_status IN ('unmatched', 'ambiguous_blocked') "
                "AND bout_id IS NULL AND match_rule IS NULL "
                "AND eligible_for_value = 0"
                ")",
                name="ck_odds_match_observations_relational",
            ),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.ForeignKeyConstraint(["review_id"], ["odds_bout_match_reviews.id"]),
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
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
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
    bind = op.get_bind()
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS uq_odds_provider_event_alias_active"
    )
    bind.exec_driver_sql(
        "DROP INDEX IF EXISTS uq_odds_bout_match_reviews_pending_provider_ext"
    )
    existing = _existing_tables()
    # Drop dependents before reviews (FK from match observations).
    for table in (
        "odds_match_observations",
        "odds_bout_lifecycle_observations",
        "odds_provider_event_aliases",
        "odds_bout_match_reviews",
    ):
        if table in existing:
            op.drop_table(table)
    install_odds_sqlite_guards(op.get_bind())
