"""Snapshot orchestration for The Odds API reference quotes (DWCS-201)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.odds.normalize import normalize_odds_payload
from mma_model.odds.store import OddsQuoteStore, QuoteStoreResult
from mma_model.odds.the_odds_api import TheOddsApiClient, default_fixture_dir
from mma_model.odds.types import (
    PROVIDER_LABEL_THE_ODDS_API,
    PROVIDER_THE_ODDS_API,
    NormalizeReport,
    QuotaHeaders,
)


@dataclass(frozen=True)
class SnapshotResult:
    provider: str
    series: str
    mode: str
    markets: str
    regions: str
    empty: bool
    quote_count: int
    inserted: int
    deduped: int
    skipped_unsupported_markets: tuple[str, ...]
    unknown_missing_markets: tuple[str, ...]
    quota: dict[str, int | None]
    snapshot_at: str | None
    observed_at: str
    used_fixtures: bool
    claims_bet365: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_odds_client(
    *,
    provider: str,
    api_key: str | None = None,
    fixture_dir: Path | None = None,
    allow_fixtures: bool = True,
) -> tuple[TheOddsApiClient, bool]:
    """Build a client; fall back to fixtures when no live key is configured."""
    if provider not in {PROVIDER_THE_ODDS_API, PROVIDER_LABEL_THE_ODDS_API}:
        raise ValueError(
            f"unsupported odds provider {provider!r}; "
            f"DWCS-201 supports only {PROVIDER_LABEL_THE_ODDS_API}"
        )
    client = TheOddsApiClient(api_key=api_key, fixture_dir=None)
    if client.has_api_key:
        return client, False
    if not allow_fixtures:
        raise RuntimeError(
            "ODDS_API_KEY is required unless fixture mode is enabled "
            "(default for offline snapshot)."
        )
    fixtures = fixture_dir or default_fixture_dir()
    return TheOddsApiClient(api_key="", fixture_dir=fixtures), True


def run_odds_snapshot(
    session: Session,
    *,
    series: str = "dwcs",
    provider: str = PROVIDER_LABEL_THE_ODDS_API,
    markets: str = "h2h",
    regions: str = "us",
    historical_date: datetime | str | None = None,
    fixture_dir: Path | None = None,
    observed_at: datetime | None = None,
) -> SnapshotResult:
    """Fetch (or fixture-load), normalize, and append-only store reference quotes."""
    observed = observed_at or datetime.now(UTC)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)

    client, used_fixtures = resolve_odds_client(
        provider=provider,
        fixture_dir=fixture_dir,
        allow_fixtures=True,
    )
    store = OddsQuoteStore(session)

    if historical_date is not None:
        response = client.fetch_historical_odds(
            date=historical_date,
            regions=regions,
            markets=markets,
            odds_format="decimal",
        )
        endpoint = "historical_odds"
        mode = "historical"
    else:
        response = client.fetch_current_odds(
            regions=regions,
            markets=markets,
            odds_format="decimal",
        )
        endpoint = "current_odds"
        mode = "current"

    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint=endpoint,
        observed_at=observed,
        quota=response.quota,
        empty_response=response.empty,
    )

    report = normalize_odds_payload(
        response.events,
        observed_at=observed,
        region=regions.split(",")[0].strip() or "us",
        odds_format="decimal",
        requested_markets=[m.strip() for m in markets.split(",") if m.strip()],
        snapshot_at=response.snapshot_at,
        provider=PROVIDER_THE_ODDS_API,
    )
    store_result = _persist_report(store, report)
    session.flush()

    return SnapshotResult(
        provider=PROVIDER_THE_ODDS_API,
        series=series,
        mode=mode,
        markets=markets,
        regions=regions,
        empty=response.empty,
        quote_count=len(report.quotes),
        inserted=store_result.inserted,
        deduped=store_result.deduped,
        skipped_unsupported_markets=report.skipped_unsupported_markets,
        unknown_missing_markets=report.unknown_missing_markets,
        quota=response.quota.as_dict(),
        snapshot_at=None
        if response.snapshot_at is None
        else response.snapshot_at.isoformat(),
        observed_at=observed.isoformat(),
        used_fixtures=used_fixtures,
        claims_bet365=False,
    )


def run_odds_audit(
    session: Session,
    *,
    series: str = "dwcs",
    provider: str = PROVIDER_LABEL_THE_ODDS_API,
    markets: str = "h2h",
    regions: str = "us",
    fixture_dir: Path | None = None,
) -> dict[str, Any]:
    """Sanitized audit summary: events, market discovery sample, quota, no prices."""
    observed = datetime.now(UTC)
    client, used_fixtures = resolve_odds_client(
        provider=provider,
        fixture_dir=fixture_dir,
        allow_fixtures=True,
    )
    store = OddsQuoteStore(session)

    events_response = client.list_events()
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint="events",
        observed_at=observed,
        quota=events_response.quota,
        empty_response=events_response.empty,
    )

    discovery: dict[str, Any] | None = None
    if events_response.events:
        sample = events_response.events[0]
        market_response = client.discover_markets(sample.id, regions=regions)
        store.record_quota(
            provider=PROVIDER_THE_ODDS_API,
            endpoint="market_discovery",
            observed_at=observed,
            quota=market_response.quota,
            empty_response=market_response.empty,
        )
        discovery = {
            "event_id": sample.id,
            "empty": market_response.empty,
            "market_keys": sorted({m.market_key for m in market_response.markets}),
            "bookmaker_keys": sorted({m.bookmaker_key for m in market_response.markets}),
            "quota": market_response.quota.as_dict(),
        }

    odds_response = client.fetch_current_odds(
        regions=regions,
        markets=markets,
        odds_format="decimal",
    )
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint="current_odds",
        observed_at=observed,
        quota=odds_response.quota,
        empty_response=odds_response.empty,
    )
    report = normalize_odds_payload(
        odds_response.events,
        observed_at=observed,
        region=regions.split(",")[0].strip() or "us",
        odds_format="decimal",
        requested_markets=[m.strip() for m in markets.split(",") if m.strip()],
        provider=PROVIDER_THE_ODDS_API,
    )

    return {
        "provider": PROVIDER_THE_ODDS_API,
        "series": series,
        "used_fixtures": used_fixtures,
        "claims_bet365": False,
        "events": {
            "count": len(events_response.events),
            "empty": events_response.empty,
            "quota": events_response.quota.as_dict(),
            "ids": [event.id for event in events_response.events],
        },
        "market_discovery": discovery,
        "current_odds": {
            "empty": odds_response.empty,
            "event_count": len(odds_response.events),
            "normalized_quote_count": len(report.quotes),
            "skipped_unsupported_markets": list(report.skipped_unsupported_markets),
            "unknown_missing_markets": list(report.unknown_missing_markets),
            "quota": odds_response.quota.as_dict(),
            # Prices intentionally omitted from audit output.
        },
        "product_note": (
            "Exact bookmaker lines are optional enrichment. Sportsbook-agnostic "
            "actionable price guidance remains the required fallback. Reference "
            "odds are never labeled as Bet365."
        ),
    }


def empty_quota_report(quota: QuotaHeaders, *, empty: bool) -> dict[str, Any]:
    """Report empty-response quota semantics for operators/tests."""
    return {
        "empty": empty,
        "quota": quota.as_dict(),
        "billed": bool(quota.requests_last and quota.requests_last > 0),
    }


def _persist_report(store: OddsQuoteStore, report: NormalizeReport) -> QuoteStoreResult:
    return store.append_quotes(report.quotes)
