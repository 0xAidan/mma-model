"""Durable pipeline / orchestrator job-run ledger (DWCS-401).

Separate from ``odds_snapshot_job_runs`` so non-odds jobs do not overload the
DWCS-205 odds ledger.
"""

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


_JOB_STATUS_SQL = "'started', 'success', 'failed', 'skipped', 'dependency_blocked'"
_ERROR_CLASS_SQL = (
    "'transient', 'authentication', 'entitlement', 'schema', "
    "'identity_unresolved', 'stale_quote', 'missing_odds', "
    "'dependency_blocked', 'overlap', 'internal'"
)


class PipelineJobRun(Base):
    """Append-ish orchestrator job ledger keyed by durable idempotency_key.

    Successful logical runs are unique on ``idempotency_key`` via
    ``success_token = 1``. Failed / blocked attempts may share a key.
    """

    __tablename__ = "pipeline_job_runs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            "success_token",
            name="uq_pipeline_job_runs_idem_success",
        ),
        CheckConstraint(
            f"status IN ({_JOB_STATUS_SQL})",
            name="ck_pipeline_job_runs_status",
        ),
        CheckConstraint(
            "("
            "(status = 'success' AND success_token IS NOT NULL AND success_token = 1) "
            "OR "
            "(status != 'success' AND success_token IS NULL)"
            ")",
            name="ck_pipeline_job_runs_status_success_token",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_pipeline_job_runs_idem_nonempty",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_pipeline_job_runs_finished_ge_started",
        ),
        CheckConstraint(
            "("
            "status != 'success' OR finished_at IS NOT NULL"
            ")",
            name="ck_pipeline_job_runs_success_finished",
        ),
        CheckConstraint(
            "("
            "status != 'failed' OR ("
            "error_class IS NOT NULL AND length(trim(error_class)) > 0"
            ")"
            ")",
            name="ck_pipeline_job_runs_failed_error_class",
        ),
        CheckConstraint(
            "("
            "error_class IS NULL OR error_class IN (" + _ERROR_CLASS_SQL + ")"
            ")",
            name="ck_pipeline_job_runs_error_class",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_pipeline_job_runs_attempt_positive",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_pipeline_job_runs_duration_nonneg",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(256), index=True)
    success_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True, default="dwcs")
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    bout_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(32), default="event")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_slot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    counts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_quota: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


__all__ = ["PipelineJobRun"]
