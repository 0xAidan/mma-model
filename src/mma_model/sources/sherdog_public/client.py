"""Sherdog public HTTP snapshot client (DWCS-105)."""

from __future__ import annotations

from pathlib import Path

from mma_model.history.access import public_get_text
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http_politeness import HttpPolitenessConfig, load_http_politeness

BASE_HOST = "sherdog.com"
BASE_URL = "https://www.sherdog.com"


class SherdogPublicClient:
    """Thin wrapper around PoliteHttpClient for Sherdog public pages."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        politeness: HttpPolitenessConfig | None = None,
        robots_disallow: bool = False,
        transport=None,
    ) -> None:
        self.politeness = politeness or load_http_politeness()
        self._http = PoliteHttpClient(
            host=BASE_HOST,
            politeness=self.politeness,
            cache_dir=cache_dir,
            robots_disallow=robots_disallow,
            transport=transport,
        )

    @property
    def polite_http(self) -> PoliteHttpClient:
        return self._http

    def close(self) -> None:
        self._http.close()

    @classmethod
    def live_base_url(cls) -> str:
        return BASE_URL

    @classmethod
    def fighter_url(cls, fighter_external_id: str) -> str:
        return f"{BASE_URL}/fighter/{fighter_external_id}"

    @classmethod
    def events_url(cls) -> str:
        return f"{BASE_URL}/events/"

    def fetch_fighter(self, fighter_external_id: str) -> tuple[str, str]:
        url = self.fighter_url(fighter_external_id)
        return public_get_text(self._http, url, host=BASE_HOST)

    def fetch_url(self, url: str) -> tuple[str, str]:
        return public_get_text(self._http, url, host=BASE_HOST)
