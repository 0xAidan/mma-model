"""Polite ESPN public JSON client (no cookies, no API key)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mma_model.sources.espn_public.errors import EspnSchemaError
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http_politeness import HttpPolitenessConfig, load_http_politeness

SITE_HOST = "site.api.espn.com"
CORE_HOST = "sports.core.api.espn.com"
SCOREBOARD_PATH = "/apis/site/v2/sports/mma/ufc/scoreboard"
ODDS_PATH = (
    "/v2/sports/mma/leagues/ufc/events/{event_id}/competitions/{competition_id}/odds"
)


class EspnPublicClient:
    """Scoreboard + optional competition odds over allowlisted ESPN JSON hosts."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        politeness: HttpPolitenessConfig | None = None,
        robots_disallow: bool = False,
        site_transport=None,
        core_transport=None,
    ) -> None:
        self.politeness = politeness or load_http_politeness()
        self._site = PoliteHttpClient(
            host=SITE_HOST,
            politeness=self.politeness,
            cache_dir=cache_dir,
            robots_disallow=robots_disallow,
            transport=site_transport,
        )
        self._core = PoliteHttpClient(
            host=CORE_HOST,
            politeness=self.politeness,
            cache_dir=cache_dir,
            robots_disallow=robots_disallow,
            transport=core_transport,
        )

    def close(self) -> None:
        self._site.close()
        self._core.close()

    def fetch_scoreboard(
        self,
        *,
        dates: str,
        limit: int = 200,
    ) -> tuple[dict[str, Any], str]:
        url = f"https://{SITE_HOST}{SCOREBOARD_PATH}?dates={dates}&limit={limit}"
        text, digest = self._site.get_text(url)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EspnSchemaError(f"ESPN scoreboard is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise EspnSchemaError("ESPN scoreboard root must be an object")
        return payload, digest

    def fetch_competition_odds(
        self,
        *,
        event_id: str,
        competition_id: str,
    ) -> tuple[dict[str, Any], str]:
        path = ODDS_PATH.format(event_id=event_id, competition_id=competition_id)
        url = f"https://{CORE_HOST}{path}"
        text, digest = self._core.get_text(url)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EspnSchemaError(f"ESPN odds is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise EspnSchemaError("ESPN odds root must be an object")
        return payload, digest
