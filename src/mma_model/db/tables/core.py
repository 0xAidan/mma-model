"""Canonical fighters, events, bouts, and observation tables (DWCS-100)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.models import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class CanonicalFighter(Base):
    __tablename__ = "canonical_fighters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    display_name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class FighterSourceId(Base):
    """Maps a provider external ID to a canonical fighter UUID."""

    __tablename__ = "fighter_source_ids"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_fighter_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fighter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class FighterAlias(Base):
    __tablename__ = "fighter_aliases"
    __table_args__ = (
        UniqueConstraint("fighter_id", "alias", name="uq_fighter_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fighter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    alias: Mapped[str] = mapped_column(String(200))
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(400))
    series: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    scheduled_start_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(400), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class EventSourceId(Base):
    __tablename__ = "event_source_ids"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_event_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_events.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class CanonicalBout(Base):
    __tablename__ = "canonical_bouts"
    __table_args__ = (
        CheckConstraint("fighter_a_id != fighter_b_id", name="ck_bout_distinct_fighters"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_events.id"), index=True
    )
    fighter_a_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    fighter_b_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    scheduled_rounds: Mapped[int] = mapped_column(Integer, default=3)
    weight_class: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="scheduled", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class BoutSourceId(Base):
    __tablename__ = "bout_source_ids"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_bout_source_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bout_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_bouts.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class BoutParticipant(Base):
    __tablename__ = "bout_participants"
    __table_args__ = (
        UniqueConstraint("bout_id", "fighter_id", name="uq_bout_participant_fighter"),
        UniqueConstraint("bout_id", "corner", name="uq_bout_participant_corner"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bout_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_bouts.id"), index=True
    )
    fighter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    corner: Mapped[str] = mapped_column(String(8))  # "a" | "b"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class BoutResultVersion(Base):
    """Immutable event-night/current result revisions; winner must be a participant when set.

    Corrections append a new ``revision`` under the same ``version_kind``; prior rows
    are never updated in place.
    """

    __tablename__ = "bout_result_versions"
    __table_args__ = (
        UniqueConstraint(
            "bout_id",
            "version_kind",
            "revision",
            name="uq_bout_result_version_revision",
        ),
        CheckConstraint(
            "winner_fighter_id IS NULL OR winner_fighter_id = fighter_a_id "
            "OR winner_fighter_id = fighter_b_id",
            name="ck_result_winner_is_participant",
        ),
        CheckConstraint("fighter_a_id != fighter_b_id", name="ck_result_distinct_fighters"),
        CheckConstraint("revision >= 1", name="ck_bout_result_revision_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bout_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_bouts.id"), index=True
    )
    version_kind: Mapped[str] = mapped_column(String(32))  # event_night | current
    revision: Mapped[int] = mapped_column(Integer, default=1)
    fighter_a_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id")
    )
    fighter_b_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id")
    )
    winner_fighter_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), nullable=True
    )
    result_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    method: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    ending_round: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_str: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class FighterProfileObservation(Base):
    """Bitemporal fighter profile fact (height, reach, stance, DOB, etc.)."""

    __tablename__ = "fighter_profile_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fighter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    attribute: Mapped[str] = mapped_column(String(64), index=True)
    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_num: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class FighterStatObservation(Base):
    """Bitemporal per-bout fighter statistic observation."""

    __tablename__ = "fighter_stat_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fighter_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("canonical_fighters.id"), index=True
    )
    bout_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("canonical_bouts.id"), nullable=True, index=True
    )
    stat_key: Mapped[str] = mapped_column(String(64), index=True)
    value_num: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default="unknown")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
