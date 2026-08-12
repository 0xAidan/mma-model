"""Harden DWCS-105 history FKs, CHECKs, and provenance columns.

Revision ID: 0009_history_constraints
Revises: 0008_regional_history
Create Date: 2026-08-12

Upgrade adds constraints/columns on DWCS-105-owned tables only. Downgrade
removes those additions and preserves 0008 history rows plus all pre-105 data.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0009_history_constraints"
down_revision: Union[str, Sequence[str], None] = "0008_regional_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    if table not in set(inspect(bind).get_table_names()):
        return set()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def upgrade() -> None:
    existing = _tables()
    if "history_source_bouts" in existing:
        cols = _columns("history_source_bouts")
        with op.batch_alter_table("history_source_bouts") as batch:
            if "event_time_precision" not in cols:
                batch.add_column(
                    sa.Column(
                        "event_time_precision",
                        sa.String(length=32),
                        nullable=False,
                        server_default="date_only",
                    )
                )
            if "observation_origin" not in cols:
                batch.add_column(
                    sa.Column(
                        "observation_origin",
                        sa.String(length=32),
                        nullable=False,
                        server_default="unknown",
                    )
                )
            batch.create_check_constraint(
                "ck_history_bout_identity_status",
                "identity_status IN ('linked', 'queued', 'blocked', 'unresolved')",
            )
            batch.create_check_constraint(
                "ck_history_bout_status",
                "bout_status IN ('completed', 'cancelled', 'replacement', "
                "'scheduled', 'unknown')",
            )
            batch.create_check_constraint(
                "ck_history_bout_version_kind",
                "version_kind IN ('event_night', 'current', 'correction')",
            )
            batch.create_check_constraint(
                "ck_history_bout_is_current",
                "is_current_record IN (0, 1)",
            )
            batch.create_check_constraint(
                "ck_history_bout_left_truncated",
                "left_truncated IN (0, 1)",
            )
            batch.create_check_constraint(
                "ck_history_bout_time_precision",
                "event_time_precision IN ('date_only', 'exact', 'unknown')",
            )
            batch.create_check_constraint(
                "ck_history_bout_origin",
                "observation_origin IN ('synthetic_fixture', 'live_public', 'unknown')",
            )

    if "history_reconstructions" in existing:
        with op.batch_alter_table("history_reconstructions") as batch:
            batch.create_foreign_key(
                "fk_history_reconstructions_fighter",
                "canonical_fighters",
                ["fighter_canonical_id"],
                ["id"],
            )

    if "history_conflicts" in existing:
        with op.batch_alter_table("history_conflicts") as batch:
            batch.create_foreign_key(
                "fk_history_conflicts_fighter",
                "canonical_fighters",
                ["fighter_canonical_id"],
                ["id"],
            )

    if "history_explicit_records" in existing:
        cols = _columns("history_explicit_records")
        with op.batch_alter_table("history_explicit_records") as batch:
            if "feature_eligible" not in cols:
                batch.add_column(
                    sa.Column(
                        "feature_eligible",
                        sa.Integer(),
                        nullable=False,
                        server_default="0",
                    )
                )
            batch.create_foreign_key(
                "fk_history_explicit_fighter",
                "canonical_fighters",
                ["fighter_canonical_id"],
                ["id"],
            )
            batch.create_check_constraint(
                "ck_history_explicit_feature",
                "feature_eligible IN (0, 1)",
            )
            batch.create_check_constraint(
                "ck_history_explicit_current",
                "is_current_mutable IN (0, 1)",
            )

    if "history_source_failures" in existing:
        cols = _columns("history_source_failures")
        with op.batch_alter_table("history_source_failures") as batch:
            if "subject" not in cols:
                batch.add_column(
                    sa.Column(
                        "subject",
                        sa.String(length=128),
                        nullable=False,
                        server_default="",
                    )
                )
            if "payload_hash" not in cols:
                batch.add_column(sa.Column("payload_hash", sa.String(length=64), nullable=True))
            if "checkpoint_token" not in cols:
                batch.add_column(
                    sa.Column("checkpoint_token", sa.String(length=128), nullable=True)
                )
            batch.drop_constraint("uq_history_source_failure_scope", type_="unique")
            batch.create_unique_constraint(
                "uq_history_source_failure_subject",
                ["source", "reason", "scope", "subject"],
            )


def downgrade() -> None:
    existing = _tables()
    if "history_source_failures" in existing:
        cols = _columns("history_source_failures")
        with op.batch_alter_table("history_source_failures") as batch:
            batch.drop_constraint("uq_history_source_failure_subject", type_="unique")
            batch.create_unique_constraint(
                "uq_history_source_failure_scope",
                ["source", "reason", "scope"],
            )
            if "checkpoint_token" in cols:
                batch.drop_column("checkpoint_token")
            if "payload_hash" in cols:
                batch.drop_column("payload_hash")
            if "subject" in cols:
                batch.drop_column("subject")

    if "history_explicit_records" in existing:
        cols = _columns("history_explicit_records")
        with op.batch_alter_table("history_explicit_records") as batch:
            batch.drop_constraint("fk_history_explicit_fighter", type_="foreignkey")
            batch.drop_constraint("ck_history_explicit_feature", type_="check")
            batch.drop_constraint("ck_history_explicit_current", type_="check")
            if "feature_eligible" in cols:
                batch.drop_column("feature_eligible")

    if "history_conflicts" in existing:
        with op.batch_alter_table("history_conflicts") as batch:
            batch.drop_constraint("fk_history_conflicts_fighter", type_="foreignkey")

    if "history_reconstructions" in existing:
        with op.batch_alter_table("history_reconstructions") as batch:
            batch.drop_constraint("fk_history_reconstructions_fighter", type_="foreignkey")

    if "history_source_bouts" in existing:
        cols = _columns("history_source_bouts")
        with op.batch_alter_table("history_source_bouts") as batch:
            batch.drop_constraint("ck_history_bout_identity_status", type_="check")
            batch.drop_constraint("ck_history_bout_status", type_="check")
            batch.drop_constraint("ck_history_bout_version_kind", type_="check")
            batch.drop_constraint("ck_history_bout_is_current", type_="check")
            batch.drop_constraint("ck_history_bout_left_truncated", type_="check")
            batch.drop_constraint("ck_history_bout_time_precision", type_="check")
            batch.drop_constraint("ck_history_bout_origin", type_="check")
            if "observation_origin" in cols:
                batch.drop_column("observation_origin")
            if "event_time_precision" in cols:
                batch.drop_column("event_time_precision")
