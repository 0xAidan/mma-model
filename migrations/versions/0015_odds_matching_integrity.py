"""Harden DWCS-203 matching integrity + odds-bout review queue.

Revision ID: 0015_odds_matching_integrity
Revises: 0014_odds_matching
Create Date: 2026-08-12

Adds dedicated odds-bout match reviews, FK/CHECK constraints, partial unique
active alias index, and append-only guards for match/lifecycle tables.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0015_odds_matching_integrity"
down_revision: Union[str, Sequence[str], None] = "0014_odds_matching"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _existing_indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {row["name"] for row in inspect(bind).get_indexes(table)}


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

    # Rebuild match observations with FKs / relational CHECKs.
    if "odds_match_observations" in existing:
        op.execute(text("ALTER TABLE odds_match_observations RENAME TO odds_match_observations_old"))
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
        op.execute(
            text(
                "INSERT INTO odds_match_observations ("
                "id, dedupe_key, provider, external_event_id, bout_id, match_status, "
                "match_rule, reason, review_id, eligible_for_value, observed_at, created_at"
                ") SELECT "
                "id, dedupe_key, provider, external_event_id, bout_id, match_status, "
                "match_rule, reason, NULL, "
                "CASE WHEN eligible_for_value IN (0,1) THEN eligible_for_value ELSE 0 END, "
                "observed_at, created_at "
                "FROM odds_match_observations_old"
            )
        )
        op.drop_table("odds_match_observations_old")
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

    # Rebuild lifecycle observations with bout FK.
    if "odds_bout_lifecycle_observations" in existing:
        op.execute(
            text(
                "ALTER TABLE odds_bout_lifecycle_observations "
                "RENAME TO odds_bout_lifecycle_observations_old"
            )
        )
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
        op.execute(
            text(
                "INSERT INTO odds_bout_lifecycle_observations ("
                "id, dedupe_key, bout_id, provider, external_event_id, lifecycle, "
                "evidence_kind, detail, price_decimal, observed_at, created_at"
                ") SELECT "
                "id, dedupe_key, bout_id, provider, external_event_id, lifecycle, "
                "evidence_kind, detail, price_decimal, observed_at, created_at "
                "FROM odds_bout_lifecycle_observations_old"
            )
        )
        op.drop_table("odds_bout_lifecycle_observations_old")
        for name, col in (
            ("ix_odds_bout_lifecycle_observations_dedupe_key", "dedupe_key"),
            ("ix_odds_bout_lifecycle_observations_bout_id", "bout_id"),
            ("ix_odds_bout_lifecycle_observations_provider", "provider"),
            ("ix_odds_bout_lifecycle_observations_external_event_id", "external_event_id"),
            ("ix_odds_bout_lifecycle_observations_lifecycle", "lifecycle"),
            ("ix_odds_bout_lifecycle_observations_observed_at", "observed_at"),
        ):
            op.create_index(name, "odds_bout_lifecycle_observations", [col])

    # Alias superseded_at consistency + partial unique active index.
    if "odds_provider_event_aliases" in existing:
        op.execute(
            text(
                "ALTER TABLE odds_provider_event_aliases "
                "RENAME TO odds_provider_event_aliases_old"
            )
        )
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
        op.execute(
            text(
                "INSERT INTO odds_provider_event_aliases ("
                "id, provider, external_event_id, bout_id, alias_version, status, "
                "match_rule, evidence_json, created_at, superseded_at"
                ") SELECT "
                "id, provider, external_event_id, bout_id, alias_version, status, "
                "match_rule, evidence_json, created_at, "
                "CASE "
                "WHEN status = 'superseded' AND superseded_at IS NULL "
                "THEN created_at ELSE superseded_at END "
                "FROM odds_provider_event_aliases_old"
            )
        )
        op.drop_table("odds_provider_event_aliases_old")
        for name, col in (
            ("ix_odds_provider_event_aliases_provider", "provider"),
            ("ix_odds_provider_event_aliases_external_event_id", "external_event_id"),
            ("ix_odds_provider_event_aliases_bout_id", "bout_id"),
            ("ix_odds_provider_event_aliases_status", "status"),
        ):
            op.create_index(name, "odds_provider_event_aliases", [col])

    indexes = _existing_indexes("odds_provider_event_aliases")
    if "uq_odds_provider_event_alias_active" not in indexes:
        op.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_odds_provider_event_alias_active "
                "ON odds_provider_event_aliases (provider, external_event_id) "
                "WHERE status = 'active'"
            )
        )

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
    if "odds_bout_match_reviews" in existing:
        # Clear FK references first.
        if "odds_match_observations" in existing:
            op.execute(text("UPDATE odds_match_observations SET review_id = NULL"))
        op.drop_table("odds_bout_match_reviews")
    install_odds_sqlite_guards(op.get_bind())
