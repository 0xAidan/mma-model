"""Durable idempotency ledger for odds snapshot jobs (DWCS-205)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds_jobs import OddsSnapshotJobRun
from mma_model.odds.normalize import ensure_utc

BATCH_EVENT_ID = "__batch__"


class JobLedgerDuplicate(RuntimeError):
    """Logical snapshot already succeeded for this idempotency key."""


class JobLedgerTimeError(ValueError):
    """Invalid explicit job timing / cutoff ordering."""


class JobLedgerIntegrityError(ValueError):
    """Status-specific / ID-list integrity failure before DB write."""


def batch_idempotency_key(batch_key: str) -> str:
    """Durable logical identity for a sport-wide paid batch slot."""
    value = str(batch_key).strip()
    if not value:
        raise ValueError("batch_key must be non-empty")
    return f"odds_batch:{value}"


def find_successful_run(
    session: Session, *, idempotency_key: str
) -> OddsSnapshotJobRun | None:
    return session.scalar(
        select(OddsSnapshotJobRun).where(
            OddsSnapshotJobRun.idempotency_key == idempotency_key,
            OddsSnapshotJobRun.success_token == 1,
        )
    )


def find_successful_batch_run(
    session: Session, *, batch_key: str
) -> OddsSnapshotJobRun | None:
    return find_successful_run(
        session, idempotency_key=batch_idempotency_key(batch_key)
    )


def _validate_id_list(values: Sequence[int] | None, *, field: str) -> list[int] | None:
    if values is None:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for raw in values:
        value = int(raw)
        if value <= 0:
            raise JobLedgerIntegrityError(f"{field} must contain positive ids")
        if value in seen:
            raise JobLedgerIntegrityError(f"{field} must contain unique ids")
        seen.add(value)
        out.append(value)
    return out


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
    remaining_source: str | None = None,
    snapshot_quote_ids: list[int] | tuple[int, ...] | None = None,
    snapshot_availability_ids: list[int] | tuple[int, ...] | None = None,
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
    if cutoff is not None and cutoff > stamp:
        raise JobLedgerTimeError("requested_cutoff must be <= as_of")

    if status == "success":
        existing = find_successful_run(session, idempotency_key=idempotency_key)
        if existing is not None:
            raise JobLedgerDuplicate(
                f"idempotency key already succeeded: {idempotency_key}"
            )
        success_token = 1
    else:
        success_token = None

    if status == "failed" and not (error_class and str(error_class).strip()):
        raise JobLedgerIntegrityError("failed status requires error_class")
    if status in {"deferred_quota", "exhausted"} and actual_cost is not None:
        raise JobLedgerIntegrityError(
            f"{status} must not record actual_cost (unexecuted)"
        )

    if actual_cost is not None and actual_cost < 0:
        raise ValueError("actual_cost must be nonnegative when present")

    source = actual_cost_source
    if actual_cost is not None and source is None:
        raise ValueError("actual_cost requires actual_cost_source provenance")
    if actual_cost is None and source not in {None, "missing"}:
        raise ValueError("missing actual_cost requires source None or 'missing'")

    quote_ids = _validate_id_list(snapshot_quote_ids, field="snapshot_quote_ids")
    availability_ids = _validate_id_list(
        snapshot_availability_ids, field="snapshot_availability_ids"
    )
    quote_ids_json = None if quote_ids is None else json.dumps(quote_ids)
    availability_ids_json = (
        None if availability_ids is None else json.dumps(availability_ids)
    )

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
        remaining_source=remaining_source,
        snapshot_quote_ids=quote_ids_json,
        snapshot_availability_ids=availability_ids_json,
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
    "BATCH_EVENT_ID",
    "JobLedgerDuplicate",
    "JobLedgerIntegrityError",
    "JobLedgerTimeError",
    "batch_idempotency_key",
    "find_successful_batch_run",
    "find_successful_run",
    "record_job_run",
    "slot_succeeded",
]
