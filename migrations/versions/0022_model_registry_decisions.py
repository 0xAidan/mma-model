"""Add model registry decision audit ledger (DWCS-402).

Revision ID: 0022_model_registry_decisions
Revises: 0021_pipeline_job_runs
Create Date: 2026-08-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_model_registry_decisions"
down_revision: Union[str, Sequence[str], None] = "0021_pipeline_job_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIONS_SQL = "'retrain', 'promote', 'rollback', 'reject'"
_LANES_SQL = "'champion', 'shadow', 'none'"


def upgrade() -> None:
    op.create_table(
        "model_registry_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("artifact_digest", sa.String(length=64), nullable=True),
        sa.Column("config_hash", sa.String(length=64), nullable=True),
        sa.Column("prior_champion_digest", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evaluator_hash", sa.String(length=64), nullable=True),
        sa.Column("health_ok", sa.Boolean(), nullable=True),
        sa.Column("gates_json", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            f"action IN ({_ACTIONS_SQL})",
            name="ck_model_registry_decisions_action",
        ),
        sa.CheckConstraint(
            f"lane IN ({_LANES_SQL})",
            name="ck_model_registry_decisions_lane",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_model_registry_decisions_reason_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) > 0",
            name="ck_model_registry_decisions_actor_nonempty",
        ),
        sa.CheckConstraint(
            "(artifact_digest IS NULL OR length(artifact_digest) = 64)",
            name="ck_model_registry_decisions_artifact_digest",
        ),
        sa.CheckConstraint(
            "(config_hash IS NULL OR length(config_hash) = 64)",
            name="ck_model_registry_decisions_config_hash",
        ),
        sa.CheckConstraint(
            "(prior_champion_digest IS NULL OR length(prior_champion_digest) = 64)",
            name="ck_model_registry_decisions_prior_digest",
        ),
        sa.CheckConstraint(
            "(evaluator_hash IS NULL OR length(evaluator_hash) = 64)",
            name="ck_model_registry_decisions_evaluator_hash",
        ),
    )
    op.create_index(
        "ix_model_registry_decisions_at",
        "model_registry_decisions",
        ["at"],
    )
    op.create_index(
        "ix_model_registry_decisions_action",
        "model_registry_decisions",
        ["action"],
    )
    op.create_index(
        "ix_model_registry_decisions_artifact_digest",
        "model_registry_decisions",
        ["artifact_digest"],
    )
    op.create_index(
        "ix_model_registry_decisions_seq",
        "model_registry_decisions",
        ["seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_registry_decisions_seq", table_name="model_registry_decisions")
    op.drop_index(
        "ix_model_registry_decisions_artifact_digest",
        table_name="model_registry_decisions",
    )
    op.drop_index("ix_model_registry_decisions_action", table_name="model_registry_decisions")
    op.drop_index("ix_model_registry_decisions_at", table_name="model_registry_decisions")
    op.drop_table("model_registry_decisions")
