"""DWCS-404 weekly lifecycle integration (fixture-only, no live network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.pipeline_jobs import PipelineJobRun
from mma_model.db.tables.recommendations import (
    ObservedPrice,
    OfficialPublication,
    Prediction,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)
from mma_model.domain.markets import RecommendationState
from mma_model.grade.service import StateEventType
from mma_model.jobs.types import NON_RETRYABLE_ERRORS, JobErrorClass
from mma_model.observability.publish_guard import FilesystemPublishPointer
from tests.fixtures.week_lifecycle.runner import (
    ACTIVE_AT_T60,
    BOUT_CV,
    BOUT_NEW,
    BOUT_NOBET,
    BOUT_OLD,
    BOUT_STALE,
    BOUT_UNPRICED,
    EVENT_ID,
    MAX_DB_GROWTH_BYTES,
    MAX_RUNTIME_SEC,
    run_week_lifecycle,
)


@pytest.fixture(scope="module")
def lifecycle(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("dwcs404")
    return run_week_lifecycle(work)


def _session(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return factory(), engine


def test_weekly_lifecycle_end_to_end(lifecycle) -> None:
    """Prove T−72h → +24h card: recommendations, settlements, failures, LKG."""
    result = lifecycle
    assert result.runtime_sec < MAX_RUNTIME_SEC
    assert 0 < result.db_bytes < MAX_DB_GROWTH_BYTES

    session, engine = _session(result.db_path)
    try:
        pubs = session.scalars(
            select(OfficialPublication).where(OfficialPublication.event_id == EVENT_ID)
        ).all()
        by_bout = {p.bout_id: p for p in pubs}

        # Canonical versions: old remains; new is official for the active card.
        assert BOUT_OLD in by_bout
        assert BOUT_NEW in by_bout
        old_pub = by_bout[BOUT_OLD]
        new_pub = by_bout[BOUT_NEW]
        assert old_pub.id != new_pub.id

        repl_events = session.scalars(
            select(RecommendationStateEvent).where(
                RecommendationStateEvent.official_publication_id == old_pub.id,
                RecommendationStateEvent.event_type
                == StateEventType.REPLACEMENT_INVALIDATED.value,
            )
        ).all()
        assert repl_events, "replacement must append state event, not delete"
        assert session.get(OfficialPublication, old_pub.id) is not None

        # Active five-bout recommendation states from frozen 307 policy.
        assert by_bout[BOUT_CV].state == RecommendationState.CONFIRMED_VALUE.value
        assert by_bout[BOUT_UNPRICED].state == RecommendationState.PRICE_TARGET.value
        assert by_bout[BOUT_NOBET].state == RecommendationState.NO_BET.value
        assert by_bout[BOUT_STALE].state == RecommendationState.NO_BET.value
        assert by_bout[BOUT_STALE].primary_reason == "stale_line"
        assert by_bout[BOUT_NEW].state == RecommendationState.PRICE_TARGET.value
        for bout_id in ACTIVE_AT_T60:
            assert bout_id in by_bout

        # Official T−60 immutable; post-cutoff line is append-only state event.
        cv = by_bout[BOUT_CV]
        line_events = session.scalars(
            select(RecommendationStateEvent).where(
                RecommendationStateEvent.official_publication_id == cv.id,
                RecommendationStateEvent.event_type == StateEventType.LINE_CHANGE.value,
            )
        ).all()
        assert line_events
        assert cv.state == RecommendationState.CONFIRMED_VALUE.value

        # Price targets immutable after publication.
        assert cv.price_target_id is not None
        target = session.get(PriceTarget, cv.price_target_id)
        assert target is not None
        assert target.fair_decimal == session.get(PriceTarget, cv.price_target_id).fair_decimal

        # Quote lifecycle: confirmed_value has an observed price; unpriced does not.
        cv_quotes = session.scalars(
            select(ObservedPrice).where(ObservedPrice.official_publication_id == cv.id)
        ).all()
        assert cv_quotes
        unpriced = by_bout[BOUT_UNPRICED]
        unpriced_quotes = session.scalars(
            select(ObservedPrice).where(
                ObservedPrice.official_publication_id == unpriced.id
            )
        ).all()
        assert unpriced_quotes == []

        # Line-cross evidence in fixture ledger.
        cv_pre = [
            q
            for q in result.quote_ledger
            if q["bout_id"] == BOUT_CV and not q.get("post_official")
        ]
        assert any(float(q["offered_decimal"]) < 2.50 for q in cv_pre)
        assert any(float(q["offered_decimal"]) >= 2.50 for q in cv_pre)

        # Sporting grades for every prediction.
        grades = session.scalars(select(PredictionGrade)).all()
        event_night = [g for g in grades if g.result_version_kind == "event_night"]
        current = [g for g in grades if g.result_version_kind == "current"]
        assert len(event_night) == len(result.prediction_ids)
        assert current, "later correction must append current grades"
        assert all(g.reason_code for g in event_night)

        # Betting settlements only for priced confirmed_value.
        settlements = session.scalars(select(RecommendationSettlement)).all()
        event_night_settle = [
            s for s in settlements if s.result_version_kind == "event_night"
        ]
        assert event_night_settle
        for row in event_night_settle:
            pub = session.get(OfficialPublication, row.official_publication_id)
            assert pub is not None
            assert pub.state == RecommendationState.CONFIRMED_VALUE.value
            assert row.observed_price_id is not None
            assert row.reason_code
            assert row.profit is not None
            assert row.roi is not None

        # Price-target-only rows never receive ROI or CLV settlements.
        pt_ids = {p.id for p in pubs if p.state == RecommendationState.PRICE_TARGET.value}
        for row in settlements:
            assert row.official_publication_id not in pt_ids
            if row.official_publication_id == unpriced.id:
                raise AssertionError("unpriced price_target must not settle")

        # Event-night settlement survives later correction (append, no rewrite).
        night_ids = {s.id for s in event_night_settle}
        current_settle = [s for s in settlements if s.result_version_kind == "current"]
        assert current_settle
        for sid in night_ids:
            assert session.get(RecommendationSettlement, sid) is not None

        # Grading twice is idempotent (no duplicate event_night rows).
        night_count = session.scalar(
            select(func.count()).select_from(PredictionGrade).where(
                PredictionGrade.result_version_kind == "event_night"
            )
        )
        assert night_count == len(result.prediction_ids) == len(event_night)

        predictions = session.scalars(
            select(Prediction).where(Prediction.event_id == EVENT_ID)
        ).all()
        pred_by_bout = {p.bout_id: p for p in predictions}
        nc_grade = next(g for g in event_night if g.prediction_id == pred_by_bout[BOUT_NEW].id)
        assert "no_contest" in nc_grade.reason_code
        cv_grade = next(g for g in event_night if g.prediction_id == pred_by_bout[BOUT_CV].id)
        assert "draw" in cv_grade.reason_code

        cv_settle = next(
            s
            for s in event_night_settle
            if s.official_publication_id == cv.id
        )
        assert "draw" in cv_settle.reason_code or cv_settle.settlement_result in {
            "void",
            "push",
        }

        # Auth / schema non-retryable (single attempt each).
        assert result.auth_attempts == 1
        assert result.schema_attempts == 1
        assert JobErrorClass.AUTHENTICATION in NON_RETRYABLE_ERRORS
        assert JobErrorClass.SCHEMA in NON_RETRYABLE_ERRORS
        auth_rows = session.scalars(
            select(PipelineJobRun).where(
                PipelineJobRun.error_class == JobErrorClass.AUTHENTICATION.value
            )
        ).all()
        assert all(r.attempt == 1 for r in auth_rows)

        # Failed retrain leaves champion unchanged.
        assert result.champion_digest_before == result.champion_digest_after

        # Failed publication left LKG live; later success advanced current.
        pointer = FilesystemPublishPointer(result.publish_root)
        assert pointer.current_release_id == result.final_release_id
        assert (
            result.publish_root / "releases" / result.lkg_release_id / "release.json"
        ).is_file()
        assert (
            result.publish_root
            / "releases"
            / str(result.final_release_id)
            / "release.json"
        ).is_file()

        # Health distinguishes missing / stale / blocked / failed / healthy.
        assert result.health_statuses.get("scheduler") == "healthy"
        assert result.health_statuses.get("database") == "missing"
        assert result.health_statuses.get("odds") == "stale"
        assert result.health_statuses.get("publish") == "blocked"
        assert result.health_statuses.get("model") == "failed"

        assert by_bout[BOUT_NOBET].state != RecommendationState.CONFIRMED_VALUE.value
        assert by_bout[BOUT_STALE].state != RecommendationState.CONFIRMED_VALUE.value
    finally:
        session.close()
        engine.dispose()
