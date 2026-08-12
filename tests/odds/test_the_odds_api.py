"""DWCS-201 The Odds API normalization, storage, and snapshot tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mma_model.db.base import Base
from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.session import sqlite_connect_pragmas
from mma_model.db.tables.odds import OddsQuotaObservation, OddsQuote
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.odds.normalize import (
    american_to_decimal,
    normalize_odds_payload,
    parse_utc_datetime,
)
from mma_model.odds.snapshot import run_odds_audit, run_odds_snapshot
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.the_odds_api import TheOddsApiClient
from mma_model.odds.types import PROVIDER_THE_ODDS_API, QuotaHeaders, QuoteAvailability

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds"
OBSERVED = datetime(2026, 8, 11, 21, 5, tzinfo=UTC)


def _load(name: str) -> dict:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds.db'}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    import mma_model.db.tables.odds  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def test_american_and_decimal_price_conversion() -> None:
    assert american_to_decimal(-150) == pytest.approx(1.666667)
    assert american_to_decimal(130) == pytest.approx(2.3)
    assert american_to_decimal(1.74) == pytest.approx(1.74)


def test_current_and_historical_fixtures_normalize_identically() -> None:
    current = _load("current_odds.json")["data"]
    historical_wrapper = _load("historical_odds.json")
    historical = historical_wrapper["data"]["data"]
    assert current == historical

    current_report = normalize_odds_payload(
        current,
        observed_at=OBSERVED,
        region="us",
        odds_format="decimal",
        requested_markets=["h2h", "totals"],
        snapshot_at=None,
    )
    historical_report = normalize_odds_payload(
        historical,
        observed_at=OBSERVED,
        region="us",
        odds_format="decimal",
        requested_markets=["h2h", "totals"],
        snapshot_at=parse_utc_datetime(historical_wrapper["timestamp"]),
    )

    assert len(current_report.quotes) == 4
    assert len(historical_report.quotes) == 4
    assert current_report.skipped_unsupported_markets == ("method_of_victory",)
    assert historical_report.skipped_unsupported_markets == ("method_of_victory",)

    def core(quote):  # noqa: ANN001
        return (
            quote.provider,
            quote.bookmaker_key,
            quote.event_id,
            quote.market_family,
            quote.outcome_key,
            quote.line_point,
            quote.price_decimal,
            quote.availability,
            quote.source_updated_at,
            quote.commence_time,
            quote.raw_ref,
        )

    assert [core(q) for q in current_report.quotes] == [
        core(q) for q in historical_report.quotes
    ]
    # Historical wrapper carries snapshot_at; current does not — dedupe keys differ.
    assert current_report.quotes[0].dedupe_key != historical_report.quotes[0].dedupe_key


def test_normalize_maps_moneyline_onto_dwcs_200_outcomes() -> None:
    report = normalize_odds_payload(
        _load("current_odds.json")["data"],
        observed_at=OBSERVED,
        region="us",
        odds_format="decimal",
        requested_markets=["h2h"],
    )
    moneyline = [q for q in report.quotes if q.market_family is MarketFamily.MONEYLINE]
    assert {q.outcome_key for q in moneyline} == {
        OutcomeKey.FIGHTER_A,
        OutcomeKey.FIGHTER_B,
    }
    assert all(q.availability is QuoteAvailability.AVAILABLE for q in moneyline)
    assert all(q.provider == PROVIDER_THE_ODDS_API for q in moneyline)
    assert "bet365" not in json.dumps([q.bookmaker_key for q in moneyline]).casefold()


def test_missing_requested_market_is_unknown_never_suspended() -> None:
    events = [
        {
            "id": "e1",
            "sport_key": "mma_mixed_martial_arts",
            "commence_time": "2026-08-12T00:00:00Z",
            "home_team": "A Fighter",
            "away_team": "B Fighter",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-08-11T21:00:00Z",
                            "outcomes": [
                                {"name": "A Fighter", "price": 1.8},
                                {"name": "B Fighter", "price": 2.0},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    report = normalize_odds_payload(
        events,
        observed_at=OBSERVED,
        region="us",
        requested_markets=["h2h", "totals"],
    )
    assert report.unknown_missing_markets == ("totals",)
    assert all(q.availability is not QuoteAvailability.SUSPENDED for q in report.quotes)


def test_quota_headers_and_empty_response_cost(tmp_path: Path) -> None:
    client = TheOddsApiClient(api_key="", fixture_dir=FIXTURES)
    current = client.fetch_current_odds()
    assert current.quota.requests_remaining == 480
    assert current.quota.requests_used == 20
    assert current.quota.requests_last == 1
    assert current.empty is False

    empty_payload = _load("empty_odds.json")
    empty_quota = QuotaHeaders.from_headers(empty_payload["headers"])
    assert empty_quota.requests_last == 0

    # Client synthesizes last=0 when an empty live-shaped response omits the header.
    synthesized = QuotaHeaders(requests_remaining=10, requests_used=0, requests_last=None)
    if synthesized.requests_last is None:
        synthesized = QuotaHeaders(
            requests_remaining=synthesized.requests_remaining,
            requests_used=synthesized.requests_used,
            requests_last=0,
        )
    assert synthesized.requests_last == 0

    session = _session(tmp_path)
    store = OddsQuoteStore(session)
    store.record_quota(
        provider=PROVIDER_THE_ODDS_API,
        endpoint="current_odds",
        observed_at=OBSERVED,
        quota=empty_quota,
        empty_response=True,
    )
    session.commit()
    row = session.scalar(select(OddsQuotaObservation))
    assert row is not None
    assert row.empty_response == 1
    assert row.requests_last == 0
    session.close()


def test_append_only_dedupe_and_guards(tmp_path: Path) -> None:
    session = _session(tmp_path)
    store = OddsQuoteStore(session)
    report = normalize_odds_payload(
        _load("current_odds.json")["data"],
        observed_at=OBSERVED,
        region="us",
        requested_markets=["h2h", "totals"],
    )
    first = store.append_quotes(report.quotes)
    second = store.append_quotes(report.quotes)
    session.commit()
    assert first.inserted == 4
    assert second.inserted == 0
    assert second.deduped == 4
    assert session.scalar(select(func.count()).select_from(OddsQuote)) == 4

    quote = session.scalar(select(OddsQuote).limit(1))
    assert quote is not None
    quote.price_decimal = 9.99
    with pytest.raises(IntegrityError, match="append-only"):
        session.commit()
    session.rollback()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text("DELETE FROM odds_quotes"))
        session.commit()
    session.rollback()
    session.close()


def test_client_events_and_market_discovery_from_fixtures() -> None:
    client = TheOddsApiClient(api_key="", fixture_dir=FIXTURES)
    events = client.list_events()
    assert events.empty is False
    assert events.events[0].id == "evt-dwcs-ref-001"
    assert events.quota.requests_last == 0

    markets = client.discover_markets(events.events[0].id)
    assert "h2h" in {m.market_key for m in markets.markets}
    assert markets.quota.requests_remaining == 499

    historical = client.fetch_historical_odds(date="2026-08-11T21:00:00Z")
    assert historical.historical is True
    assert historical.snapshot_at == datetime(2026, 8, 11, 20, 55, tzinfo=UTC)
    assert len(historical.events) == 1


def test_snapshot_and_audit_commands_use_fixtures_without_key(tmp_path: Path) -> None:
    session = _session(tmp_path)
    result = run_odds_snapshot(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h,totals",
        regions="us",
        fixture_dir=FIXTURES,
        observed_at=OBSERVED,
    )
    session.commit()
    assert result.used_fixtures is True
    assert result.claims_bet365 is False
    assert result.inserted == 4
    assert result.quota["x-requests-last"] == 1
    assert "method_of_victory" in result.skipped_unsupported_markets

    hist = run_odds_snapshot(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h,totals",
        historical_date="2026-08-11T21:00:00Z",
        fixture_dir=FIXTURES,
        observed_at=OBSERVED,
    )
    session.commit()
    assert hist.mode == "historical"
    assert hist.inserted == 4  # different dedupe because snapshot_at set
    assert hist.snapshot_at is not None

    audit = run_odds_audit(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h",
        fixture_dir=FIXTURES,
    )
    session.commit()
    blob = json.dumps(audit)
    assert audit["claims_bet365"] is False
    assert audit["events"]["count"] == 1
    assert "1.74" not in blob
    assert "2.15" not in blob
    assert "price_decimal" not in blob
    session.close()


def test_no_bet365_or_unsupported_prop_claims_in_normalized_output() -> None:
    report = normalize_odds_payload(
        _load("current_odds.json")["data"],
        observed_at=OBSERVED,
        region="us",
        requested_markets=["h2h", "totals", "method_of_victory"],
    )
    serialized = json.dumps(
        [
            {
                "book": q.bookmaker_key,
                "market": q.market_family.value,
                "outcome": q.outcome_key.value,
            }
            for q in report.quotes
        ]
    )
    assert "bet365" not in serialized.casefold()
    assert "method" not in {q.market_family.value for q in report.quotes}
    assert "method_of_victory" in report.skipped_unsupported_markets
    assert "method_of_victory" in report.unknown_missing_markets
