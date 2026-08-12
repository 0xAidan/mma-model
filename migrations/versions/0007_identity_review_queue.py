"""Identity review queue + immutable evidence tables (DWCS-104).

Revision ID: 0007_identity_review_queue
Revises: 0006_observation_pit_metadata
Create Date: 2026-08-12

Upgrade adds DWCS-104-owned tables only. Downgrade drops those tables/rows and
preserves canonical fighters / fighter_source_ids / other pre-0007 data.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0007_identity_review_queue"
down_revision: Union[str, Sequence[str], None] = "0006_observation_pit_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDENTITY_TABLES = (
    "identity_scoring_blocks",
    "identity_match_evidence",
    "identity_review_queue",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "identity_review_queue" not in existing:
        op.create_table(
            "identity_review_queue",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("normalized_name", sa.String(length=200), nullable=False),
            sa.Column("wikidata_id", sa.String(length=64), nullable=True),
            sa.Column("dob", sa.Date(), nullable=True),
            sa.Column("candidate_canonical_ids_json", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=True),
            sa.Column("bout_status", sa.String(length=32), nullable=True),
            sa.Column("prior_mapping_json", sa.Text(), nullable=True),
            sa.Column("decision_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("decided_by", sa.String(length=128), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rule_id", sa.String(length=128), nullable=False),
            sa.Column("resolver_version", sa.String(length=32), nullable=False),
            sa.Column("reversible", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "external_id",
                "status",
                name="uq_identity_review_source_external_status",
            ),
        )
        op.create_index(
            "ix_identity_review_queue_status", "identity_review_queue", ["status"]
        )
        op.create_index(
            "ix_identity_review_queue_source", "identity_review_queue", ["source"]
        )
        op.create_index(
            "ix_identity_review_queue_normalized_name",
            "identity_review_queue",
            ["normalized_name"],
        )
        op.create_index(
            "ix_identity_review_queue_bout_id", "identity_review_queue", ["bout_id"]
        )

    if "identity_match_evidence" not in existing:
        op.create_table(
            "identity_match_evidence",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolver_version", sa.String(length=32), nullable=False),
            sa.Column("rule_id", sa.String(length=128), nullable=False),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("normalized_name", sa.String(length=200), nullable=False),
            sa.Column("wikidata_id", sa.String(length=64), nullable=True),
            sa.Column("dob", sa.Date(), nullable=True),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column("before_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("after_canonical_id", sa.String(length=36), nullable=True),
            sa.Column("review_id", sa.String(length=36), nullable=True),
            sa.Column("bout_id", sa.String(length=36), nullable=True),
            sa.Column("evidence_json", sa.Text(), nullable=False),
            sa.Column("reversible", sa.Boolean(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["review_id"], ["identity_review_queue.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_identity_match_evidence_rule_id",
            "identity_match_evidence",
            ["rule_id"],
        )
        op.create_index(
            "ix_identity_match_evidence_action",
            "identity_match_evidence",
            ["action"],
        )
        op.create_index(
            "ix_identity_match_evidence_source",
            "identity_match_evidence",
            ["source"],
        )
        op.create_index(
            "ix_identity_match_evidence_review_id",
            "identity_match_evidence",
            ["review_id"],
        )
        op.create_index(
            "ix_identity_match_evidence_bout_id",
            "identity_match_evidence",
            ["bout_id"],
        )
        op.create_index(
            "ix_identity_match_evidence_status",
            "identity_match_evidence",
            ["status"],
        )

    if "identity_scoring_blocks" not in existing:
        op.create_table(
            "identity_scoring_blocks",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("review_id", sa.String(length=36), nullable=True),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("evidence_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["review_id"], ["identity_review_queue.id"]),
            sa.ForeignKeyConstraint(["evidence_id"], ["identity_match_evidence.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "bout_id",
                "review_id",
                name="uq_identity_scoring_block_bout_review",
            ),
        )
        op.create_index(
            "ix_identity_scoring_blocks_bout_id",
            "identity_scoring_blocks",
            ["bout_id"],
        )
        op.create_index(
            "ix_identity_scoring_blocks_review_id",
            "identity_scoring_blocks",
            ["review_id"],
        )
        op.create_index(
            "ix_identity_scoring_blocks_active",
            "identity_scoring_blocks",
            ["active"],
        )


def downgrade() -> None:
    """Drop only DWCS-104 identity tables; preserve canonical fighters/source IDs."""
    existing = _existing_tables()
    # Drop dependents first.
    for table in IDENTITY_TABLES:
        if table in existing:
            op.drop_table(table)
