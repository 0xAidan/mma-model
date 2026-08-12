"""DWCS-201 The Odds API normalization, storage, and snapshot tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mma_model.config import get_settings
from mma_model.db.base import Base
from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.session import sqlite_connect_pragmas
from mma_model.db.tables.odds import (
    OddsAvailabilityObservation,
    OddsQuotaObservation,
    OddsQuote,
)
from mma_model.domain.markets import OutcomeKey
from mma_model.odds.normalize import (
    OddsTimestampError,
    american_to_decimal,
    normalize_odds_payload,
    parse_single_region,
    parse_utc_datetime,
    raw_reference,
)
from mma_model.odds.snapshot import (
    OddsConfigurationError,
    OddsOfflineModeError,
    require_disposable_database_url,
    resolve_odds_client,
    run_odds_audit,
    run_odds_snapshot,
    validate_requested_series,
)
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.the_odds_api import OddsApiError, TheOddsApiClient
from mma_model.odds.types import QuoteAvailability

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds"
OBSERVED = datetime(2026, 8, 11, 21, 5, tzinfo=UTC)
SECRET = "super-secret-odds-key-123"


def _load(name: str) -> dict:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds.db'}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    import mma_model.db.tables.odds  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    return sessionmaker(bind=engine, future=True)()


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
        snapshot_at=parse_utc_datetime(historical_wrapper["timestamp"], field="timestamp"),
    )

    assert len(current_report.quotes) == 4
    assert len(historical_report.quotes) == 4
    assert current_report.skipped_unsupported_markets == ("method_of_victory",)

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
    assert current_report.quotes[0].dedupe_key != historical_report.quotes[0].dedupe_key


def test_raw_ref_uses_original_provider_price_not_converted() -> None:
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
                                {"name": "A Fighter", "price": -150},
                                {"name": "B Fighter", "price": 130},
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
        odds_format="american",
        requested_markets=["h2h"],
    )
    assert len(report.quotes) == 2
    a = next(q for q in report.quotes if q.outcome_key is OutcomeKey.FIGHTER_A)
    assert a.price_decimal == pytest.approx(1.666667)
    expected = raw_reference(
        {
            "event_id": "e1",
            "bookmaker": "fanduel",
            "market": "h2h",
            "outcome": "A Fighter",
            "point": None,
            "price": -150,
            "last_update": "2026-08-11T21:00:00Z",
        }
    )
    assert a.raw_ref == expected


def test_totals_line_points_must_be_dwcs_200_canonical() -> None:
    events = [
        {
            "id": "e1",
            "commence_time": "2026-08-12T00:00:00Z",
            "home_team": "A Fighter",
            "away_team": "B Fighter",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "totals",
                            "last_update": "2026-08-11T21:00:00Z",
                            "outcomes": [
                                {"name": "Over", "price": 1.9, "point": 2.5},
                                {"name": "Under", "price": 1.9, "point": 2.5},
                                {"name": "Over", "price": 1.8, "point": 3.5},
                                {"name": "Under", "price": 2.0, "point": 0.5},
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
        requested_markets=["totals"],
    )
    assert len(report.quotes) == 2
    assert all(q.line_point == 2.5 for q in report.quotes)
    assert any("3.5" in item for item in report.skipped_unsupported_line_points)
    assert any("0.5" in item for item in report.skipped_unsupported_line_points)


def test_unknown_missing_is_per_bookmaker_and_persisted(tmp_path: Path) -> None:
    events = [
        {
            "id": "e1",
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
                        },
                        {
                            "key": "totals",
                            "last_update": "2026-08-11T21:00:00Z",
                            "outcomes": [
                                {"name": "Over", "price": 1.9, "point": 2.5},
                                {"name": "Under", "price": 1.9, "point": 2.5},
                            ],
                        },
                    ],
                },
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-08-11T21:00:00Z",
                            "outcomes": [
                                {"name": "A Fighter", "price": 1.85},
                                {"name": "B Fighter", "price": 1.95},
                            ],
                        }
                    ],
                },
            ],
        }
    ]
    report = normalize_odds_payload(
        events,
        observed_at=OBSERVED,
        region="us",
        requested_markets=["h2h", "totals"],
    )
    unknowns = report.unknown_observations
    assert len(unknowns) == 1
    assert unknowns[0].bookmaker_key == "draftkings"
    assert unknowns[0].provider_market_key == "totals"
    assert unknowns[0].availability is QuoteAvailability.UNKNOWN

    session = _session(tmp_path)
    store = OddsQuoteStore(session)
    first = store.append_unknown_observations(unknowns)
    second = store.append_unknown_observations(unknowns)
    session.commit()
    assert first.unknown_inserted == 1
    assert second.unknown_deduped == 1
    row = session.scalar(select(OddsAvailabilityObservation))
    assert row is not None
    assert row.availability == "unknown"
    assert row.bookmaker_key == "draftkings"
    session.close()


def test_event_with_no_bookmakers_records_event_level_unknown() -> None:
    events = [
        {
            "id": "e-empty",
            "commence_time": "2026-08-12T00:00:00Z",
            "home_team": "A Fighter",
            "away_team": "B Fighter",
            "bookmakers": [],
        }
    ]
    report = normalize_odds_payload(
        events,
        observed_at=OBSERVED,
        region="us",
        requested_markets=["h2h", "totals"],
    )
    assert report.quotes == ()
    assert len(report.unknown_observations) == 2
    assert all(o.bookmaker_key is None for o in report.unknown_observations)


def test_reject_multi_region_persistence() -> None:
    with pytest.raises(ValueError, match="exactly one region"):
        parse_single_region("us,uk")
    with pytest.raises(ValueError, match="exactly one region"):
        normalize_odds_payload(
            _load("current_odds.json")["data"],
            observed_at=OBSERVED,
            region="us,uk",
            requested_markets=["h2h"],
        )


def test_observed_at_converted_to_utc_and_malformed_timestamps() -> None:
    eastern = timezone(timedelta(hours=-4))
    observed = datetime(2026, 8, 11, 17, 5, tzinfo=eastern)
    report = normalize_odds_payload(
        _load("current_odds.json")["data"],
        observed_at=observed,
        region="us",
        requested_markets=["h2h"],
    )
    assert report.quotes[0].observed_at == datetime(2026, 8, 11, 21, 5, tzinfo=UTC)

    with pytest.raises(OddsTimestampError, match="observed_at"):
        normalize_odds_payload(
            [],
            observed_at=datetime(2026, 8, 11, 21, 5),
            region="us",
        )
    with pytest.raises(OddsTimestampError, match="invalid commence_time"):
        parse_utc_datetime("not-a-date", field="commence_time")


def test_no_key_default_cannot_mutate_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(OddsConfigurationError, match="ODDS_API_KEY is required"):
        resolve_odds_client(provider="the-odds-api", offline_fixtures=False)

    session = _session(tmp_path)
    before_quotes = session.scalar(select(func.count()).select_from(OddsQuote))
    before_quota = session.scalar(select(func.count()).select_from(OddsQuotaObservation))
    with pytest.raises(OddsConfigurationError):
        run_odds_snapshot(session, provider="the-odds-api", offline_fixtures=False)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(OddsQuote)) == before_quotes
    assert (
        session.scalar(select(func.count()).select_from(OddsQuotaObservation))
        == before_quota
    )
    session.close()
    get_settings.cache_clear()


def test_explicit_offline_fixtures_require_disposable_db() -> None:
    with pytest.raises(OddsOfflineModeError, match="fixture-dir"):
        resolve_odds_client(provider="the-odds-api", offline_fixtures=True)
    with pytest.raises(OddsOfflineModeError, match="disposable"):
        require_disposable_database_url(None)
    with pytest.raises(OddsOfflineModeError, match="live data/mma.db"):
        require_disposable_database_url("sqlite:///data/mma.db")


def test_snapshot_and_audit_explicit_offline_mode(tmp_path: Path) -> None:
    session = _session(tmp_path)
    result = run_odds_snapshot(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h,totals",
        regions="us",
        fixture_dir=FIXTURES,
        offline_fixtures=True,
        observed_at=OBSERVED,
    )
    session.commit()
    assert result.used_fixtures is True
    assert result.canonical_series_verified is False
    assert result.provider_scope == "provider_unmatched"
    assert result.region == "us"
    assert result.inserted == 4
    assert "method_of_victory" in result.skipped_unsupported_markets

    hist = run_odds_snapshot(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h,totals",
        historical_date="2026-08-11T21:00:00Z",
        fixture_dir=FIXTURES,
        offline_fixtures=True,
        observed_at=OBSERVED,
    )
    session.commit()
    assert hist.mode == "historical"
    assert hist.inserted == 4

    audit = run_odds_audit(
        session,
        series="dwcs",
        provider="the-odds-api",
        markets="h2h",
        fixture_dir=FIXTURES,
        offline_fixtures=True,
    )
    session.commit()
    blob = json.dumps(audit)
    assert audit["canonical_series_verified"] is False
    assert audit["provider_scope"] == "provider_unmatched"
    assert "1.74" not in blob
    assert "price_decimal" not in blob
    session.close()


def test_client_rejects_blank_events_and_mismatched_discovery_fixture() -> None:
    client = TheOddsApiClient(api_key="", fixture_dir=FIXTURES)
    events = client.list_events()
    assert events.events[0].id == "evt-dwcs-ref-001"
    markets = client.discover_markets("evt-dwcs-ref-001")
    assert "h2h" in {m.market_key for m in markets.markets}
    with pytest.raises(OddsApiError, match="no entry for event_id"):
        client.discover_markets("evt-other")

    with pytest.raises(OddsApiError, match="missing id"):
        TheOddsApiClient(api_key=SECRET)._parse_event(
            {
                "id": "",
                "home_team": "A",
                "away_team": "B",
                "commence_time": "2026-08-12T00:00:00Z",
            }
        )


def test_transport_errors_never_leak_api_key() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed", request=request)

    client = TheOddsApiClient(
        api_key=SECRET,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(OddsApiError) as exc_info:
        client.fetch_current_odds(regions="us", markets="h2h")
    text = str(exc_info.value)
    assert SECRET not in text
    assert quote(SECRET, safe="") not in text
    assert exc_info.value.__cause__ is None

    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = TheOddsApiClient(api_key=SECRET, transport=httpx.MockTransport(_timeout))
    with pytest.raises(OddsApiError) as exc_info:
        client.list_events()
    assert SECRET not in str(exc_info.value)
    assert SECRET not in repr(exc_info.value)

    def _status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key {SECRET}", request=request)

    client = TheOddsApiClient(api_key=SECRET, transport=httpx.MockTransport(_status))
    with pytest.raises(OddsApiError) as exc_info:
        client.fetch_current_odds()
    assert SECRET not in str(exc_info.value)

    def _bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    client = TheOddsApiClient(api_key=SECRET, transport=httpx.MockTransport(_bad_json))
    with pytest.raises(OddsApiError, match="Invalid JSON"):
        client.fetch_current_odds()


def test_append_only_quote_guards(tmp_path: Path) -> None:
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
    assert second.deduped == 4

    quote = session.scalar(select(OddsQuote).limit(1))
    assert quote is not None
    quote.price_decimal = 9.99
    with pytest.raises(IntegrityError, match="append-only"):
        session.commit()
    session.rollback()
    session.close()


def test_unsupported_series_rejected() -> None:
    with pytest.raises(OddsConfigurationError, match="unsupported requested series"):
        validate_requested_series("ufc-only")
