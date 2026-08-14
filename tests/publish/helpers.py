"""Helpers for DWCS-500 publish tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.grade.service import (
    StateEventType,
    append_state_event,
    publish_model_run,
    publish_official_t60,
    publish_predictions,
    record_observed_price,
)
from mma_model.publish.builder import build_release_files
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES
from mma_model.recommend.policy import QuoteSourceKind, RenderedThresholds

UTC_TZ = UTC
FIXED_CUTOFF = datetime(2026, 8, 11, 17, 0, 0, tzinfo=UTC)
FIXED_PUBLISHED = datetime(2026, 8, 11, 17, 0, 5, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def open_publish_session(tmp_path: Path, *, name: str = "publish.db") -> tuple[Session, object]:
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


def seed_prediction(
    session: Session,
    *,
    bout_id: str,
    event_id: str = "evt-1",
    p50: float = 0.55,
    selection_suffix: str = "fighter_a",
):
    run, _ = publish_model_run(
        session,
        idempotency_key=f"run:{bout_id}",
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
                "idempotency_key": f"pred:{bout_id}:ml:{selection_suffix}",
                "event_id": event_id,
                "bout_id": bout_id,
                "selection_id": f"{event_id}:{bout_id}:moneyline:{selection_suffix}",
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_A,
                "line_point": None,
                "p50": p50,
                "p25": max(0.01, p50 - 0.07),
                "probability_semantics": "exhaustive",
                "cutoff_at": FIXED_CUTOFF,
                "published_at": FIXED_PUBLISHED,
            }
        ],
    )
    prediction, _ = preds[0]
    return run, prediction


def seed_publication(
    session: Session,
    *,
    bout_id: str,
    state: RecommendationState,
    performance_lane: str = "paper",
    with_observed: bool = False,
    observed_decimal: float = 2.4,
    event_id: str = "evt-1",
    p50: float = 0.55,
    add_replacement_warning: bool = False,
    add_stale_line: bool = False,
):
    run, prediction = seed_prediction(
        session, bout_id=bout_id, event_id=event_id, p50=p50
    )
    thresholds = sample_thresholds() if state is not RecommendationState.NO_BET else None
    pub, _ = publish_official_t60(
        session,
        event_id=event_id,
        bout_id=bout_id,
        selection_id=prediction.selection_id,
        state=state,
        cutoff_at=FIXED_CUTOFF,
        published_at=FIXED_PUBLISHED,
        performance_lane=performance_lane,  # type: ignore[arg-type]
        reasons=(state.value,),
        primary_reason=state.value,
        detail=f"fixture {state.value}",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        prediction_id=prediction.id,
        thresholds=thresholds,
        model_run_id=run.id,
        policy_hash=HASH_A,
        config_hash=HASH_D,
        series="dwcs",
    )
    if with_observed:
        record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="fixture_book",
            decimal_odds=observed_decimal,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
            detail="fixture observed",
            idempotency_key=f"obs:{pub.id}",
        )
    if add_replacement_warning:
        append_state_event(
            session,
            official_publication_id=pub.id,
            event_type=StateEventType.REPLACEMENT_INVALIDATED,
            observed_at=FIXED_PUBLISHED,
            reason_code="fighter_replaced",
            detail="replacement on card",
            payload={"bout_id": bout_id},
            idempotency_key=f"state:{pub.id}:replacement",
        )
    if add_stale_line:
        append_state_event(
            session,
            official_publication_id=pub.id,
            event_type="stale_line",
            observed_at=FIXED_PUBLISHED,
            reason_code="stale_quote",
            detail="line went stale",
            payload={"new_decimal": observed_decimal},
            idempotency_key=f"state:{pub.id}:stale",
        )
    session.commit()
    return pub


def write_golden_release(
    session: Session,
    root: Path,
    release_id: str = "golden-v1",
) -> dict[str, str]:
    files = build_release_files(
        session,
        release_id=release_id,
        event_id="evt-1",
        window_slot="t60",
    )
    out = root / "golden"
    out.mkdir(parents=True, exist_ok=True)
    for name in DASHBOARD_RELEASE_FILES:
        (out / name).write_text(files[name], encoding="utf-8")
        assert name in files
    # Pretty copies for inspection in fixtures dir are optional; return bodies.
    return files


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
