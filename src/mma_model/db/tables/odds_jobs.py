"""Durable odds job run / idempotency ledger (DWCS-205)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


_JOB_STATUS_SQL = (
    "'started', 'success', 'failed', 'deferred_quota', 'exhausted', "
    "'no_op', 'duplicate'"
)


class OddsSnapshotJobRun(Base):
    """Append-ish job ledger keyed by durable idempotency_key.

    Successful logical snapshots are unique on ``idempotency_key`` so retries
    do not duplicate work. Failed/deferred rows may share a key only when the
    prior attempt was non-success (enforced in application CAS helpers).
    """

    __tablename__ = "odds_snapshot_job_runs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            "success_token",
            name="uq_odds_snapshot_job_runs_idem_success",
        ),
        CheckConstraint(
            f"status IN ({_JOB_STATUS_SQL})",
            name="ck_odds_snapshot_job_runs_status",
        ),
        CheckConstraint(
            "("
            "(status = 'success' AND success_token IS NOT NULL AND success_token = 1) "
            "OR "
            "(status != 'success' AND success_token IS NULL)"
            ")",
            name="ck_odds_snapshot_job_runs_status_success_token",
        ),
        CheckConstraint(
            "estimated_cost >= 0 AND (actual_cost IS NULL OR actual_cost >= 0)",
            name="ck_odds_snapshot_job_runs_costs",
        ),
        CheckConstraint(
            "("
            "actual_cost_source IS NULL AND actual_cost IS NULL"
            ") OR ("
            "actual_cost_source IN ('provider', 'inferred_empty_zero') "
            "AND actual_cost IS NOT NULL"
            ") OR ("
            "actual_cost_source = 'missing' AND actual_cost IS NULL"
            ")",
            name="ck_odds_snapshot_job_runs_actual_cost_provenance",
        ),
        CheckConstraint(
            "("
            "snapshot_at IS NULL OR requested_cutoff IS NULL OR "
            "snapshot_at <= requested_cutoff"
            ")",
            name="ck_odds_snapshot_job_runs_snapshot_le_cutoff",
        ),
        CheckConstraint(
            "("
            "requested_cutoff IS NULL OR requested_cutoff <= as_of"
            ")",
            name="ck_odds_snapshot_job_runs_cutoff_le_as_of",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_odds_snapshot_job_runs_finished_ge_started",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_odds_snapshot_job_runs_idem_nonempty",
        ),
        CheckConstraint(
            "("
            "status != 'success' OR finished_at IS NOT NULL"
            ")",
            name="ck_odds_snapshot_job_runs_success_finished",
        ),
        CheckConstraint(
            "("
            "status != 'failed' OR ("
            "error_class IS NOT NULL AND length(trim(error_class)) > 0"
            ")"
            ")",
            name="ck_odds_snapshot_job_runs_failed_error_class",
        ),
        CheckConstraint(
            "("
            "status NOT IN ('deferred_quota', 'exhausted') OR actual_cost IS NULL"
            ")",
            name="ck_odds_snapshot_job_runs_unexecuted_no_actual_cost",
        ),
        CheckConstraint(
            "("
            "snapshot_quote_ids IS NULL OR ("
            "json_valid(snapshot_quote_ids) AND "
            "json_type(snapshot_quote_ids) = 'array'"
            ")"
            ")",
            name="ck_odds_snapshot_job_runs_quote_ids_json",
        ),
        CheckConstraint(
            "("
            "snapshot_availability_ids IS NULL OR ("
            "json_valid(snapshot_availability_ids) AND "
            "json_type(snapshot_availability_ids) = 'array'"
            ")"
            ")",
            name="ck_odds_snapshot_job_runs_availability_ids_json",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    # success_token is 1 for terminal success, null otherwise so multiple
    # non-success attempts can exist while success remains unique per key.
    success_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str] = mapped_column(String(32))
    markets: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    mode: Mapped[str] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    requested_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    window_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    estimated_cost: Mapped[int] = mapped_column(Integer, default=0)
    actual_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_cost_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remaining_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_quote_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_availability_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


__all__ = ["OddsSnapshotJobRun"]
