"""The Odds API client — typed events, markets, current/historical odds (DWCS-201).

Preserves ``fetch_mma_odds`` for legacy ``mma-model odds`` compatibility.
Live HTTP requires ``ODDS_API_KEY``. Offline fixtures require an explicit
``fixture_dir`` supplied by the caller (never an implicit tests/ path).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from mma_model.config import get_settings
from mma_model.odds.normalize import OddsTimestampError, parse_utc_datetime, sanitize_secret_text
from mma_model.odds.types import (
    DiscoveredMarket,
    EventsResponse,
    MarketDiscoveryResponse,
    OddsEvent,
    OddsResponse,
    QuotaHeaders,
)

MMA_KEY = "mma_mixed_martial_arts"
THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


class OddsApiError(RuntimeError):
    """Raised for configuration or transport failures (never embeds API keys)."""


class TheOddsApiClient:
    """Typed client for The Odds API MMA reference quotes."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str = THE_ODDS_API_BASE,
        timeout: float = 60.0,
        fixture_dir: Path | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = (api_key if api_key is not None else settings.odds_api_key).strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._fixture_dir = fixture_dir

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def list_events(self, *, sport: str = MMA_KEY) -> EventsResponse:
        """GET /sports/{sport}/events — does not consume quota credits."""
        if self._fixture_dir is not None:
            return self._events_from_fixture("events.json")
        payload, headers = self._get_json(f"/sports/{sport}/events", params={})
        events = tuple(self._parse_event(item) for item in _as_list(payload))
        quota = QuotaHeaders.from_headers(headers)
        return EventsResponse(events=events, quota=quota, empty=len(events) == 0)

    def discover_markets(
        self,
        event_id: str,
        *,
        sport: str = MMA_KEY,
        regions: str = "us",
    ) -> MarketDiscoveryResponse:
        """GET /sports/{sport}/events/{eventId}/markets."""
        if self._fixture_dir is not None:
            return self._markets_from_fixture(event_id)
        path = f"/sports/{sport}/events/{event_id}/markets"
        payload, headers = self._get_json(path, params={"regions": regions})
        bookmakers = tuple(
            dict(item)
            for item in (payload.get("bookmakers") if isinstance(payload, Mapping) else [])
            or []
            if isinstance(item, Mapping)
        )
        markets = tuple(_iter_discovered_markets(bookmakers))
        return MarketDiscoveryResponse(
            event_id=event_id,
            markets=markets,
            quota=QuotaHeaders.from_headers(headers),
            empty=len(markets) == 0,
            raw_bookmakers=bookmakers,
        )

    def fetch_current_odds(
        self,
        *,
        sport: str = MMA_KEY,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> OddsResponse:
        """GET /sports/{sport}/odds — featured markets for upcoming/live events."""
        if self._fixture_dir is not None:
            return self._odds_from_fixture("current_odds.json", historical=False)
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
        }
        payload, headers = self._get_json(f"/sports/{sport}/odds", params=params)
        events = tuple(dict(item) for item in _as_list(payload) if isinstance(item, Mapping))
        quota = QuotaHeaders.from_headers(headers)
        empty = len(events) == 0
        if empty and quota.requests_last is None:
            quota = QuotaHeaders(
                requests_remaining=quota.requests_remaining,
                requests_used=quota.requests_used,
                requests_last=0,
            )
        return OddsResponse(events=events, quota=quota, empty=empty, historical=False)

    def fetch_historical_odds(
        self,
        *,
        date: datetime | str,
        sport: str = MMA_KEY,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> OddsResponse:
        """GET /historical/sports/{sport}/odds — snapshot at or before ``date``."""
        if self._fixture_dir is not None:
            return self._odds_from_fixture("historical_odds.json", historical=True)
        date_text = _format_snapshot_date(date)
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": odds_format,
            "date": date_text,
        }
        payload, headers = self._get_json(f"/historical/sports/{sport}/odds", params=params)
        if not isinstance(payload, Mapping):
            raise OddsApiError("Unexpected historical odds payload; expected an object")
        data = payload.get("data") or []
        events = tuple(dict(item) for item in _as_list(data) if isinstance(item, Mapping))
        quota = QuotaHeaders.from_headers(headers)
        empty = len(events) == 0
        if empty and quota.requests_last is None:
            quota = QuotaHeaders(
                requests_remaining=quota.requests_remaining,
                requests_used=quota.requests_used,
                requests_last=0,
            )
        try:
            snapshot_at = parse_utc_datetime(payload.get("timestamp"), field="timestamp")
            previous_timestamp = parse_utc_datetime(
                payload.get("previous_timestamp"), field="previous_timestamp"
            )
            next_timestamp = parse_utc_datetime(
                payload.get("next_timestamp"), field="next_timestamp"
            )
        except OddsTimestampError as exc:
            raise OddsApiError(str(exc)) from None
        return OddsResponse(
            events=events,
            quota=quota,
            empty=empty,
            snapshot_at=snapshot_at,
            previous_timestamp=previous_timestamp,
            next_timestamp=next_timestamp,
            historical=True,
        )

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str],
    ) -> tuple[Any, dict[str, str]]:
        if not self._api_key:
            raise OddsApiError(
                "ODDS_API_KEY is required for live odds requests. "
                "Pass an explicit fixture_dir only for offline/test mode."
            )
        query = {"apiKey": self._api_key, "dateFormat": "iso", **dict(params)}
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.get(url, params=query)
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    raise OddsApiError("Invalid JSON payload from The Odds API") from None
                headers = {k: v for k, v in response.headers.items()}
                return payload, headers
        except OddsApiError:
            raise
        except httpx.HTTPError as exc:
            raise OddsApiError(sanitize_secret_text(str(exc), self._api_key)) from None
        except Exception as exc:  # pragma: no cover - defensive
            raise OddsApiError(
                sanitize_secret_text(f"odds request failed: {exc}", self._api_key)
            ) from None

    def _events_from_fixture(self, name: str) -> EventsResponse:
        payload = self._load_fixture(name)
        events = tuple(self._parse_event(item) for item in _as_list(payload.get("data", payload)))
        quota = QuotaHeaders.from_headers(payload.get("headers") or {})
        return EventsResponse(events=events, quota=quota, empty=len(events) == 0)

    def _markets_from_fixture(self, event_id: str) -> MarketDiscoveryResponse:
        payload = self._load_fixture("market_discovery.json")
        by_event = payload.get("by_event_id")
        if isinstance(by_event, Mapping):
            body = by_event.get(event_id)
            if not isinstance(body, Mapping):
                raise OddsApiError(
                    f"market discovery fixture has no entry for event_id={event_id!r}"
                )
        else:
            body = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
            if not isinstance(body, Mapping):
                raise OddsApiError("market discovery fixture missing data object")
            fixture_event_id = str(body.get("id") or "").strip()
            if fixture_event_id and fixture_event_id != event_id:
                raise OddsApiError(
                    f"market discovery fixture event_id {fixture_event_id!r} "
                    f"does not match requested {event_id!r}"
                )
        bookmakers = tuple(
            dict(item)
            for item in (body.get("bookmakers") if isinstance(body, Mapping) else []) or []
            if isinstance(item, Mapping)
        )
        markets = tuple(_iter_discovered_markets(bookmakers))
        return MarketDiscoveryResponse(
            event_id=event_id,
            markets=markets,
            quota=QuotaHeaders.from_headers(payload.get("headers") or {}),
            empty=len(markets) == 0,
            raw_bookmakers=bookmakers,
        )

    def _odds_from_fixture(self, name: str, *, historical: bool) -> OddsResponse:
        payload = self._load_fixture(name)
        headers = QuotaHeaders.from_headers(payload.get("headers") or {})
        if historical:
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
            if not isinstance(data, Mapping):
                raise OddsApiError("historical odds fixture missing data object")
            events = tuple(
                dict(item)
                for item in _as_list(data.get("data") if "data" in data else data.get("events"))
                if isinstance(item, Mapping)
            )
            if not events and isinstance(payload.get("events"), list):
                events = tuple(
                    dict(item) for item in payload["events"] if isinstance(item, Mapping)
                )
            empty = len(events) == 0
            if empty and headers.requests_last is None:
                headers = QuotaHeaders(
                    requests_remaining=headers.requests_remaining,
                    requests_used=headers.requests_used,
                    requests_last=0,
                )
            try:
                snapshot_at = parse_utc_datetime(
                    payload.get("timestamp") or data.get("timestamp"),
                    field="timestamp",
                )
                previous_timestamp = parse_utc_datetime(
                    payload.get("previous_timestamp") or data.get("previous_timestamp"),
                    field="previous_timestamp",
                )
                next_timestamp = parse_utc_datetime(
                    payload.get("next_timestamp") or data.get("next_timestamp"),
                    field="next_timestamp",
                )
            except OddsTimestampError as exc:
                raise OddsApiError(str(exc)) from None
            return OddsResponse(
                events=events,
                quota=headers,
                empty=empty,
                snapshot_at=snapshot_at,
                previous_timestamp=previous_timestamp,
                next_timestamp=next_timestamp,
                historical=True,
            )
        events = tuple(
            dict(item)
            for item in _as_list(payload.get("data", payload.get("events", [])))
            if isinstance(item, Mapping)
        )
        empty = len(events) == 0
        if empty and headers.requests_last is None:
            headers = QuotaHeaders(
                requests_remaining=headers.requests_remaining,
                requests_used=headers.requests_used,
                requests_last=0,
            )
        return OddsResponse(events=events, quota=headers, empty=empty, historical=False)

    def _load_fixture(self, name: str) -> dict[str, Any]:
        if self._fixture_dir is None:
            raise OddsApiError("fixture_dir is required for offline odds fixtures")
        path = self._fixture_dir / name
        if not path.is_file():
            raise OddsApiError(f"missing odds fixture: {path.name}")
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise OddsApiError(f"odds fixture must be an object: {path.name}")
        return loaded

    def _parse_event(self, item: Any) -> OddsEvent:
        if not isinstance(item, Mapping):
            raise OddsApiError("Unexpected event object")
        event_id = str(item.get("id") or "").strip()
        home_team = str(item.get("home_team") or "").strip()
        away_team = str(item.get("away_team") or "").strip()
        if not event_id:
            raise OddsApiError("Event missing id")
        if not home_team or not away_team:
            raise OddsApiError(f"Event {event_id!r} missing participant names")
        try:
            commence = parse_utc_datetime(item.get("commence_time"), field="commence_time")
        except OddsTimestampError as exc:
            raise OddsApiError(str(exc)) from None
        if commence is None:
            raise OddsApiError(f"Event {event_id!r} missing commence_time")
        return OddsEvent(
            id=event_id,
            sport_key=str(item.get("sport_key") or MMA_KEY),
            sport_title=(
                None
                if item.get("sport_title") in (None, "")
                else str(item.get("sport_title"))
            ),
            commence_time=commence,
            home_team=home_team,
            away_team=away_team,
        )


