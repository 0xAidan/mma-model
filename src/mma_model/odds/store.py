"""Append-only storage for normalized The Odds API reference quotes (DWCS-201)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.tables.odds import OddsEventRow, OddsQuotaObservation, OddsQuote
from mma_model.odds.types import (
    NormalizedQuote,
    OddsEvent,
    QuotaHeaders,
)


@dataclass(frozen=True)
class QuoteStoreResult:
    inserted: int
    deduped: int
    event_upserts: int


class OddsQuoteStore:
    """Persist provider events and append-only quotes with deduplication."""

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
        row = OddsQuotaObservation(
            provider=provider,
            endpoint=endpoint,
            observed_at=observed_at,
            requests_remaining=quota.requests_remaining,
            requests_used=quota.requests_used,
            requests_last=quota.requests_last,
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
