"""Shared helpers for DWCS-400 grading ledger tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.grade.service import (
    publish_model_run,
    publish_official_t60,
    publish_predictions,
)
from mma_model.markets.settlement import BoutSettlementFacts
from mma_model.recommend.policy import RenderedThresholds

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC_TZ = UTC
FIXED_CUTOFF = datetime(2026, 8, 11, 17, 0, 0, tzinfo=UTC)
FIXED_PUBLISHED = datetime(2026, 8, 11, 17, 0, 5, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def open_test_session(tmp_path: Path, *, name: str = "grade.db") -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal(), engine


def sample_thresholds() -> RenderedThresholds:
    return RenderedThresholds(
        fair_decimal=2.0,
        actionable_decimal=2.1,
        strong_value_decimal=2.2,
        fair_american=100.0,
        actionable_american=110.0,
        strong_value_american=120.0,
        fair_or_better="+100 or better",
        actionable_or_better="+110 or better",
        strong_value_or_better="+120 or better",
        actionable_ev_target=0.05,
        strong_value_ev_target=0.1,
    )


def decisive_a_ko() -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=1,
        elapsed_seconds_in_round=45,
    )


def seed_model_and_prediction(session: Session):
    run, _ = publish_model_run(
        session,
        idempotency_key="run:test:1",
        spec_id="ridge_v1",
        artifact_digest=HASH_A,
        model_hash=HASH_B,
        feature_hash=HASH_C,
        config_hash=HASH_D,
        data_hash=HASH_E,
        created_at=FIXED_PUBLISHED,
    )
    preds = publish_predictions(
        session,
        model_run=run,
        rows=[
            {
                "idempotency_key": "pred:bout1:ml:a",
                "event_id": "evt-1",
                "bout_id": "bout-1",
                "selection_id": "evt-1:bout-1:moneyline:fighter_a",
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_A,
                "line_point": None,
                "p50": 0.55,
                "p25": 0.48,
                "probability_semantics": "exhaustive",
                "cutoff_at": FIXED_CUTOFF,
                "published_at": FIXED_PUBLISHED,
            }
        ],
    )
    prediction, _ = preds[0]
    return run, prediction


def seed_official(
    session: Session,
    *,
    state: RecommendationState,
    prediction_id: str | None,
    model_run_id: str | None,
    performance_lane: str = "paper",
    selection_id: str = "evt-1:bout-1:moneyline:fighter_a",
    thresholds: RenderedThresholds | None = None,
):
    return publish_official_t60(
        session,
        event_id="evt-1",
        bout_id="bout-1",
        selection_id=selection_id,
        state=state,
        cutoff_at=FIXED_CUTOFF,
        published_at=FIXED_PUBLISHED,
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        prediction_id=prediction_id,
        thresholds=thresholds if thresholds is not None else sample_thresholds(),
        model_run_id=model_run_id,
        policy_hash=HASH_A,
        config_hash=HASH_D,
        performance_lane=performance_lane,  # type: ignore[arg-type]
        series="dwcs",
    )
