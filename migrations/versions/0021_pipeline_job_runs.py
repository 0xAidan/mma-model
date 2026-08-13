"""Add pipeline orchestrator job-run ledger (DWCS-401).

Revision ID: 0021_pipeline_job_runs
Revises: 0020_recommendation_ledgers
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_pipeline_job_runs"
down_revision: Union[str, Sequence[str], None] = "0020_recommendation_ledgers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_STATUS_SQL = "'started', 'success', 'failed', 'skipped', 'dependency_blocked'"
_ERROR_CLASS_SQL = (
    "'transient', 'authentication', 'entitlement', 'schema', "
    "'identity_unresolved', 'stale_quote', 'missing_odds', "
    "'dependency_blocked', 'overlap', 'internal'"
)


def upgrade() -> None:
    op.create_table(
        "pipeline_job_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("success_token", sa.Integer(), nullable=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("series", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("bout_id", sa.String(length=128), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_slot", sa.String(length=64), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("counts_json", sa.Text(), nullable=True),
        sa.Column("source_quota", sa.String(length=128), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_JOB_STATUS_SQL})",
            name="ck_pipeline_job_runs_status",
        ),
        sa.CheckConstraint(
            "("
            "(status = 'success' AND success_token IS NOT NULL AND success_token = 1) "
            "OR "
            "(status != 'success' AND success_token IS NULL)"
            ")",
            name="ck_pipeline_job_runs_status_success_token",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_pipeline_job_runs_idem_nonempty",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_pipeline_job_runs_finished_ge_started",
        ),
        sa.CheckConstraint(
            "("
            "status != 'success' OR finished_at IS NOT NULL"
            ")",
            name="ck_pipeline_job_runs_success_finished",
        ),
        sa.CheckConstraint(
            "("
            "status != 'failed' OR ("
            "error_class IS NOT NULL AND length(trim(error_class)) > 0"
            ")"
            ")",
            name="ck_pipeline_job_runs_failed_error_class",
        ),
        sa.CheckConstraint(
            "("
            "error_class IS NULL OR error_class IN (" + _ERROR_CLASS_SQL + ")"
            ")",
            name="ck_pipeline_job_runs_error_class",
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name="ck_pipeline_job_runs_attempt_positive",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_pipeline_job_runs_duration_nonneg",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            "success_token",
            name="uq_pipeline_job_runs_idem_success",
        ),
    )
    op.create_index(
        "ix_pipeline_job_runs_idempotency_key",
        "pipeline_job_runs",
        ["idempotency_key"],
    )
    op.create_index("ix_pipeline_job_runs_job_type", "pipeline_job_runs", ["job_type"])
    op.create_index("ix_pipeline_job_runs_status", "pipeline_job_runs", ["status"])
    op.create_index("ix_pipeline_job_runs_series", "pipeline_job_runs", ["series"])
    op.create_index("ix_pipeline_job_runs_event_id", "pipeline_job_runs", ["event_id"])
    op.create_index("ix_pipeline_job_runs_bout_id", "pipeline_job_runs", ["bout_id"])
    op.create_index("ix_pipeline_job_runs_as_of", "pipeline_job_runs", ["as_of"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_job_runs_as_of", table_name="pipeline_job_runs")
    op.drop_index("ix_pipeline_job_runs_bout_id", table_name="pipeline_job_runs")
    op.drop_index("ix_pipeline_job_runs_event_id", table_name="pipeline_job_runs")
    op.drop_index("ix_pipeline_job_runs_series", table_name="pipeline_job_runs")
    op.drop_index("ix_pipeline_job_runs_status", table_name="pipeline_job_runs")
    op.drop_index("ix_pipeline_job_runs_job_type", table_name="pipeline_job_runs")
    op.drop_index(
        "ix_pipeline_job_runs_idempotency_key", table_name="pipeline_job_runs"
    )
    op.drop_table("pipeline_job_runs")
