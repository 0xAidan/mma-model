"""Baseline: existing seven-table UFC Stats schema.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TABLES = (
    "fighters",
    "events",
    "fights",
    "fight_fighter_stats",
    "ingest_cursors",
    "odds_snapshots",
    "fighter_composites",
)


def _existing_tables() -> set[str]:
    bind = op.get_bind()
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()

    if "fighters" not in existing:
        op.create_table(
            "fighters",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("nickname", sa.String(length=200), nullable=True),
            sa.Column("height_in", sa.Float(), nullable=True),
            sa.Column("reach_in", sa.Float(), nullable=True),
            sa.Column("stance", sa.String(length=32), nullable=True),
            sa.Column("dob", sa.Date(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "events" not in existing:
        op.create_table(
            "events",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=400), nullable=False),
            sa.Column("event_date", sa.Date(), nullable=True),
            sa.Column("location", sa.String(length=400), nullable=True),
            sa.Column("raw_url", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "fights" not in existing:
        op.create_table(
            "fights",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("event_id", sa.String(length=32), nullable=False),
            sa.Column("fighter_a_id", sa.String(length=32), nullable=False),
            sa.Column("fighter_b_id", sa.String(length=32), nullable=False),
            sa.Column("winner_id", sa.String(length=32), nullable=True),
            sa.Column("weight_class", sa.String(length=120), nullable=True),
            sa.Column("method", sa.String(length=80), nullable=True),
            sa.Column("fight_round", sa.Integer(), nullable=True),
            sa.Column("time_str", sa.String(length=32), nullable=True),
            sa.Column("detail_ingested", sa.Boolean(), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
            sa.ForeignKeyConstraint(["fighter_a_id"], ["fighters.id"]),
            sa.ForeignKeyConstraint(["fighter_b_id"], ["fighters.id"]),
            sa.ForeignKeyConstraint(["winner_id"], ["fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fights_event_id", "fights", ["event_id"])
        op.create_index("ix_fights_fighter_a_id", "fights", ["fighter_a_id"])
        op.create_index("ix_fights_fighter_b_id", "fights", ["fighter_b_id"])

    if "fight_fighter_stats" not in existing:
        op.create_table(
            "fight_fighter_stats",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fight_id", sa.String(length=32), nullable=False),
            sa.Column("fighter_id", sa.String(length=32), nullable=False),
            sa.Column("kd", sa.Integer(), nullable=False),
            sa.Column("sig_str_landed", sa.Integer(), nullable=False),
            sa.Column("sig_str_attempted", sa.Integer(), nullable=False),
            sa.Column("sig_str_pct", sa.Float(), nullable=True),
            sa.Column("total_str_landed", sa.Integer(), nullable=False),
            sa.Column("total_str_attempted", sa.Integer(), nullable=False),
            sa.Column("td_landed", sa.Integer(), nullable=False),
            sa.Column("td_attempted", sa.Integer(), nullable=False),
            sa.Column("td_pct", sa.Float(), nullable=True),
            sa.Column("sub_att", sa.Integer(), nullable=False),
            sa.Column("rev", sa.Integer(), nullable=False),
            sa.Column("ctrl_seconds", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["fight_id"], ["fights.id"]),
            sa.ForeignKeyConstraint(["fighter_id"], ["fighters.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("fight_id", "fighter_id", name="uq_fight_fighter_stats"),
        )
        op.create_index("ix_fight_fighter_stats_fight_id", "fight_fighter_stats", ["fight_id"])
        op.create_index(
            "ix_fight_fighter_stats_fighter_id", "fight_fighter_stats", ["fighter_id"]
        )

    if "ingest_cursors" not in existing:
        op.create_table(
            "ingest_cursors",
            sa.Column("cursor_name", sa.String(length=64), nullable=False),
            sa.Column("next_page", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("cursor_name"),
        )

    if "odds_snapshots" not in existing:
        op.create_table(
            "odds_snapshots",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sport_key", sa.String(length=80), nullable=False),
            sa.Column("event_id_external", sa.String(length=128), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_odds_snapshots_fetched_at", "odds_snapshots", ["fetched_at"])

    if "fighter_composites" not in existing:
        op.create_table(
            "fighter_composites",
            sa.Column("fighter_id", sa.String(length=32), nullable=False),
            sa.Column("as_of_fight_id", sa.String(length=32), nullable=False),
            sa.Column("strike_score", sa.Float(), nullable=False),
            sa.Column("grapple_score", sa.Float(), nullable=False),
            sa.Column("pace_score", sa.Float(), nullable=False),
            sa.Column("momentum_score", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["as_of_fight_id"], ["fights.id"]),
            sa.ForeignKeyConstraint(["fighter_id"], ["fighters.id"]),
            sa.PrimaryKeyConstraint("fighter_id", "as_of_fight_id"),
        )


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(LEGACY_TABLES):
        if table in existing:
            op.drop_table(table)
