"""Run the DWCS-404 weekly lifecycle against temp SQLite (fixture-only)."""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.pipeline_jobs import PipelineJobRun
from mma_model.db.tables.recommendations import OfficialPublication
from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.grade.service import (
    StateEventType,
    append_state_event,
    grade_predictions,
    publish_model_run,
    publish_official_t60,
    publish_predictions,
    settle_recommendations,
)
from mma_model.jobs.due import OrchestratorCadence
from mma_model.jobs.handlers import HandlerRegistry
from mma_model.jobs.orchestrator import run_jobs_tick
from mma_model.jobs.types import (
    EventContext,
    HandlerResult,
    JobErrorClass,
    JobStatus,
    JobType,
)
from mma_model.markets.settlement import BoutSettlementFacts
from mma_model.observability.health import (
    HealthStatus,
    build_health_report,
    make_component,
)
from mma_model.observability.publish_guard import FilesystemPublishPointer
from mma_model.recommend.policy import (
    SelectionCandidate,
    load_recommendation_policy,
    render_thresholds,
)
from tests.recommend.helpers import eligible_quote, make_candidate

FIXTURE_ROOT = Path(__file__).resolve().parent
EVENT_START = datetime(2024, 8, 13, 2, 0, 0, tzinfo=UTC)
EVENT_ID = "dwcs-lifecycle-fixture-1"
CUTOFF = EVENT_START - timedelta(minutes=60)
SERIES = "dwcs"
CADENCE = OrchestratorCadence(backup_hour_utc=6)

BOUT_CV = "bout-cv"
BOUT_UNPRICED = "bout-unpriced"
BOUT_NOBET = "bout-nobet"
BOUT_STALE = "bout-stale"
BOUT_OLD = "bout-old"
BOUT_NEW = "bout-new"

ACTIVE_AT_T60 = (BOUT_CV, BOUT_UNPRICED, BOUT_NOBET, BOUT_STALE, BOUT_NEW)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64

# Hard upper bounds for the fixture run (not flake-prone exact counts).
MAX_RUNTIME_SEC = 120.0
MAX_DB_GROWTH_BYTES = 8 * 1024 * 1024


@dataclass
class LifecycleResult:
    """Artifacts produced by ``run_week_lifecycle`` for assertions / smoke."""

    db_path: Path
    publish_root: Path
    lock_path: Path
    runtime_sec: float
    db_bytes: int
    champion_digest_before: str
    champion_digest_after: str
    lkg_release_id: str
    final_release_id: str | None
    quote_ledger: list[dict[str, Any]] = field(default_factory=list)
    health_statuses: dict[str, str] = field(default_factory=dict)
    tick_summaries: list[dict[str, Any]] = field(default_factory=list)
    prediction_ids: list[str] = field(default_factory=list)
    publication_ids: list[str] = field(default_factory=list)
    auth_attempts: int = 0
    schema_attempts: int = 0
    card: dict[str, Any] = field(default_factory=dict)


def assert_not_live_db(db_path: Path | str) -> None:
    """Refuse writing to the live repository database."""
    resolved = Path(db_path).resolve()
    text = str(resolved).replace("\\", "/").lower()
    if text.endswith("/data/mma.db") or text.endswith("data/mma.db"):
        raise RuntimeError(f"refusing live data/mma.db path: {resolved}")


def load_card(fixture_dir: Path | None = None) -> dict[str, Any]:
    root = fixture_dir or FIXTURE_ROOT
    return json.loads((root / "card.json").read_text(encoding="utf-8"))


def _facts_from_card(card: dict[str, Any]) -> dict[str, BoutSettlementFacts]:
    out: dict[str, BoutSettlementFacts] = {}
    for bout_id, raw in dict(card.get("results") or {}).items():
        kwargs: dict[str, Any] = {
            "scheduled_rounds": int(raw.get("scheduled_rounds") or 3),
            "result_class": raw.get("result_class"),
        }
        if raw.get("winner_side") is not None:
            kwargs["winner_side"] = raw["winner_side"]
        if raw.get("method") is not None:
            kwargs["method"] = raw["method"]
        if raw.get("ending_round") is not None:
            kwargs["ending_round"] = int(raw["ending_round"])
        if raw.get("elapsed_seconds_in_round") is not None:
            kwargs["elapsed_seconds_in_round"] = int(raw["elapsed_seconds_in_round"])
        out[bout_id] = BoutSettlementFacts(**kwargs)
    return out


def _open_session(db_path: Path) -> tuple[Session, Any]:
    assert_not_live_db(db_path)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return SessionLocal(), engine


