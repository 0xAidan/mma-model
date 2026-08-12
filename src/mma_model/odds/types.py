"""Typed Odds API response and normalized quote contracts (DWCS-201).

Exact bookmaker lines are optional enrichment. Reference quotes must never be
labeled as Bet365. Missing lines are ``unknown``, never ``suspended`` without
provider evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from mma_model.domain.markets import MarketFamily, OutcomeKey

PROVIDER_THE_ODDS_API: Final[str] = "the_odds_api"
PROVIDER_LABEL_THE_ODDS_API: Final[str] = "the-odds-api"


class QuoteAvailability(StrEnum):
    """Line availability for a normalized quote."""

    AVAILABLE = "available"
    UNKNOWN = "unknown"
    SUSPENDED = "suspended"


class OddsMarketKey(StrEnum):
    """Provider market keys this ticket normalizes (supported subset)."""

    H2H = "h2h"
    TOTALS = "totals"


SUPPORTED_PROVIDER_MARKETS: Final[frozenset[str]] = frozenset(
    member.value for member in OddsMarketKey
)

PROVIDER_MARKET_TO_FAMILY: Final[Mapping[str, MarketFamily]] = {
    OddsMarketKey.H2H.value: MarketFamily.MONEYLINE,
    OddsMarketKey.TOTALS.value: MarketFamily.TOTALS,
}


@dataclass(frozen=True)
class QuotaHeaders:
    """Usage quota headers returned by The Odds API."""

    requests_remaining: int | None
    requests_used: int | None
    requests_last: int | None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> QuotaHeaders:
        return cls(
            requests_remaining=_parse_int_header(headers, "x-requests-remaining"),
            requests_used=_parse_int_header(headers, "x-requests-used"),
            requests_last=_parse_int_header(headers, "x-requests-last"),
        )

    def as_dict(self) -> dict[str, int | None]:
        return {
            "x-requests-remaining": self.requests_remaining,
            "x-requests-used": self.requests_used,
            "x-requests-last": self.requests_last,
        }


@dataclass(frozen=True)
class OddsEvent:
    """Provider event without bookmaker prices."""

    id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str
    sport_title: str | None = None


@dataclass(frozen=True)
class DiscoveredMarket:
    """One recently seen market key for a bookmaker on an event."""

    bookmaker_key: str
    bookmaker_title: str
    market_key: str
    last_update: datetime | None


@dataclass(frozen=True)
class NormalizedQuote:
    """Append-ready reference quote mapped onto DWCS-200 market contracts."""

    provider: str
    bookmaker_key: str
    bookmaker_title: str
    region: str
    event_id: str
    home_team: str
    away_team: str
    market_family: MarketFamily
    provider_market_key: str
    outcome_key: OutcomeKey
    outcome_label: str
    line_point: float | None
    price_decimal: float
    availability: QuoteAvailability
    observed_at: datetime
    source_updated_at: datetime | None
    commence_time: datetime
    snapshot_at: datetime | None
    raw_ref: str
    dedupe_key: str


@dataclass(frozen=True)
class EventsResponse:
    events: tuple[OddsEvent, ...]
    quota: QuotaHeaders
    empty: bool


@dataclass(frozen=True)
class MarketDiscoveryResponse:
    event_id: str
    markets: tuple[DiscoveredMarket, ...]
    quota: QuotaHeaders
    empty: bool
    raw_bookmakers: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class OddsResponse:
    """Current or historical odds payload plus quota."""

    events: tuple[Mapping[str, Any], ...]
    quota: QuotaHeaders
    empty: bool
    snapshot_at: datetime | None = None
    previous_timestamp: datetime | None = None
    next_timestamp: datetime | None = None
    historical: bool = False


@dataclass(frozen=True)
class NormalizeReport:
    """Result of normalizing a current/historical odds payload."""

    quotes: tuple[NormalizedQuote, ...]
    skipped_unsupported_markets: tuple[str, ...]
    skipped_unmapped_outcomes: tuple[str, ...]
    unknown_missing_markets: tuple[str, ...]


def _parse_int_header(headers: Mapping[str, str], name: str) -> int | None:
    raw = None
    for key, value in headers.items():
        if key.lower() == name.lower():
            raw = value
            break
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None
