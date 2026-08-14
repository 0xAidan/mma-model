"""Parse ESPN competition odds JSON. Empty items are unknown, never Bet365."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mma_model.odds.bookmaker_keys import is_bet365_bookmaker_key, normalize_bookmaker_key
from mma_model.sources.espn_public.errors import EspnSchemaError
from mma_model.sources.policy import SourceId

ESPN_ODDS_PROVIDER = SourceId.ESPN_PUBLIC.value


@dataclass(frozen=True)
class EspnMoneylineSide:
    athlete_id: str | None
    american: int


@dataclass(frozen=True)
class EspnMoneylineQuote:
    bookmaker_key: str
    bookmaker_title: str
    sides: tuple[EspnMoneylineSide, ...]


@dataclass(frozen=True)
class EspnOddsParse:
    """One competition odds document: quotes and/or an unknown observation."""

    empty: bool
    quotes: tuple[EspnMoneylineQuote, ...]
    skipped_bet365: int = 0


def _as_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _moneyline(payload: Mapping[str, Any]) -> int | None:
    for key in ("moneyLine", "moneyline", "money_line"):
        raw = payload.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    current = _as_mapping(payload.get("current"))
    if current:
        return _moneyline(current)
    return None


def _side(payload: Mapping[str, Any]) -> EspnMoneylineSide | None:
    american = _moneyline(payload)
    if american is None:
        return None
    athlete = _as_mapping(payload.get("athlete"))
    athlete_id = str(athlete.get("id") or payload.get("athleteId") or "").strip() or None
    return EspnMoneylineSide(athlete_id=athlete_id, american=american)


def _provider_label(item: Mapping[str, Any]) -> tuple[str, str]:
    provider = _as_mapping(item.get("provider"))
    title = str(provider.get("name") or provider.get("displayName") or "espn").strip()
    key = normalize_bookmaker_key(str(provider.get("id") or title or "espn"))
    if not key:
        key = "espn"
    return key, title or key


def parse_espn_odds(payload: Mapping[str, Any]) -> EspnOddsParse:
    if not isinstance(payload, Mapping) or "items" not in payload:
        raise EspnSchemaError("ESPN odds JSON must include an items array")
    items = payload.get("items")
    if not isinstance(items, list):
        raise EspnSchemaError("ESPN odds items must be an array")
    if not items:
        return EspnOddsParse(empty=True, quotes=())

    quotes: list[EspnMoneylineQuote] = []
    skipped_bet365 = 0
    for raw in items:
        if not isinstance(raw, Mapping):
            raise EspnSchemaError("ESPN odds item must be an object")
        book_key, book_title = _provider_label(raw)
        if is_bet365_bookmaker_key(book_key) or is_bet365_bookmaker_key(book_title):
            skipped_bet365 += 1
            continue
        home = _side(_as_mapping(raw.get("homeTeamOdds") or raw.get("homeOdds")))
        away = _side(_as_mapping(raw.get("awayTeamOdds") or raw.get("awayOdds")))
        sides = tuple(side for side in (home, away) if side is not None)
        if len(sides) != 2:
            continue
        quotes.append(
            EspnMoneylineQuote(
                bookmaker_key=book_key,
                bookmaker_title=book_title,
                sides=sides,
            )
        )
    if not quotes:
        return EspnOddsParse(empty=True, quotes=(), skipped_bet365=skipped_bet365)
    return EspnOddsParse(empty=False, quotes=tuple(quotes), skipped_bet365=skipped_bet365)


