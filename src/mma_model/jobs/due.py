"""Event-relative due-job calculation (DWCS-401).

All windows use the explicit ``as_of`` / ``--now`` UTC timestamp. No hidden
wall-clock is consulted when deciding what is due.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from mma_model.jobs.types import (
    JOB_DEPENDENCIES,
    DueJob,
    EventContext,
    JobType,
    sort_due_jobs,
)
from mma_model.odds.normalize import ensure_utc

DEFAULT_BACKUP_HOUR_UTC = 6
DEFAULT_OFFICIAL_OFFSET_MIN = 60
DEFAULT_SCORE_READY_OFFSET_MIN = 61
DEFAULT_ODDS_OPEN_HOURS = 72
DEFAULT_RESULTS_INTERVAL_MIN = 10
DEFAULT_RESULTS_WINDOW_HOURS = 4
DEFAULT_RECONCILE_24H_HOURS = 24
DEFAULT_RECONCILE_7D_DAYS = 7


@dataclass(frozen=True)
class OrchestratorCadence:
    """Cadence knobs loaded from ``config/jobs.yaml`` (with safe defaults)."""

    backup_hour_utc: int = DEFAULT_BACKUP_HOUR_UTC
    official_offset_minutes: int = DEFAULT_OFFICIAL_OFFSET_MIN
    score_ready_offset_minutes: int = DEFAULT_SCORE_READY_OFFSET_MIN
    odds_open_hours_before_start: int = DEFAULT_ODDS_OPEN_HOURS
    results_interval_minutes: int = DEFAULT_RESULTS_INTERVAL_MIN
    results_window_hours_after_start: int = DEFAULT_RESULTS_WINDOW_HOURS
    reconcile_24h_hours: int = DEFAULT_RECONCILE_24H_HOURS
    reconcile_7d_days: int = DEFAULT_RECONCILE_7D_DAYS
    series: str = "dwcs"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> OrchestratorCadence:
        data = dict(raw or {})
        cadence = dict(data.get("cadence") or data)
        return cls(
            backup_hour_utc=int(
                cadence.get("backup_hour_utc", DEFAULT_BACKUP_HOUR_UTC)
            ),
            official_offset_minutes=int(
                cadence.get("official_offset_minutes", DEFAULT_OFFICIAL_OFFSET_MIN)
            ),
            score_ready_offset_minutes=int(
                cadence.get(
                    "score_ready_offset_minutes", DEFAULT_SCORE_READY_OFFSET_MIN
                )
            ),
            odds_open_hours_before_start=int(
                cadence.get(
                    "odds_open_hours_before_start", DEFAULT_ODDS_OPEN_HOURS
                )
            ),
            results_interval_minutes=int(
                cadence.get("results_interval_minutes", DEFAULT_RESULTS_INTERVAL_MIN)
            ),
            results_window_hours_after_start=int(
                cadence.get(
                    "results_window_hours_after_start", DEFAULT_RESULTS_WINDOW_HOURS
                )
            ),
            reconcile_24h_hours=int(
                cadence.get("reconcile_24h_hours", DEFAULT_RECONCILE_24H_HOURS)
            ),
            reconcile_7d_days=int(
                cadence.get("reconcile_7d_days", DEFAULT_RECONCILE_7D_DAYS)
            ),
            series=str(cadence.get("series") or data.get("series") or "dwcs"),
        )


def load_orchestrator_cadence(path: Path | None = None) -> OrchestratorCadence:
    if path is None:
        path = Path(__file__).resolve().parents[3] / "config" / "jobs.yaml"
    if not path.is_file():
        return OrchestratorCadence()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    orch = payload.get("orchestrator") or {}
    return OrchestratorCadence.from_mapping(orch)


def _date_bucket(as_of: datetime) -> str:
    return ensure_utc(as_of, field="as_of").date().isoformat()


def _deps(job_type: JobType) -> tuple[JobType, ...]:
    return JOB_DEPENDENCIES.get(job_type, ())


def _series_key(job_type: JobType, *, series: str, as_of: datetime) -> str:
    return f"{job_type.value}:{series}:{_date_bucket(as_of)}"


def _event_key(
    job_type: JobType,
    *,
    event_id: str,
    slot: str | None = None,
) -> str:
    if slot:
        return f"{job_type.value}:{event_id}:{slot}"
    return f"{job_type.value}:{event_id}"


def _results_slot(event_start: datetime, as_of: datetime, interval_min: int) -> str:
    start = ensure_utc(event_start, field="event_start")
    now = ensure_utc(as_of, field="as_of")
    elapsed = max(0, int((now - start).total_seconds()))
    bucket = (elapsed // (interval_min * 60)) * interval_min
    return f"tplus_{bucket:04d}m"


def compute_due_jobs(
    *,
    as_of: datetime,
    events: Sequence[EventContext] = (),
    cadence: OrchestratorCadence | None = None,
    include_series_daily: bool = True,
) -> tuple[DueJob, ...]:
    """Return due jobs for ``as_of`` in stable execution order.

    Series-level daily jobs (discover / ingest-history) are due for the UTC
    calendar day of ``as_of``. Event-relative jobs use the event start and the
    documented offsets in ``OrchestratorCadence`` / ``config/jobs.yaml``.
    """
    stamp = ensure_utc(as_of, field="as_of")
    cfg = cadence or OrchestratorCadence()
    series = cfg.series
    due: list[DueJob] = []

    if include_series_daily:
        due.append(
            DueJob(
                job_type=JobType.DISCOVER,
                idempotency_key=_series_key(
                    JobType.DISCOVER, series=series, as_of=stamp
                ),
                dependencies=_deps(JobType.DISCOVER),
                scope="series",
                series=series,
                window_slot=_date_bucket(stamp),
            )
        )
        due.append(
            DueJob(
                job_type=JobType.INGEST_HISTORY,
                idempotency_key=_series_key(
                    JobType.INGEST_HISTORY, series=series, as_of=stamp
                ),
                dependencies=_deps(JobType.INGEST_HISTORY),
                scope="series",
                series=series,
                window_slot=_date_bucket(stamp),
            )
        )

    if stamp.hour == cfg.backup_hour_utc:
        due.append(
            DueJob(
                job_type=JobType.BACKUP,
                idempotency_key=_series_key(
                    JobType.BACKUP, series=series, as_of=stamp
                ),
                dependencies=_deps(JobType.BACKUP),
                scope="series",
                series=series,
                window_slot=_date_bucket(stamp),
            )
        )

    for event in events:
        due.extend(_event_due_jobs(event=event, as_of=stamp, cadence=cfg))

    return sort_due_jobs(due)


def _event_due_jobs(
    *,
    event: EventContext,
    as_of: datetime,
    cadence: OrchestratorCadence,
) -> list[DueJob]:
    start = ensure_utc(event.event_start, field="event_start")
    series = event.series or cadence.series
    event_id = event.event_id
    odds_open = start - timedelta(hours=cadence.odds_open_hours_before_start)
    score_ready = start - timedelta(minutes=cadence.score_ready_offset_minutes)
    official = start - timedelta(minutes=cadence.official_offset_minutes)
    results_end = start + timedelta(hours=cadence.results_window_hours_after_start)
    reconcile_24h = start + timedelta(hours=cadence.reconcile_24h_hours)
    reconcile_7d = start + timedelta(days=cadence.reconcile_7d_days)

    jobs: list[DueJob] = []

    # Pre-event identity enrichment from odds-open through first bell.
    if odds_open <= as_of < start:
        jobs.append(
            DueJob(
                job_type=JobType.IDENTITY,
                idempotency_key=_event_key(
                    JobType.IDENTITY,
                    event_id=event_id,
                    slot=_date_bucket(as_of),
                ),
                dependencies=_deps(JobType.IDENTITY),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=_date_bucket(as_of),
            )
        )

    # Odds live window: T-72h through (but not including) event start.
    # Slot dedup lives in the existing DWCS-205 snapshot-odds job.
    if odds_open <= as_of < start:
        jobs.append(
            DueJob(
                job_type=JobType.SNAPSHOT_ODDS,
                idempotency_key=_event_key(
                    JobType.SNAPSHOT_ODDS,
                    event_id=event_id,
                    slot=as_of.replace(second=0, microsecond=0).isoformat(),
                ),
                dependencies=_deps(JobType.SNAPSHOT_ODDS),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=as_of.replace(second=0, microsecond=0).isoformat(),
            )
        )

    # Score ready from T-61m until event start.
    if score_ready <= as_of < start:
        jobs.append(
            DueJob(
                job_type=JobType.SCORE,
                idempotency_key=_event_key(
                    JobType.SCORE,
                    event_id=event_id,
                    slot=official.isoformat(),
                ),
                dependencies=_deps(JobType.SCORE),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=official.isoformat(),
            )
        )

    # Official recommend + publish from T-60m until event start.
    if official <= as_of < start:
        jobs.append(
            DueJob(
                job_type=JobType.RECOMMEND,
                idempotency_key=_event_key(
                    JobType.RECOMMEND,
                    event_id=event_id,
                    slot=f"t60:{official.isoformat()}",
                ),
                dependencies=_deps(JobType.RECOMMEND),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=official.isoformat(),
            )
        )
        jobs.append(
            DueJob(
                job_type=JobType.PUBLISH,
                idempotency_key=_event_key(
                    JobType.PUBLISH,
                    event_id=event_id,
                    slot=f"t60:{official.isoformat()}",
                ),
                dependencies=_deps(JobType.PUBLISH),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=official.isoformat(),
            )
        )

    # Results every 10 minutes from start through +4h (inclusive).
    if start <= as_of <= results_end:
        slot = _results_slot(start, as_of, cadence.results_interval_minutes)
        jobs.append(
            DueJob(
                job_type=JobType.RESULTS,
                idempotency_key=_event_key(
                    JobType.RESULTS, event_id=event_id, slot=slot
                ),
                dependencies=_deps(JobType.RESULTS),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot=slot,
            )
        )
        jobs.append(
            DueJob(
                job_type=JobType.GRADE,
                idempotency_key=_event_key(
                    JobType.GRADE, event_id=event_id, slot="event_night"
                ),
                dependencies=_deps(JobType.GRADE),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot="event_night",
            )
        )

    if as_of >= reconcile_24h:
        # Allow grade to catch up before reconcile when the live window missed it.
        jobs.append(
            DueJob(
                job_type=JobType.GRADE,
                idempotency_key=_event_key(
                    JobType.GRADE, event_id=event_id, slot="event_night"
                ),
                dependencies=_deps(JobType.GRADE),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot="event_night",
            )
        )
        jobs.append(
            DueJob(
                job_type=JobType.RECONCILE_24H,
                idempotency_key=_event_key(JobType.RECONCILE_24H, event_id=event_id),
                dependencies=_deps(JobType.RECONCILE_24H),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot="plus_24h",
            )
        )
        jobs.append(
            DueJob(
                job_type=JobType.RETRAIN,
                idempotency_key=_event_key(
                    JobType.RETRAIN, event_id=event_id, slot="post-24h"
                ),
                dependencies=_deps(JobType.RETRAIN),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot="post-24h",
            )
        )

    if as_of >= reconcile_7d:
        jobs.append(
            DueJob(
                job_type=JobType.RECONCILE_7D,
                idempotency_key=_event_key(JobType.RECONCILE_7D, event_id=event_id),
                dependencies=_deps(JobType.RECONCILE_7D),
                event_id=event_id,
                scope="event",
                series=series,
                window_slot="plus_7d",
            )
        )

    return jobs


__all__ = [
    "DEFAULT_BACKUP_HOUR_UTC",
    "OrchestratorCadence",
    "compute_due_jobs",
    "load_orchestrator_cadence",
]
