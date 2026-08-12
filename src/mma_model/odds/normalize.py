"""Normalize The Odds API payloads into DWCS-200 market contracts (DWCS-201)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as url_quote

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.odds.types import (
    PROVIDER_MARKET_TO_FAMILY,
    PROVIDER_THE_ODDS_API,
    SUPPORTED_PROVIDER_MARKETS,
    NormalizedQuote,
    NormalizeReport,
    QuoteAvailability,
    UnknownMarketObservation,
)

_OUTCOME_NAME_ALIASES: Mapping[str, OutcomeKey] = {
    "over": OutcomeKey.OVER,
    "under": OutcomeKey.UNDER,
}


class OddsTimestampError(ValueError):
    """Malformed or unusable provider/operator timestamp."""


def ensure_utc(value: datetime, *, field: str) -> datetime:
    """Require an aware datetime and return its UTC equivalent."""
    if value.tzinfo is None:
        raise OddsTimestampError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def parse_utc_datetime(value: object, *, field: str = "timestamp") -> datetime | None:
    """Parse Odds API ISO/unix timestamps into aware UTC datetimes.

    Empty/missing values return None. Malformed values raise OddsTimestampError.
    Provider ISO strings must include an explicit offset (``Z`` or ``±HH:MM``);
    timezone-naive strings are rejected rather than silently assumed UTC.
    Unix epoch values are treated as UTC.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value, field=field)
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=UTC)
        # Preserve original for error context; normalize Z before fromisoformat.
        original = text
        text = text.replace("Z", "+00:00")
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            raise OddsTimestampError(
                f"{field} must include UTC offset (naive rejected): {original!r}"
            )
        return dt.astimezone(UTC)
    except OddsTimestampError:
        raise
    except (TypeError, ValueError, OSError):
        raise OddsTimestampError(f"invalid {field}: {value!r}") from None


def american_to_decimal(price: float | int) -> float:
    """Convert American odds to decimal; pass through values already decimal-like."""
    american = float(price)
    if american >= 100:
        return round(1.0 + american / 100.0, 6)
    if american <= -100:
        return round(1.0 + 100.0 / abs(american), 6)
    if american > 1.0:
        return round(american, 6)
    raise ValueError(f"unrecognized odds price: {price!r}")


def decimal_price(price: object, *, odds_format: str) -> float:
    """Normalize a provider price into decimal odds."""
    if price is None or price == "":
        raise ValueError("missing price")
    numeric = float(price)
    fmt = odds_format.lower().strip()
    if fmt == "decimal":
        if numeric <= 1.0:
            raise ValueError(f"invalid decimal price: {price!r}")
        return round(numeric, 6)
    if fmt == "american":
        return american_to_decimal(numeric)
    raise ValueError(f"unsupported odds_format: {odds_format!r}")


