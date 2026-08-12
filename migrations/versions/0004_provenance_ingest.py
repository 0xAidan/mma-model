"""Add provenance ingest tables (runs, raw observations, checkpoints).

Revision ID: 0004_provenance_ingest
Revises: 0003_legacy_import
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0004_provenance_ingest"
down_revision: Union[str, Sequence[str], None] = "0003_legacy_import"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROVENANCE_TABLES = (
    "ingest_runs",
    "raw_observations",
    "source_checkpoints",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "ingest_runs" not in existing:
        op.create_table(
            "ingest_runs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("stream", sa.String(length=64), nullable=False),
            sa.Column("scope", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_class", sa.String(length=128), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("observation_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_ingest_runs_source", "ingest_runs", ["source"])
        op.create_index("ix_ingest_runs_stream", "ingest_runs", ["stream"])
        op.create_index("ix_ingest_runs_scope", "ingest_runs", ["scope"])
        op.create_index("ix_ingest_runs_status", "ingest_runs", ["status"])

    if "raw_observations" not in existing:
        op.create_table(
            "raw_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("ingest_run_id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("stream", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("entity_kind", sa.String(length=64), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("raw_ref", sa.String(length=64), nullable=False),
            sa.Column("detail_level", sa.String(length=32), nullable=False),
            sa.Column("version_kind", sa.String(length=32), nullable=True),
            sa.Column("schema_version", sa.String(length=32), nullable=False),
            sa.Column("subject_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["ingest_run_id"], ["ingest_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "stream",
                "external_id",
                "payload_hash",
                name="uq_raw_obs_provenance",
            ),
        )
        op.create_index("ix_raw_observations_ingest_run_id", "raw_observations", ["ingest_run_id"])
        op.create_index("ix_raw_observations_source", "raw_observations", ["source"])
        op.create_index("ix_raw_observations_stream", "raw_observations", ["stream"])
        op.create_index("ix_raw_observations_external_id", "raw_observations", ["external_id"])
        op.create_index("ix_raw_observations_payload_hash", "raw_observations", ["payload_hash"])
        op.create_index("ix_raw_observations_subject_id", "raw_observations", ["subject_id"])

    if "source_checkpoints" not in existing:
        op.create_table(
            "source_checkpoints",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("stream", sa.String(length=64), nullable=False),
            sa.Column("scope", sa.String(length=128), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=False),
            sa.Column("cursor_token", sa.String(length=512), nullable=False),
            sa.Column("last_ingest_run_id", sa.String(length=36), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["last_ingest_run_id"], ["ingest_runs.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source",
                "stream",
                "scope",
                "version",
                name="uq_source_checkpoint_key",
            ),
        )
        op.create_index("ix_source_checkpoints_source", "source_checkpoints", ["source"])
        op.create_index("ix_source_checkpoints_stream", "source_checkpoints", ["stream"])
        op.create_index("ix_source_checkpoints_scope", "source_checkpoints", ["scope"])


def downgrade() -> None:
    """Remove only DWCS-101 provenance structures; preserve prior schema/data."""
    existing = _existing_tables()
    for table in reversed(PROVENANCE_TABLES):
        if table in existing:
            op.drop_table(table)
