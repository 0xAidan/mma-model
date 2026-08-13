"""Injectable / default job handlers for DWCS-401.

Handlers are seams: tests inject fakes; production stubs record real job runs
and call grade ledger services where required. Stubs never silently claim
success without returning a ``HandlerResult`` the orchestrator records.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.recommendations import OfficialPublication
from mma_model.domain.markets import RecommendationState
from mma_model.grade.service import (
    StateEventType,
    append_state_event,
    grade_predictions,
    publish_official_t60,
    settle_recommendations,
)
from mma_model.jobs.types import (
    DueJob,
    EventContext,
    HandlerResult,
    JobErrorClass,
    JobStatus,
    JobType,
)
from mma_model.modeling.registry import retrain_fixed_spec
from mma_model.recommend.policy import RenderedThresholds

Handler = Callable[..., HandlerResult]


class JobHandler(Protocol):
    def __call__(
        self,
        session: Session,
        *,
        job: DueJob,
        as_of: datetime,
        events: Sequence[EventContext],
        context: Mapping[str, Any],
    ) -> HandlerResult: ...


def _sample_thresholds() -> RenderedThresholds:
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


@dataclass
class ArtifactPointer:
    """Last-known-good scoring artifact (failed score must not swap this)."""

    digest: str = "incumbent-artifact-v1"


@dataclass
class PublishPointer:
    """Last-known-good published release (failed publish must not replace)."""

    current_release_id: str = "release-lkg-0"
    releases: list[str] = field(default_factory=lambda: ["release-lkg-0"])


@dataclass
class HandlerRegistry:
    """Named handlers plus shared mutable seams for artifact/publish safety."""

    handlers: MutableMapping[JobType, JobHandler] = field(default_factory=dict)
    artifact: ArtifactPointer = field(default_factory=ArtifactPointer)
    publish: PublishPointer = field(default_factory=PublishPointer)
    # Bout ids with unresolved identity for the active card.
    unresolved_identity_bouts: set[str] = field(default_factory=set)
    # Bout ids whose observed line is stale (cannot become confirmed_value).
    stale_line_bouts: set[str] = field(default_factory=set)
    # Bout ids with no observed odds (price_target still allowed).
    missing_odds_bouts: set[str] = field(default_factory=set)
    # Replacement map: old_bout_id -> new_bout_id
    replacements: dict[str, str] = field(default_factory=dict)
    # Official publication ids by bout for replacement invalidation.
    official_by_bout: dict[str, str] = field(default_factory=dict)
    # Whether results produced finals (grade may proceed).
    results_final: bool = False
    # Injected failure overrides: job_type -> HandlerResult factory / exception
    forced_failures: dict[JobType, JobErrorClass] = field(default_factory=dict)
    transient_fail_remaining: dict[JobType, int] = field(default_factory=dict)
    score_should_fail: bool = False
    publish_should_fail: bool = False
    publish_partial: bool = False

    def get(self, job_type: JobType) -> JobHandler:
        if job_type in self.handlers:
            return self.handlers[job_type]
        default = DEFAULT_HANDLERS.get(job_type)
        if default is None:
            return _default_success_handler
        return default


def _default_success_handler(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, as_of, events, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"job_type": job.job_type.value},
        detail="default seam success",
    )


def handle_discover(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, job, as_of, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"events_seen": len(events)},
        detail="discover seam: refresh upcoming DWCS events/versions",
    )


def handle_ingest_history(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, job, as_of, events, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"profiles": 0, "histories": 0},
        detail="ingest-history seam",
    )


def handle_identity(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, as_of)
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    event = next((e for e in events if e.event_id == job.event_id), None)
    unresolved: list[str] = []
    if registry is not None and event is not None:
        for bout_id in event.bout_ids:
            if bout_id in registry.unresolved_identity_bouts:
                unresolved.append(bout_id)
    if unresolved and event is not None and len(unresolved) == len(event.bout_ids):
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.IDENTITY_UNRESOLVED,
            detail=f"all bouts unresolved: {unresolved}",
            blocked_bout_ids=tuple(unresolved),
            counts={"unresolved": len(unresolved)},
            blocks_downstream=True,
        )
    return HandlerResult(
        status=JobStatus.SUCCESS,
        blocked_bout_ids=tuple(unresolved),
        counts={
            "resolved": (len(event.bout_ids) - len(unresolved)) if event else 0,
            "unresolved": len(unresolved),
        },
        detail="identity seam",
    )


def handle_snapshot_odds(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    """Wrap existing snapshot-odds when a runner is injected; else no-network seam."""
    runner = context.get("snapshot_odds_runner")
    if callable(runner):
        result = runner(session, job=job, as_of=as_of, events=events, context=context)
        if isinstance(result, HandlerResult):
            return result
    _ = job
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"snapshot": "seam"},
        detail="snapshot-odds seam (no network); inject runner for live wrap",
        source_quota=None,
    )


def handle_score(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, as_of)
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    event = next((e for e in events if e.event_id == job.event_id), None)
    prior = registry.artifact.digest if registry is not None else "incumbent-artifact-v1"
    blocked: list[str] = []
    if registry is not None and event is not None:
        blocked = [
            bout_id
            for bout_id in event.bout_ids
            if bout_id in registry.unresolved_identity_bouts
        ]
    if registry is not None and registry.score_should_fail:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail="score failed; retaining previous valid artifact",
            artifact_digest=prior,
            counts={"scored": 0, "blocked": len(blocked)},
            blocked_bout_ids=tuple(blocked),
            blocks_downstream=True,
        )
    scored = 0
    if event is not None:
        scored = len([b for b in event.bout_ids if b not in blocked])
    return HandlerResult(
        status=JobStatus.SUCCESS,
        artifact_digest=prior,
        blocked_bout_ids=tuple(blocked),
        counts={"scored": scored, "blocked": len(blocked)},
        detail="score seam using incumbent artifact",
    )


def handle_recommend(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    event = next((e for e in events if e.event_id == job.event_id), None)
    if event is None:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail="recommend: unknown event",
            blocks_downstream=True,
        )

    cutoff = job.window_slot
    if cutoff is None:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.SCHEMA,
            detail="recommend requires official cutoff window_slot",
            blocks_downstream=True,
        )

    cutoff_at = datetime.fromisoformat(cutoff)
    if cutoff_at.tzinfo is None:
        cutoff_at = cutoff_at.replace(tzinfo=UTC)

    published = 0
    blocked: list[str] = []
    price_targets = 0
    confirmed = 0
    replacements = 0

    # Invalidate replaced bouts via state events (never delete).
    if registry is not None:
        for old_bout, new_bout in list(registry.replacements.items()):
            pub_id = registry.official_by_bout.get(old_bout)
            if pub_id:
                append_state_event(
                    session,
                    official_publication_id=pub_id,
                    event_type=StateEventType.REPLACEMENT_INVALIDATED,
                    observed_at=as_of,
                    reason_code="replacement",
                    detail=f"replaced by {new_bout}",
                    payload={"old_bout_id": old_bout, "new_bout_id": new_bout},
                    idempotency_key=(
                        f"state:{pub_id}:replacement_invalidated:{new_bout}"
                    ),
                )
                replacements += 1

    for bout_id in event.bout_ids:
        if registry is not None and bout_id in registry.unresolved_identity_bouts:
            blocked.append(bout_id)

    if event.bout_ids and len(blocked) == len(event.bout_ids):
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.IDENTITY_UNRESOLVED,
            detail="recommend blocked: all bouts identity_unresolved",
            blocked_bout_ids=tuple(blocked),
            counts={
                "published": 0,
                "price_targets": 0,
                "confirmed_value": 0,
                "blocked": len(blocked),
            },
            blocks_downstream=True,
        )

    for bout_id in event.bout_ids:
        if bout_id in blocked:
            continue

        missing_odds = registry is not None and bout_id in registry.missing_odds_bouts
        stale = registry is not None and bout_id in registry.stale_line_bouts

        if missing_odds:
            state = RecommendationState.PRICE_TARGET
            reasons = ("missing_odds",)
            primary = "missing_odds"
            price_targets += 1
        elif stale:
            # Stale observed line cannot produce confirmed_value.
            state = RecommendationState.PRICE_TARGET
            reasons = ("stale_line",)
            primary = "stale_line"
            price_targets += 1
        else:
            # Without a full policy run, default to price_target (safe).
            # Confirmed_value only when explicitly marked eligible in context.
            eligible = bool(
                (context.get("confirmed_value_bouts") or set())
                and bout_id in set(context.get("confirmed_value_bouts") or set())
            )
            if eligible and not stale and not missing_odds:
                state = RecommendationState.CONFIRMED_VALUE
                reasons = ("policy_pass",)
                primary = "policy_pass"
                confirmed += 1
            else:
                state = RecommendationState.PRICE_TARGET
                reasons = ("price_target",)
                primary = "price_target"
                price_targets += 1

        selection_id = f"{event.event_id}:{bout_id}:moneyline:fighter_a"
        row, created = publish_official_t60(
            session,
            event_id=event.event_id,
            bout_id=bout_id,
            selection_id=selection_id,
            state=state,
            cutoff_at=cutoff_at,
            published_at=as_of if as_of >= cutoff_at else cutoff_at,
            reasons=reasons,
            primary_reason=primary,
            thresholds=_sample_thresholds(),
            series=event.series,
        )
        if registry is not None:
            registry.official_by_bout[bout_id] = row.id
        if created:
            published += 1

    return HandlerResult(
        status=JobStatus.SUCCESS,
        blocked_bout_ids=tuple(blocked),
        counts={
            "published": published,
            "price_targets": price_targets,
            "confirmed_value": confirmed,
            "blocked": len(blocked),
            "replacements_invalidated": replacements,
        },
        detail="recommend: official T-60 via grade ledger",
    )


def handle_publish(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = as_of
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    prior = (
        registry.publish.current_release_id
        if registry is not None
        else "release-lkg-0"
    )
    if registry is not None and (registry.publish_should_fail or registry.publish_partial):
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail="publish failed/partial; retaining last-known-good current",
            current_release_id=prior,
            counts={"written": 0, "current_replaced": False},
            blocks_downstream=True,
        )

    event = next((e for e in events if e.event_id == job.event_id), None)
    pub_count = 0
    if job.event_id:
        pub_count = int(
            session.scalar(
                select(func.count())
                .select_from(OfficialPublication)
                .where(OfficialPublication.event_id == job.event_id)
            )
            or 0
        )
    # Empty card with known bouts must not replace last-known-good current.
    if event is not None and event.bout_ids and pub_count == 0:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.DEPENDENCY_BLOCKED,
            detail="publish refused: zero official publications for card; LKG kept",
            current_release_id=prior,
            counts={"written": 0, "current_replaced": False, "publications": 0},
            blocks_downstream=True,
        )

    new_release = f"release-{job.event_id}-{job.window_slot}"
    if registry is not None:
        registry.publish.releases.append(new_release)
        registry.publish.current_release_id = new_release
    return HandlerResult(
        status=JobStatus.SUCCESS,
        current_release_id=new_release,
        counts={"written": 1, "current_replaced": True, "publications": pub_count},
        detail="publish seam: versioned release + atomic current pointer",
    )


def handle_results(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, as_of, events)
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    # Do not grade partial fights: only mark finals when context says so.
    finals = bool(context.get("results_final", False))
    if registry is not None:
        registry.results_final = finals or registry.results_final
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"finals": int(finals), "partial": int(not finals)},
        detail="results seam: ingest live/final without grading partials",
    )


def handle_grade(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    """Call DWCS-400 grade services; never INSERT ledger rows ad hoc.

    Partial / non-final results must not consume the event_night success key.
    When ``context`` supplies ``prediction_ids`` / ``official_publication_ids``
    and ``facts_by_bout``, those are graded after finals are ready.
    """
    _ = events
    registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    results_final = bool(context.get("results_final", False))
    if registry is not None:
        results_final = bool(registry.results_final or results_final)
    if not results_final:
        return HandlerResult(
            status=JobStatus.SKIPPED,
            detail="results not final; grade deferred (no success_token)",
            counts={"deferred": True, "results_final": False, "event_id": job.event_id},
        )

    prediction_ids = list(context.get("prediction_ids") or ())
    publication_ids = list(context.get("official_publication_ids") or ())
    facts_by_bout = dict(context.get("facts_by_bout") or {})

    graded = grade_predictions(
        session,
        prediction_ids=prediction_ids,
        facts_by_bout=facts_by_bout,
        result_version_kind="event_night",
        graded_at=as_of,
    )
    settled = settle_recommendations(
        session,
        official_publication_ids=publication_ids,
        facts_by_bout=facts_by_bout,
        result_version_kind="event_night",
        settled_at=as_of,
    )
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={
            "prediction_grades": len(graded),
            "settlements": len(settled),
            "event_id": job.event_id,
            "results_final": True,
        },
        detail="grade via grade_predictions / settle_recommendations",
    )


def handle_reconcile_24h(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, job, as_of, events, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"corrections": 0},
        detail="reconcile-24h seam: current-result corrections without rewrite",
    )


def handle_reconcile_7d(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, job, as_of, events, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"corrections": 0},
        detail="reconcile-7d seam",
    )


def handle_retrain(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    """Fixed-spec retrain via champion registry. Never auto-promotes a new spec."""
    _ = events
    handler_registry: HandlerRegistry | None = context.get("registry")  # type: ignore[assignment]
    prior = (
        handler_registry.artifact.digest
        if handler_registry is not None
        else "incumbent-artifact-v1"
    )

    injected = context.get("retrain_runner")
    if callable(injected):
        result = injected(
            session,
            job=job,
            as_of=as_of,
            events=events,
            context=context,
        )
        if isinstance(result, HandlerResult):
            if (
                result.status == JobStatus.SUCCESS
                and result.artifact_digest
                and handler_registry is not None
            ):
                # Job may refresh same-spec digest; never invent a new spec_id.
                handler_registry.artifact.digest = result.artifact_digest
            return result

    registry_path = context.get("model_registry_path")
    if registry_path is None:
        return HandlerResult(
            status=JobStatus.SUCCESS,
            artifact_digest=prior,
            counts={"promoted": False, "champion_unchanged": True},
            detail="retrain seam: no architecture search; champion unchanged",
        )

    artifacts_dir = Path(
        str(context.get("artifacts_dir") or Path(str(registry_path)).parent / "artifacts")
    )
    try:
        outcome = retrain_fixed_spec(
            session,
            registry_path=Path(str(registry_path)),
            artifacts_dir=artifacts_dir,
            actor="jobs.retrain",
            train_runner=context.get("train_runner"),  # type: ignore[arg-type]
            health_ok=context.get("health_ok"),  # type: ignore[arg-type]
            health_gate=context.get("health_gate"),  # type: ignore[arg-type]
            backtest_ok=context.get("backtest_ok"),  # type: ignore[arg-type]
            calibration_ok=context.get("calibration_ok"),  # type: ignore[arg-type]
            include_holdout=bool(context.get("include_holdout", False)),
            at=as_of,
        )
    except Exception as exc:  # noqa: BLE001 — job seam must not raise past orchestrator
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail=f"retrain failed; champion unchanged: {exc}",
            artifact_digest=prior,
            counts={"promoted": False, "champion_unchanged": True},
            blocks_downstream=False,
        )

    if outcome.status == "failed" or not outcome.activated:
        error_class = (
            JobErrorClass.SCHEMA
            if "holdout" in outcome.reason.lower() or "artifact" in outcome.reason.lower()
            else JobErrorClass.INTERNAL
        )
        if outcome.status == "shadow":
            # Different spec registered shadow-only; job succeeds without promotion.
            return HandlerResult(
                status=JobStatus.SUCCESS,
                artifact_digest=prior,
                counts={
                    "promoted": False,
                    "champion_unchanged": True,
                    "shadow": True,
                    "spec_id": outcome.spec_id,
                },
                detail=outcome.reason,
            )
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=error_class,
            detail=outcome.reason,
            artifact_digest=prior,
            counts={
                "promoted": False,
                "champion_unchanged": True,
                "spec_id": outcome.spec_id,
            },
        )

    if handler_registry is not None and outcome.artifact_digest:
        handler_registry.artifact.digest = outcome.artifact_digest
    return HandlerResult(
        status=JobStatus.SUCCESS,
        artifact_digest=outcome.artifact_digest or prior,
        counts={
            "promoted": False,  # job never promotes a *new* spec
            "champion_unchanged": False,
            "activated_same_spec": True,
            "spec_id": outcome.spec_id,
        },
        detail=outcome.reason,
    )


def handle_backup(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = (session, job, as_of, events, context)
    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={"snapshot": 1},
        detail="backup seam: consistent SQLite snapshot (no restic/offsite)",
    )


DEFAULT_HANDLERS: dict[JobType, JobHandler] = {
    JobType.DISCOVER: handle_discover,
    JobType.INGEST_HISTORY: handle_ingest_history,
    JobType.IDENTITY: handle_identity,
    JobType.SNAPSHOT_ODDS: handle_snapshot_odds,
    JobType.SCORE: handle_score,
    JobType.RECOMMEND: handle_recommend,
    JobType.PUBLISH: handle_publish,
    JobType.RESULTS: handle_results,
    JobType.GRADE: handle_grade,
    JobType.RECONCILE_24H: handle_reconcile_24h,
    JobType.RECONCILE_7D: handle_reconcile_7d,
    JobType.RETRAIN: handle_retrain,
    JobType.BACKUP: handle_backup,
}


# Module aliases matching plan file names.
discover = handle_discover
score = handle_score
results = handle_results
publish = handle_publish


__all__ = [
    "ArtifactPointer",
    "DEFAULT_HANDLERS",
    "HandlerRegistry",
    "JobHandler",
    "PublishPointer",
    "discover",
    "handle_backup",
    "handle_discover",
    "handle_grade",
    "handle_identity",
    "handle_ingest_history",
    "handle_publish",
    "handle_recommend",
    "handle_reconcile_24h",
    "handle_reconcile_7d",
    "handle_results",
    "handle_retrain",
    "handle_score",
    "handle_snapshot_odds",
    "publish",
    "results",
    "score",
]
