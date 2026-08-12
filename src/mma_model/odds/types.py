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

ALLOWED_REQUESTED_SERIES: Final[frozenset[str]] = frozenset({"dwcs", "mma"})
PROVIDER_SCOPE_UNMATCHED: Final[str] = "provider_unmatched"


class QuoteAvailability(StrEnum):
    """Line availability for a normalized quote or market observation."""

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

# Families persisted by DWCS-201 (supported provider mappings only).
PERSISTED_MARKET_FAMILY_VALUES: Final[tuple[str, ...]] = tuple(
    sorted({family.value for family in PROVIDER_MARKET_TO_FAMILY.values()})
)

REQUESTS_LAST_SOURCE_PROVIDER: Final[str] = "provider"
REQUESTS_LAST_SOURCE_MISSING: Final[str] = "missing"
REQUESTS_LAST_SOURCE_INFERRED_EMPTY: Final[str] = "inferred_empty_zero"
REQUESTS_LAST_SOURCES: Final[frozenset[str]] = frozenset(
    {
        REQUESTS_LAST_SOURCE_PROVIDER,
        REQUESTS_LAST_SOURCE_MISSING,
        REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    }
)

QUOTE_AVAILABILITY_VALUES: Final[frozenset[str]] = frozenset(
    member.value for member in QuoteAvailability
)


def assert_supported_provider_market_pair(
    provider_market_key: str,
    market_family: MarketFamily | str,
) -> None:
    """Reject unknown provider keys and mismatched DWCS-201 family pairs."""
    expected = PROVIDER_MARKET_TO_FAMILY.get(provider_market_key)
    if expected is None:
        raise ValueError(
            f"unsupported provider_market_key for persistence: {provider_market_key!r}"
        )
    family_value = (
        market_family.value
        if isinstance(market_family, MarketFamily)
        else str(market_family)
    )
    if family_value != expected.value:
        raise ValueError(
            "mismatched provider_market_key/market_family pair: "
            f"{provider_market_key!r} -> {family_value!r} "
            f"(expected {expected.value!r})"
        )


@dataclass(frozen=True)
class QuotaHeaders:
    """Usage quota headers returned by The Odds API.

    ``requests_last`` is the raw parsed ``x-requests-last`` header only.
    Empty-response zero-cost policy is recorded separately via
    ``requests_last_inferred`` / ``requests_last_source`` so provider-reported
    ``0`` remains distinguishable from a missing header.
    """

    requests_remaining: int | None
    requests_used: int | None
    requests_last: int | None
    requests_last_inferred: int | None = None
    requests_last_source: str = REQUESTS_LAST_SOURCE_MISSING

    def __post_init__(self) -> None:
        for name, value in (
            ("requests_remaining", self.requests_remaining),
            ("requests_used", self.requests_used),
            ("requests_last", self.requests_last),
            ("requests_last_inferred", self.requests_last_inferred),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be nonnegative (got {value!r})")

        source = self.requests_last_source
        if source == REQUESTS_LAST_SOURCE_PROVIDER:
            if self.requests_last is None or self.requests_last_inferred is not None:
                raise ValueError(
                    "provider quota source requires non-null requests_last "
                    "and null requests_last_inferred"
                )
            return
        if source == REQUESTS_LAST_SOURCE_INFERRED_EMPTY:
            if self.requests_last is not None or self.requests_last_inferred != 0:
                raise ValueError(
                    "inferred_empty_zero requires null requests_last and "
                    "requests_last_inferred=0"
                )
            return
        if source == REQUESTS_LAST_SOURCE_MISSING:
            if self.requests_last is not None or self.requests_last_inferred is not None:
                raise ValueError(
                    "missing quota source requires null requests_last and "
                    "null requests_last_inferred"
                )
            return
        raise ValueError(f"unsupported requests_last_source: {source!r}")

    @classmethod
    def from_headers(
        cls, headers: Mapping[str, str], *, empty: bool = False
    ) -> QuotaHeaders:
        remaining = _parse_int_header(headers, "x-requests-remaining")
        used = _parse_int_header(headers, "x-requests-used")
        last = _parse_int_header(headers, "x-requests-last")
        if last is not None:
            return cls(
                requests_remaining=remaining,
                requests_used=used,
                requests_last=last,
                requests_last_inferred=None,
                requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
            )
        if empty:
            return cls(
                requests_remaining=remaining,
                requests_used=used,
                requests_last=None,
                requests_last_inferred=0,
                requests_last_source=REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
            )
        return cls(
            requests_remaining=remaining,
            requests_used=used,
            requests_last=None,
            requests_last_inferred=None,
            requests_last_source=REQUESTS_LAST_SOURCE_MISSING,
        )

    @property
    def expected_cost(self) -> int | None:
        """Provider-reported last cost, else inferred empty-policy cost."""
        if self.requests_last is not None:
            return self.requests_last
        return self.requests_last_inferred

    def as_dict(self) -> dict[str, Any]:
        return {
            "x-requests-remaining": self.requests_remaining,
            "x-requests-used": self.requests_used,
            "x-requests-last": self.requests_last,
            "requests_last_inferred": self.requests_last_inferred,
            "requests_last_source": self.requests_last_source,
            "expected_cost": self.expected_cost,
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

    def __post_init__(self) -> None:
        assert_supported_provider_market_pair(
            self.provider_market_key, self.market_family
        )
        if self.availability.value not in QUOTE_AVAILABILITY_VALUES:
            raise ValueError(f"unsupported quote availability: {self.availability!r}")
        if self.price_decimal <= 1.0:
            raise ValueError(
                f"price_decimal must be > 1.0 for reference quotes (got {self.price_decimal!r})"
            )


@dataclass(frozen=True)
class UnknownMarketObservation:
    """Auditable missing-market observation for supported contracts only.

    Never used for unsupported-by-normalizer markets. Never suspended without
    provider evidence. ``snapshot_at`` set ⇒ historical provider snapshot;
    ``snapshot_at`` null ⇒ current poll observation.
    """

    provider: str
    region: str
    event_id: str
    home_team: str
    away_team: str
    bookmaker_key: str | None
    bookmaker_title: str | None
    provider_market_key: str
    market_family: MarketFamily
    availability: QuoteAvailability
    observed_at: datetime
    commence_time: datetime
    snapshot_at: datetime | None
    dedupe_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.market_family, MarketFamily):
            raise TypeError(
                f"market_family must be MarketFamily, got {type(self.market_family)!r}"
            )
        assert_supported_provider_market_pair(
            self.provider_market_key, self.market_family
        )
        if self.availability is not QuoteAvailability.UNKNOWN:
            raise ValueError(
                "UnknownMarketObservation.availability must be UNKNOWN "
                f"(got {self.availability!r})"
            )

    @property
    def poll_kind(self) -> str:
        return "historical" if self.snapshot_at is not None else "current"


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
    unknown_observations: tuple[UnknownMarketObservation, ...]
    skipped_unsupported_markets: tuple[str, ...]
    skipped_unmapped_outcomes: tuple[str, ...]
    skipped_unsupported_line_points: tuple[str, ...]


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
