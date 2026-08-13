"""Append-only storage for normalized The Odds API reference quotes (DWCS-201)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.tables.odds import (
    OddsAvailabilityObservation,
    OddsEventRow,
    OddsManualPriceObservation,
    OddsQuotaObservation,
    OddsQuote,
)
from mma_model.odds.manual_price import MANUAL_SOURCE_LABEL, ObservedPrice, PriceSourceKind
from mma_model.odds.types import (
    REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    NormalizedQuote,
    OddsEvent,
    QuotaHeaders,
    UnknownMarketObservation,
    assert_supported_provider_market_pair,
)


@dataclass(frozen=True)
class QuoteStoreResult:
    inserted: int
    deduped: int
    event_upserts: int
    unknown_inserted: int = 0
    unknown_deduped: int = 0
    manual_inserted: int = 0
    manual_deduped: int = 0


class OddsQuoteStore:
    """Persist provider events and append-only quotes/availability with dedupe."""

    def __init__(self, session: Session) -> None:
        self._session = session
        bind = session.get_bind()
        if bind is not None:
            install_odds_sqlite_guards(bind)

    def upsert_event(self, event: OddsEvent, *, provider: str) -> OddsEventRow:
        existing = self._session.scalar(
            select(OddsEventRow).where(
                OddsEventRow.provider == provider,
                OddsEventRow.external_event_id == event.id,
            )
        )
        if existing is None:
            row = OddsEventRow(
                provider=provider,
                external_event_id=event.id,
                sport_key=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                commence_time=event.commence_time,
            )
            self._session.add(row)
            self._session.flush()
            return row
        existing.sport_key = event.sport_key
        existing.home_team = event.home_team
        existing.away_team = event.away_team
        existing.commence_time = event.commence_time
        existing.updated_at = datetime.now(UTC)
        self._session.flush()
        return existing

    def record_quota(
        self,
        *,
        provider: str,
        endpoint: str,
        observed_at: datetime,
        quota: QuotaHeaders,
        empty_response: bool,
    ) -> OddsQuotaObservation:
        # QuotaHeaders.__post_init__ already enforces source/value pairing.
        if (
            quota.requests_last_source == REQUESTS_LAST_SOURCE_INFERRED_EMPTY
            and not empty_response
        ):
            raise ValueError(
                "inferred_empty_zero quota provenance requires empty_response=True"
            )
        row = OddsQuotaObservation(
            provider=provider,
            endpoint=endpoint,
            observed_at=observed_at,
            requests_remaining=quota.requests_remaining,
            requests_used=quota.requests_used,
            requests_last=quota.requests_last,
            requests_last_inferred=quota.requests_last_inferred,
            requests_last_source=quota.requests_last_source,
            empty_response=1 if empty_response else 0,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def append_quotes(
        self,
        quotes: Sequence[NormalizedQuote],
        *,
        events_by_external_id: dict[str, OddsEventRow] | None = None,
    ) -> QuoteStoreResult:
        """Insert quotes; identical ``dedupe_key`` rows are skipped (no update)."""
        event_map = dict(events_by_external_id or {})
        inserted = 0
        deduped = 0
        event_upserts = 0

        for quote in quotes:
            assert_supported_provider_market_pair(
                quote.provider_market_key, quote.market_family
            )
            if quote.price_decimal <= 1.0:
                raise ValueError(
                    f"refusing quote with price_decimal <= 1.0: {quote.price_decimal!r}"
                )
            event_row = event_map.get(quote.event_id)
            if event_row is None:
                event_row = self.upsert_event(
                    OddsEvent(
                        id=quote.event_id,
                        sport_key="mma_mixed_martial_arts",
                        commence_time=quote.commence_time,
                        home_team=quote.home_team,
                        away_team=quote.away_team,
                    ),
                    provider=quote.provider,
                )
                event_map[quote.event_id] = event_row
                event_upserts += 1

            existing = self._session.scalar(
                select(OddsQuote.id).where(OddsQuote.dedupe_key == quote.dedupe_key)
            )
            if existing is not None:
                deduped += 1
                continue

            self._session.add(
                OddsQuote(
                    dedupe_key=quote.dedupe_key,
                    provider=quote.provider,
                    bookmaker_key=quote.bookmaker_key,
                    bookmaker_title=quote.bookmaker_title,
                    region=quote.region,
                    event_id=event_row.id,
                    external_event_id=quote.event_id,
                    market_family=quote.market_family.value,
                    provider_market_key=quote.provider_market_key,
                    outcome_key=quote.outcome_key.value,
                    outcome_label=quote.outcome_label,
                    line_point=quote.line_point,
                    price_decimal=quote.price_decimal,
                    availability=quote.availability.value,
                    observed_at=quote.observed_at,
                    source_updated_at=quote.source_updated_at,
                    commence_time=quote.commence_time,
                    snapshot_at=quote.snapshot_at,
                    raw_ref=quote.raw_ref,
                )
            )
            inserted += 1

        self._session.flush()
        return QuoteStoreResult(
            inserted=inserted,
            deduped=deduped,
            event_upserts=event_upserts,
        )

    def append_unknown_observations(
        self,
        observations: Sequence[UnknownMarketObservation],
        *,
        events_by_external_id: dict[str, OddsEventRow] | None = None,
    ) -> QuoteStoreResult:
        """Insert unknown-market observations with append-only dedupe."""
        event_map = dict(events_by_external_id or {})
        inserted = 0
        deduped = 0
        event_upserts = 0

        for obs in observations:
            assert_supported_provider_market_pair(
                obs.provider_market_key, obs.market_family
            )
            event_row = event_map.get(obs.event_id)
            if event_row is None:
                event_row = self.upsert_event(
                    OddsEvent(
                        id=obs.event_id,
                        sport_key="mma_mixed_martial_arts",
                        commence_time=obs.commence_time,
                        home_team=obs.home_team,
                        away_team=obs.away_team,
                    ),
                    provider=obs.provider,
                )
                event_map[obs.event_id] = event_row
                event_upserts += 1

            existing = self._session.scalar(
                select(OddsAvailabilityObservation.id).where(
                    OddsAvailabilityObservation.dedupe_key == obs.dedupe_key
                )
            )
            if existing is not None:
                deduped += 1
                continue

            self._session.add(
                OddsAvailabilityObservation(
                    dedupe_key=obs.dedupe_key,
                    provider=obs.provider,
                    region=obs.region,
                    event_id=event_row.id,
                    external_event_id=obs.event_id,
                    bookmaker_key=obs.bookmaker_key,
                    bookmaker_title=obs.bookmaker_title,
                    provider_market_key=obs.provider_market_key,
                    market_family=obs.market_family.value,
                    availability=obs.availability.value,
                    observed_at=obs.observed_at,
                    commence_time=obs.commence_time,
                    snapshot_at=obs.snapshot_at,
                )
            )
            inserted += 1

        self._session.flush()
        return QuoteStoreResult(
            inserted=0,
            deduped=0,
            event_upserts=event_upserts,
            unknown_inserted=inserted,
            unknown_deduped=deduped,
        )

    def append_manual_prices(
        self,
        observations: Sequence[ObservedPrice],
    ) -> QuoteStoreResult:
        """Persist user_observed price/lifecycle rows (append-only dedupe)."""
        inserted = 0
        deduped = 0
        for obs in observations:
            if obs.source_kind is not PriceSourceKind.USER_OBSERVED:
                raise ValueError(
                    "append_manual_prices accepts only user_observed rows "
                    f"(got {obs.source_kind!r})"
                )
            if obs.automated:
                raise ValueError("manual price rows must set automated=False")
            existing = self._session.scalar(
                select(OddsManualPriceObservation.id).where(
                    OddsManualPriceObservation.dedupe_key == obs.dedupe_key
                )
            )
            if existing is not None:
                deduped += 1
                continue
            self._session.add(
                OddsManualPriceObservation(
                    dedupe_key=obs.dedupe_key,
                    source_kind=MANUAL_SOURCE_LABEL,
                    automated=0,
                    bookmaker_key=obs.bookmaker_key,
                    bookmaker_title=obs.bookmaker_title,
                    region=obs.region,
                    market_family=obs.market_family.value,
                    outcome_key=obs.outcome_key.value,
                    line_point=obs.line_point,
                    price_decimal=obs.price_decimal,
                    lifecycle=obs.lifecycle.value,
                    attempted_provider=obs.attempted_provider,
                    observed_at=obs.observed_at,
                    source_updated_at=obs.source_updated_at,
                    event_external_id=obs.event_external_id,
                    selection_identity=obs.selection_identity,
                    detail=obs.detail,
                )
            )
            inserted += 1
        self._session.flush()
        return QuoteStoreResult(
            inserted=inserted,
            deduped=deduped,
            event_upserts=0,
            manual_inserted=inserted,
            manual_deduped=deduped,
        )
