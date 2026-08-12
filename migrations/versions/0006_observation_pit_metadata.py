"""Persist four-clock PIT / quality / attributes on raw_observations.

Revision ID: 0006_observation_pit_metadata
Revises: 0005_provenance_revisions
Create Date: 2026-08-12

Adds nullable PIT/quality columns so pre-0006 rows remain intact on upgrade and
downgrade. Downgrade drops only the new columns; observation identity/payload
rows are preserved.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0006_observation_pit_metadata"
down_revision: Union[str, Sequence[str], None] = "0005_provenance_revisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("source_published_at", sa.DateTime(timezone=True)),
    ("proxy_published_at", sa.DateTime(timezone=True)),
    ("timestamp_quality", sa.String(length=64)),
    ("timestamp_quality_source", sa.String(length=128)),
    ("quality_tier", sa.String(length=32)),
    ("attributes_json", sa.Text()),
)


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "raw_observations" not in tables:
        return
    cols = _columns("raw_observations")
    with op.batch_alter_table("raw_observations") as batch:
        for name, col_type in NEW_COLUMNS:
            if name not in cols:
                batch.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    """Drop PIT/quality columns only; preserve all pre-0006 observation rows."""
    tables = set(inspect(op.get_bind()).get_table_names())
    if "raw_observations" not in tables:
        return
    cols = _columns("raw_observations")
    with op.batch_alter_table("raw_observations") as batch:
        for name, _col_type in NEW_COLUMNS:
            if name in cols:
                batch.drop_column(name)
