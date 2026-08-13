"""Strengthen odds job ledger integrity + availability IDs (DWCS-205).

Revision ID: 0018_odds_job_ledger_integrity
Revises: 0017_odds_snapshot_jobs
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_odds_job_ledger_integrity"
down_revision: Union[str, Sequence[str], None] = "0017_odds_snapshot_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("odds_snapshot_job_runs") as batch:
        batch.add_column(
            sa.Column("snapshot_availability_ids", sa.Text(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_cutoff_le_as_of",
            "requested_cutoff IS NULL OR requested_cutoff <= as_of",
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_success_finished",
            "status != 'success' OR finished_at IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_failed_error_class",
            "status != 'failed' OR ("
            "error_class IS NOT NULL AND length(trim(error_class)) > 0)",
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_unexecuted_no_actual_cost",
            "status NOT IN ('deferred_quota', 'exhausted') OR actual_cost IS NULL",
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_quote_ids_json",
            "snapshot_quote_ids IS NULL OR ("
            "json_valid(snapshot_quote_ids) AND "
            "json_type(snapshot_quote_ids) = 'array')",
        )
        batch.create_check_constraint(
            "ck_odds_snapshot_job_runs_availability_ids_json",
            "snapshot_availability_ids IS NULL OR ("
            "json_valid(snapshot_availability_ids) AND "
            "json_type(snapshot_availability_ids) = 'array')",
        )


def downgrade() -> None:
    with op.batch_alter_table("odds_snapshot_job_runs") as batch:
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_availability_ids_json", type_="check"
        )
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_quote_ids_json", type_="check"
        )
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_unexecuted_no_actual_cost", type_="check"
        )
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_failed_error_class", type_="check"
        )
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_success_finished", type_="check"
        )
        batch.drop_constraint(
            "ck_odds_snapshot_job_runs_cutoff_le_as_of", type_="check"
        )
        batch.drop_column("snapshot_availability_ids")
