"""Normalize The Odds API payloads into DWCS-200 market contracts (DWCS-201)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from mma_model.domain.markets import MarketFamily, OutcomeKey, assert_known_outcome
from mma_model.odds.types import (
    PROVIDER_MARKET_TO_FAMILY,
    PROVIDER_THE_ODDS_API,
    SUPPORTED_PROVIDER_MARKETS,
    NormalizedQuote,
    NormalizeReport,
    QuoteAvailability,
)

_OUTCOME_NAME_ALIASES: Mapping[str, OutcomeKey] = {
    "over": OutcomeKey.OVER,
    "under": OutcomeKey.UNDER,
}


def parse_utc_datetime(value: object) -> datetime | None:
    """Parse Odds API ISO/unix timestamps into aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(float(text), tz=UTC)
    text = text.replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def american_to_decimal(price: float | int) -> float:
    """Convert American odds to decimal; pass through values already decimal-like."""
    american = float(price)
    if american >= 100:
        return round(1.0 + american / 100.0, 6)
    if american <= -100:
        return round(1.0 + 100.0 / abs(american), 6)
    # Odds API decimal prices are typically > 1.0; treat remaining as decimal.
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
    """Content-addressed reference for a sanitized raw fragment (no secrets)."""
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
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware UTC")
    requested = tuple(
        m.strip() for m in (requested_markets or ("h2h",)) if str(m).strip()
    )
    quotes: list[NormalizedQuote] = []
    skipped_unsupported: set[str] = set()
    skipped_unmapped: set[str] = set()
    unknown_missing: set[str] = set()

    for event in events:
        event_id = str(event.get("id") or "").strip()
        home_team = str(event.get("home_team") or "").strip()
        away_team = str(event.get("away_team") or "").strip()
        commence = parse_utc_datetime(event.get("commence_time"))
        if not event_id or not home_team or not away_team or commence is None:
            continue

        seen_supported: set[str] = set()
        for bookmaker in event.get("bookmakers") or []:
            if not isinstance(bookmaker, Mapping):
                continue
            book_key = str(bookmaker.get("key") or "").strip()
            book_title = str(bookmaker.get("title") or book_key).strip()
            if not book_key:
                continue
            # Never relabel reference books as Bet365.
            if book_key.casefold() in {"consensus", "reference", "average"}:
                continue

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
                seen_supported.add(market_key)
                source_updated = parse_utc_datetime(
                    market.get("last_update") or bookmaker.get("last_update")
                )
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
                    try:
                        price = decimal_price(outcome.get("price"), odds_format=odds_format)
                    except ValueError:
                        skipped_unmapped.add(f"{market_key}:{label}:bad_price")
                        continue
                    point_raw = outcome.get("point")
                    line_point = None if point_raw in (None, "") else float(point_raw)
                    if family is MarketFamily.TOTALS and line_point is None:
                        skipped_unmapped.add(f"{market_key}:{label}:missing_point")
                        continue
                    if family is not MarketFamily.TOTALS:
                        line_point = None

                    fragment = {
                        "event_id": event_id,
                        "bookmaker": book_key,
                        "market": market_key,
                        "outcome": label,
                        "point": line_point,
                        "price": price,
                        "last_update": None
                        if source_updated is None
                        else source_updated.isoformat(),
                    }
                    raw_ref = raw_reference(fragment)
                    dedupe = quote_dedupe_key(
                        provider=provider,
                        event_id=event_id,
                        bookmaker_key=book_key,
                        region=region,
                        market_family=family,
                        outcome_key=outcome_key,
                        line_point=line_point,
                        price_decimal=price,
                        source_updated_at=source_updated,
                        commence_time=commence,
                        snapshot_at=snapshot_at,
                    )
                    quotes.append(
                        NormalizedQuote(
                            provider=provider,
                            bookmaker_key=book_key,
                            bookmaker_title=book_title,
                            region=region,
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
                            observed_at=observed_at,
                            source_updated_at=source_updated,
                            commence_time=commence,
                            snapshot_at=snapshot_at,
                            raw_ref=raw_ref,
                            dedupe_key=dedupe,
                        )
                    )

        for market_key in requested:
            if market_key in SUPPORTED_PROVIDER_MARKETS and market_key not in seen_supported:
                unknown_missing.add(market_key)
            elif market_key not in SUPPORTED_PROVIDER_MARKETS:
                skipped_unsupported.add(market_key)
                unknown_missing.add(market_key)

    return NormalizeReport(
        quotes=tuple(quotes),
        skipped_unsupported_markets=tuple(sorted(skipped_unsupported)),
        skipped_unmapped_outcomes=tuple(sorted(skipped_unmapped)),
        unknown_missing_markets=tuple(sorted(unknown_missing)),
    )


def _names_equal(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())
