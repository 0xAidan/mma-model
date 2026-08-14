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
from mma_model.db.tables.model_registry import ModelRegistryDecision
from mma_model.db.tables.pipeline_jobs import PipelineJobRun
from mma_model.db.tables.recommendations import (
    OfficialPublication,
    PriceTarget,
    RecommendationSettlement,
)
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
from mma_model.jobs.types import EventContext, JobErrorClass, JobStatus, JobType
from mma_model.markets.settlement import BoutSettlementFacts
from mma_model.modeling.artifacts import PINNED_RIDGE_SPEC_HASH
from mma_model.modeling.baselines import TrainReport, run_protocol_train
from mma_model.modeling.promotion import DecisionAction
from mma_model.modeling.registry import (
    load_model_registry,
    store_artifact_by_digest,
    write_registry_document,
)
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    HealthStatus,
    build_health_report,
    dumps_health,
    make_component,
)
from mma_model.observability.publish_guard import FilesystemPublishPointer
from mma_model.quality.constants import EXIT_OK
from mma_model.quality.models import GateResult
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
# Extra identity-only bout: blocked without failing the five-bout card.
BOUT_ISO = "bout-iso-unresolved"

ACTIVE_AT_T60 = (BOUT_CV, BOUT_UNPRICED, BOUT_NOBET, BOUT_STALE, BOUT_NEW)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64

MAX_RUNTIME_SEC = 120.0
MAX_DB_GROWTH_BYTES = 8 * 1024 * 1024


@dataclass
class HealthEvidence:
    """Facts collected during the lifecycle used to derive health statuses."""

    discover_succeeded: bool = False
    unresolved_identity_bouts: tuple[str, ...] = ()
    has_stale_line_bout: bool = False
    publish_failed_lkg_retained: bool = False
    publish_succeeded_later: bool = False
    grade_event_night_ok: bool = False
    retrain_failed: bool = False
    backup_job_ran: bool = False
    quota_probed: bool = False
    odds_auth_or_schema_failed: bool = False


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
    model_registry_path: Path
    lkg_release_id: str
    final_release_id: str | None
    health_state_path: Path
    health_statuses: dict[str, str] = field(default_factory=dict)
    health_evidence: dict[str, Any] = field(default_factory=dict)
    price_target_snapshot: dict[str, Any] = field(default_factory=dict)
    event_night_cv_settlement: dict[str, Any] = field(default_factory=dict)
    current_cv_settlement: dict[str, Any] = field(default_factory=dict)
    quote_ledger: list[dict[str, Any]] = field(default_factory=list)
    tick_summaries: list[dict[str, Any]] = field(default_factory=list)
    prediction_ids: list[str] = field(default_factory=list)
    publication_ids: list[str] = field(default_factory=list)
    auth_attempts: int = 0
    schema_attempts: int = 0
    registry_reject_count: int = 0
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


def _pass_health_gate() -> GateResult:
    return GateResult(
        ok=True,
        exit_code=EXIT_OK,
        blocker_codes=(),
        passed_codes=("lifecycle_fixture_health",),
        informational_codes=(),
        gates=(),
    )


def _seed_model_registry(work_dir: Path) -> tuple[Path, Path, str]:
    """Seed a real champion registry (same pattern as DWCS-402 tests)."""
    artifacts = work_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    report = run_protocol_train(output_path=artifacts / "seed.json")
    digest, stored = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    registry_path = work_dir / "model_registry.yaml"
    write_registry_document(
        registry_path,
        champion_digest=digest,
        artifact_relpath=str(stored),
        champion_config_hash=PINNED_RIDGE_SPEC_HASH,
    )
    return registry_path, artifacts, digest


