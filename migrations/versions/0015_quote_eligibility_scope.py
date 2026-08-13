"""Add quote-scoped lifecycle identity and quote dedupe_version (DWCS-203).

Revision ID: 0015_quote_eligibility_scope
Revises: 0014_odds_matching
Create Date: 2026-08-13

Scoped lock/removal columns on lifecycle observations (null = bout/event-wide).
``odds_quotes.dedupe_version`` marks v1 legacy vs v2 raw/participant keys.
Integrity: FK quote_id → odds_quotes.id, lookup indexes, nonempty / catalog /
terminal-bout-scope CHECKs. Append-only: legacy quote rows keep v1 keys.
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

_LIFECYCLE_SCOPE_SHAPE_SQL = (
    "("
    "bookmaker_key IS NULL AND region IS NULL AND market_family IS NULL "
    "AND outcome_key IS NULL AND line_point IS NULL AND quote_id IS NULL"
    ") OR ("
    "quote_id IS NOT NULL"
    ") OR ("
    "bookmaker_key IS NOT NULL AND length(trim(bookmaker_key)) > 0 "
    "AND market_family IS NOT NULL AND length(trim(market_family)) > 0"
    ")"
)
_LIFECYCLE_TERMINAL_BOUT_SCOPE_SQL = (
    "lifecycle NOT IN ('cancelled', 'replaced', 'review_blocked') OR ("
    "bookmaker_key IS NULL AND region IS NULL AND market_family IS NULL "
    "AND outcome_key IS NULL AND line_point IS NULL AND quote_id IS NULL"
    ")"
)
_LIFECYCLE_SELECTION_STATE_SQL = (
    "("
    "bookmaker_key IS NULL AND region IS NULL AND market_family IS NULL "
    "AND outcome_key IS NULL AND line_point IS NULL AND quote_id IS NULL"
    ") OR ("
    "lifecycle IN ('active', 'stale', 'missing_unknown', 'locked')"
    ")"
)
_LIFECYCLE_SCOPE_NONEMPTY_SQL = (
    "(bookmaker_key IS NULL OR length(trim(bookmaker_key)) > 0) AND "
    "(region IS NULL OR length(trim(region)) > 0) AND "
    "(market_family IS NULL OR length(trim(market_family)) > 0) AND "
    "(outcome_key IS NULL OR length(trim(outcome_key)) > 0)"
)
_LIFECYCLE_SCOPE_FAMILY_OUTCOME_SQL = (
    "("
    "outcome_key IS NULL AND line_point IS NULL"
    ") OR ("
    "market_family = 'moneyline' AND outcome_key IN ('fighter_a', 'fighter_b') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'totals' AND outcome_key IN ('over', 'under') "
    "AND line_point IN (1.5, 2.5)"
    ") OR ("
    "market_family = 'goes_distance' "
    "AND outcome_key IN ('goes_distance', 'inside_distance') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'method' "
    "AND outcome_key IN ('ko_tko', 'submission', 'decision', 'other_stoppage') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'fighter_by_method' AND outcome_key IN ("
    "'a_ko_tko', 'a_submission', 'a_other_stoppage', 'a_decision', "
    "'b_ko_tko', 'b_submission', 'b_other_stoppage', 'b_decision'"
    ") AND line_point IS NULL"
    ") OR ("
    "market_family = 'exact_round' "
    "AND outcome_key IN ('round_1', 'round_2', 'round_3', 'round_4', 'round_5') "
    "AND line_point IS NULL"
    ")"
)

_SCOPE_INDEXES = (
    ("ix_odds_bout_lifecycle_observations_bookmaker_key", "bookmaker_key"),
    ("ix_odds_bout_lifecycle_observations_region", "region"),
    ("ix_odds_bout_lifecycle_observations_market_family", "market_family"),
    ("ix_odds_bout_lifecycle_observations_outcome_key", "outcome_key"),
    ("ix_odds_bout_lifecycle_observations_quote_id", "quote_id"),
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {idx["name"] for idx in inspect(op.get_bind()).get_indexes(table)}


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
        with op.batch_alter_table(
            "odds_bout_lifecycle_observations",
            recreate="always",
        ) as batch:
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
            batch.create_foreign_key(
                "fk_odds_bout_lifecycle_quote_id",
                "odds_quotes",
                ["quote_id"],
                ["id"],
            )
            batch.create_check_constraint(
                "ck_odds_bout_lifecycle_scope_shape",
                _LIFECYCLE_SCOPE_SHAPE_SQL,
            )
            batch.create_check_constraint(
                "ck_odds_bout_lifecycle_terminal_bout_scope",
                _LIFECYCLE_TERMINAL_BOUT_SCOPE_SQL,
            )
            batch.create_check_constraint(
                "ck_odds_bout_lifecycle_selection_state",
                _LIFECYCLE_SELECTION_STATE_SQL,
            )
            batch.create_check_constraint(
                "ck_odds_bout_lifecycle_scope_nonempty",
                _LIFECYCLE_SCOPE_NONEMPTY_SQL,
            )
            batch.create_check_constraint(
                "ck_odds_bout_lifecycle_scope_family_outcome",
                _LIFECYCLE_SCOPE_FAMILY_OUTCOME_SQL,
            )

        idx_names = _indexes("odds_bout_lifecycle_observations")
        for name, col in _SCOPE_INDEXES:
            if name not in idx_names:
                op.create_index(name, "odds_bout_lifecycle_observations", [col])

    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()

    if "odds_bout_lifecycle_observations" in existing:
        idx_names = _indexes("odds_bout_lifecycle_observations")
        for name, _col in _SCOPE_INDEXES:
            if name in idx_names:
                op.drop_index(name, table_name="odds_bout_lifecycle_observations")
        cols = _columns("odds_bout_lifecycle_observations")
        # Drop scope CHECKs/FK before columns so SQLite batch recreate does not
        # retain constraints that still reference quote_id / bookmaker_key.
        with op.batch_alter_table("odds_bout_lifecycle_observations") as batch:
            for cname in (
                "ck_odds_bout_lifecycle_scope_family_outcome",
                "ck_odds_bout_lifecycle_scope_nonempty",
                "ck_odds_bout_lifecycle_selection_state",
                "ck_odds_bout_lifecycle_terminal_bout_scope",
                "ck_odds_bout_lifecycle_scope_shape",
            ):
                batch.drop_constraint(cname, type_="check")
            batch.drop_constraint(
                "fk_odds_bout_lifecycle_quote_id", type_="foreignkey"
            )
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
