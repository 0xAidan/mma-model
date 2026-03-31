"""The Odds API client — live MMA odds (historical on paid tiers)."""

from __future__ import annotations

from typing import Any

import httpx

from mma_model.config import get_settings

MMA_KEY = "mma_mixed_martial_arts"


def fetch_mma_odds(regions: str = "us", markets: str = "h2h") -> list[dict[str, Any]]:
    s = get_settings()
    if not s.odds_api_key:
        raise RuntimeError("Set ODDS_API_KEY in .env to fetch odds.")
    url = "https://api.the-odds-api.com/v4/sports/" + MMA_KEY + "/odds"
    params = {
        "apiKey": s.odds_api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american",
    }
    with httpx.Client(timeout=60.0) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.json()
