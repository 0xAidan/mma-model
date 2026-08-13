"""Add quote-scoped lifecycle identity and quote dedupe_version (DWCS-203).

Revision ID: 0015_quote_eligibility_scope
Revises: 0014_odds_matching
Create Date: 2026-08-13

Scoped lock/removal columns on lifecycle observations (null = bout/event-wide).
``odds_quotes.dedupe_version`` marks v1 legacy vs v2 raw/participant keys.
Append-only: existing quote rows keep legacy keys (version 1); new inserts use 2.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0015_quote_eligibility_scope"
down_revision: Union[str, Sequence[str], None] = "0014_odds_matching"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _existing_tables()
    drop_odds_sqlite_guards(op.get_bind())

    if "odds_quotes" in existing and "dedupe_version" not in _columns("odds_quotes"):
        with op.batch_alter_table("odds_quotes") as batch:
            batch.add_column(
                sa.Column(
                    "dedupe_version",
                    sa.Integer(),
                    nullable=False,
                    # Existing rows + raw SQL inserts without the column → v1.
                    # ORM/normalize path sets v2 explicitly on new quotes.
                    server_default="1",
                )
            )
            batch.create_check_constraint(
                "ck_odds_quotes_dedupe_version",
                "dedupe_version IN (1, 2)",
            )

    if "odds_bout_lifecycle_observations" in existing:
        cols = _columns("odds_bout_lifecycle_observations")
        with op.batch_alter_table("odds_bout_lifecycle_observations") as batch:
            if "bookmaker_key" not in cols:
                batch.add_column(
                    sa.Column("bookmaker_key", sa.String(length=64), nullable=True)
                )
            if "region" not in cols:
                batch.add_column(
                    sa.Column("region", sa.String(length=32), nullable=True)
                )
            if "market_family" not in cols:
                batch.add_column(
                    sa.Column("market_family", sa.String(length=64), nullable=True)
                )
            if "outcome_key" not in cols:
                batch.add_column(
                    sa.Column("outcome_key", sa.String(length=64), nullable=True)
                )
            if "line_point" not in cols:
                batch.add_column(sa.Column("line_point", sa.Float(), nullable=True))
            if "quote_id" not in cols:
                batch.add_column(sa.Column("quote_id", sa.Integer(), nullable=True))

    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()

    if "odds_bout_lifecycle_observations" in existing:
        cols = _columns("odds_bout_lifecycle_observations")
        with op.batch_alter_table("odds_bout_lifecycle_observations") as batch:
            for name in (
                "quote_id",
                "line_point",
                "outcome_key",
                "market_family",
                "region",
                "bookmaker_key",
            ):
                if name in cols:
                    batch.drop_column(name)

    if "odds_quotes" in existing and "dedupe_version" in _columns("odds_quotes"):
        with op.batch_alter_table("odds_quotes") as batch:
            batch.drop_constraint(
                "ck_odds_quotes_dedupe_version", type_="check"
            )
            batch.drop_column("dedupe_version")

    install_odds_sqlite_guards(op.get_bind())
