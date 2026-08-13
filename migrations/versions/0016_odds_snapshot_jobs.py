"""Add odds snapshot job idempotency ledger (DWCS-205).

Revision ID: 0016_odds_snapshot_jobs
Revises: 0015_quote_eligibility_scope
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_odds_snapshot_jobs"
down_revision: Union[str, Sequence[str], None] = "0015_quote_eligibility_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOB_STATUS_SQL = (
    "'started', 'success', 'failed', 'deferred_quota', 'exhausted', "
    "'no_op', 'duplicate'"
)


def upgrade() -> None:
    op.create_table(
        "odds_snapshot_job_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("success_token", sa.Integer(), nullable=True),
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("markets", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_name", sa.String(length=32), nullable=True),
        sa.Column("estimated_cost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_cost", sa.Integer(), nullable=True),
        sa.Column("actual_cost_source", sa.String(length=64), nullable=True),
        sa.Column("remaining_source", sa.String(length=128), nullable=True),
        sa.Column("snapshot_quote_ids", sa.Text(), nullable=True),
        sa.Column("error_class", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_JOB_STATUS_SQL})",
            name="ck_odds_snapshot_job_runs_status",
        ),
        sa.CheckConstraint(
            "("
            "(status = 'success' AND success_token IS NOT NULL AND success_token = 1) "
            "OR "
            "(status != 'success' AND success_token IS NULL)"
            ")",
            name="ck_odds_snapshot_job_runs_status_success_token",
        ),
        sa.CheckConstraint(
            "estimated_cost >= 0 AND (actual_cost IS NULL OR actual_cost >= 0)",
            name="ck_odds_snapshot_job_runs_costs",
        ),
        sa.CheckConstraint(
            "("
            "actual_cost_source IS NULL AND actual_cost IS NULL"
            ") OR ("
            "actual_cost_source IN ('provider', 'inferred_empty_zero') "
            "AND actual_cost IS NOT NULL"
            ") OR ("
            "actual_cost_source = 'missing' AND actual_cost IS NULL"
            ")",
            name="ck_odds_snapshot_job_runs_actual_cost_provenance",
        ),
        sa.CheckConstraint(
            "("
            "snapshot_at IS NULL OR requested_cutoff IS NULL OR "
            "snapshot_at <= requested_cutoff"
            ")",
            name="ck_odds_snapshot_job_runs_snapshot_le_cutoff",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_odds_snapshot_job_runs_finished_ge_started",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_odds_snapshot_job_runs_idem_nonempty",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            "success_token",
            name="uq_odds_snapshot_job_runs_idem_success",
        ),
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_idempotency_key",
        "odds_snapshot_job_runs",
        ["idempotency_key"],
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_job_name",
        "odds_snapshot_job_runs",
        ["job_name"],
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_status",
        "odds_snapshot_job_runs",
        ["status"],
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_provider",
        "odds_snapshot_job_runs",
        ["provider"],
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_event_id",
        "odds_snapshot_job_runs",
        ["event_id"],
    )
    op.create_index(
        "ix_odds_snapshot_job_runs_as_of",
        "odds_snapshot_job_runs",
        ["as_of"],
    )


def downgrade() -> None:
    op.drop_index("ix_odds_snapshot_job_runs_as_of", table_name="odds_snapshot_job_runs")
    op.drop_index(
        "ix_odds_snapshot_job_runs_event_id", table_name="odds_snapshot_job_runs"
    )
    op.drop_index(
        "ix_odds_snapshot_job_runs_provider", table_name="odds_snapshot_job_runs"
    )
    op.drop_index(
        "ix_odds_snapshot_job_runs_status", table_name="odds_snapshot_job_runs"
    )
    op.drop_index(
        "ix_odds_snapshot_job_runs_job_name", table_name="odds_snapshot_job_runs"
    )
    op.drop_index(
        "ix_odds_snapshot_job_runs_idempotency_key",
        table_name="odds_snapshot_job_runs",
    )
    op.drop_table("odds_snapshot_job_runs")