def _build_candidates(
    *,
    policy: Any,
    quote_ledger: list[dict[str, Any]],
) -> dict[str, SelectionCandidate]:
    """Frozen-policy candidates for the five-bout active card."""
    # Latest pre-cutoff fresh quote for bout-cv (line-cross to actionable).
    cv_quote_raw = None
    for row in quote_ledger:
        if (
            row["bout_id"] == BOUT_CV
            and not row.get("post_official")
            and not row.get("stale")
        ):
            cv_quote_raw = row
    cv_offered = float(cv_quote_raw["offered_decimal"]) if cv_quote_raw else 2.60
    cv_observed = (
        datetime.fromisoformat(str(cv_quote_raw["at"]).replace("Z", "+00:00"))
        if cv_quote_raw
        else CUTOFF - timedelta(minutes=30)
    )

    return {
        BOUT_CV: make_candidate(
            event_id=EVENT_ID,
            bout_id=BOUT_CV,
            quote=eligible_quote(
                cv_offered,
                observed_at=cv_observed,
                cutoff=CUTOFF,
            ),
            policy=policy,
        ),
        BOUT_UNPRICED: make_candidate(
            event_id=EVENT_ID,
            bout_id=BOUT_UNPRICED,
            quote=None,
            policy=policy,
        ),
        BOUT_NOBET: make_candidate(
            event_id=EVENT_ID,
            bout_id=BOUT_NOBET,
            quote=eligible_quote(5.0, observed_at=CUTOFF - timedelta(minutes=20), cutoff=CUTOFF),
            data_quality_pass=False,
            model_qualified=False,
            policy=policy,
        ),
        BOUT_STALE: make_candidate(
            event_id=EVENT_ID,
            bout_id=BOUT_STALE,
            quote=eligible_quote(
                3.10,
                observed_at=CUTOFF - timedelta(hours=7),
                cutoff=CUTOFF,
                stale=True,
                eligible=False,
                lifecycle="stale",
            ),
            policy=policy,
        ),
        BOUT_NEW: make_candidate(
            event_id=EVENT_ID,
            bout_id=BOUT_NEW,
            quote=None,
            policy=policy,
        ),
    }


def _seed_predictions(
    session: Session,
    *,
    bout_ids: tuple[str, ...],
    as_of: datetime,
) -> tuple[str, dict[str, dict[str, str]], list[str]]:
    run, _ = publish_model_run(
        session,
        idempotency_key=f"run:{EVENT_ID}:lifecycle",
        spec_id="ridge_v1",
        artifact_digest=HASH_A,
        model_hash=HASH_B,
        feature_hash=HASH_C,
        config_hash=HASH_D,
        data_hash=HASH_E,
        created_at=as_of,
    )
    rows = []
    for bout_id in bout_ids:
        rows.append(
            {
                "idempotency_key": f"pred:{EVENT_ID}:{bout_id}:ml:a",
                "event_id": EVENT_ID,
                "bout_id": bout_id,
                "selection_id": f"{EVENT_ID}:{bout_id}:moneyline:fighter_a",
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_A,
                "line_point": None,
                "p50": 0.50,
                "p25": 0.40,
                "probability_semantics": "conditional_nonvoid",
                "cutoff_at": CUTOFF,
                "published_at": as_of if as_of >= CUTOFF else CUTOFF,
            }
        )
    published = publish_predictions(session, model_run=run, rows=rows)
    by_bout: dict[str, dict[str, str]] = {}
    prediction_ids: list[str] = []
    for prediction, _created in published:
        by_bout[prediction.bout_id] = {
            "prediction_id": prediction.id,
            "model_run_id": run.id,
        }
        prediction_ids.append(prediction.id)
    return run.id, by_bout, prediction_ids


def _seed_lkg(publish_root: Path) -> str:
    pointer = FilesystemPublishPointer(publish_root)
    release_id = "release-lkg-0"
    files = {
        "release.json": json.dumps(
            {"release_id": release_id, "lkg": True}, sort_keys=True
        ),
        "manifest.json": json.dumps(
            {"release_id": release_id, "files": ["release.json"]}, sort_keys=True
        ),
    }
    pointer.publish_release(
        release_id,
        files,
        required_files=("release.json", "manifest.json"),
    )
    return release_id


def _record_quote_snapshot(
    quote_ledger: list[dict[str, Any]],
    *,
    bout_id: str,
    as_of: datetime,
    offered: float,
    stale: bool = False,
    post_official: bool = False,
) -> None:
    quote_ledger.append(
        {
            "bout_id": bout_id,
            "at": as_of.isoformat().replace("+00:00", "Z"),
            "offered_decimal": offered,
            "stale": stale,
            "post_official": post_official,
        }
    )


