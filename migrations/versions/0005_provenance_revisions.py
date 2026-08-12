"""Append-only result revisions and scoped raw observation identity.

Revision ID: 0005_provenance_revisions
Revises: 0004_provenance_ingest
Create Date: 2026-08-11

Migrates:
- raw_observations: add scope/checkpoint_version identity when upgrading from the
  initial DWCS-101 shape that omitted them; rebuild uniqueness.
- bout_result_versions: add immutable revision sequence and replace
  (bout_id, version_kind) uniqueness with (bout_id, version_kind, revision).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = "0005_provenance_revisions"
down_revision: Union[str, Sequence[str], None] = "0004_provenance_ingest"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    bind = op.get_bind()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def _unique_names(table: str) -> set[str]:
    bind = op.get_bind()
    return {uc["name"] for uc in inspect(bind).get_unique_constraints(table) if uc.get("name")}


def upgrade() -> None:
    tables = _existing_tables()

    if "raw_observations" in tables:
        cols = _columns("raw_observations")
        if "scope" not in cols or "checkpoint_version" not in cols:
            with op.batch_alter_table("raw_observations") as batch:
                if "scope" not in cols:
                    batch.add_column(sa.Column("scope", sa.String(length=128), nullable=True))
                if "checkpoint_version" not in cols:
                    batch.add_column(
                        sa.Column("checkpoint_version", sa.String(length=64), nullable=True)
                    )
            # Backfill scope from owning ingest_run; default checkpoint version.
            op.execute(
                text(
                    "UPDATE raw_observations "
                    "SET scope = ("
                    "  SELECT ingest_runs.scope FROM ingest_runs "
                    "  WHERE ingest_runs.id = raw_observations.ingest_run_id"
                    ") "
                    "WHERE scope IS NULL"
                )
            )
            op.execute(
                text(
                    "UPDATE raw_observations "
                    "SET checkpoint_version = 'v1' "
                    "WHERE checkpoint_version IS NULL"
                )
            )
            op.execute(
                text(
                    "UPDATE raw_observations SET scope = 'unknown' "
                    "WHERE scope IS NULL OR scope = ''"
                )
            )
            with op.batch_alter_table("raw_observations") as batch:
                batch.alter_column(
                    "scope",
                    existing_type=sa.String(length=128),
                    nullable=False,
                )
                batch.alter_column(
                    "checkpoint_version",
                    existing_type=sa.String(length=64),
                    nullable=False,
                )
                # Recreate uniqueness with scope/checkpoint_version.
                if "uq_raw_obs_provenance" in _unique_names("raw_observations"):
                    batch.drop_constraint("uq_raw_obs_provenance", type_="unique")
                batch.create_unique_constraint(
                    "uq_raw_obs_provenance",
                    [
                        "source",
                        "stream",
                        "scope",
                        "checkpoint_version",
                        "external_id",
                        "payload_hash",
                    ],
                )
            if "ix_raw_observations_scope" not in {
                idx["name"] for idx in inspect(op.get_bind()).get_indexes("raw_observations")
            }:
                op.create_index("ix_raw_observations_scope", "raw_observations", ["scope"])

        # Allow explicit blob absence (NULL raw_ref) without dangling claims.
        raw_cols = {
            c["name"]: c for c in inspect(op.get_bind()).get_columns("raw_observations")
        }
        if raw_cols.get("raw_ref") is not None and raw_cols["raw_ref"].get("nullable") is False:
            with op.batch_alter_table("raw_observations") as batch:
                batch.alter_column(
                    "raw_ref",
                    existing_type=sa.String(length=64),
                    nullable=True,
                )

    if "bout_result_versions" in tables:
        cols = _columns("bout_result_versions")
        if "revision" not in cols:
            with op.batch_alter_table("bout_result_versions") as batch:
                batch.add_column(sa.Column("revision", sa.Integer(), nullable=True))
            op.execute(text("UPDATE bout_result_versions SET revision = 1 WHERE revision IS NULL"))
            with op.batch_alter_table("bout_result_versions") as batch:
                batch.alter_column(
                    "revision",
                    existing_type=sa.Integer(),
                    nullable=False,
                )
                uniques = _unique_names("bout_result_versions")
                if "uq_bout_result_version_kind" in uniques:
                    batch.drop_constraint("uq_bout_result_version_kind", type_="unique")
                if "uq_bout_result_version_revision" not in uniques:
                    batch.create_unique_constraint(
                        "uq_bout_result_version_revision",
                        ["bout_id", "version_kind", "revision"],
                    )


def downgrade() -> None:
    """Reverse 0005 result-revision changes only.

    Scoped raw_observation identity remains owned by ``0004_provenance_ingest``
    (including the safe backfill path for pre-fix 0004 databases).
    """
    tables = _existing_tables()

    if "bout_result_versions" not in tables:
        return
    if "revision" not in _columns("bout_result_versions"):
        return

    # Collapse to a single row per (bout_id, version_kind): keep highest revision.
    conn = op.get_bind()
    rows = conn.execute(
        text(
            "SELECT bout_id, version_kind, MAX(revision) AS max_rev "
            "FROM bout_result_versions GROUP BY bout_id, version_kind"
        )
    ).mappings().all()
    for row in rows:
        conn.execute(
            text(
                "DELETE FROM bout_result_versions "
                "WHERE bout_id = :bout_id AND version_kind = :version_kind "
                "AND revision < :max_rev"
            ),
            {
                "bout_id": row["bout_id"],
                "version_kind": row["version_kind"],
                "max_rev": row["max_rev"],
            },
        )
    with op.batch_alter_table("bout_result_versions") as batch:
        uniques = _unique_names("bout_result_versions")
        if "uq_bout_result_version_revision" in uniques:
            batch.drop_constraint("uq_bout_result_version_revision", type_="unique")
        batch.drop_column("revision")
        if "uq_bout_result_version_kind" not in uniques:
            batch.create_unique_constraint(
                "uq_bout_result_version_kind",
                ["bout_id", "version_kind"],
            )
