"""Provenance tables: ingest runs, raw observations, source checkpoints (DWCS-101)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.models import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class IngestRun(Base):
    """One bounded ingest execution for a source/stream/scope."""

    __tablename__ = "ingest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)
    stream: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_class: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class RawObservation(Base):
    """Immutable provenance row for a source-neutral observation payload."""

    __tablename__ = "raw_observations"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "stream",
            "external_id",
            "payload_hash",
            name="uq_raw_obs_provenance",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ingest_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ingest_runs.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(64), index=True)
    stream: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    entity_kind: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_ref: Mapped[str] = mapped_column(String(64))
    detail_level: Mapped[str] = mapped_column(String(32), default="partial")
    version_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(32), default="1")
    subject_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class SourceCheckpoint(Base):
    """Resumable cursor keyed by source/stream/scope/version (no cross-profile collision)."""

    __tablename__ = "source_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "stream",
            "scope",
            "version",
            name="uq_source_checkpoint_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    stream: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[str] = mapped_column(String(64))
    cursor_token: Mapped[str] = mapped_column(String(512))
    last_ingest_run_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ingest_runs.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
