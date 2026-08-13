"""Add append-only manual price observations with full integrity (DWCS-202).

Revision ID: 0013_odds_manual_prices
Revises: 0012_odds_availability
Create Date: 2026-08-12

Creates ``odds_manual_price_observations`` with DWCS-200 selection CHECKs,
``attempted_provider`` provenance, and append-only SQLite guards.

If an older unshipped draft of this table exists without ``attempted_provider``,
rows are rebuilt in place with zero loss: entitlement rows backfill
``attempted_provider`` from ``detail`` (``provider=<id>:``) or
``legacy_unspecified``. This revision supersedes the removed draft ``0014``.
"""

from __future__ import annotations

import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

from mma_model.db.odds_guards import drop_odds_sqlite_guards, install_odds_sqlite_guards

revision: str = "0013_odds_manual_prices"
down_revision: Union[str, Sequence[str], None] = "0012_odds_availability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FAMILY_OUTCOME_LINE_SQL = (
    "("
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

_PROVIDER_DETAIL_RE = re.compile(r"^provider=([^:\s]+)\s*:")


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {col["name"] for col in inspect(op.get_bind()).get_columns(table)}


def _create_final_table() -> None:
    op.create_table(
        "odds_manual_price_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("automated", sa.Integer(), nullable=False),
        sa.Column("bookmaker_key", sa.String(length=64), nullable=False),
        sa.Column("bookmaker_title", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("market_family", sa.String(length=64), nullable=False),
        sa.Column("outcome_key", sa.String(length=64), nullable=False),
        sa.Column("line_point", sa.Float(), nullable=True),
        sa.Column("price_decimal", sa.Float(), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("attempted_provider", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_external_id", sa.String(length=128), nullable=True),
        sa.Column("settlement_identity", sa.String(length=200), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('user_observed')",
            name="ck_odds_manual_source_kind",
        ),
        sa.CheckConstraint("automated IN (0, 1)", name="ck_odds_manual_automated"),
        sa.CheckConstraint("automated = 0", name="ck_odds_manual_non_automated"),
        sa.CheckConstraint(
            "lifecycle IN ('available', 'unknown', 'suspended', "
            "'locked', 'removed', 'entitlement_failed')",
            name="ck_odds_manual_lifecycle",
        ),
        sa.CheckConstraint(
            "("
            "lifecycle = 'available' AND price_decimal IS NOT NULL "
            "AND price_decimal > 1.0"
            ") OR ("
            "lifecycle != 'available' AND price_decimal IS NULL"
            ")",
            name="ck_odds_manual_price_provenance",
        ),
        sa.CheckConstraint(
            "("
            "lifecycle = 'entitlement_failed' "
            "AND attempted_provider IS NOT NULL "
            "AND length(trim(attempted_provider)) > 0"
            ") OR ("
            "lifecycle != 'entitlement_failed' AND attempted_provider IS NULL"
            ")",
            name="ck_odds_manual_attempted_provider",
        ),
        sa.CheckConstraint(
            _FAMILY_OUTCOME_LINE_SQL,
            name="ck_odds_manual_family_outcome_line",
        ),
        sa.CheckConstraint(
            "length(trim(bookmaker_key)) > 0",
            name="ck_odds_manual_bookmaker_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(region)) > 0",
            name="ck_odds_manual_region_nonempty",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_odds_manual_price_dedupe_key"),
    )
    for name, col in (
        ("ix_odds_manual_price_observations_dedupe_key", "dedupe_key"),
        ("ix_odds_manual_price_observations_source_kind", "source_kind"),
        ("ix_odds_manual_price_observations_bookmaker_key", "bookmaker_key"),
        ("ix_odds_manual_price_observations_region", "region"),
        ("ix_odds_manual_price_observations_market_family", "market_family"),
        ("ix_odds_manual_price_observations_outcome_key", "outcome_key"),
        ("ix_odds_manual_price_observations_lifecycle", "lifecycle"),
        ("ix_odds_manual_price_observations_attempted_provider", "attempted_provider"),
        ("ix_odds_manual_price_observations_observed_at", "observed_at"),
        ("ix_odds_manual_price_observations_event_external_id", "event_external_id"),
    ):
        op.create_index(name, "odds_manual_price_observations", [col])


def backfill_attempted_provider(lifecycle: str, detail: str | None) -> str | None:
    """Deterministic attempted_provider for rebuilt entitlement rows (never drop)."""
    if lifecycle != "entitlement_failed":
        return None
    text_detail = (detail or "").strip()
    match = _PROVIDER_DETAIL_RE.match(text_detail)
    if match:
        return match.group(1).strip()
    return "legacy_unspecified"


def _rebuild_preserving_all_rows() -> None:
    """Rebuild draft table → final schema without discarding audit rows."""
    bind = op.get_bind()
    before = bind.execute(
        text("SELECT COUNT(*) FROM odds_manual_price_observations")
    ).scalar_one()
    op.rename_table(
        "odds_manual_price_observations",
        "odds_manual_price_observations_old",
    )
    _create_final_table()
    rows = bind.execute(
        text(
            """
            SELECT
              id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
              region, market_family, outcome_key, line_point, price_decimal, lifecycle,
              observed_at, source_updated_at, event_external_id,
              settlement_identity, detail, created_at
            FROM odds_manual_price_observations_old
            ORDER BY id
            """
        )
    ).mappings().all()
    for row in rows:
        attempted = backfill_attempted_provider(
            str(row["lifecycle"]),
            None if row["detail"] is None else str(row["detail"]),
        )
        bind.execute(
            text(
                """
                INSERT INTO odds_manual_price_observations (
                  id, dedupe_key, source_kind, automated, bookmaker_key, bookmaker_title,
                  region, market_family, outcome_key, line_point, price_decimal, lifecycle,
                  attempted_provider, observed_at, source_updated_at, event_external_id,
                  settlement_identity, detail, created_at
                ) VALUES (
                  :id, :dedupe_key, :source_kind, :automated, :bookmaker_key,
                  :bookmaker_title, :region, :market_family, :outcome_key, :line_point,
                  :price_decimal, :lifecycle, :attempted_provider, :observed_at,
                  :source_updated_at, :event_external_id, :settlement_identity,
                  :detail, :created_at
                )
                """
            ),
            {
                **dict(row),
                "attempted_provider": attempted,
            },
        )
    after = bind.execute(
        text("SELECT COUNT(*) FROM odds_manual_price_observations")
    ).scalar_one()
    if after != before:
        raise RuntimeError(
            "odds_manual_price_observations rebuild lost rows: "
            f"before={before}, after={after}"
        )
    op.drop_table("odds_manual_price_observations_old")


def upgrade() -> None:
    existing = _existing_tables()
    drop_odds_sqlite_guards(op.get_bind())
    if "odds_manual_price_observations" not in existing:
        _create_final_table()
    elif "attempted_provider" not in _column_names("odds_manual_price_observations"):
        _rebuild_preserving_all_rows()
    install_odds_sqlite_guards(op.get_bind())


def downgrade() -> None:
    drop_odds_sqlite_guards(op.get_bind())
    existing = _existing_tables()
    if "odds_manual_price_observations" in existing:
        op.drop_table("odds_manual_price_observations")
    install_odds_sqlite_guards(op.get_bind())
