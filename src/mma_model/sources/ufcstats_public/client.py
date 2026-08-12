"""UFCStats public HTTP snapshot client (DWCS-102)."""

from __future__ import annotations

from pathlib import Path

from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http_politeness import HttpPolitenessConfig, load_http_politeness

BASE_HOST = "ufcstats.com"
BASE_URL = "http://www.ufcstats.com"


class UfcstatsPublicClient:
    """Thin wrapper around PoliteHttpClient for UFCStats pages."""

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

    def fetch_event_details(self, event_external_id: str) -> tuple[str, str]:
        url = f"{BASE_URL}/event-details/{event_external_id}"
        return self._http.get_text(url)

    def fetch_fight_details(self, fight_external_id: str) -> tuple[str, str]:
        url = f"{BASE_URL}/fight-details/{fight_external_id}"
        return self._http.get_text(url)
