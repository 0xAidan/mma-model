"""Append-only odds event and quote tables (DWCS-201)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class OddsEventRow(Base):
    """Provider event identity for reference odds (not bout-matched yet)."""

    __tablename__ = "odds_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_event_id", name="uq_odds_events_provider_ext"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    external_event_id: Mapped[str] = mapped_column(String(128), index=True)
    sport_key: Mapped[str] = mapped_column(String(80))
    home_team: Mapped[str] = mapped_column(String(200))
    away_team: Mapped[str] = mapped_column(String(200))
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class OddsQuotaObservation(Base):
    """Persisted quota headers from an odds HTTP/fixture response."""

    __tablename__ = "odds_quota_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    requests_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_last: Mapped[int | None] = mapped_column(Integer, nullable=True)
    empty_response: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class OddsQuote(Base):
    """Append-only normalized reference quote with semantic dedupe key."""

    __tablename__ = "odds_quotes"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_odds_quotes_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    bookmaker_key: Mapped[str] = mapped_column(String(64), index=True)
    bookmaker_title: Mapped[str] = mapped_column(String(128))
    region: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("odds_events.id"), index=True
    )
    external_event_id: Mapped[str] = mapped_column(String(128), index=True)
    market_family: Mapped[str] = mapped_column(String(64), index=True)
    provider_market_key: Mapped[str] = mapped_column(String(64))
    outcome_key: Mapped[str] = mapped_column(String(64), index=True)
    outcome_label: Mapped[str] = mapped_column(String(200))
    line_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_decimal: Mapped[float] = mapped_column(Float)
    availability: Mapped[str] = mapped_column(String(32), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    raw_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
