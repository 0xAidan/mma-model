"""DWCS-401 event-relative orchestrator tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.pipeline_jobs import PipelineJobRun
from mma_model.db.tables.recommendations import (
    OfficialPublication,
    RecommendationStateEvent,
)
from mma_model.domain.markets import RecommendationState
from mma_model.grade.service import StateEventType, publish_official_t60
from mma_model.jobs.due import OrchestratorCadence, compute_due_jobs
from mma_model.jobs.handlers import HandlerRegistry
from mma_model.jobs.locking import FileFlockLock, OverlapError, hold_overlap_lock
from mma_model.jobs.orchestrator import TickOverlapError, run_jobs_tick
from mma_model.jobs.types import (
    EventContext,
    JobErrorClass,
    JobStatus,
    JobType,
)
from mma_model.recommend.policy import RenderedThresholds
from tests.grade.helpers import seed_model_and_prediction

UTC_TZ = UTC
EVENT_START = datetime(2026, 8, 11, 18, 0, 0, tzinfo=UTC_TZ)
EVENT_ID = "dwcs-s10-e1"
BOUT_A = "bout-a"
BOUT_B = "bout-b"
CADENCE = OrchestratorCadence(backup_hour_utc=6)


def _open_session(tmp_path: Path, name: str = "orch.db") -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / name}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal(), engine


def _event(*, bouts: tuple[str, ...] = (BOUT_A, BOUT_B)) -> EventContext:
    return EventContext(
        event_id=EVENT_ID,
        event_start=EVENT_START,
        bout_ids=bouts,
        series="dwcs",
    )


def _thresholds() -> RenderedThresholds:
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


def _job_types(as_of: datetime) -> list[str]:
    due = compute_due_jobs(
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        include_series_daily=True,
    )
    return [job.job_type.value for job in due]


def test_event_relative_due_order_fixture() -> None:
    """Exact due job types/order at canonical event-relative instants."""
    t72 = EVENT_START - timedelta(hours=72)
    t61 = EVENT_START - timedelta(minutes=61)
    t60 = EVENT_START - timedelta(minutes=60)
    plus10 = EVENT_START + timedelta(minutes=10)
    plus4h = EVENT_START + timedelta(hours=4)
    plus24h = EVENT_START + timedelta(hours=24)
    plus7d = EVENT_START + timedelta(days=7)
    backup = datetime(2026, 7, 1, 6, 0, 0, tzinfo=UTC_TZ)

    assert _job_types(t72) == [
        "discover",
        "ingest-history",
        "identity",
        "snapshot-odds",
    ]
    assert _job_types(t61) == [
        "discover",
        "ingest-history",
        "identity",
        "snapshot-odds",
        "score",
    ]
    assert _job_types(t60) == [
        "discover",
        "ingest-history",
        "identity",
        "snapshot-odds",
        "score",
        "recommend",
        "publish",
    ]
    assert _job_types(EVENT_START) == [
        "discover",
        "ingest-history",
        "results",
        "grade",
    ]
    assert _job_types(plus10) == [
        "discover",
        "ingest-history",
        "results",
        "grade",
    ]
    assert _job_types(plus4h) == [
        "discover",
        "ingest-history",
        "results",
        "grade",
    ]
    assert _job_types(plus24h) == [
        "discover",
        "ingest-history",
        "grade",
        "reconcile-24h",
        "retrain",
    ]
    assert _job_types(plus7d) == [
        "discover",
        "ingest-history",
        "grade",
        "reconcile-24h",
        "reconcile-7d",
        "retrain",
    ]
    # Nightly backup hour outside the event odds/results windows.
    assert _job_types(backup) == [
        "discover",
        "ingest-history",
        "backup",
    ]


def test_repeated_tick_no_duplicate_success(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    as_of = EVENT_START - timedelta(minutes=60)
    lock_path = tmp_path / "tick.lock"
    kwargs = dict(
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=lock_path,
        context={"results_final": False},
    )
    first = run_jobs_tick(session, **kwargs)
    session.commit()
    success_before = session.scalar(
        select(func.count()).select_from(PipelineJobRun).where(
            PipelineJobRun.success_token == 1
        )
    )
    pubs_before = session.scalar(select(func.count()).select_from(OfficialPublication))
    second = run_jobs_tick(session, **kwargs)
    session.commit()
    success_after = session.scalar(
        select(func.count()).select_from(PipelineJobRun).where(
            PipelineJobRun.success_token == 1
        )
    )
    pubs_after = session.scalar(select(func.count()).select_from(OfficialPublication))
    assert first.failures == 0
    assert success_before == success_after
    assert pubs_before == pubs_after
    assert all(
        row.status in {JobStatus.SKIPPED.value, JobStatus.SUCCESS.value}
        or row.status == JobStatus.DEPENDENCY_BLOCKED.value
        for row in second.executed
        if row.job_type not in {"discover", "ingest-history"}
        or True
    )
    # Every previously successful key is skipped, not re-succeeded.
    skipped = [row for row in second.executed if row.status == JobStatus.SKIPPED.value]
    assert skipped
    engine.dispose()


def test_dry_run_no_writes(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    as_of = EVENT_START - timedelta(minutes=60)

    def _fingerprint() -> tuple[int, int, str]:
        jobs = session.scalars(select(PipelineJobRun)).all()
        pubs = session.scalars(select(OfficialPublication)).all()
        blob = json.dumps(
            {
                "jobs": sorted((j.idempotency_key, j.status) for j in jobs),
                "pubs": sorted(p.idempotency_key for p in pubs),
            },
            sort_keys=True,
        )
        return len(jobs), len(pubs), hashlib.sha256(blob.encode()).hexdigest()

    before = _fingerprint()
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        dry_run=True,
        cadence=CADENCE,
        acquire_lock=False,
    )
    session.commit()
    after = _fingerprint()
    assert result.dry_run is True
    assert result.due
    assert before == after
    plan = result.dry_run_plan()
    assert list(plan.keys()) == sorted(plan.keys())
    engine.dispose()


def test_failed_identity_isolates_bout(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.unresolved_identity_bouts.add(BOUT_A)
    as_of = EVENT_START - timedelta(minutes=60)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "id.lock",
    )
    session.commit()
    pubs = session.scalars(select(OfficialPublication)).all()
    bout_ids = {p.bout_id for p in pubs}
    assert BOUT_B in bout_ids
    assert BOUT_A not in bout_ids
    recommend = next(r for r in result.executed if r.job_type == "recommend")
    assert recommend.status == JobStatus.SUCCESS.value
    assert BOUT_A in recommend.blocked_bout_ids
    publish = next(r for r in result.executed if r.job_type == "publish")
    assert publish.status == JobStatus.SUCCESS.value
    engine.dispose()


def test_failed_score_keeps_artifact_blocks_downstream(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.artifact.digest = "champion-digest-keep"
    registry.score_should_fail = True
    as_of = EVENT_START - timedelta(minutes=60)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "score.lock",
    )
    session.commit()
    score = next(r for r in result.executed if r.job_type == "score")
    assert score.status == JobStatus.FAILED.value
    assert score.artifact_digest == "champion-digest-keep"
    assert registry.artifact.digest == "champion-digest-keep"
    recommend = next(r for r in result.executed if r.job_type == "recommend")
    publish = next(r for r in result.executed if r.job_type == "publish")
    assert recommend.status == JobStatus.DEPENDENCY_BLOCKED.value
    assert publish.status == JobStatus.DEPENDENCY_BLOCKED.value
    assert session.scalar(select(func.count()).select_from(OfficialPublication)) == 0
    engine.dispose()


def test_failed_publish_keeps_lkg_pointer(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.publish.current_release_id = "release-lkg-keep"
    registry.publish_should_fail = True
    as_of = EVENT_START - timedelta(minutes=60)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "pub.lock",
    )
    session.commit()
    publish = next(r for r in result.executed if r.job_type == "publish")
    assert publish.status == JobStatus.FAILED.value
    assert publish.current_release_id == "release-lkg-keep"
    assert registry.publish.current_release_id == "release-lkg-keep"
    engine.dispose()


def test_missing_odds_price_target_no_confirmed(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.missing_odds_bouts.add(BOUT_A)
    registry.missing_odds_bouts.add(BOUT_B)
    as_of = EVENT_START - timedelta(minutes=60)
    run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "odds.lock",
        context={"confirmed_value_bouts": {BOUT_A, BOUT_B}},
    )
    session.commit()
    pubs = session.scalars(select(OfficialPublication)).all()
    assert pubs
    assert all(p.state == RecommendationState.PRICE_TARGET.value for p in pubs)
    assert all(p.state != RecommendationState.CONFIRMED_VALUE.value for p in pubs)
    engine.dispose()


def test_stale_line_cannot_be_confirmed_value(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.stale_line_bouts.add(BOUT_A)
    as_of = EVENT_START - timedelta(minutes=60)
    run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event(bouts=(BOUT_A,))],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "stale.lock",
        context={"confirmed_value_bouts": {BOUT_A}},
    )
    session.commit()
    pub = session.scalar(select(OfficialPublication))
    assert pub is not None
    assert pub.state == RecommendationState.PRICE_TARGET.value
    assert pub.primary_reason == "stale_line"
    engine.dispose()


def test_replacement_invalidates_via_state_event(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    cutoff = EVENT_START - timedelta(minutes=60)
    old_pub, _ = publish_official_t60(
        session,
        event_id=EVENT_ID,
        bout_id="bout-old",
        selection_id=f"{EVENT_ID}:bout-old:moneyline:fighter_a",
        state=RecommendationState.PRICE_TARGET,
        cutoff_at=cutoff,
        published_at=cutoff,
        thresholds=_thresholds(),
    )
    session.commit()
    registry.official_by_bout["bout-old"] = old_pub.id
    registry.replacements["bout-old"] = "bout-new"
    run_jobs_tick(
        session,
        as_of=cutoff,
        events=[_event(bouts=("bout-new",))],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "repl.lock",
    )
    session.commit()
    # Old official row remains.
    still = session.get(OfficialPublication, old_pub.id)
    assert still is not None
    events = session.scalars(
        select(RecommendationStateEvent).where(
            RecommendationStateEvent.official_publication_id == old_pub.id
        )
    ).all()
    assert any(
        e.event_type == StateEventType.REPLACEMENT_INVALIDATED.value for e in events
    )
    new_pubs = session.scalars(
        select(OfficialPublication).where(OfficialPublication.bout_id == "bout-new")
    ).all()
    assert new_pubs
    engine.dispose()


def test_auth_schema_not_retried(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.forced_failures[JobType.DISCOVER] = JobErrorClass.AUTHENTICATION
    as_of = EVENT_START - timedelta(hours=72)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "auth.lock",
    )
    session.commit()
    discover_rows = session.scalars(
        select(PipelineJobRun).where(PipelineJobRun.job_type == "discover")
    ).all()
    assert len(discover_rows) == 1
    assert discover_rows[0].status == JobStatus.FAILED.value
    assert discover_rows[0].error_class == JobErrorClass.AUTHENTICATION.value
    assert discover_rows[0].attempt == 1
    ingest = next(r for r in result.executed if r.job_type == "ingest-history")
    assert ingest.status == JobStatus.DEPENDENCY_BLOCKED.value
    engine.dispose()


def test_transient_retried_bounded(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    # Fail twice then succeed on third attempt (max=3).
    registry.transient_fail_remaining[JobType.BACKUP] = 2
    as_of = datetime(2026, 8, 10, 6, 0, 0, tzinfo=UTC_TZ)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "transient.lock",
        max_transient_attempts=3,
    )
    session.commit()
    backup_rows = session.scalars(
        select(PipelineJobRun).where(PipelineJobRun.job_type == "backup")
    ).all()
    assert any(r.status == JobStatus.SUCCESS.value for r in backup_rows)
    assert len(backup_rows) == 3  # 2 failed + 1 success
    backup_exec = next(r for r in result.executed if r.job_type == "backup")
    assert backup_exec.status == JobStatus.SUCCESS.value
    assert backup_exec.attempt == 3
    engine.dispose()


def test_overlap_lock_fails_closed(tmp_path: Path) -> None:
    session, engine = _open_session(tmp_path)
    lock_path = tmp_path / "overlap.lock"
    first = FileFlockLock(lock_path)
    with hold_overlap_lock(first):
        with pytest.raises((TickOverlapError, OverlapError)):
            run_jobs_tick(
                session,
                as_of=EVENT_START - timedelta(hours=72),
                events=[_event()],
                cadence=CADENCE,
                lock=FileFlockLock(lock_path),
            )
    engine.dispose()


def test_grade_calls_service_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, engine = _open_session(tmp_path)
    seed_model_and_prediction(session)
    session.commit()
    calls = {"grade": 0, "settle": 0}

    import mma_model.jobs.handlers as handlers_mod

    real_grade = handlers_mod.grade_predictions
    real_settle = handlers_mod.settle_recommendations

    def spy_grade(*args, **kwargs):
        calls["grade"] += 1
        return real_grade(*args, **kwargs)

    def spy_settle(*args, **kwargs):
        calls["settle"] += 1
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(handlers_mod, "grade_predictions", spy_grade)
    monkeypatch.setattr(handlers_mod, "settle_recommendations", spy_settle)

    registry = HandlerRegistry()
    registry.results_final = True
    # Seed a prior results success so grade deps pass on a +24h-style path,
    # then run at event start where results+grade are both due.
    as_of = EVENT_START
    first = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "grade.lock",
        context={"results_final": True, "prediction_ids": [], "facts_by_bout": {}},
    )
    session.commit()
    assert first.failures == 0
    assert calls["grade"] >= 1
    assert calls["settle"] >= 1
    grade_success = session.scalar(
        select(func.count()).select_from(PipelineJobRun).where(
            PipelineJobRun.job_type == "grade",
            PipelineJobRun.success_token == 1,
        )
    )
    second = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "grade.lock",
        context={"results_final": True},
    )
    session.commit()
    grade_success_after = session.scalar(
        select(func.count()).select_from(PipelineJobRun).where(
            PipelineJobRun.job_type == "grade",
            PipelineJobRun.success_token == 1,
        )
    )
    assert grade_success == grade_success_after == 1
    grade_rows = [r for r in second.executed if r.job_type == "grade"]
    assert grade_rows and grade_rows[0].status == JobStatus.SKIPPED.value
    engine.dispose()


def test_retrain_due_only_after_24h_reconcile_no_promotion(tmp_path: Path) -> None:
    # Not due before +24h.
    before = compute_due_jobs(
        as_of=EVENT_START + timedelta(hours=4),
        events=[_event()],
        cadence=CADENCE,
    )
    assert "retrain" not in [j.job_type.value for j in before]

    session, engine = _open_session(tmp_path)
    registry = HandlerRegistry()
    registry.artifact.digest = "champion-fixed"
    registry.results_final = True
    # Run results+grade at event start first so reconcile deps can chain at +24h.
    run_jobs_tick(
        session,
        as_of=EVENT_START,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "retrain1.lock",
        context={"results_final": True},
    )
    session.commit()
    as_of = EVENT_START + timedelta(hours=24)
    result = run_jobs_tick(
        session,
        as_of=as_of,
        events=[_event()],
        cadence=CADENCE,
        registry=registry,
        lock_path=tmp_path / "retrain2.lock",
        context={"results_final": True},
    )
    session.commit()
    retrain = next(r for r in result.executed if r.job_type == "retrain")
    assert retrain.status == JobStatus.SUCCESS.value
    assert retrain.artifact_digest == "champion-fixed"
    assert retrain.counts.get("promoted") is False
    assert registry.artifact.digest == "champion-fixed"
    engine.dispose()


def test_cli_dry_run_exits_zero(tmp_path: Path) -> None:
    from mma_model.cli import main

    db = tmp_path / "cli.db"
    code = main(
        [
            "jobs",
            "tick",
            "--now",
            "2026-08-11T18:00:00Z",
            "--dry-run",
            "--database-url",
            f"sqlite:///{db}",
            "--event-id",
            EVENT_ID,
            "--event-start",
            "2026-08-11T18:00:00Z",
        ]
    )
    assert code == 0
