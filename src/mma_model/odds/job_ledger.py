"""Durable idempotency ledger for odds snapshot jobs (DWCS-205)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds_jobs import OddsSnapshotJobRun
from mma_model.odds.normalize import ensure_utc


class JobLedgerDuplicate(RuntimeError):
    """Logical snapshot already succeeded for this idempotency key."""


class JobLedgerTimeError(ValueError):
    """Invalid explicit job timing / cutoff ordering."""


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
    finished_at: datetime,
    estimated_cost: int = 0,
    actual_cost: int | None = None,
    actual_cost_source: str | None = None,
    requested_cutoff: datetime | None = None,
    snapshot_at: datetime | None = None,
    window_name: str | None = None,
    error_class: str | None = None,
    detail: str | None = None,
    started_at: datetime | None = None,
) -> OddsSnapshotJobRun:
    """Persist a job run using explicit completion time (no hidden wall clock)."""
    stamp = ensure_utc(as_of, field="as_of")
    started = ensure_utc(started_at or stamp, field="started_at")
    finished = ensure_utc(finished_at, field="finished_at")
    if finished < started:
        raise JobLedgerTimeError("finished_at must be >= started_at")
    cutoff = (
        None
        if requested_cutoff is None
        else ensure_utc(requested_cutoff, field="requested_cutoff")
    )
    snap = None if snapshot_at is None else ensure_utc(snapshot_at, field="snapshot_at")
    if snap is not None and cutoff is not None and snap > cutoff:
        raise JobLedgerTimeError("snapshot_at must be <= requested_cutoff")

    if status == "success":
        existing = find_successful_run(session, idempotency_key=idempotency_key)
        if existing is not None:
            raise JobLedgerDuplicate(
                f"idempotency key already succeeded: {idempotency_key}"
            )
        success_token = 1
    else:
        success_token = None

    if actual_cost is not None and actual_cost < 0:
        raise ValueError("actual_cost must be nonnegative when present")

    source = actual_cost_source
    if actual_cost is not None and source is None:
        raise ValueError("actual_cost requires actual_cost_source provenance")
    if actual_cost is None and source not in {None, "missing"}:
        raise ValueError("missing actual_cost requires source None or 'missing'")

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
        actual_cost_source=source,
        error_class=error_class,
        detail=detail,
        started_at=started,
        finished_at=finished,
    )
    session.add(row)
    session.flush()
    return row


def slot_succeeded(
    session: Session,
    *,
    idempotency_key: str,
) -> bool:
    return find_successful_run(session, idempotency_key=idempotency_key) is not None


__all__ = [
    "JobLedgerDuplicate",
    "JobLedgerTimeError",
    "find_successful_run",
    "record_job_run",
    "slot_succeeded",
]