def raw_reference(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    """Content-addressed reference for a sanitized original fragment (no secrets)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quote_dedupe_key(
    *,
    provider: str,
    event_id: str,
    bookmaker_key: str,
    region: str,
    market_family: MarketFamily,
    outcome_key: OutcomeKey,
    line_point: float | None,
    price_decimal: float,
    source_updated_at: datetime | None,
    commence_time: datetime,
    snapshot_at: datetime | None,
) -> str:
    """Stable identity for append-only quote deduplication."""
    point = "" if line_point is None else f"{float(line_point):.4f}"
    src = "" if source_updated_at is None else source_updated_at.isoformat()
    snap = "" if snapshot_at is None else snapshot_at.isoformat()
    material = "|".join(
        [
            provider,
            event_id,
            bookmaker_key,
            region,
            market_family.value,
            outcome_key.value,
            point,
            f"{price_decimal:.6f}",
            src,
            commence_time.isoformat(),
            snap,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def unknown_dedupe_key(
    *,
    provider: str,
    event_id: str,
    bookmaker_key: str | None,
    region: str,
    provider_market_key: str,
    observed_at: datetime,
    snapshot_at: datetime | None,
) -> str:
    """Stable identity for append-only unknown-market observations.

    Historical rows key on provider ``snapshot_at`` so reruns of the same
    snapshot are idempotent. Current polls key on operator ``observed_at`` so
    distinct wall-clock polls remain distinct.
    """
    time_key = (
        snapshot_at.isoformat()
        if snapshot_at is not None
        else observed_at.isoformat()
    )
    material = "|".join(
        [
            provider,
            event_id,
            bookmaker_key or "",
            region,
            provider_market_key,
            QuoteAvailability.UNKNOWN.value,
            "historical" if snapshot_at is not None else "current",
            time_key,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_single_region(regions: str) -> str:
    """Require exactly one region label for truthful persistence."""
    parts = [part.strip() for part in str(regions).split(",") if part.strip()]
    if len(parts) != 1:
        raise ValueError(
            f"exactly one region is required for odds persistence; got {regions!r}"
        )
    return parts[0]


def map_outcome_key(
    *,
    market_family: MarketFamily,
    outcome_name: str,
    home_team: str,
    away_team: str,
) -> OutcomeKey | None:
    """Map a provider outcome name onto a DWCS-200 outcome key."""
    name = outcome_name.strip()
    if not name:
        return None
    if market_family is MarketFamily.MONEYLINE:
        if _names_equal(name, home_team):
            return OutcomeKey.FIGHTER_A
        if _names_equal(name, away_team):
            return OutcomeKey.FIGHTER_B
        return None
    if market_family is MarketFamily.TOTALS:
        return _OUTCOME_NAME_ALIASES.get(name.casefold())
    return None


def normalize_odds_payload(
    events: Sequence[Mapping[str, Any]],
    *,
    observed_at: datetime,
    region: str,
    odds_format: str = "decimal",
    requested_markets: Sequence[str] | None = None,
    snapshot_at: datetime | None = None,
    provider: str = PROVIDER_THE_ODDS_API,
) -> NormalizeReport:
    """Normalize current or historical Odds API event payloads into quotes.

    Current and historical event objects share the same bookmaker/market schema,
    so identical event bodies normalize identically regardless of wrapper.
    """
    observed_utc = ensure_utc(observed_at, field="observed_at")
    region_key = parse_single_region(region)
    snapshot_utc = (
        None if snapshot_at is None else ensure_utc(snapshot_at, field="snapshot_at")
    )
    requested = tuple(
        m.strip() for m in (requested_markets or ("h2h",)) if str(m).strip()
    )
    quotes: list[NormalizedQuote] = []
    unknowns: list[UnknownMarketObservation] = []
    skipped_unsupported: set[str] = set()
    skipped_unmapped: set[str] = set()
    skipped_line_points: set[str] = set()

    for event in events:
        event_id = str(event.get("id") or "").strip()
        home_team = str(event.get("home_team") or "").strip()
        away_team = str(event.get("away_team") or "").strip()
        try:
            commence = parse_utc_datetime(event.get("commence_time"), field="commence_time")
        except OddsTimestampError:
            skipped_unmapped.add(f"{event_id or '?'}:bad_commence_time")
            continue
        if not event_id or not home_team or not away_team or commence is None:
            skipped_unmapped.add(f"{event_id or '?'}:incomplete_event")
            continue

        bookmakers = [
            book
            for book in (event.get("bookmakers") or [])
            if isinstance(book, Mapping) and str(book.get("key") or "").strip()
        ]
        # Drop consensus/reference labels that must never be treated as Bet365.
        bookmakers = [
            book
            for book in bookmakers
            if str(book.get("key") or "").casefold()
            not in {"consensus", "reference", "average"}
        ]

        if not bookmakers:
            for market_key in requested:
                if market_key not in SUPPORTED_PROVIDER_MARKETS:
                    # Unsupported-by-normalizer ≠ provider-missing: skip only.
                    skipped_unsupported.add(market_key)
                    continue
                unknowns.append(
                    _unknown_observation(
                        provider=provider,
                        region=region_key,
                        event_id=event_id,
                        home_team=home_team,
                        away_team=away_team,
                        bookmaker_key=None,
                        bookmaker_title=None,
                        provider_market_key=market_key,
                        observed_at=observed_utc,
                        commence_time=commence,
                        snapshot_at=snapshot_utc,
                    )
                )
            continue

        for bookmaker in bookmakers:
            book_key = str(bookmaker.get("key") or "").strip()
            book_title = str(bookmaker.get("title") or book_key).strip()
            # Markets present in the payload (including rejected malformed ones).
            # Malformed ≠ provider-missing: never emit UNKNOWN for these.
            present_supported: set[str] = set()

            for market in bookmaker.get("markets") or []:
                if not isinstance(market, Mapping):
                    continue
                market_key = str(market.get("key") or "").strip()
                if not market_key:
                    continue
                if market_key not in SUPPORTED_PROVIDER_MARKETS:
                    skipped_unsupported.add(market_key)
                    continue
                family = PROVIDER_MARKET_TO_FAMILY[market_key]
                catalog = catalog_for_family(family)
                present_supported.add(market_key)
                try:
                    source_updated = parse_utc_datetime(
                        market.get("last_update") or bookmaker.get("last_update"),
                        field="source_updated_at",
                    )
                except OddsTimestampError:
                    # Fail closed at market scope: do not store any quotes from
                    # this market with weakened PIT provenance.
                    skipped_unmapped.add(
                        f"{event_id}:{book_key}:{market_key}:bad_last_update"
                    )
                    continue

                for outcome in market.get("outcomes") or []:
                    if not isinstance(outcome, Mapping):
                        continue
                    label = str(outcome.get("name") or "").strip()
                    outcome_key = map_outcome_key(
                        market_family=family,
                        outcome_name=label,
                        home_team=home_team,
                        away_team=away_team,
                    )
                    if outcome_key is None:
                        skipped_unmapped.add(f"{market_key}:{label}")
                        continue
                    assert_known_outcome(family, outcome_key)
                    raw_price = outcome.get("price")
                    try:
                        price = decimal_price(raw_price, odds_format=odds_format)
                    except ValueError:
                        skipped_unmapped.add(f"{market_key}:{label}:bad_price")
                        continue
                    point_raw = outcome.get("point")
                    try:
                        line_point = None if point_raw in (None, "") else float(point_raw)
                    except (TypeError, ValueError):
                        skipped_unmapped.add(f"{market_key}:{label}:bad_point")
                        continue
                    if family is not MarketFamily.TOTALS:
                        line_point = None
                    if not catalog.is_valid_line_point(line_point):
                        skipped_line_points.add(
                            f"{market_key}:{label}:{point_raw!r}"
                        )
                        continue

                    # Hash the sanitized original provider fragment (pre-conversion).
                    fragment = {
                        "event_id": event_id,
                        "bookmaker": book_key,
                        "market": market_key,
                        "outcome": label,
                        "point": point_raw,
                        "price": raw_price,
                        "last_update": market.get("last_update")
                        or bookmaker.get("last_update"),
                    }
                    raw_ref = raw_reference(fragment)
                    dedupe = quote_dedupe_key(
                        provider=provider,
                        event_id=event_id,
                        bookmaker_key=book_key,
                        region=region_key,
                        market_family=family,
                        outcome_key=outcome_key,
                        line_point=line_point,
                        price_decimal=price,
                        source_updated_at=source_updated,
                        commence_time=commence,
                        snapshot_at=snapshot_utc,
                    )
                    quotes.append(
                        NormalizedQuote(
                            provider=provider,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            region=region_key,
                            event_id=event_id,
                            home_team=home_team,
                            away_team=away_team,
                            market_family=family,
                            provider_market_key=market_key,
                            outcome_key=outcome_key,
                            outcome_label=label,
                            line_point=line_point,
                            price_decimal=price,
                            availability=QuoteAvailability.AVAILABLE,
                            observed_at=observed_utc,
                            source_updated_at=source_updated,
                            commence_time=commence,
                            snapshot_at=snapshot_utc,
                            raw_ref=raw_ref,
                            dedupe_key=dedupe,
                        )
                    )

            for market_key in requested:
                if market_key not in SUPPORTED_PROVIDER_MARKETS:
                    # Never persist UNKNOWN for unsupported contracts.
                    skipped_unsupported.add(market_key)
                    continue
                if market_key not in present_supported:
                    unknowns.append(
                        _unknown_observation(
                            provider=provider,
                            region=region_key,
                            event_id=event_id,
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            provider_market_key=market_key,
                            observed_at=observed_utc,
                            commence_time=commence,
                            snapshot_at=snapshot_utc,
                        )
                    )

    return NormalizeReport(
        quotes=tuple(quotes),
        unknown_observations=tuple(unknowns),
        skipped_unsupported_markets=tuple(sorted(skipped_unsupported)),
        skipped_unmapped_outcomes=tuple(sorted(skipped_unmapped)),
        skipped_unsupported_line_points=tuple(sorted(skipped_line_points)),
    )


def sanitize_secret_text(message: str, secret: str) -> str:
    """Redact exact and URL-encoded secret forms from error text."""
    cleaned = message
    if not secret:
        return cleaned
    for candidate in {secret, url_quote(secret, safe="")}:
        if candidate:
            cleaned = cleaned.replace(candidate, "***")
    return cleaned


def _unknown_observation(
    *,
    provider: str,
    region: str,
    event_id: str,
    home_team: str,
    away_team: str,
    bookmaker_key: str | None,
    bookmaker_title: str | None,
    provider_market_key: str,
    observed_at: datetime,
    commence_time: datetime,
    snapshot_at: datetime | None,
) -> UnknownMarketObservation:
    if provider_market_key not in SUPPORTED_PROVIDER_MARKETS:
        raise ValueError(
            f"refusing unknown observation for unsupported market {provider_market_key!r}"
        )
    family = PROVIDER_MARKET_TO_FAMILY[provider_market_key]
    return UnknownMarketObservation(
        provider=provider,
        region=region,
        event_id=event_id,
        home_team=home_team,
        away_team=away_team,
        bookmaker_key=bookmaker_key,
        bookmaker_title=bookmaker_title,
        provider_market_key=provider_market_key,
        market_family=family,
        availability=QuoteAvailability.UNKNOWN,
        observed_at=observed_at,
        commence_time=commence_time,
        snapshot_at=snapshot_at,
        dedupe_key=unknown_dedupe_key(
            provider=provider,
            event_id=event_id,
            bookmaker_key=bookmaker_key,
            region=region,
            provider_market_key=provider_market_key,
            observed_at=observed_at,
            snapshot_at=snapshot_at,
        ),
    )


def _names_equal(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())
