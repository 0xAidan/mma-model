"""ESPN public scoreboard parse and polite client (offline fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id, canonical_fighter_id
from mma_model.sources.espn_public.client import EspnPublicClient
from mma_model.sources.espn_public.errors import EspnSchemaError
from mma_model.sources.espn_public.parser import parse_espn_scoreboard
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.http.polite_client import UrlNotAllowedError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/espn/scoreboard_upcoming_v1.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_keeps_scheduled_dwcs_and_drops_fight_night_and_completed() -> None:
    events = parse_espn_scoreboard(_payload())
    assert len(events) == 1
    event = events[0]
    assert event.espn_event_id == "600060733"
    assert event.name.startswith("Dana White")
    assert event.series == "dwcs"
    assert len(event.fights) == 1
    fight = event.fights[0]
    assert fight.competition_id == "401903489"
    assert fight.fighter_a_name == "Alex Contender"
    assert fight.fighter_b_name == "Jordan Prospect"
    assert canonical_event_id(event.espn_event_id) == canonical_event_id("600060733")
    assert canonical_bout_id(fight.competition_id) == canonical_bout_id("401903489")
    assert canonical_fighter_id(fight.fighter_a_id) != canonical_fighter_id(
        fight.fighter_b_id
    )


def test_parse_rejects_missing_events() -> None:
    with pytest.raises(EspnSchemaError):
        parse_espn_scoreboard({"leagues": []})


def test_client_fetches_allowlisted_scoreboard(tmp_path: Path) -> None:
    body = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "site.api.espn.com"
        assert request.url.path.endswith("/scoreboard")
        return httpx.Response(200, content=body, request=request)

    client = EspnPublicClient(
        cache_dir=tmp_path / "cache",
        site_transport=httpx.MockTransport(handler),
        core_transport=httpx.MockTransport(handler),
    )
    try:
        payload, digest = client.fetch_scoreboard(dates="2026")
        assert payload["events"][2]["id"] == "600060733"
        assert len(digest) == 64
    finally:
        client.close()


def test_client_stops_on_data_403(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden", request=request)

    client = EspnPublicClient(
        cache_dir=tmp_path / "cache",
        site_transport=httpx.MockTransport(handler),
        core_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SourceBlockedError, match="http_403"):
            client.fetch_scoreboard(dates="2026")
    finally:
        client.close()


def test_client_refuses_summary_path(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"should not request {request.url}")

    client = EspnPublicClient(
        cache_dir=tmp_path / "cache",
        site_transport=httpx.MockTransport(handler),
        core_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(UrlNotAllowedError):
            client._site.get_text(
                "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/summary?event=1"
            )
    finally:
        client.close()
