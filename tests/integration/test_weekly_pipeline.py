"""DWCS-404 weekly lifecycle integration (fixture-only, no live network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners
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
from mma_model.modeling.registry import load_model_registry
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    load_health_state,
)
from mma_model.observability.publish_guard import FilesystemPublishPointer
from tests.fixtures.week_lifecycle.runner import (
    ACTIVE_AT_T60,
    BOUT_CV,
    BOUT_ISO,
    BOUT_NEW,
    BOUT_NOBET,
    BOUT_OLD,
    BOUT_STALE,
    BOUT_UNPRICED,
    EVENT_ID,
    MAX_DB_GROWTH_BYTES,
    MAX_RUNTIME_SEC,
    HealthEvidence,
    derive_health_from_evidence,
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

        assert BOUT_OLD in by_bout
        assert BOUT_NEW in by_bout
        assert BOUT_ISO not in by_bout  # unresolved identity never published
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

        assert by_bout[BOUT_CV].state == RecommendationState.CONFIRMED_VALUE.value
        assert by_bout[BOUT_UNPRICED].state == RecommendationState.PRICE_TARGET.value
        assert by_bout[BOUT_NOBET].state == RecommendationState.NO_BET.value
        assert by_bout[BOUT_STALE].state == RecommendationState.NO_BET.value
        assert by_bout[BOUT_STALE].primary_reason == "stale_line"
        assert by_bout[BOUT_NEW].state == RecommendationState.PRICE_TARGET.value
        for bout_id in ACTIVE_AT_T60:
            assert bout_id in by_bout

        cv = by_bout[BOUT_CV]
        line_events = session.scalars(
            select(RecommendationStateEvent).where(
                RecommendationStateEvent.official_publication_id == cv.id,
                RecommendationStateEvent.event_type == StateEventType.LINE_CHANGE.value,
            )
        ).all()
        assert line_events
        assert cv.state == RecommendationState.CONFIRMED_VALUE.value

        # Price-target immutability vs snapshot captured at official T−60.
        assert cv.price_target_id is not None
        target = session.get(PriceTarget, cv.price_target_id)
        assert target is not None
        snap = result.price_target_snapshot
        assert snap
        assert target.id == snap["price_target_id"]
        assert target.fair_decimal == snap["fair_decimal"]
        assert target.actionable_decimal == snap["actionable_decimal"]
        assert target.strong_value_decimal == snap["strong_value_decimal"]
        assert target.thresholds_hash == snap["thresholds_hash"]
        assert target.fair_american == snap["fair_american"]
        assert target.actionable_american == snap["actionable_american"]
        assert target.strong_value_american == snap["strong_value_american"]

        cv_quotes = session.scalars(
            select(ObservedPrice).where(ObservedPrice.official_publication_id == cv.id)
        ).all()
        assert cv_quotes
        unpriced = by_bout[BOUT_UNPRICED]
        unpriced_quote_count = session.scalar(
            select(func.count()).select_from(ObservedPrice).where(
                ObservedPrice.official_publication_id == unpriced.id
            )
        )
        assert unpriced_quote_count == 0

        cv_pre = [
            q
            for q in result.quote_ledger
            if q["bout_id"] == BOUT_CV and not q.get("post_official")
        ]
        assert any(float(q["offered_decimal"]) < 2.50 for q in cv_pre)
        assert any(float(q["offered_decimal"]) >= 2.50 for q in cv_pre)

        grades = session.scalars(select(PredictionGrade)).all()
        event_night = [g for g in grades if g.result_version_kind == "event_night"]
        current = [g for g in grades if g.result_version_kind == "current"]
        assert len(event_night) == len(result.prediction_ids)
        assert current, "later correction must append current grades"
        assert all(g.reason_code for g in event_night)

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

        # No settlements for price_target / no_bet; no PnL fields on those pubs.
        pt_or_nobet_ids = {
            p.id
            for p in pubs
            if p.state
            in {
                RecommendationState.PRICE_TARGET.value,
                RecommendationState.NO_BET.value,
            }
        }
        for row in settlements:
            assert row.official_publication_id not in pt_or_nobet_ids
        assert all(
            s.official_publication_id != unpriced.id for s in settlements
        )
        # Unpriced bout never carries profit/roi/clv via any settlement.
        for row in settlements:
            if row.official_publication_id == unpriced.id:
                raise AssertionError("unpriced bout must not settle")

        # Overturned current vs frozen event-night for bout-cv.
        night_snap = result.event_night_cv_settlement
        current_snap = result.current_cv_settlement
        assert night_snap and current_snap
        night_row = session.get(RecommendationSettlement, night_snap["id"])
        current_row = session.get(RecommendationSettlement, current_snap["id"])
        assert night_row is not None
        assert current_row is not None
        assert night_row.id != current_row.id
        assert night_row.reason_code == night_snap["reason_code"]
        assert night_row.settlement_result == night_snap["settlement_result"]
        assert night_row.profit == night_snap["profit"]
        assert "draw" in night_row.reason_code or night_row.settlement_result == "void"
        assert current_row.reason_code != night_row.reason_code
        assert current_row.settlement_result != night_row.settlement_result
        assert current_row.result_version_kind == "current"
        assert current_row.revision == 2

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
        nc_grade = next(
            g for g in event_night if g.prediction_id == pred_by_bout[BOUT_NEW].id
        )
        assert "no_contest" in nc_grade.reason_code
        cv_grade = next(
            g for g in event_night if g.prediction_id == pred_by_bout[BOUT_CV].id
        )
        assert "draw" in cv_grade.reason_code
        current_cv_grade = next(
            g
            for g in current
            if g.prediction_id == pred_by_bout[BOUT_CV].id and g.revision == 2
        )
        assert current_cv_grade.reason_code != cv_grade.reason_code

        assert result.auth_attempts == 1
        assert result.schema_attempts == 1
        assert JobErrorClass.AUTHENTICATION in NON_RETRYABLE_ERRORS
        assert JobErrorClass.SCHEMA in NON_RETRYABLE_ERRORS

        # Retrain went through registry; champion digest unchanged on disk.
        assert result.champion_digest_before == result.champion_digest_after
        reloaded = load_model_registry(
            path=result.model_registry_path, enforce_pinned_digest=False
        )
        assert reloaded.champion.artifact_digest == result.champion_digest_before
        assert result.registry_reject_count >= 1

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

        # Derived health from evidence (not invented component names).
        assert result.health_state_path.is_file()
        loaded = load_health_state(result.health_state_path)
        loaded_statuses = {c.name: c.status.value for c in loaded.components}
        assert set(loaded_statuses) == set(HEALTH_COMPONENT_NAMES)
        assert loaded_statuses == result.health_statuses

        evidence = HealthEvidence(
            discover_succeeded=bool(result.health_evidence["discover_succeeded"]),
            unresolved_identity_bouts=tuple(
                result.health_evidence["unresolved_identity_bouts"]
            ),
            has_stale_line_bout=bool(result.health_evidence["has_stale_line_bout"]),
            publish_failed_lkg_retained=bool(
                result.health_evidence["publish_failed_lkg_retained"]
            ),
            publish_succeeded_later=bool(
                result.health_evidence["publish_succeeded_later"]
            ),
            grade_event_night_ok=bool(result.health_evidence["grade_event_night_ok"]),
            retrain_failed=bool(result.health_evidence["retrain_failed"]),
            backup_job_ran=bool(result.health_evidence["backup_job_ran"]),
            quota_probed=bool(result.health_evidence["quota_probed"]),
            odds_auth_or_schema_failed=bool(
                result.health_evidence["odds_auth_or_schema_failed"]
            ),
        )
        expected = {
            name: status.value
            for name, status in derive_health_from_evidence(
                evidence, as_of="ignored"
            ).items()
        }
        assert result.health_statuses == expected
        assert evidence.retrain_failed is True
        assert evidence.grade_event_night_ok is True
        assert evidence.publish_failed_lkg_retained is True
        assert evidence.has_stale_line_bout is True
        assert BOUT_ISO in evidence.unresolved_identity_bouts
        assert evidence.backup_job_ran is False
        assert evidence.quota_probed is False

        status_values = set(result.health_statuses.values())
        assert {
            "healthy",
            "missing",
            "stale",
            "blocked",
            "failed",
        }.issubset(status_values)
        assert result.health_statuses["grade"] == "healthy"
        assert result.health_statuses["model"] == "failed"
        assert result.health_statuses["identity"] == "blocked"
        assert result.health_statuses["odds"] == "stale"
        assert result.health_statuses["staleness"] == "stale"
        assert result.health_statuses["backup"] == "missing"
        assert result.health_statuses["quota"] == "missing"
        assert result.health_statuses["publish"] in {"blocked", "failed"}

        assert by_bout[BOUT_NOBET].state != RecommendationState.CONFIRMED_VALUE.value
        assert by_bout[BOUT_STALE].state != RecommendationState.CONFIRMED_VALUE.value
    finally:
        session.close()
        engine.dispose()
