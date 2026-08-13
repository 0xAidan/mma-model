"""Shared types for the DWCS-401 event-relative job orchestrator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class JobType(StrEnum):
    DISCOVER = "discover"
    INGEST_HISTORY = "ingest-history"
    IDENTITY = "identity"
    SNAPSHOT_ODDS = "snapshot-odds"
    SCORE = "score"
    RECOMMEND = "recommend"
    PUBLISH = "publish"
    RESULTS = "results"
    GRADE = "grade"
    RECONCILE_24H = "reconcile-24h"
    RECONCILE_7D = "reconcile-7d"
    RETRAIN = "retrain"
    BACKUP = "backup"


class JobErrorClass(StrEnum):
    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    ENTITLEMENT = "entitlement"
    SCHEMA = "schema"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    STALE_QUOTE = "stale_quote"
    MISSING_ODDS = "missing_odds"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    OVERLAP = "overlap"
    INTERNAL = "internal"


class JobStatus(StrEnum):
    STARTED = "started"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DEPENDENCY_BLOCKED = "dependency_blocked"


# Jobs that must not be retried after a single definitive failure.
NON_RETRYABLE_ERRORS = frozenset(
    {
        JobErrorClass.AUTHENTICATION,
        JobErrorClass.ENTITLEMENT,
        JobErrorClass.SCHEMA,
        JobErrorClass.IDENTITY_UNRESOLVED,
        JobErrorClass.OVERLAP,
        JobErrorClass.DEPENDENCY_BLOCKED,
    }
)

DEFAULT_MAX_TRANSIENT_ATTEMPTS = 3

# Stable execution order when multiple job types are due on the same tick.
JOB_EXECUTION_ORDER: tuple[JobType, ...] = (
    JobType.DISCOVER,
    JobType.INGEST_HISTORY,
    JobType.IDENTITY,
    JobType.SNAPSHOT_ODDS,
    JobType.SCORE,
    JobType.RECOMMEND,
    JobType.PUBLISH,
    JobType.RESULTS,
    JobType.GRADE,
    JobType.RECONCILE_24H,
    JobType.RECONCILE_7D,
    JobType.RETRAIN,
    JobType.BACKUP,
)

# Declared dependencies (job_type -> required successful upstream types).
# Bout-scoped identity is enforced separately inside score/recommend handlers.
JOB_DEPENDENCIES: Mapping[JobType, tuple[JobType, ...]] = {
    JobType.DISCOVER: (),
    JobType.INGEST_HISTORY: (JobType.DISCOVER,),
    JobType.IDENTITY: (JobType.INGEST_HISTORY,),
    JobType.SNAPSHOT_ODDS: (),
    JobType.SCORE: (),
    JobType.RECOMMEND: (JobType.SCORE,),
    JobType.PUBLISH: (JobType.RECOMMEND,),
    JobType.RESULTS: (),
    JobType.GRADE: (JobType.RESULTS,),
    JobType.RECONCILE_24H: (JobType.GRADE,),
    JobType.RECONCILE_7D: (JobType.RECONCILE_24H,),
    JobType.RETRAIN: (JobType.RECONCILE_24H,),
    JobType.BACKUP: (),
}


@dataclass(frozen=True)
class EventContext:
    """Canonical upcoming/active event used for due-job calculation."""

    event_id: str
    event_start: datetime
    bout_ids: tuple[str, ...] = ()
    series: str = "dwcs"


@dataclass(frozen=True)
class DueJob:
    """One scheduled unit of work for a single tick."""

    job_type: JobType
    idempotency_key: str
    dependencies: tuple[JobType, ...]
    event_id: str | None = None
    bout_id: str | None = None
    scope: str = "event"
    window_slot: str | None = None
    series: str = "dwcs"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_type": self.job_type.value,
            "idempotency_key": self.idempotency_key,
            "dependencies": [dep.value for dep in self.dependencies],
            "event_id": self.event_id,
            "bout_id": self.bout_id,
            "scope": self.scope,
            "window_slot": self.window_slot,
            "series": self.series,
        }


@dataclass
class HandlerResult:
    """Outcome returned by an injectable job handler."""

    status: JobStatus = JobStatus.SUCCESS
    error_class: JobErrorClass | None = None
    detail: str = ""
    counts: dict[str, Any] = field(default_factory=dict)
    source_quota: str | None = None
    # Bout-level blocks that should not fail the whole card.
    blocked_bout_ids: tuple[str, ...] = ()
    # Artifact / publish pointer safety signals for tests and callers.
    artifact_digest: str | None = None
    current_release_id: str | None = None
    # When True, orchestrator must not treat this as a successful dep for
    # card-level publish (e.g. score failed but prior artifact retained).
    blocks_downstream: bool = False


@dataclass(frozen=True)
class TickJobResult:
    job_type: str
    idempotency_key: str
    status: str
    event_id: str | None = None
    bout_id: str | None = None
    error_class: str | None = None
    detail: str = ""
    attempt: int = 1
    duration_ms: int | None = None
    counts: Mapping[str, Any] = field(default_factory=dict)
    blocked_bout_ids: tuple[str, ...] = ()
    artifact_digest: str | None = None
    current_release_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["counts"] = dict(self.counts)
        payload["blocked_bout_ids"] = list(self.blocked_bout_ids)
        return payload


@dataclass(frozen=True)
class TickResult:
    as_of: str
    dry_run: bool
    due: tuple[DueJob, ...]
    executed: tuple[TickJobResult, ...] = ()
    failures: int = 0
    overlap: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "dry_run": self.dry_run,
            "due": [job.as_dict() for job in self.due],
            "executed": [row.as_dict() for row in self.executed],
            "failures": self.failures,
            "overlap": self.overlap,
        }

    def dry_run_plan(self) -> dict[str, Any]:
        """Deterministic JSON plan with sorted keys for CLI dry-run."""
        return {
            "as_of": self.as_of,
            "dependencies": {
                job.idempotency_key: [dep.value for dep in job.dependencies]
                for job in self.due
            },
            "dry_run": True,
            "due": [job.as_dict() for job in self.due],
            "idempotency_keys": [job.idempotency_key for job in self.due],
            "job_types": [job.job_type.value for job in self.due],
        }


def sort_due_jobs(jobs: Sequence[DueJob]) -> tuple[DueJob, ...]:
    order = {job_type: index for index, job_type in enumerate(JOB_EXECUTION_ORDER)}

    def _key(job: DueJob) -> tuple[int, str, str, str]:
        return (
            order.get(job.job_type, 999),
            job.event_id or "",
            job.bout_id or "",
            job.idempotency_key,
        )

    return tuple(sorted(jobs, key=_key))


__all__ = [
    "DEFAULT_MAX_TRANSIENT_ATTEMPTS",
    "DueJob",
    "EventContext",
    "HandlerResult",
    "JOB_DEPENDENCIES",
    "JOB_EXECUTION_ORDER",
    "JobErrorClass",
    "JobStatus",
    "JobType",
    "NON_RETRYABLE_ERRORS",
    "TickJobResult",
    "TickResult",
    "sort_due_jobs",
]