def derive_health_from_evidence(
    evidence: HealthEvidence,
    *,
    as_of: str,
) -> dict[str, HealthStatus]:
    """Map lifecycle evidence onto real HEALTH_COMPONENT_NAMES statuses."""
    statuses: dict[str, HealthStatus] = {}

    statuses["sources"] = (
        HealthStatus.HEALTHY if evidence.discover_succeeded else HealthStatus.MISSING
    )
    statuses["identity"] = (
        HealthStatus.BLOCKED
        if evidence.unresolved_identity_bouts
        else HealthStatus.HEALTHY
    )
    # Stale observed line on the card drives odds + staleness.
    statuses["odds"] = (
        HealthStatus.STALE if evidence.has_stale_line_bout else HealthStatus.HEALTHY
    )
    if evidence.odds_auth_or_schema_failed and statuses["odds"] is HealthStatus.HEALTHY:
        statuses["odds"] = HealthStatus.FAILED
    statuses["staleness"] = (
        HealthStatus.STALE if evidence.has_stale_line_bout else HealthStatus.HEALTHY
    )
    statuses["model"] = (
        HealthStatus.FAILED if evidence.retrain_failed else HealthStatus.HEALTHY
    )
    # Failed publish that retained LKG is blocked (current not advanced).
    if evidence.publish_failed_lkg_retained and not evidence.publish_succeeded_later:
        statuses["publish"] = HealthStatus.BLOCKED
    elif evidence.publish_failed_lkg_retained:
        # Failure occurred; later success recovered current — still record failure.
        statuses["publish"] = HealthStatus.FAILED
    elif evidence.publish_succeeded_later:
        statuses["publish"] = HealthStatus.HEALTHY
    else:
        statuses["publish"] = HealthStatus.MISSING
    statuses["grade"] = (
        HealthStatus.HEALTHY if evidence.grade_event_night_ok else HealthStatus.MISSING
    )
    statuses["backup"] = (
        HealthStatus.HEALTHY if evidence.backup_job_ran else HealthStatus.MISSING
    )
    statuses["quota"] = (
        HealthStatus.HEALTHY if evidence.quota_probed else HealthStatus.MISSING
    )

    # Every packaged component must appear.
    for name in HEALTH_COMPONENT_NAMES:
        statuses.setdefault(name, HealthStatus.MISSING)
    return statuses


def _build_candidates(
    *,
    policy: Any,
    quote_ledger: list[dict[str, Any]],
) -> dict[str, SelectionCandidate]:
    """Frozen-policy candidates for the five-bout active card."""
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
            quote=eligible_quote(
                5.0, observed_at=CUTOFF - timedelta(minutes=20), cutoff=CUTOFF
            ),
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


def _snapshot_price_target(session: Session, publication_id: str) -> dict[str, Any]:
    pub = session.get(OfficialPublication, publication_id)
    if pub is None or pub.price_target_id is None:
        raise RuntimeError("expected confirmed_value publication with price target")
    target = session.get(PriceTarget, pub.price_target_id)
    if target is None:
        raise RuntimeError("missing price target row")
    return {
        "price_target_id": target.id,
        "fair_decimal": target.fair_decimal,
        "actionable_decimal": target.actionable_decimal,
        "strong_value_decimal": target.strong_value_decimal,
        "fair_american": target.fair_american,
        "actionable_american": target.actionable_american,
        "strong_value_american": target.strong_value_american,
        "thresholds_hash": target.thresholds_hash,
    }