def fetch_mma_odds(regions: str = "us", markets: str = "h2h") -> list[dict[str, Any]]:
    """Legacy compatibility helper used by ``mma-model odds``."""
    client = TheOddsApiClient()
    if not client.has_api_key:
        raise OddsApiError("Set ODDS_API_KEY in .env to fetch odds.")
    response = client.fetch_current_odds(
        regions=regions,
        markets=markets,
        odds_format="american",
    )
    return [dict(event) for event in response.events]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise OddsApiError("Unexpected payload; expected a JSON list")


def _iter_discovered_markets(
    bookmakers: Sequence[Mapping[str, Any]],
) -> list[DiscoveredMarket]:
    found: list[DiscoveredMarket] = []
    for bookmaker in bookmakers:
        book_key = str(bookmaker.get("key") or "").strip()
        book_title = str(bookmaker.get("title") or book_key).strip()
        if not book_key:
            continue
        for market in bookmaker.get("markets") or []:
            if not isinstance(market, Mapping):
                continue
            market_key = str(market.get("key") or "").strip()
            if not market_key:
                continue
            try:
                last_update = parse_utc_datetime(
                    market.get("last_update"), field="last_update"
                )
            except OddsTimestampError:
                last_update = None
            found.append(
                DiscoveredMarket(
                    bookmaker_key=book_key,
                    bookmaker_title=book_title,
                    market_key=market_key,
                    last_update=last_update,
                )
            )
    return found


def _format_snapshot_date(value: datetime | str) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = str(value).strip()
    if not text:
        raise OddsApiError("historical snapshot date is required")
    return text
