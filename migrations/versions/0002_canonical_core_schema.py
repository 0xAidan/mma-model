"""Canonical UUID core schema for fighters/events/bouts.

Revision ID: 0002_canonical_core
Revises: 0001_baseline
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002_canonical_core"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CANONICAL_TABLES = (
    "canonical_fighters",
    "fighter_source_ids",
    "fighter_aliases",
    "canonical_events",
    "event_source_ids",
    "canonical_bouts",
    "bout_source_ids",
    "bout_participants",
    "bout_result_versions",
    "fighter_profile_observations",
    "fighter_stat_observations",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "canonical_fighters" not in existing:
        op.create_table(
            "canonical_fighters",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "fighter_source_ids" not in existing:
        op.create_table(
            "fighter_source_ids",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fighter_id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "external_id", name="uq_fighter_source_external"),
        )
        op.create_index("ix_fighter_source_ids_fighter_id", "fighter_source_ids", ["fighter_id"])
        op.create_index("ix_fighter_source_ids_source", "fighter_source_ids", ["source"])

    if "fighter_aliases" not in existing:
        op.create_table(
            "fighter_aliases",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fighter_id", sa.String(length=36), nullable=False),
            sa.Column("alias", sa.String(length=200), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("fighter_id", "alias", name="uq_fighter_alias"),
        )
        op.create_index("ix_fighter_aliases_fighter_id", "fighter_aliases", ["fighter_id"])

    if "canonical_events" not in existing:
        op.create_table(
            "canonical_events",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=400), nullable=False),
            sa.Column("series", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("event_date", sa.Date(), nullable=True),
            sa.Column("location", sa.String(length=400), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_canonical_events_series", "canonical_events", ["series"])
        op.create_index("ix_canonical_events_status", "canonical_events", ["status"])

    if "event_source_ids" not in existing:
        op.create_table(
            "event_source_ids",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["canonical_events.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "external_id", name="uq_event_source_external"),
        )
        op.create_index("ix_event_source_ids_event_id", "event_source_ids", ["event_id"])
        op.create_index("ix_event_source_ids_source", "event_source_ids", ["source"])

    if "canonical_bouts" not in existing:
        op.create_table(
            "canonical_bouts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("event_id", sa.String(length=36), nullable=False),
            sa.Column("fighter_a_id", sa.String(length=36), nullable=False),
            sa.Column("fighter_b_id", sa.String(length=36), nullable=False),
            sa.Column("scheduled_rounds", sa.Integer(), nullable=False),
            sa.Column("weight_class", sa.String(length=120), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "fighter_a_id != fighter_b_id", name="ck_bout_distinct_fighters"
            ),
            sa.ForeignKeyConstraint(["event_id"], ["canonical_events.id"]),
            sa.ForeignKeyConstraint(["fighter_a_id"], ["canonical_fighters.id"]),
            sa.ForeignKeyConstraint(["fighter_b_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_canonical_bouts_event_id", "canonical_bouts", ["event_id"])
        op.create_index("ix_canonical_bouts_fighter_a_id", "canonical_bouts", ["fighter_a_id"])
        op.create_index("ix_canonical_bouts_fighter_b_id", "canonical_bouts", ["fighter_b_id"])
        op.create_index("ix_canonical_bouts_status", "canonical_bouts", ["status"])

    if "bout_source_ids" not in existing:
        op.create_table(
            "bout_source_ids",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "external_id", name="uq_bout_source_external"),
        )
        op.create_index("ix_bout_source_ids_bout_id", "bout_source_ids", ["bout_id"])
        op.create_index("ix_bout_source_ids_source", "bout_source_ids", ["source"])

    if "bout_participants" not in existing:
        op.create_table(
            "bout_participants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("fighter_id", sa.String(length=36), nullable=False),
            sa.Column("corner", sa.String(length=8), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.ForeignKeyConstraint(["fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("bout_id", "fighter_id", name="uq_bout_participant_fighter"),
            sa.UniqueConstraint("bout_id", "corner", name="uq_bout_participant_corner"),
        )
        op.create_index("ix_bout_participants_bout_id", "bout_participants", ["bout_id"])
        op.create_index("ix_bout_participants_fighter_id", "bout_participants", ["fighter_id"])

    if "bout_result_versions" not in existing:
        op.create_table(
            "bout_result_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=False),
            sa.Column("version_kind", sa.String(length=32), nullable=False),
            sa.Column("fighter_a_id", sa.String(length=36), nullable=False),
            sa.Column("fighter_b_id", sa.String(length=36), nullable=False),
            sa.Column("winner_fighter_id", sa.String(length=36), nullable=True),
            sa.Column("result_type", sa.String(length=32), nullable=True),
            sa.Column("method", sa.String(length=80), nullable=True),
            sa.Column("ending_round", sa.Integer(), nullable=True),
            sa.Column("time_str", sa.String(length=32), nullable=True),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "winner_fighter_id IS NULL OR winner_fighter_id = fighter_a_id "
                "OR winner_fighter_id = fighter_b_id",
                name="ck_result_winner_is_participant",
            ),
            sa.CheckConstraint(
                "fighter_a_id != fighter_b_id", name="ck_result_distinct_fighters"
            ),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.ForeignKeyConstraint(["fighter_a_id"], ["canonical_fighters.id"]),
            sa.ForeignKeyConstraint(["fighter_b_id"], ["canonical_fighters.id"]),
            sa.ForeignKeyConstraint(["winner_fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("bout_id", "version_kind", name="uq_bout_result_version_kind"),
        )
        op.create_index("ix_bout_result_versions_bout_id", "bout_result_versions", ["bout_id"])

    if "fighter_profile_observations" not in existing:
        op.create_table(
            "fighter_profile_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fighter_id", sa.String(length=36), nullable=False),
            sa.Column("attribute", sa.String(length=64), nullable=False),
            sa.Column("value_text", sa.Text(), nullable=True),
            sa.Column("value_num", sa.Float(), nullable=True),
            sa.Column("value_date", sa.Date(), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_fighter_profile_observations_fighter_id",
            "fighter_profile_observations",
            ["fighter_id"],
        )
        op.create_index(
            "ix_fighter_profile_observations_attribute",
            "fighter_profile_observations",
            ["attribute"],
        )

    if "fighter_stat_observations" not in existing:
        op.create_table(
            "fighter_stat_observations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fighter_id", sa.String(length=36), nullable=False),
            sa.Column("bout_id", sa.String(length=36), nullable=True),
            sa.Column("stat_key", sa.String(length=64), nullable=False),
            sa.Column("value_num", sa.Float(), nullable=True),
            sa.Column("value_text", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["bout_id"], ["canonical_bouts.id"]),
            sa.ForeignKeyConstraint(["fighter_id"], ["canonical_fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_fighter_stat_observations_fighter_id",
            "fighter_stat_observations",
            ["fighter_id"],
        )
        op.create_index(
            "ix_fighter_stat_observations_bout_id",
            "fighter_stat_observations",
            ["bout_id"],
        )
        op.create_index(
            "ix_fighter_stat_observations_stat_key",
            "fighter_stat_observations",
            ["stat_key"],
        )


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(CANONICAL_TABLES):
        if table in existing:
            op.drop_table(table)
