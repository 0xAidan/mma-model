"""Regional history tables (DWCS-105).

These structures are owned by migration 0008. Adapters never write them
directly; IngestRepository applies SourceObservationRecord rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


CLASSIFICATION_VALUES = ("professional", "amateur", "unknown")
REGULATED_US_VALUES = ("true", "false", "unknown")
RESULT_VALUES = ("win", "loss", "draw", "nc", "unknown", "cancelled")
BOUT_STATUS_VALUES = ("completed", "cancelled", "replacement", "scheduled", "unknown")
IDENTITY_STATUS_VALUES = ("linked", "queued", "blocked", "unresolved")


class HistorySourceBout(Base):
    """Source-level regional/pre-UFC bout observation with explicit missingness."""

    __tablename__ = "history_source_bouts"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_bout_id",
            "version_kind",
            "revision",
            name="uq_history_source_bout_revision",
        ),
        CheckConstraint(
            "classification IN ('professional', 'amateur', 'unknown')",
            name="ck_history_bout_classification",
        ),
        CheckConstraint(
            "regulated_us IN ('true', 'false', 'unknown')",
            name="ck_history_bout_regulated_us",
        ),
        CheckConstraint(
            "result IN ('win', 'loss', 'draw', 'nc', 'unknown', 'cancelled')",
            name="ck_history_bout_result",
        ),
        CheckConstraint("revision >= 1", name="ck_history_bout_revision_positive"),
        CheckConstraint(
            "identity_status IN ('linked', 'queued', 'blocked', 'unresolved')",
            name="ck_history_bout_identity_status",
        ),
        CheckConstraint(
            "bout_status IN ('completed', 'cancelled', 'replacement', 'scheduled', 'unknown')",
            name="ck_history_bout_status",
        ),
        CheckConstraint(
            "version_kind IN ('event_night', 'current', 'correction')",
            name="ck_history_bout_version_kind",
        ),
        CheckConstraint("is_current_record IN (0, 1)", name="ck_history_bout_is_current"),
        CheckConstraint("left_truncated IN (0, 1)", name="ck_history_bout_left_truncated"),
        CheckConstraint(
            "event_time_precision IN ('date_only', 'exact', 'unknown')",
            name="ck_history_bout_time_precision",
        ),
        CheckConstraint(
            "observation_origin IN ('synthetic_fixture', 'live_public', 'unknown')",
            name="ck_history_bout_origin",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)
    stream: Mapped[str] = mapped_column(String(64), default="fighter_history")
    external_bout_id: Mapped[str] = mapped_column(String(128), index=True)
    fighter_source: Mapped[str] = mapped_column(String(64))
    fighter_external_id: Mapped[str] = mapped_column(String(128), index=True)
    fighter_name: Mapped[str] = mapped_column(String(200))
    fighter_canonical_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), nullable=True, index=True
    )
    opponent_source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    opponent_external_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    opponent_name: Mapped[str] = mapped_column(String(200))
    opponent_canonical_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), nullable=True
    )
    event_name: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    event_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    classification: Mapped[str] = mapped_column(String(32), default="unknown")
    regulated_us: Mapped[str] = mapped_column(String(16), default="unknown")
    result: Mapped[str] = mapped_column(String(32), default="unknown")
    method: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    ending_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_str: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    elapsed_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    scheduled_rounds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    promotion: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    missing_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    left_truncated: Mapped[int] = mapped_column(Integer, default=0)
    parser_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_class: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version_kind: Mapped[str] = mapped_column(String(32), default="event_night")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    bout_status: Mapped[str] = mapped_column(String(32), default="completed")
    quality_tier: Mapped[str] = mapped_column(String(32), default="bronze")
    timestamp_quality: Mapped[str] = mapped_column(String(64), default="unknown")
    timestamp_quality_source: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    proxy_published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    identity_status: Mapped[str] = mapped_column(String(32), default="unresolved")
    is_current_record: Mapped[int] = mapped_column(Integer, default=0)
    event_time_precision: Mapped[str] = mapped_column(String(32), default="date_only")
    observation_origin: Mapped[str] = mapped_column(String(32), default="unknown")
    wikidata_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class HistoryConflict(Base):
    """Preserved disagreement; never a silent overwrite."""

    __tablename__ = "history_conflicts"
    __table_args__ = (
        UniqueConstraint("conflict_key", name="uq_history_conflict_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    conflict_key: Mapped[str] = mapped_column(String(256), index=True)
    conflict_type: Mapped[str] = mapped_column(String(64), index=True)
    fighter_canonical_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), nullable=True, index=True
    )
    left_source: Mapped[str] = mapped_column(String(64))
    left_external_id: Mapped[str] = mapped_column(String(128))
    right_source: Mapped[str] = mapped_column(String(64))
    right_external_id: Mapped[str] = mapped_column(String(128))
    detail_json: Mapped[str] = mapped_column(Text)
    quality_tier: Mapped[str] = mapped_column(String(32), default="conflict")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class HistorySourceFailure(Base):
    """Typed source kill/failure; never inferred as zero coverage."""

    __tablename__ = "history_source_failures"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "reason",
            "scope",
            "subject",
            name="uq_history_source_failure_subject",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(128), default="default")
    subject: Mapped[str] = mapped_column(String(128), default="")
    host: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    path_category: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    checkpoint_token: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class HistoryFrontier(Base):
    """Deterministic crawl frontier keyed by fighter/event/source ID."""

    __tablename__ = "history_frontier"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "entity_kind",
            "entity_id",
            name="uq_history_frontier_entity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    entity_kind: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(128), index=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    cursor_json: Mapped[str] = mapped_column(Text, default="{}")
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class HistoryReconstruction(Base):
    """Append-only pre-fight reconstruction snapshot for a fighter+cutoff."""

    __tablename__ = "history_reconstructions"
    __table_args__ = (
        UniqueConstraint(
            "fighter_canonical_id",
            "cutoff",
            "reconstruction_version",
            name="uq_history_reconstruction_cutoff",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    fighter_canonical_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reconstruction_version: Mapped[str] = mapped_column(String(32), default="1")
    payload_json: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class HistoryExplicitRecord(Base):
    """Explicit source-stated pre-fight record used only for comparison."""

    __tablename__ = "history_explicit_records"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "fighter_external_id",
            "as_of",
            name="uq_history_explicit_record",
        ),
        CheckConstraint("feature_eligible IN (0, 1)", name="ck_history_explicit_feature"),
        CheckConstraint("is_current_mutable IN (0, 1)", name="ck_history_explicit_current"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)
    fighter_external_id: Mapped[str] = mapped_column(String(128), index=True)
    fighter_canonical_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), nullable=True, index=True
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    wins: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    losses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    draws: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    no_contests: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(32), default="unknown")
    is_current_mutable: Mapped[int] = mapped_column(Integer, default=0)
    feature_eligible: Mapped[int] = mapped_column(Integer, default=0)
    payload_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
