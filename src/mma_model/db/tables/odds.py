"""Append-only odds event, quote, and availability tables (DWCS-201)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base

# Keep SQL literals in sync with mma_model.odds.types supported mappings.
_PROVIDER_MARKET_FAMILY_PAIR_SQL = (
    "(provider_market_key = 'h2h' AND market_family = 'moneyline') OR "
    "(provider_market_key = 'totals' AND market_family = 'totals')"
)
_QUOTE_AVAILABILITY_SQL = "'available', 'suspended', 'unknown'"
_QUOTA_SOURCE_SQL = "'inferred_empty_zero', 'missing', 'provider'"
_QUOTA_PROVENANCE_SQL = (
    "("
    "requests_last_source = 'provider' "
    "AND requests_last IS NOT NULL "
    "AND requests_last_inferred IS NULL"
    ") OR ("
    "requests_last_source = 'inferred_empty_zero' "
    "AND requests_last IS NULL "
    "AND requests_last_inferred = 0 "
    "AND empty_response = 1"
    ") OR ("
    "requests_last_source = 'missing' "
    "AND requests_last IS NULL "
    "AND requests_last_inferred IS NULL"
    ")"
)


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
    """Persisted quota headers from an odds HTTP/fixture response.

    ``requests_last`` stores the raw provider header (None if absent).
    Inferred empty-response cost is stored separately so audit can distinguish
    provider-reported ``0`` from policy inference.
    """

    __tablename__ = "odds_quota_observations"
    __table_args__ = (
        CheckConstraint(
            f"requests_last_source IN ({_QUOTA_SOURCE_SQL})",
            name="ck_odds_quota_requests_last_source",
        ),
        CheckConstraint(
            _QUOTA_PROVENANCE_SQL,
            name="ck_odds_quota_requests_last_provenance",
        ),
        CheckConstraint(
            "(requests_remaining IS NULL OR requests_remaining >= 0) AND "
            "(requests_used IS NULL OR requests_used >= 0) AND "
            "(requests_last IS NULL OR requests_last >= 0) AND "
            "(requests_last_inferred IS NULL OR requests_last_inferred >= 0)",
            name="ck_odds_quota_nonnegative",
        ),
        CheckConstraint(
            "empty_response IN (0, 1)",
            name="ck_odds_quota_empty_response",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    endpoint: Mapped[str] = mapped_column(String(128), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    requests_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_last: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_last_inferred: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requests_last_source: Mapped[str] = mapped_column(String(32), nullable=False)
    empty_response: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class OddsQuote(Base):
    """Append-only normalized reference quote with semantic dedupe key."""

    __tablename__ = "odds_quotes"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_odds_quotes_dedupe_key"),
        CheckConstraint(
            _PROVIDER_MARKET_FAMILY_PAIR_SQL,
            name="ck_odds_quotes_provider_market_family",
        ),
        CheckConstraint(
            f"availability IN ({_QUOTE_AVAILABILITY_SQL})",
            name="ck_odds_quotes_availability",
        ),
        CheckConstraint(
            "price_decimal > 1.0",
            name="ck_odds_quotes_price_decimal",
        ),
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


class OddsAvailabilityObservation(Base):
    """Append-only unknown/missing market observations with event/book identity.

    Supported DWCS-201 provider mappings only (`h2h`↔`moneyline`,
    `totals`↔`totals`). ``snapshot_at`` set marks a historical provider
    snapshot; null marks a current poll. Availability is ``unknown``
    (missing), never implied suspension.
    """

    __tablename__ = "odds_availability_observations"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_odds_availability_dedupe_key"),
        CheckConstraint(
            _PROVIDER_MARKET_FAMILY_PAIR_SQL,
            name="ck_odds_availability_provider_market_family",
        ),
        CheckConstraint(
            "availability = 'unknown'",
            name="ck_odds_availability_availability",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("odds_events.id"), index=True
    )
    external_event_id: Mapped[str] = mapped_column(String(128), index=True)
    bookmaker_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    bookmaker_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_market_key: Mapped[str] = mapped_column(String(64), index=True)
    market_family: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    availability: Mapped[str] = mapped_column(String(32), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    commence_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


_MANUAL_SOURCE_SQL = "'user_observed'"
_MANUAL_LIFECYCLE_SQL = (
    "'available', 'unknown', 'suspended', 'locked', 'removed', 'entitlement_failed'"
)
_MANUAL_PRICE_PROVENANCE_SQL = (
    "("
    "lifecycle = 'available' AND price_decimal IS NOT NULL AND price_decimal > 1.0"
    ") OR ("
    "lifecycle != 'available' AND price_decimal IS NULL"
    ")"
)
_MANUAL_ATTEMPTED_PROVIDER_SQL = (
    "("
    "lifecycle = 'entitlement_failed' "
    "AND attempted_provider IS NOT NULL "
    "AND length(trim(attempted_provider)) > 0"
    ") OR ("
    "lifecycle != 'entitlement_failed' AND attempted_provider IS NULL"
    ")"
)
# Keep in sync with DWCS-200 catalog (moneyline/totals/goes_distance/method/
# fighter_by_method/exact_round + totals points 1.5/2.5 only).
_MANUAL_FAMILY_OUTCOME_LINE_SQL = (
    "("
    "market_family = 'moneyline' AND outcome_key IN ('fighter_a', 'fighter_b') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'totals' AND outcome_key IN ('over', 'under') "
    "AND line_point IN (1.5, 2.5)"
    ") OR ("
    "market_family = 'goes_distance' "
    "AND outcome_key IN ('goes_distance', 'inside_distance') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'method' "
    "AND outcome_key IN ('ko_tko', 'submission', 'decision', 'other_stoppage') "
    "AND line_point IS NULL"
    ") OR ("
    "market_family = 'fighter_by_method' AND outcome_key IN ("
    "'a_ko_tko', 'a_submission', 'a_other_stoppage', 'a_decision', "
    "'b_ko_tko', 'b_submission', 'b_other_stoppage', 'b_decision'"
    ") AND line_point IS NULL"
    ") OR ("
    "market_family = 'exact_round' "
    "AND outcome_key IN ('round_1', 'round_2', 'round_3', 'round_4', 'round_5') "
    "AND line_point IS NULL"
    ")"
)


class OddsManualPriceObservation(Base):
    """Append-only user-observed (non-automated) price / lifecycle observations.

    Exact EV confirmation may use these rows. Locked/removed/entitlement-failed
    rows never store a forward-filled price. ``attempted_provider`` is required
    only for ``entitlement_failed``.
    """

    __tablename__ = "odds_manual_price_observations"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_odds_manual_price_dedupe_key"),
        CheckConstraint(
            f"source_kind IN ({_MANUAL_SOURCE_SQL})",
            name="ck_odds_manual_source_kind",
        ),
        CheckConstraint(
            "automated IN (0, 1)",
            name="ck_odds_manual_automated",
        ),
        CheckConstraint(
            "automated = 0",
            name="ck_odds_manual_non_automated",
        ),
        CheckConstraint(
            f"lifecycle IN ({_MANUAL_LIFECYCLE_SQL})",
            name="ck_odds_manual_lifecycle",
        ),
        CheckConstraint(
            _MANUAL_PRICE_PROVENANCE_SQL,
            name="ck_odds_manual_price_provenance",
        ),
        CheckConstraint(
            _MANUAL_ATTEMPTED_PROVIDER_SQL,
            name="ck_odds_manual_attempted_provider",
        ),
        CheckConstraint(
            _MANUAL_FAMILY_OUTCOME_LINE_SQL,
            name="ck_odds_manual_family_outcome_line",
        ),
        CheckConstraint(
            "length(trim(bookmaker_key)) > 0",
            name="ck_odds_manual_bookmaker_key_nonempty",
        ),
        CheckConstraint(
            "length(trim(region)) > 0",
            name="ck_odds_manual_region_nonempty",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), index=True)
    automated: Mapped[int] = mapped_column(Integer, default=0)
    bookmaker_key: Mapped[str] = mapped_column(String(64), index=True)
    bookmaker_title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    region: Mapped[str] = mapped_column(String(32), index=True)
    market_family: Mapped[str] = mapped_column(String(64), index=True)
    outcome_key: Mapped[str] = mapped_column(String(64), index=True)
    line_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(32), index=True)
    attempted_provider: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    event_external_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    settlement_identity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
