"""Pipeline job-run ledger helpers (DWCS-401)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.pipeline_jobs import PipelineJobRun
from mma_model.jobs.types import JobErrorClass, JobStatus, JobType
from mma_model.odds.normalize import ensure_utc


class PipelineLedgerDuplicate(RuntimeError):
    """Logical pipeline job already succeeded for this idempotency key."""


class PipelineLedgerIntegrityError(ValueError):
    """Invalid ledger write before DB persistence."""


def find_successful_pipeline_run(
    session: Session, *, idempotency_key: str
) -> PipelineJobRun | None:
    return session.scalar(
        select(PipelineJobRun).where(
            PipelineJobRun.idempotency_key == idempotency_key,
            PipelineJobRun.success_token == 1,
        )
    )


def count_attempts(session: Session, *, idempotency_key: str) -> int:
    value = session.scalar(
        select(func.count()).select_from(PipelineJobRun).where(
            PipelineJobRun.idempotency_key == idempotency_key
        )
    )
    return int(value or 0)


def has_successful_job_type(
    session: Session,
    *,
    job_type: JobType | str,
    event_id: str | None = None,
    series: str = "dwcs",
) -> bool:
    job_value = job_type.value if isinstance(job_type, JobType) else str(job_type)
    stmt = select(PipelineJobRun.id).where(
        PipelineJobRun.job_type == job_value,
        PipelineJobRun.success_token == 1,
        PipelineJobRun.series == series,
    )
    if event_id is None:
        stmt = stmt.where(PipelineJobRun.event_id.is_(None))
    else:
        stmt = stmt.where(PipelineJobRun.event_id == event_id)
    return session.scalar(stmt.limit(1)) is not None


def record_pipeline_job_run(
    session: Session,
    *,
    idempotency_key: str,
    job_type: JobType | str,
    status: JobStatus | str,
    as_of: datetime,
    finished_at: datetime,
    started_at: datetime | None = None,
    series: str = "dwcs",
    event_id: str | None = None,
    bout_id: str | None = None,
    scope: str = "event",
    window_slot: str | None = None,
    attempt: int = 1,
    counts: Mapping[str, Any] | None = None,
    source_quota: str | None = None,
    error_class: JobErrorClass | str | None = None,
    detail: str | None = None,
    duration_ms: int | None = None,
) -> PipelineJobRun:
    """Persist a pipeline job run using explicit completion time (no wall clock)."""
    stamp = ensure_utc(as_of, field="as_of")
    started = ensure_utc(started_at or stamp, field="started_at")
    finished = ensure_utc(finished_at, field="finished_at")
    if finished < started:
        raise PipelineLedgerIntegrityError("finished_at must be >= started_at")
    if attempt < 1:
        raise PipelineLedgerIntegrityError("attempt must be >= 1")
    if duration_ms is not None and duration_ms < 0:
        raise PipelineLedgerIntegrityError("duration_ms must be nonnegative")

    job_value = job_type.value if isinstance(job_type, JobType) else str(job_type)
    status_value = status.value if isinstance(status, JobStatus) else str(status)
    error_value = (
        None
        if error_class is None
        else (
            error_class.value
            if isinstance(error_class, JobErrorClass)
            else str(error_class)
        )
    )

    if status_value == JobStatus.SUCCESS.value:
        existing = find_successful_pipeline_run(
            session, idempotency_key=idempotency_key
        )
        if existing is not None:
            raise PipelineLedgerDuplicate(
                f"idempotency key already succeeded: {idempotency_key}"
            )
        success_token = 1
    else:
        success_token = None

    if status_value == JobStatus.FAILED.value and not (
        error_value and str(error_value).strip()
    ):
        raise PipelineLedgerIntegrityError("failed status requires error_class")

    counts_json = None if counts is None else json.dumps(dict(counts), sort_keys=True)
    row = PipelineJobRun(
        idempotency_key=idempotency_key,
        success_token=success_token,
        job_type=job_value,
        status=status_value,
        series=series,
        event_id=event_id,
        bout_id=bout_id,
        scope=scope,
        as_of=stamp,
        window_slot=window_slot,
        attempt=attempt,
        counts_json=counts_json,
        source_quota=source_quota,
        error_class=error_value,
        detail=detail,
        duration_ms=duration_ms,
        started_at=started,
        finished_at=finished,
        created_at=finished,
    )
    session.add(row)
    session.flush()
    return row


__all__ = [
    "PipelineLedgerDuplicate",
    "PipelineLedgerIntegrityError",
    "count_attempts",
    "find_successful_pipeline_run",
    "has_successful_job_type",
    "record_pipeline_job_run",
]
