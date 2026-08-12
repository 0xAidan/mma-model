"""Regional/pre-UFC history structures (DWCS-105).

Revision ID: 0008_regional_history
Revises: 0007_identity_review_queue
Create Date: 2026-08-12

Upgrade adds DWCS-105-owned tables only. Downgrade drops those tables/rows and
preserves canonical / identity / provenance data from prior revisions.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0008_regional_history"
down_revision: Union[str, Sequence[str], None] = "0007_identity_review_queue"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

HISTORY_TABLES = (
    "history_explicit_records",
    "history_reconstructions",
    "history_frontier",
    "history_source_failures",
    "history_conflicts",
    "history_source_bouts",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "history_source_bouts" not in existing:
        op.create_table(
            "history_source_bouts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("stream", sa.String(length=64), nullable=False),
            sa.Column("external_bout_id", sa.String(length=128), nullable=False),
            sa.Column("fighter_source", sa.String(length=64), nullable=False),
            sa.Column("fighter_external_id", sa.String(length=128), nullable=False),
            sa.Column("fighter_name", sa.String(length=200), nullable=False),
            sa.Column("fighter_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("opponent_source", sa.String(length=64), nullable=True),
            sa.Column("opponent_external_id", sa.String(length=128), nullable=True),
            sa.Column("opponent_name", sa.String(length=200), nullable=False),
            sa.Column("opponent_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("event_name", sa.String(length=400), nullable=True),
            sa.Column("event_date", sa.Date(), nullable=True),
            sa.Column("event_external_id", sa.String(length=128), nullable=True),
            sa.Column("classification", sa.String(length=32), nullable=False),
            sa.Column("regulated_us", sa.String(length=16), nullable=False),
            sa.Column("result", sa.String(length=32), nullable=False),
            sa.Column("method", sa.String(length=80), nullable=True),
            sa.Column("ending_round", sa.Integer(), nullable=True),
            sa.Column("time_str", sa.String(length=32), nullable=True),
            sa.Column("elapsed_seconds", sa.Integer(), nullable=True),
            sa.Column("scheduled_rounds", sa.Integer(), nullable=True),
            sa.Column("promotion", sa.String(length=200), nullable=True),
            sa.Column("missing_reason", sa.String(length=128), nullable=True),
            sa.Column("left_truncated", sa.Integer(), nullable=False),
            sa.Column("parser_version", sa.String(length=64), nullable=True),
            sa.Column("source_class", sa.String(length=64), nullable=True),
            sa.Column("source_url", sa.String(length=500), nullable=True),
            sa.Column("version_kind", sa.String(length=32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("bout_status", sa.String(length=32), nullable=False),
            sa.Column("quality_tier", sa.String(length=32), nullable=False),
            sa.Column("timestamp_quality", sa.String(length=64), nullable=False),
            sa.Column("timestamp_quality_source", sa.String(length=128), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proxy_published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("raw_ref", sa.String(length=64), nullable=True),
            sa.Column("identity_status", sa.String(length=32), nullable=False),
            sa.Column("is_current_record", sa.Integer(), nullable=False),
            sa.Column("wikidata_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["fighter_canonical_id"], ["canonical_fighters.id"]),
            sa.ForeignKeyConstraint(["opponent_canonical_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "external_bout_id",
                "version_kind",
                "revision",
                name="uq_history_source_bout_revision",
            ),
            sa.CheckConstraint(
                "classification IN ('professional', 'amateur', 'unknown')",
                name="ck_history_bout_classification",
            ),
            sa.CheckConstraint(
                "regulated_us IN ('true', 'false', 'unknown')",
                name="ck_history_bout_regulated_us",
            ),
            sa.CheckConstraint(
                "result IN ('win', 'loss', 'draw', 'nc', 'unknown', 'cancelled')",
                name="ck_history_bout_result",
            ),
            sa.CheckConstraint("revision >= 1", name="ck_history_bout_revision_positive"),
        )
        op.create_index(
            "ix_history_source_bouts_source", "history_source_bouts", ["source"]
        )
        op.create_index(
            "ix_history_source_bouts_external_bout_id",
            "history_source_bouts",
            ["external_bout_id"],
        )
        op.create_index(
            "ix_history_source_bouts_fighter_external_id",
            "history_source_bouts",
            ["fighter_external_id"],
        )
        op.create_index(
            "ix_history_source_bouts_fighter_canonical_id",
            "history_source_bouts",
            ["fighter_canonical_id"],
        )
        op.create_index(
            "ix_history_source_bouts_payload_hash",
            "history_source_bouts",
            ["payload_hash"],
        )

    if "history_conflicts" not in existing:
        op.create_table(
            "history_conflicts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("conflict_key", sa.String(length=256), nullable=False),
            sa.Column("conflict_type", sa.String(length=64), nullable=False),
            sa.Column("fighter_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("left_source", sa.String(length=64), nullable=False),
            sa.Column("left_external_id", sa.String(length=128), nullable=False),
            sa.Column("right_source", sa.String(length=64), nullable=False),
            sa.Column("right_external_id", sa.String(length=128), nullable=False),
            sa.Column("detail_json", sa.Text(), nullable=False),
            sa.Column("quality_tier", sa.String(length=32), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("conflict_key", name="uq_history_conflict_key"),
        )
        op.create_index(
            "ix_history_conflicts_conflict_type", "history_conflicts", ["conflict_type"]
        )
        op.create_index(
            "ix_history_conflicts_fighter_canonical_id",
            "history_conflicts",
            ["fighter_canonical_id"],
        )

    if "history_source_failures" not in existing:
        op.create_table(
            "history_source_failures",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column("scope", sa.String(length=128), nullable=False),
            sa.Column("host", sa.String(length=128), nullable=True),
            sa.Column("path_category", sa.String(length=256), nullable=True),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("evidence_json", sa.Text(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "reason",
                "scope",
                name="uq_history_source_failure_scope",
            ),
        )
        op.create_index(
            "ix_history_source_failures_source", "history_source_failures", ["source"]
        )
        op.create_index(
            "ix_history_source_failures_reason", "history_source_failures", ["reason"]
        )

    if "history_frontier" not in existing:
        op.create_table(
            "history_frontier",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("entity_kind", sa.String(length=32), nullable=False),
            sa.Column("entity_id", sa.String(length=128), nullable=False),
            sa.Column("depth", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("cursor_json", sa.Text(), nullable=False),
            sa.Column("page_count", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "entity_kind",
                "entity_id",
                name="uq_history_frontier_entity",
            ),
        )
        op.create_index("ix_history_frontier_source", "history_frontier", ["source"])
        op.create_index(
            "ix_history_frontier_entity_id", "history_frontier", ["entity_id"]
        )
        op.create_index("ix_history_frontier_status", "history_frontier", ["status"])

    if "history_reconstructions" not in existing:
        op.create_table(
            "history_reconstructions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("fighter_canonical_id", sa.String(length=36), nullable=False),
            sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reconstruction_version", sa.String(length=32), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "fighter_canonical_id",
                "cutoff",
                "reconstruction_version",
                name="uq_history_reconstruction_cutoff",
            ),
        )
        op.create_index(
            "ix_history_reconstructions_fighter_canonical_id",
            "history_reconstructions",
            ["fighter_canonical_id"],
        )
        op.create_index(
            "ix_history_reconstructions_cutoff", "history_reconstructions", ["cutoff"]
        )

    if "history_explicit_records" not in existing:
        op.create_table(
            "history_explicit_records",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("fighter_external_id", sa.String(length=128), nullable=False),
            sa.Column("fighter_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("wins", sa.Integer(), nullable=True),
            sa.Column("losses", sa.Integer(), nullable=True),
            sa.Column("draws", sa.Integer(), nullable=True),
            sa.Column("no_contests", sa.Integer(), nullable=True),
            sa.Column("classification", sa.String(length=32), nullable=False),
            sa.Column("is_current_mutable", sa.Integer(), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "fighter_external_id",
                "as_of",
                name="uq_history_explicit_record",
            ),
        )
        op.create_index(
            "ix_history_explicit_records_source", "history_explicit_records", ["source"]
        )
        op.create_index(
            "ix_history_explicit_records_fighter_external_id",
            "history_explicit_records",
            ["fighter_external_id"],
        )
        op.create_index(
            "ix_history_explicit_records_fighter_canonical_id",
            "history_explicit_records",
            ["fighter_canonical_id"],
        )


def downgrade() -> None:
    existing = _existing_tables()
    for table in HISTORY_TABLES:
        if table in existing:
            op.drop_table(table)