def run_week_lifecycle(
    work_dir: Path,
    *,
    fixture_dir: Path | None = None,
) -> LifecycleResult:
    """Drive scheduler ticks from T−72h through +24h on temp SQLite."""
    fixture_dir = fixture_dir or FIXTURE_ROOT
    card = load_card(fixture_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    db_path = work_dir / "lifecycle.db"
    assert_not_live_db(db_path)
    publish_root = work_dir / "publish"
    lock_path = work_dir / "tick.lock"
    publish_root.mkdir(parents=True, exist_ok=True)

    session, engine = _open_session(db_path)
    policy = load_recommendation_policy()
    registry = HandlerRegistry()
    registry.artifact.digest = HASH_A
    registry.missing_odds_bouts.update({BOUT_UNPRICED, BOUT_NEW})
    registry.stale_line_bouts.add(BOUT_STALE)

    lkg_id = _seed_lkg(publish_root)
    registry.publish.current_release_id = lkg_id
    registry.publish.releases = [lkg_id]

    quote_ledger: list[dict[str, Any]] = []
    # Seed early below-actionable line for bout-cv (line-cross later).
    for row in card.get("quote_timeline") or []:
        if row.get("post_official"):
            continue
        quote_ledger.append(dict(row))

    champion_before = registry.artifact.digest
    auth_attempts = 0
    schema_attempts = 0
    tick_summaries: list[dict[str, Any]] = []
    prediction_ids: list[str] = []
    predictions_by_bout: dict[str, dict[str, str]] = {}
    model_run_id: str | None = None
    publication_ids: list[str] = []

    # Early card includes bout-old; after replacement the active five use bout-new.
    early_bouts = (BOUT_CV, BOUT_UNPRICED, BOUT_NOBET, BOUT_STALE, BOUT_OLD)
    active_bouts = ACTIVE_AT_T60
    event = EventContext(
        event_id=EVENT_ID,
        event_start=EVENT_START,
        bout_ids=early_bouts,
        series=SERIES,
    )

    facts = _facts_from_card(card)
    t0 = time.perf_counter()

    def _ctx(**extra: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "registry": registry,
            "publish_root": str(publish_root),
            "recommendation_policy": policy,
            "recommendation_candidates": _build_candidates(
                policy=policy, quote_ledger=quote_ledger
            ),
            "predictions_by_bout": predictions_by_bout,
            "model_run_id": model_run_id,
            "prediction_ids": list(prediction_ids),
            "official_publication_ids": list(publication_ids),
            "facts_by_bout": facts,
            "results_final": False,
            "no_bet_bouts": {BOUT_NOBET},
        }
        base.update(extra)
        return base

    def _tick(as_of: datetime, **extra: Any) -> Any:
        nonlocal publication_ids
        result = run_jobs_tick(
            session,
            as_of=as_of,
            events=[event],
            cadence=CADENCE,
            registry=registry,
            lock_path=lock_path,
            context=_ctx(**extra),
        )
        session.commit()
        pubs = session.scalars(
            select(OfficialPublication).where(OfficialPublication.event_id == EVENT_ID)
        ).all()
        publication_ids = [p.id for p in pubs]
        tick_summaries.append(
            {
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "failures": result.failures,
                "jobs": [
                    {
                        "job_type": row.job_type,
                        "status": row.status,
                        "error_class": row.error_class,
                        "detail": row.detail,
                    }
                    for row in result.executed
                ],
            }
        )
        return result

    try:
        # --- T−72h discover / identity / odds ---
        _tick(EVENT_START - timedelta(hours=72))

        # --- Mid-window odds: capture below-actionable CV line ---
        _record_quote_snapshot(
            quote_ledger,
            bout_id=BOUT_CV,
            as_of=datetime(2024, 8, 12, 12, 0, tzinfo=UTC),
            offered=2.30,
        )
        _tick(datetime(2024, 8, 12, 12, 0, tzinfo=UTC))

        # --- Inject AUTH failure on snapshot-odds (non-retryable) ---
        registry.forced_failures[JobType.SNAPSHOT_ODDS] = JobErrorClass.AUTHENTICATION
        auth_result = _tick(datetime(2024, 8, 12, 15, 0, tzinfo=UTC))
        auth_rows = [
            r
            for r in auth_result.executed
            if r.job_type == JobType.SNAPSHOT_ODDS.value
        ]
        auth_attempts = len(
            session.scalars(
                select(PipelineJobRun).where(
                    PipelineJobRun.job_type == JobType.SNAPSHOT_ODDS.value,
                    PipelineJobRun.error_class == JobErrorClass.AUTHENTICATION.value,
                )
            ).all()
        )
        assert auth_rows, "expected snapshot-odds auth failure row"
        del registry.forced_failures[JobType.SNAPSHOT_ODDS]

        # --- Inject SCHEMA failure on snapshot-odds (non-retryable) ---
        registry.forced_failures[JobType.SNAPSHOT_ODDS] = JobErrorClass.SCHEMA
        schema_result = _tick(datetime(2024, 8, 12, 16, 0, tzinfo=UTC))
        schema_rows = [
            r
            for r in schema_result.executed
            if r.job_type == JobType.SNAPSHOT_ODDS.value
        ]
        schema_attempts = len(
            session.scalars(
                select(PipelineJobRun).where(
                    PipelineJobRun.job_type == JobType.SNAPSHOT_ODDS.value,
                    PipelineJobRun.error_class == JobErrorClass.SCHEMA.value,
                )
            ).all()
        )
        assert schema_rows, "expected snapshot-odds schema failure row"
        del registry.forced_failures[JobType.SNAPSHOT_ODDS]

        # --- Line cross + replacement before T−60 ---
        cross_at = datetime(2024, 8, 13, 0, 30, tzinfo=UTC)
        _record_quote_snapshot(
            quote_ledger,
            bout_id=BOUT_CV,
            as_of=cross_at,
            offered=2.60,
        )
        # Pre-publish bout-old so recommend can invalidate without delete.
        old_thresholds = render_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
        old_pub, _ = publish_official_t60(
            session,
            event_id=EVENT_ID,
            bout_id=BOUT_OLD,
            selection_id=f"{EVENT_ID}:{BOUT_OLD}:moneyline:fighter_a",
            state=RecommendationState.PRICE_TARGET,
            cutoff_at=CUTOFF,
            published_at=CUTOFF,
            thresholds=old_thresholds,
            reasons=("pre_replacement",),
            primary_reason="pre_replacement",
            market_family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            policy_hash=policy.content_hash,
            config_hash=HASH_D,
            series=SERIES,
        )
        session.commit()
        registry.official_by_bout[BOUT_OLD] = old_pub.id
        registry.replacements[BOUT_OLD] = BOUT_NEW
        event = EventContext(
            event_id=EVENT_ID,
            event_start=EVENT_START,
            bout_ids=active_bouts,
            series=SERIES,
        )
        _tick(cross_at)

        # --- T−61m score: seed predictions for sporting grades ---
        score_at = EVENT_START - timedelta(minutes=61)
        model_run_id, predictions_by_bout, prediction_ids = _seed_predictions(
            session, bout_ids=active_bouts, as_of=score_at
        )
        session.commit()
        _tick(score_at)

        # --- T−60m recommend + failed publish (LKG retained) ---
        registry.publish_should_fail = True
        t60 = CUTOFF
        fail_pub = _tick(t60, results_final=False)
        publish_fail = next(
            (r for r in fail_pub.executed if r.job_type == JobType.PUBLISH.value),
            None,
        )
        assert publish_fail is not None
        assert publish_fail.status == JobStatus.FAILED.value
        pointer = FilesystemPublishPointer(publish_root)
        assert pointer.current_release_id == lkg_id
        assert (publish_root / "releases" / lkg_id / "release.json").is_file()
        registry.publish_should_fail = False

        # Post-official line change → state event only (official row immutable).
        cv_pub = session.scalar(
            select(OfficialPublication).where(
                OfficialPublication.event_id == EVENT_ID,
                OfficialPublication.bout_id == BOUT_CV,
            )
        )
        assert cv_pub is not None
        post_at = datetime(2024, 8, 13, 1, 30, tzinfo=UTC)
        append_state_event(
            session,
            official_publication_id=cv_pub.id,
            event_type=StateEventType.LINE_CHANGE,
            observed_at=post_at,
            reason_code="line_moved_post_official",
            detail="post T-60 line drift",
            payload={"new_decimal": 2.45},
            idempotency_key=f"state:{cv_pub.id}:line_change:2.45",
        )
        session.commit()
        _record_quote_snapshot(
            quote_ledger,
            bout_id=BOUT_CV,
            as_of=post_at,
            offered=2.45,
            post_official=True,
        )

        # --- Later publish success (same T-60 publish key retries) ---
        ok_pub = _tick(post_at)
        publish_ok = next(
            (r for r in ok_pub.executed if r.job_type == JobType.PUBLISH.value),
            None,
        )
        assert publish_ok is not None
        assert publish_ok.status == JobStatus.SUCCESS.value
        pointer = FilesystemPublishPointer(publish_root)
        final_release = pointer.current_release_id
        assert final_release is not None
        assert final_release != lkg_id
        assert (publish_root / "releases" / final_release / "release.json").is_file()
        # LKG files survive.
        assert (publish_root / "releases" / lkg_id / "release.json").is_file()

        # --- Event start: grade deferred until finals ---
        _tick(EVENT_START, results_final=False)
        grade_success = session.scalar(
            select(func.count()).select_from(PipelineJobRun).where(
                PipelineJobRun.job_type == JobType.GRADE.value,
                PipelineJobRun.success_token == 1,
            )
        )
        assert grade_success == 0

        # --- Event-night finals + grade ---
        night = EVENT_START + timedelta(minutes=20)
        registry.results_final = True
        _tick(
            night,
            results_final=True,
            prediction_ids=list(prediction_ids),
            official_publication_ids=list(publication_ids),
            facts_by_bout=facts,
        )

        # Append a later result correction (current revision) without rewrite.
        grade_predictions(
            session,
            prediction_ids=prediction_ids,
            facts_by_bout=facts,
            result_version_kind="current",
            revision=2,
            graded_at=night + timedelta(hours=1),
        )
        settle_recommendations(
            session,
            official_publication_ids=publication_ids,
            facts_by_bout=facts,
            result_version_kind="current",
            revision=2,
            settled_at=night + timedelta(hours=1),
        )
        session.commit()

        # Idempotent re-grade of event_night.
        grade_predictions(
            session,
            prediction_ids=prediction_ids,
            facts_by_bout=facts,
            result_version_kind="event_night",
            revision=1,
            graded_at=night,
        )
        settle_recommendations(
            session,
            official_publication_ids=publication_ids,
            facts_by_bout=facts,
            result_version_kind="event_night",
            revision=1,
            settled_at=night,
        )
        session.commit()

        # --- +24h: inject retrain failure; champion unchanged ---
        def _failing_retrain(
            _session: Session,
            *,
            job: Any,
            as_of: datetime,
            events: Any,
            context: Any,
        ) -> HandlerResult:
            _ = (job, as_of, events, context)
            return HandlerResult(
                status=JobStatus.FAILED,
                error_class=JobErrorClass.INTERNAL,
                detail="injected model-training failure; champion unchanged",
                artifact_digest=registry.artifact.digest,
                counts={"promoted": False, "champion_unchanged": True},
            )

        plus24 = EVENT_START + timedelta(hours=24)
        _tick(
            plus24,
            results_final=True,
            prediction_ids=list(prediction_ids),
            official_publication_ids=list(publication_ids),
            facts_by_bout=facts,
            retrain_runner=_failing_retrain,
        )
        champion_after = registry.artifact.digest

        # Health status distinctions (fixture-assembled, no live probes).
        as_of_stamp = plus24.isoformat().replace("+00:00", "Z")
        report = build_health_report(
            [
                make_component("scheduler", HealthStatus.HEALTHY, as_of=as_of_stamp),
                make_component("database", HealthStatus.MISSING, as_of=as_of_stamp),
                make_component("odds", HealthStatus.STALE, as_of=as_of_stamp),
                make_component("publish", HealthStatus.BLOCKED, as_of=as_of_stamp),
                make_component("model", HealthStatus.FAILED, as_of=as_of_stamp),
            ],
            as_of=as_of_stamp,
        )
        health_statuses = {c.name: c.status.value for c in report.components}

        runtime_sec = time.perf_counter() - t0
        session.commit()
        engine.dispose()
        db_bytes = db_path.stat().st_size

        return LifecycleResult(
            db_path=db_path,
            publish_root=publish_root,
            lock_path=lock_path,
            runtime_sec=runtime_sec,
            db_bytes=db_bytes,
            champion_digest_before=champion_before,
            champion_digest_after=champion_after,
            lkg_release_id=lkg_id,
            final_release_id=final_release,
            quote_ledger=quote_ledger,
            health_statuses=health_statuses,
            tick_summaries=tick_summaries,
            prediction_ids=prediction_ids,
            publication_ids=publication_ids,
            auth_attempts=auth_attempts,
            schema_attempts=schema_attempts,
            card=card,
        )
    except Exception:
        session.close()
        engine.dispose()
        raise
    finally:
        with contextlib.suppress(Exception):
            session.close()