def _settlement_snapshot(row: RecommendationSettlement) -> dict[str, Any]:
    return {
        "id": row.id,
        "reason_code": row.reason_code,
        "settlement_result": row.settlement_result,
        "profit": row.profit,
        "roi": row.roi,
        "clv": row.clv,
        "result_version_kind": row.result_version_kind,
        "revision": row.revision,
    }


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
    health_state_path = work_dir / "health_state.json"
    publish_root.mkdir(parents=True, exist_ok=True)

    session, engine = _open_session(db_path)
    policy = load_recommendation_policy()
    registry = HandlerRegistry()
    registry.missing_odds_bouts.update({BOUT_UNPRICED, BOUT_NEW})
    registry.stale_line_bouts.add(BOUT_STALE)
    registry.unresolved_identity_bouts.add(BOUT_ISO)

    model_registry_path, artifacts_dir, champion_digest = _seed_model_registry(work_dir)
    registry.artifact.digest = champion_digest
    champion_before = load_model_registry(
        path=model_registry_path, enforce_pinned_digest=False
    ).champion.artifact_digest
    assert champion_before == champion_digest

    lkg_id = _seed_lkg(publish_root)
    registry.publish.current_release_id = lkg_id
    registry.publish.releases = [lkg_id]

    quote_ledger: list[dict[str, Any]] = []
    for row in card.get("quote_timeline") or []:
        if row.get("post_official"):
            continue
        quote_ledger.append(dict(row))

    evidence = HealthEvidence(
        has_stale_line_bout=True,
        unresolved_identity_bouts=(BOUT_ISO,),
    )
    auth_attempts = 0
    schema_attempts = 0
    tick_summaries: list[dict[str, Any]] = []
    prediction_ids: list[str] = []
    predictions_by_bout: dict[str, dict[str, str]] = {}
    model_run_id: str | None = None
    publication_ids: list[str] = []
    price_target_snapshot: dict[str, Any] = {}
    event_night_cv_settlement: dict[str, Any] = {}
    current_cv_settlement: dict[str, Any] = {}

    early_bouts = (BOUT_CV, BOUT_UNPRICED, BOUT_NOBET, BOUT_STALE, BOUT_OLD, BOUT_ISO)
    active_bouts = (*ACTIVE_AT_T60, BOUT_ISO)
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
            "model_registry_path": str(model_registry_path),
            "artifacts_dir": str(artifacts_dir),
            "health_result": _pass_health_gate(),
            "backtest_ok": True,
            "calibration_ok": True,
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
        if any(
            row.job_type == JobType.DISCOVER.value and row.status == JobStatus.SUCCESS.value
            for row in result.executed
        ):
            evidence.discover_succeeded = True
        if any(
            row.job_type == JobType.BACKUP.value and row.status == JobStatus.SUCCESS.value
            for row in result.executed
        ):
            evidence.backup_job_ran = True
        return result

    try:
        _tick(EVENT_START - timedelta(hours=72))

        _record_quote_snapshot(
            quote_ledger,
            bout_id=BOUT_CV,
            as_of=datetime(2024, 8, 12, 12, 0, tzinfo=UTC),
            offered=2.30,
        )
        _tick(datetime(2024, 8, 12, 12, 0, tzinfo=UTC))

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
        evidence.odds_auth_or_schema_failed = True
        del registry.forced_failures[JobType.SNAPSHOT_ODDS]

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

        cross_at = datetime(2024, 8, 13, 0, 30, tzinfo=UTC)
        _record_quote_snapshot(
            quote_ledger,
            bout_id=BOUT_CV,
            as_of=cross_at,
            offered=2.60,
        )
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

        score_at = EVENT_START - timedelta(minutes=61)
        model_run_id, predictions_by_bout, prediction_ids = _seed_predictions(
            session, bout_ids=ACTIVE_AT_T60, as_of=score_at
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
        evidence.publish_failed_lkg_retained = True
        registry.publish_should_fail = False

        # Capture immutable price-target fields immediately after official T−60.
        cv_pub = session.scalar(
            select(OfficialPublication).where(
                OfficialPublication.event_id == EVENT_ID,
                OfficialPublication.bout_id == BOUT_CV,
            )
        )
        assert cv_pub is not None
        assert cv_pub.state == RecommendationState.CONFIRMED_VALUE.value
        price_target_snapshot = _snapshot_price_target(session, cv_pub.id)

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
        assert (publish_root / "releases" / lkg_id / "release.json").is_file()
        evidence.publish_succeeded_later = True

        _tick(EVENT_START, results_final=False)
        grade_success = session.scalar(
            select(func.count()).select_from(PipelineJobRun).where(
                PipelineJobRun.job_type == JobType.GRADE.value,
                PipelineJobRun.success_token == 1,
            )
        )
        assert grade_success == 0

        night = EVENT_START + timedelta(minutes=20)
        registry.results_final = True
        _tick(
            night,
            results_final=True,
            prediction_ids=list(prediction_ids),
            official_publication_ids=list(publication_ids),
            facts_by_bout=facts,
        )
        evidence.grade_event_night_ok = (
            session.scalar(
                select(func.count()).select_from(PipelineJobRun).where(
                    PipelineJobRun.job_type == JobType.GRADE.value,
                    PipelineJobRun.success_token == 1,
                )
            )
            == 1
        )

        # Capture event-night settlement for bout-cv before overturning facts.
        night_settle = session.scalar(
            select(RecommendationSettlement).where(
                RecommendationSettlement.official_publication_id == cv_pub.id,
                RecommendationSettlement.result_version_kind == "event_night",
                RecommendationSettlement.revision == 1,
            )
        )
        assert night_settle is not None
        event_night_cv_settlement = _settlement_snapshot(night_settle)

        # Overturn bout-cv draw → decisive winner (append-only current revision).
        corrected_facts = dict(facts)
        corrected_facts[BOUT_CV] = BoutSettlementFacts(
            scheduled_rounds=3,
            result_class="decisive",
            winner_side="a",
            method="decision",
            ending_round=3,
        )
        grade_predictions(
            session,
            prediction_ids=prediction_ids,
            facts_by_bout=corrected_facts,
            result_version_kind="current",
            revision=2,
            graded_at=night + timedelta(hours=1),
        )
        settle_recommendations(
            session,
            official_publication_ids=publication_ids,
            facts_by_bout=corrected_facts,
            result_version_kind="current",
            revision=2,
            settled_at=night + timedelta(hours=1),
        )
        session.commit()

        current_settle = session.scalar(
            select(RecommendationSettlement).where(
                RecommendationSettlement.official_publication_id == cv_pub.id,
                RecommendationSettlement.result_version_kind == "current",
                RecommendationSettlement.revision == 2,
            )
        )
        assert current_settle is not None
        current_cv_settlement = _settlement_snapshot(current_settle)

        # Idempotent re-grade of event_night (same original facts).
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

        # --- +24h: real retrain_fixed_spec path with failing train_runner ---
        def _boom(*, output_path: Path, include_holdout: bool = False) -> TrainReport:
            _ = (output_path, include_holdout)
            raise RuntimeError("injected model-training failure")

        plus24 = EVENT_START + timedelta(hours=24)
        retrain_tick = _tick(
            plus24,
            results_final=True,
            prediction_ids=list(prediction_ids),
            official_publication_ids=list(publication_ids),
            facts_by_bout=facts,
            train_runner=_boom,
            # Explicitly no retrain_runner stub — handle_retrain → retrain_fixed_spec.
        )
        retrain_rows = [
            r for r in retrain_tick.executed if r.job_type == JobType.RETRAIN.value
        ]
        assert retrain_rows
        assert retrain_rows[0].status == JobStatus.FAILED.value
        evidence.retrain_failed = True

        champion_after = load_model_registry(
            path=model_registry_path, enforce_pinned_digest=False
        ).champion.artifact_digest
        registry_reject_count = int(
            session.scalar(
                select(func.count()).select_from(ModelRegistryDecision).where(
                    ModelRegistryDecision.action == DecisionAction.REJECT.value
                )
            )
            or 0
        )
        assert champion_after == champion_before == champion_digest
        assert registry_reject_count >= 1

        # Price targets still byte-identical after line-change + re-grade.
        after_pt = _snapshot_price_target(session, cv_pub.id)
        assert after_pt == price_target_snapshot

        # Night settlement unchanged after overturn.
        night_again = session.get(RecommendationSettlement, event_night_cv_settlement["id"])
        assert night_again is not None
        assert _settlement_snapshot(night_again) == event_night_cv_settlement

        as_of_stamp = plus24.isoformat().replace("+00:00", "Z")
        derived = derive_health_from_evidence(evidence, as_of=as_of_stamp)
        report = build_health_report(
            [
                make_component(
                    name,
                    derived[name],
                    as_of=as_of_stamp,
                    detail=f"derived from lifecycle evidence ({name})",
                )
                for name in HEALTH_COMPONENT_NAMES
            ],
            as_of=as_of_stamp,
        )
        health_state_path.write_text(dumps_health(report), encoding="utf-8")
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
            model_registry_path=model_registry_path,
            lkg_release_id=lkg_id,
            final_release_id=final_release,
            health_state_path=health_state_path,
            health_statuses=health_statuses,
            health_evidence={
                "discover_succeeded": evidence.discover_succeeded,
                "unresolved_identity_bouts": list(evidence.unresolved_identity_bouts),
                "has_stale_line_bout": evidence.has_stale_line_bout,
                "publish_failed_lkg_retained": evidence.publish_failed_lkg_retained,
                "publish_succeeded_later": evidence.publish_succeeded_later,
                "grade_event_night_ok": evidence.grade_event_night_ok,
                "retrain_failed": evidence.retrain_failed,
                "backup_job_ran": evidence.backup_job_ran,
                "quota_probed": evidence.quota_probed,
                "odds_auth_or_schema_failed": evidence.odds_auth_or_schema_failed,
            },
            price_target_snapshot=price_target_snapshot,
            event_night_cv_settlement=event_night_cv_settlement,
            current_cv_settlement=current_cv_settlement,
            quote_ledger=quote_ledger,
            tick_summaries=tick_summaries,
            prediction_ids=prediction_ids,
            publication_ids=publication_ids,
            auth_attempts=auth_attempts,
            schema_attempts=schema_attempts,
            registry_reject_count=registry_reject_count,
            card=card,
        )
    except Exception:
        session.close()
        engine.dispose()
        raise
    finally:
        with contextlib.suppress(Exception):
            session.close()
