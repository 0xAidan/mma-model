"""Add append-only prediction / recommendation / grading ledgers (DWCS-400).

Revision ID: 0020_recommendation_ledgers
Revises: 0019_odds_job_id_array_triggers
Create Date: 2026-08-13

Upgrade creates ledger tables and SQLite append-only guards.
Downgrade drops those triggers/tables only.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

from mma_model.db.grade_guards import drop_grade_sqlite_guards, install_grade_sqlite_guards
from mma_model.db.tables.recommendations import (
    ModelRun,
    ObservedPrice,
    OfficialPublication,
    Prediction,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)

revision: str = "0020_recommendation_ledgers"
down_revision: Union[str, Sequence[str], None] = "0019_odds_job_id_array_triggers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEDGER_TABLES = (
    "model_runs",
    "predictions",
    "price_targets",
    "official_publications",
    "recommendation_state_events",
    "observed_prices",
    "prediction_grades",
    "recommendation_settlements",
)

# Create order respects FKs; drop order is reverse.
_CREATE_MODELS = (
    ModelRun,
    Prediction,
    PriceTarget,
    OfficialPublication,
    RecommendationStateEvent,
    ObservedPrice,
    PredictionGrade,
    RecommendationSettlement,
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_tables()
    for model in _CREATE_MODELS:
        if model.__tablename__ not in existing:
            model.__table__.create(bind, checkfirst=True)
    install_grade_sqlite_guards(bind)


def downgrade() -> None:
    bind = op.get_bind()
    drop_grade_sqlite_guards(bind)
    existing = _existing_tables()
    for name in reversed(LEDGER_TABLES):
        if name in existing:
            op.drop_table(name)
