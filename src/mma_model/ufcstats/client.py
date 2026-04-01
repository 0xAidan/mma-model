"""HTTP client for ufcstats.com with polite delay."""

from __future__ import annotations

import time

import httpx

from mma_model.config import get_settings


class UFCStatsClient:
    BASE = "http://www.ufcstats.com"

    def __init__(self) -> None:
        s = get_settings()
        self._delay = s.ufcstats_request_delay_sec
        self._ua = s.ufcstats_user_agent
        self._last = 0.0
        self._client = httpx.Client(timeout=60.0, headers={"User-Agent": self._ua})

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last = time.monotonic()

    def get_text(self, url: str) -> str:
        self._wait()
        r = self._client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.text


def fetch_events_list_page(
    client: UFCStatsClient,
    *,
    segment: str = "completed",
    page: int | str = "all",
) -> str:
    """Fetch events index: segment is `completed` or `upcoming`. page=1,2,… or `all`."""
    return client.get_text(f"{client.BASE}/statistics/events/{segment}?page={page}")


def fetch_completed_events_page(client: UFCStatsClient, page: int | str = "all") -> str:
    """Backward-compatible alias for completed events only."""
    return fetch_events_list_page(client, segment="completed", page=page)


def fetch_url(client: UFCStatsClient, url: str) -> str:
    return client.get_text(url)
