"""Durable idempotency ledger for odds snapshot jobs (DWCS-205)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds_jobs import OddsSnapshotJobRun
from mma_model.odds.normalize import ensure_utc


class JobLedgerDuplicate(RuntimeError):
    """Logical snapshot already succeeded for this idempotency key."""


def find_successful_run(
    session: Session, *, idempotency_key: str
) -> OddsSnapshotJobRun | None:
    return session.scalar(
        select(OddsSnapshotJobRun).where(
            OddsSnapshotJobRun.idempotency_key == idempotency_key,
            OddsSnapshotJobRun.success_token == 1,
        )
    )


def record_job_run(
    session: Session,
    *,
    idempotency_key: str,
    job_name: str,
    status: str,
    provider: str,
    region: str,
    markets: str,
    event_id: str,
    mode: str,
    as_of: datetime,
    estimated_cost: int = 0,
    actual_cost: int | None = None,
    requested_cutoff: datetime | None = None,
    snapshot_at: datetime | None = None,
    window_name: str | None = None,
    error_class: str | None = None,
    detail: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> OddsSnapshotJobRun:
    """Persist a job run. Successful keys are unique; retries hit duplicate."""
    stamp = ensure_utc(as_of, field="as_of")
    started = ensure_utc(started_at or stamp, field="started_at")
    finished = (
        None if finished_at is None else ensure_utc(finished_at, field="finished_at")
    )
    cutoff = (
        None
        if requested_cutoff is None
        else ensure_utc(requested_cutoff, field="requested_cutoff")
    )
    snap = None if snapshot_at is None else ensure_utc(snapshot_at, field="snapshot_at")

    if status == "success":
        existing = find_successful_run(session, idempotency_key=idempotency_key)
        if existing is not None:
            raise JobLedgerDuplicate(
                f"idempotency key already succeeded: {idempotency_key}"
            )
        success_token = 1
    else:
        success_token = None

    row = OddsSnapshotJobRun(
        idempotency_key=idempotency_key,
        success_token=success_token,
        job_name=job_name,
        status=status,
        provider=provider,
        region=region,
        markets=markets,
        event_id=event_id,
        mode=mode,
        as_of=stamp,
        requested_cutoff=cutoff,
        snapshot_at=snap,
        window_name=window_name,
        estimated_cost=int(estimated_cost),
        actual_cost=actual_cost,
        error_class=error_class,
        detail=detail,
        started_at=started,
        finished_at=finished or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def last_success_at_for_event(
    session: Session,
    *,
    event_id: str,
    provider: str,
    region: str,
    markets: str,
) -> datetime | None:
    row = session.scalar(
        select(OddsSnapshotJobRun)
        .where(
            OddsSnapshotJobRun.event_id == event_id,
            OddsSnapshotJobRun.provider == provider,
            OddsSnapshotJobRun.region == region,
            OddsSnapshotJobRun.markets == markets,
            OddsSnapshotJobRun.success_token == 1,
        )
        .order_by(OddsSnapshotJobRun.finished_at.desc(), OddsSnapshotJobRun.created_at.desc())
        .limit(1)
    )
    if row is None or row.finished_at is None:
        return None
    return ensure_utc(row.finished_at, field="finished_at")


__all__ = [
    "JobLedgerDuplicate",
    "find_successful_run",
    "last_success_at_for_event",
    "record_job_run",
]
