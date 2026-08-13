"""Validate odds job quote/availability ID JSON arrays (DWCS-205).

Revision ID: 0019_odds_job_id_array_triggers
Revises: 0018_odds_job_ledger_integrity
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from mma_model.db.odds_guards import install_odds_sqlite_guards

revision: str = "0019_odds_job_id_array_triggers"
down_revision: Union[str, Sequence[str], None] = "0018_odds_job_ledger_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ID_ARRAY_TRIGGERS = (
    "odds_snapshot_job_runs_quote_ids_insert",
    "odds_snapshot_job_runs_quote_ids_update",
    "odds_snapshot_job_runs_availability_ids_insert",
    "odds_snapshot_job_runs_availability_ids_update",
)


def upgrade() -> None:
    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    for name in _ID_ARRAY_TRIGGERS:
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
