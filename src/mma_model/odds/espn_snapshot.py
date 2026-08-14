"""Snapshot ESPN public competition odds for upcoming DWCS cards."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    EventSourceId,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.jobs.horizons import TICK_EVENT_HORIZON
from mma_model.jobs.types import HandlerResult, JobStatus
from mma_model.odds.events_for_schedule import load_upcoming_dwcs_events_from_db
from mma_model.odds.normalize import ensure_utc, quote_dedupe_key, unknown_dedupe_key
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import (
    NormalizedQuote,
    QuoteAvailability,
    UnknownMarketObservation,
)
from mma_model.sources.espn_public.client import EspnPublicClient
from mma_model.sources.espn_public.errors import EspnSchemaError
from mma_model.sources.espn_public.odds import ESPN_ODDS_PROVIDER, parse_espn_odds
from mma_model.sources.espn_public.parser import ESPN_IDENTITY_SOURCE
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.value.odds import american_to_decimal

_REGION = "us"


@dataclass(frozen=True)
class _BoutOddsTarget:
    bout_id: str
    espn_event_id: str
    competition_id: str
    fighter_a_id: str
    fighter_b_id: str
    fighter_a_name: str
    fighter_b_name: str
    commence_time: datetime


def _fighter_name(session: Session, fighter_id: str) -> str:
    fighter = session.get(CanonicalFighter, fighter_id)
    return (fighter.display_name if fighter is not None else fighter_id).strip()


def _targets(session: Session, *, as_of: datetime) -> tuple[_BoutOddsTarget, ...]:
    rows = load_upcoming_dwcs_events_from_db(
        session, as_of=as_of, horizon=TICK_EVENT_HORIZON
    )
    found: list[_BoutOddsTarget] = []
    for item in rows:
        event = session.get(CanonicalEvent, str(item["event_id"]))
        if event is None or event.scheduled_start_at is None:
            continue
        event_src = session.scalar(
            select(EventSourceId).where(
                EventSourceId.event_id == event.id,
                EventSourceId.source == ESPN_IDENTITY_SOURCE,
            )
        )
        if event_src is None:
            continue
        start = event.scheduled_start_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        else:
            start = start.astimezone(UTC)
        for bout_id in item.get("bout_ids") or ():
            bout = session.get(CanonicalBout, str(bout_id))
            if bout is None:
                continue
            bout_src = session.scalar(
                select(BoutSourceId).where(
                    BoutSourceId.bout_id == bout.id,
                    BoutSourceId.source == ESPN_IDENTITY_SOURCE,
                )
            )
            if bout_src is None:
                continue
            found.append(
                _BoutOddsTarget(
                    bout_id=bout.id,
                    espn_event_id=event_src.external_id,
                    competition_id=bout_src.external_id,
                    fighter_a_id=bout.fighter_a_id,
                    fighter_b_id=bout.fighter_b_id,
                    fighter_a_name=_fighter_name(session, bout.fighter_a_id),
                    fighter_b_name=_fighter_name(session, bout.fighter_b_id),
                    commence_time=start,
                )
            )
    return tuple(found)


def _unknown_for(
    target: _BoutOddsTarget,
    *,
    observed_at: datetime,
) -> UnknownMarketObservation:
    return UnknownMarketObservation(
        provider=ESPN_ODDS_PROVIDER,
        region=_REGION,
        event_id=target.competition_id,
        home_team=target.fighter_a_name,
        away_team=target.fighter_b_name,
        bookmaker_key=None,
        bookmaker_title=None,
        provider_market_key="h2h",
        market_family=MarketFamily.MONEYLINE,
        availability=QuoteAvailability.UNKNOWN,
        observed_at=observed_at,
        commence_time=target.commence_time,
        snapshot_at=None,
        dedupe_key=unknown_dedupe_key(
            provider=ESPN_ODDS_PROVIDER,
            event_id=target.competition_id,
            bookmaker_key=None,
            region=_REGION,
            provider_market_key="h2h",
            observed_at=observed_at,
            snapshot_at=None,
        ),
    )


def _outcome_for_side(target: _BoutOddsTarget, athlete_id: str | None, index: int) -> OutcomeKey:
    if athlete_id and athlete_id == target.fighter_a_id:
        return OutcomeKey.FIGHTER_A
    if athlete_id and athlete_id == target.fighter_b_id:
        return OutcomeKey.FIGHTER_B
    return OutcomeKey.FIGHTER_A if index == 0 else OutcomeKey.FIGHTER_B


def _quotes_for(
    target: _BoutOddsTarget,
    *,
    parsed,
    observed_at: datetime,
    raw_ref: str,
) -> list[NormalizedQuote]:
    quotes: list[NormalizedQuote] = []
    for book in parsed.quotes:
        for index, side in enumerate(book.sides):
            decimal = american_to_decimal(float(side.american))
            outcome = _outcome_for_side(target, side.athlete_id, index)
            quotes.append(
                NormalizedQuote(
                    provider=ESPN_ODDS_PROVIDER,
                    bookmaker_key=book.bookmaker_key,
                    bookmaker_title=book.bookmaker_title,
                    region=_REGION,
                    event_id=target.competition_id,
                    home_team=target.fighter_a_name,
                    away_team=target.fighter_b_name,
                    market_family=MarketFamily.MONEYLINE,
                    provider_market_key="h2h",
                    outcome_key=outcome,
                    outcome_label=outcome.value,
                    line_point=None,
                    price_decimal=decimal,
                    availability=QuoteAvailability.AVAILABLE,
                    observed_at=observed_at,
                    source_updated_at=None,
                    commence_time=target.commence_time,
                    snapshot_at=None,
                    raw_ref=raw_ref,
                    dedupe_key=quote_dedupe_key(
                        provider=ESPN_ODDS_PROVIDER,
                        event_id=target.competition_id,
                        bookmaker_key=book.bookmaker_key,
                        region=_REGION,
                        market_family=MarketFamily.MONEYLINE,
                        outcome_key=outcome,
                        line_point=None,
                        price_decimal=decimal,
                        source_updated_at=None,
                        commence_time=target.commence_time,
                        snapshot_at=None,
                        raw_ref=raw_ref,
                        home_team=target.fighter_a_name,
                        away_team=target.fighter_b_name,
                    ),
                )
            )
    return quotes


def run_espn_odds_snapshot(
    session: Session,
    *,
    as_of: datetime,
    context: Mapping[str, Any],
) -> HandlerResult:
    """Fetch ESPN /odds per upcoming ESPN bout. Empty items stay unknown."""
    observed = ensure_utc(as_of, field="as_of")
    targets = _targets(session, as_of=observed)
    if not targets:
        return HandlerResult(
            status=JobStatus.SUCCESS,
            counts={"targets": 0, "unknown": 0, "quotes": 0},
            detail="espn odds: no upcoming ESPN-mapped DWCS bouts",
        )

    fixtures: Mapping[str, Any] = dict(context.get("espn_odds") or {})
    cache_dir = Path(str(context.get("cache_dir") or "/tmp/dwcs-espn-odds"))
    client: EspnPublicClient | None = None
    owns_client = False
    injected = context.get("espn_odds_client")
    if injected is not None:
        client = injected
    elif not fixtures:
        client = EspnPublicClient(cache_dir=cache_dir)
        owns_client = True

    store = OddsQuoteStore(session)
    unknown: list[UnknownMarketObservation] = []
    quotes: list[NormalizedQuote] = []
    blocked = 0
    try:
        for target in targets:
            payload = fixtures.get(target.competition_id)
            digest = "fixture"
            if payload is None:
                if client is None:
                    unknown.append(_unknown_for(target, observed_at=observed))
                    continue
                try:
                    payload, digest = client.fetch_competition_odds(
                        event_id=target.espn_event_id,
                        competition_id=target.competition_id,
                    )
                except SourceBlockedError:
                    blocked += 1
                    unknown.append(_unknown_for(target, observed_at=observed))
                    continue
            try:
                parsed = parse_espn_odds(payload)
            except EspnSchemaError:
                unknown.append(_unknown_for(target, observed_at=observed))
                continue
            if parsed.empty:
                unknown.append(_unknown_for(target, observed_at=observed))
                continue
            quotes.extend(
                _quotes_for(
                    target,
                    parsed=parsed,
                    observed_at=observed,
                    raw_ref=digest,
                )
            )
        quote_result = store.append_quotes(quotes) if quotes else None
        unknown_result = store.append_unknown_observations(unknown) if unknown else None
        session.flush()
    finally:
        if owns_client and client is not None:
            client.close()

    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={
            "targets": len(targets),
            "unknown": (unknown_result.unknown_inserted if unknown_result else 0),
            "quotes": (quote_result.inserted if quote_result else 0),
            "blocked": blocked,
        },
        detail=(
            "espn odds: recorded unknown availability"
            if not quotes
            else "espn odds: stored public moneylines (not Bet365)"
        ),
        blocks_downstream=False,
    )
