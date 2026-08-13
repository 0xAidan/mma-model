"""Event-relative weekly scheduler tick (DWCS-401).

One idempotent ``jobs tick`` under a global overlap lock. Due work is computed
from canonical event timestamps and an explicit ``--now`` / ``as_of`` UTC.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.jobs.due import (
    OrchestratorCadence,
    compute_due_jobs,
    load_orchestrator_cadence,
)
from mma_model.jobs.handlers import HandlerRegistry
from mma_model.jobs.ledger import (
    count_attempts,
    find_successful_pipeline_run,
    has_successful_job_type,
    record_pipeline_job_run,
)
from mma_model.jobs.locking import (
    FileFlockLock,
    OverlapError,
    OverlapProtection,
    hold_overlap_lock,
)
from mma_model.jobs.types import (
    DEFAULT_MAX_TRANSIENT_ATTEMPTS,
    JOB_DEPENDENCIES,
    NON_RETRYABLE_ERRORS,
    DueJob,
    EventContext,
    HandlerResult,
    JobErrorClass,
    JobStatus,
    JobType,
    TickJobResult,
    TickResult,
)
from mma_model.odds.normalize import ensure_utc

SERIES_JOB_TYPES = frozenset(
    {JobType.DISCOVER, JobType.INGEST_HISTORY, JobType.BACKUP}
)


class TickOverlapError(OverlapError):
    """Concurrent tick rejected (fail closed)."""


class TickConfigurationError(ValueError):
    """Invalid tick inputs (naive datetime, live DB, etc.)."""


def _dependency_satisfied(
    session: Session,
    *,
    dep: JobType,
    job: DueJob,
    succeeded_types: set[tuple[JobType, str | None]],
) -> bool:
    event_key = None if dep in SERIES_JOB_TYPES else job.event_id

    if (dep, event_key) in succeeded_types:
        return True
    return has_successful_job_type(
        session,
        job_type=dep,
        event_id=event_key,
        series=job.series,
    )


def _check_dependencies(
    session: Session,
    *,
    job: DueJob,
    succeeded_types: set[tuple[JobType, str | None]],
) -> tuple[bool, tuple[JobType, ...]]:
    missing: list[JobType] = []
    for dep in JOB_DEPENDENCIES.get(job.job_type, ()):
        if not _dependency_satisfied(
            session, dep=dep, job=job, succeeded_types=succeeded_types
        ):
            missing.append(dep)
    return (not missing, tuple(missing))


def _apply_forced_failure(
    registry: HandlerRegistry | None,
    job: DueJob,
) -> HandlerResult | None:
    if registry is None:
        return None
    remaining = registry.transient_fail_remaining.get(job.job_type)
    if remaining is not None and remaining > 0:
        registry.transient_fail_remaining[job.job_type] = remaining - 1
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.TRANSIENT,
            detail=f"injected transient failure ({remaining} remaining before success)",
            blocks_downstream=True,
        )
    forced = registry.forced_failures.get(job.job_type)
    if forced is not None:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=forced,
            detail=f"injected definitive failure: {forced.value}",
            blocks_downstream=True,
        )
    return None


def _run_handler(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    registry: HandlerRegistry | None,
    context: Mapping[str, Any],
) -> HandlerResult:
    injected = _apply_forced_failure(registry, job)
    if injected is not None:
        return injected

    merged: dict[str, Any] = dict(context)
    if registry is not None:
        merged.setdefault("registry", registry)

    handler = (
        registry.get(job.job_type)
        if registry is not None
        else HandlerRegistry().get(job.job_type)
    )
    try:
        return handler(
            session,
            job=job,
            as_of=as_of,
            events=events,
            context=merged,
        )
    except Exception as exc:  # noqa: BLE001 - boundary: map to internal
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail=f"handler raised: {exc}",
            blocks_downstream=True,
        )


def _execute_job(
    session: Session,
    *,
    job: DueJob,
    as_of: datetime,
    events: Sequence[EventContext],
    registry: HandlerRegistry | None,
    context: Mapping[str, Any],
    max_transient_attempts: int,
) -> TickJobResult:
    existing = find_successful_pipeline_run(
        session, idempotency_key=job.idempotency_key
    )
    if existing is not None:
        return TickJobResult(
            job_type=job.job_type.value,
            idempotency_key=job.idempotency_key,
            status=JobStatus.SKIPPED.value,
            event_id=job.event_id,
            bout_id=job.bout_id,
            detail="already succeeded",
            attempt=existing.attempt,
            counts={"duplicate": True},
            duration_ms=existing.duration_ms,
        )

    attempts_prior = count_attempts(session, idempotency_key=job.idempotency_key)
    attempt = attempts_prior + 1

    started_at = as_of
    t0 = time.perf_counter()
    result = _run_handler(
        session,
        job=job,
        as_of=as_of,
        events=events,
        registry=registry,
        context=context,
    )

    status = result.status
    error_class = result.error_class
    detail = result.detail

    if status == JobStatus.FAILED and error_class is not None:
        if (
            error_class == JobErrorClass.TRANSIENT
            and attempt < max_transient_attempts
        ):
            # Bounded retry within this tick.
            while (
                attempt < max_transient_attempts
                and status == JobStatus.FAILED
                and error_class == JobErrorClass.TRANSIENT
            ):
                duration_ms = max(0, int((time.perf_counter() - t0) * 1000))
                record_pipeline_job_run(
                    session,
                    idempotency_key=job.idempotency_key,
                    job_type=job.job_type,
                    status=JobStatus.FAILED,
                    as_of=as_of,
                    started_at=started_at,
                    finished_at=as_of,
                    series=job.series,
                    event_id=job.event_id,
                    bout_id=job.bout_id,
                    scope=job.scope,
                    window_slot=job.window_slot,
                    attempt=attempt,
                    counts=result.counts,
                    source_quota=result.source_quota,
                    error_class=error_class,
                    detail=detail,
                    duration_ms=duration_ms,
                )
                attempt += 1
                t0 = time.perf_counter()
                result = _run_handler(
                    session,
                    job=job,
                    as_of=as_of,
                    events=events,
                    registry=registry,
                    context=context,
                )
                status = result.status
                error_class = result.error_class
                detail = result.detail

        if (
            status == JobStatus.FAILED
            and error_class in NON_RETRYABLE_ERRORS
            and error_class != JobErrorClass.TRANSIENT
        ):
            # Single definitive failure — do not retry.
            pass

    duration_ms = max(0, int((time.perf_counter() - t0) * 1000))

    if status == JobStatus.SUCCESS:
        record_pipeline_job_run(
            session,
            idempotency_key=job.idempotency_key,
            job_type=job.job_type,
            status=JobStatus.SUCCESS,
            as_of=as_of,
            started_at=started_at,
            finished_at=as_of,
            series=job.series,
            event_id=job.event_id,
            bout_id=job.bout_id,
            scope=job.scope,
            window_slot=job.window_slot,
            attempt=attempt,
            counts=result.counts,
            source_quota=result.source_quota,
            detail=detail or None,
            duration_ms=duration_ms,
        )
    elif status == JobStatus.FAILED:
        record_pipeline_job_run(
            session,
            idempotency_key=job.idempotency_key,
            job_type=job.job_type,
            status=JobStatus.FAILED,
            as_of=as_of,
            started_at=started_at,
            finished_at=as_of,
            series=job.series,
            event_id=job.event_id,
            bout_id=job.bout_id,
            scope=job.scope,
            window_slot=job.window_slot,
            attempt=attempt,
            counts=result.counts,
            source_quota=result.source_quota,
            error_class=error_class or JobErrorClass.INTERNAL,
            detail=detail or None,
            duration_ms=duration_ms,
        )
    elif status == JobStatus.DEPENDENCY_BLOCKED:
        record_pipeline_job_run(
            session,
            idempotency_key=job.idempotency_key,
            job_type=job.job_type,
            status=JobStatus.DEPENDENCY_BLOCKED,
            as_of=as_of,
            started_at=started_at,
            finished_at=as_of,
            series=job.series,
            event_id=job.event_id,
            bout_id=job.bout_id,
            scope=job.scope,
            window_slot=job.window_slot,
            attempt=attempt,
            counts=result.counts,
            error_class=JobErrorClass.DEPENDENCY_BLOCKED,
            detail=detail or None,
            duration_ms=duration_ms,
        )
    elif status == JobStatus.SKIPPED:
        # Deferred / not-ready (e.g. grade before finals). No success_token.
        record_pipeline_job_run(
            session,
            idempotency_key=job.idempotency_key,
            job_type=job.job_type,
            status=JobStatus.SKIPPED,
            as_of=as_of,
            started_at=started_at,
            finished_at=as_of,
            series=job.series,
            event_id=job.event_id,
            bout_id=job.bout_id,
            scope=job.scope,
            window_slot=job.window_slot,
            attempt=attempt,
            counts=result.counts,
            detail=detail or None,
            duration_ms=duration_ms,
        )

    return TickJobResult(
        job_type=job.job_type.value,
        idempotency_key=job.idempotency_key,
        status=status.value if isinstance(status, JobStatus) else str(status),
        event_id=job.event_id,
        bout_id=job.bout_id,
        error_class=(
            error_class.value
            if isinstance(error_class, JobErrorClass)
            else error_class
        ),
        detail=detail,
        attempt=attempt,
        duration_ms=duration_ms,
        counts=result.counts,
        blocked_bout_ids=result.blocked_bout_ids,
        artifact_digest=result.artifact_digest,
        current_release_id=result.current_release_id,
    )


def run_jobs_tick(
    session: Session | None,
    *,
    as_of: datetime,
    events: Sequence[EventContext] = (),
    dry_run: bool = False,
    lock: OverlapProtection | None = None,
    lock_path: Path | None = None,
    cadence: OrchestratorCadence | None = None,
    registry: HandlerRegistry | None = None,
    context: Mapping[str, Any] | None = None,
    include_series_daily: bool = True,
    max_transient_attempts: int = DEFAULT_MAX_TRANSIENT_ATTEMPTS,
    acquire_lock: bool = True,
) -> TickResult:
    """Compute and optionally execute due jobs for an explicit UTC ``as_of``.

    Dry-run performs no DB writes and does not invoke handler side effects.
    Concurrent ticks fail closed via ``OverlapError`` / ``TickOverlapError``.
    """
    stamp = ensure_utc(as_of, field="as_of")
    cfg = cadence or load_orchestrator_cadence()
    due = compute_due_jobs(
        as_of=stamp,
        events=events,
        cadence=cfg,
        include_series_daily=include_series_daily,
    )

    if dry_run:
        return TickResult(
            as_of=stamp.isoformat().replace("+00:00", "Z"),
            dry_run=True,
            due=due,
        )

    if session is None:
        raise TickConfigurationError("session is required when dry_run is False")

    ctx: MutableMapping[str, Any] = dict(context or {})
    active_registry = registry or HandlerRegistry()
    ctx.setdefault("registry", active_registry)

    overlap_lock = lock
    if overlap_lock is None and acquire_lock:
        path = lock_path or Path("/tmp/mma-jobs-tick.lock")
        overlap_lock = FileFlockLock(path)

    def _run() -> TickResult:
        executed: list[TickJobResult] = []
        failures = 0
        succeeded_types: set[tuple[JobType, str | None]] = set()
        # Seed from prior ledger successes relevant to this tick.
        for job in due:
            for dep in JOB_DEPENDENCIES.get(job.job_type, ()):
                event_key = None if dep in SERIES_JOB_TYPES else job.event_id
                if has_successful_job_type(
                    session,
                    job_type=dep,
                    event_id=event_key,
                    series=job.series,
                ):
                    succeeded_types.add((dep, event_key))

        blocked_publish_events: set[str] = set()

        for job in due:
            ok, missing = _check_dependencies(
                session, job=job, succeeded_types=succeeded_types
            )
            if not ok:
                record_pipeline_job_run(
                    session,
                    idempotency_key=job.idempotency_key,
                    job_type=job.job_type,
                    status=JobStatus.DEPENDENCY_BLOCKED,
                    as_of=stamp,
                    started_at=stamp,
                    finished_at=stamp,
                    series=job.series,
                    event_id=job.event_id,
                    bout_id=job.bout_id,
                    scope=job.scope,
                    window_slot=job.window_slot,
                    attempt=count_attempts(
                        session, idempotency_key=job.idempotency_key
                    )
                    + 1,
                    error_class=JobErrorClass.DEPENDENCY_BLOCKED,
                    detail=(
                        "blocked by missing deps: "
                        + ",".join(dep.value for dep in missing)
                    ),
                    duration_ms=0,
                )
                executed.append(
                    TickJobResult(
                        job_type=job.job_type.value,
                        idempotency_key=job.idempotency_key,
                        status=JobStatus.DEPENDENCY_BLOCKED.value,
                        event_id=job.event_id,
                        bout_id=job.bout_id,
                        error_class=JobErrorClass.DEPENDENCY_BLOCKED.value,
                        detail=(
                            "blocked by missing deps: "
                            + ",".join(dep.value for dep in missing)
                        ),
                    )
                )
                failures += 1
                continue

            # Card-level publish blocked when score failed for this event.
            if (
                job.job_type == JobType.PUBLISH
                and job.event_id
                and job.event_id in blocked_publish_events
            ):
                record_pipeline_job_run(
                    session,
                    idempotency_key=job.idempotency_key,
                    job_type=job.job_type,
                    status=JobStatus.DEPENDENCY_BLOCKED,
                    as_of=stamp,
                    started_at=stamp,
                    finished_at=stamp,
                    series=job.series,
                    event_id=job.event_id,
                    scope=job.scope,
                    window_slot=job.window_slot,
                    attempt=1,
                    error_class=JobErrorClass.DEPENDENCY_BLOCKED,
                    detail="publish blocked: upstream score/identity failed",
                    duration_ms=0,
                )
                executed.append(
                    TickJobResult(
                        job_type=job.job_type.value,
                        idempotency_key=job.idempotency_key,
                        status=JobStatus.DEPENDENCY_BLOCKED.value,
                        event_id=job.event_id,
                        error_class=JobErrorClass.DEPENDENCY_BLOCKED.value,
                        detail="publish blocked: upstream score/identity failed",
                    )
                )
                failures += 1
                continue

            # Recommend blocked when score failed for the card.
            if (
                job.job_type == JobType.RECOMMEND
                and job.event_id
                and job.event_id in blocked_publish_events
            ):
                record_pipeline_job_run(
                    session,
                    idempotency_key=job.idempotency_key,
                    job_type=job.job_type,
                    status=JobStatus.DEPENDENCY_BLOCKED,
                    as_of=stamp,
                    started_at=stamp,
                    finished_at=stamp,
                    series=job.series,
                    event_id=job.event_id,
                    scope=job.scope,
                    window_slot=job.window_slot,
                    attempt=1,
                    error_class=JobErrorClass.DEPENDENCY_BLOCKED,
                    detail="recommend blocked: upstream score/identity failed",
                    duration_ms=0,
                )
                executed.append(
                    TickJobResult(
                        job_type=job.job_type.value,
                        idempotency_key=job.idempotency_key,
                        status=JobStatus.DEPENDENCY_BLOCKED.value,
                        event_id=job.event_id,
                        error_class=JobErrorClass.DEPENDENCY_BLOCKED.value,
                        detail="recommend blocked: upstream score/identity failed",
                    )
                )
                failures += 1
                continue

            row = _execute_job(
                session,
                job=job,
                as_of=stamp,
                events=events,
                registry=active_registry,
                context=ctx,
                max_transient_attempts=max_transient_attempts,
            )
            executed.append(row)

            if row.status == JobStatus.SUCCESS.value or (
                row.status == JobStatus.SKIPPED.value
                and bool((row.counts or {}).get("duplicate"))
            ):
                event_key = None if job.job_type in SERIES_JOB_TYPES else job.event_id
                succeeded_types.add((job.job_type, event_key))
            elif row.status == JobStatus.SKIPPED.value:
                # Deferred / not-ready (e.g. grade before finals): do not
                # satisfy deps and do not count as a hard tick failure.
                pass
            else:
                failures += 1
                blocks_card = (
                    job.event_id
                    and row.status == JobStatus.FAILED.value
                    and (
                        job.job_type == JobType.SCORE
                        or (
                            job.job_type == JobType.IDENTITY
                            and row.error_class
                            == JobErrorClass.IDENTITY_UNRESOLVED.value
                        )
                        or (
                            job.job_type == JobType.RECOMMEND
                            and row.error_class
                            == JobErrorClass.IDENTITY_UNRESOLVED.value
                        )
                    )
                )
                if blocks_card and job.event_id:
                    blocked_publish_events.add(job.event_id)

        return TickResult(
            as_of=stamp.isoformat().replace("+00:00", "Z"),
            dry_run=False,
            due=due,
            executed=tuple(executed),
            failures=failures,
        )

    if overlap_lock is None:
        return _run()

    try:
        with hold_overlap_lock(overlap_lock):
            return _run()
    except OverlapError as exc:
        raise TickOverlapError(str(exc)) from exc


def dry_run_plan_json(
    *,
    as_of: datetime,
    events: Sequence[EventContext] = (),
    cadence: OrchestratorCadence | None = None,
    include_series_daily: bool = True,
) -> str:
    result = run_jobs_tick(
        None,
        as_of=as_of,
        events=events,
        dry_run=True,
        cadence=cadence,
        include_series_daily=include_series_daily,
        acquire_lock=False,
    )
    return json.dumps(result.dry_run_plan(), sort_keys=True, indent=2)


__all__ = [
    "SERIES_JOB_TYPES",
    "TickConfigurationError",
    "TickOverlapError",
    "dry_run_plan_json",
    "run_jobs_tick",
]
