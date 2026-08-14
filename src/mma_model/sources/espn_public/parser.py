"""Parse ESPN UFC scoreboard JSON into scheduled DWCS cards only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mma_model.dwcs.names import is_dwcs_event_name, series_for_event_name
from mma_model.sources.espn_public.errors import EspnSchemaError

ESPN_IDENTITY_SOURCE = "espn"
_COMPLETED_STATUS_NAMES = frozenset(
    {
        "STATUS_FINAL",
        "STATUS_COMPLETED",
        "STATUS_CANCELLED",
        "STATUS_CANCELED",
        "STATUS_POSTPONED",
    }
)
_INACTIVE_STATES = frozenset({"post", "final"})


@dataclass(frozen=True)
class EspnUpcomingFight:
    """One scheduled ESPN competition with two named athletes."""

    competition_id: str
    fighter_a_id: str
    fighter_a_name: str
    fighter_b_id: str
    fighter_b_name: str
    weight_class: str | None
    start: datetime | None


@dataclass(frozen=True)
class EspnUpcomingEvent:
    """One scheduled DWCS card from the ESPN scoreboard."""

    espn_event_id: str
    name: str
    start: datetime
    location: str
    series: str
    fights: tuple[EspnUpcomingFight, ...]


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_espn_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _aware(parsed)


def status_is_scheduled(status: Mapping[str, Any] | None) -> bool:
    payload = dict(status or {})
    typ = dict(payload.get("type") or {})
    if typ.get("completed") is True:
        return False
    name = str(typ.get("name") or "").strip().upper()
    state = str(typ.get("state") or "").strip().lower()
    if name in _COMPLETED_STATUS_NAMES or "CANCEL" in name:
        return False
    if state in _INACTIVE_STATES:
        return False
    return True


def _venue_location(competitions: list[Mapping[str, Any]]) -> str:
    if not competitions:
        return ""
    venue = dict(competitions[0].get("venue") or {})
    address = dict(venue.get("address") or {})
    parts = [
        str(venue.get("fullName") or "").strip(),
        str(address.get("city") or "").strip(),
        str(address.get("state") or "").strip(),
        str(address.get("country") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def _athlete_id_and_name(competitor: Mapping[str, Any]) -> tuple[str, str]:
    athlete = dict(competitor.get("athlete") or {})
    athlete_id = str(athlete.get("id") or competitor.get("id") or "").strip()
    name = str(
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("shortName")
        or ""
    ).strip()
    return athlete_id, name


def _parse_fight(competition: Mapping[str, Any]) -> EspnUpcomingFight | None:
    if not status_is_scheduled(competition.get("status")):
        return None
    competition_id = str(competition.get("id") or "").strip()
    if not competition_id:
        return None
    competitors = [dict(item) for item in (competition.get("competitors") or [])]
    if len(competitors) != 2:
        return None
    competitors.sort(
        key=lambda item: (
            int(item.get("order") or 0),
            str(item.get("id") or ""),
        )
    )
    left_id, left_name = _athlete_id_and_name(competitors[0])
    right_id, right_name = _athlete_id_and_name(competitors[1])
    if not left_id or not right_id or left_id == right_id:
        return None
    if not left_name or not right_name:
        return None
    typ = dict(competition.get("type") or {})
    weight = str(typ.get("text") or typ.get("abbreviation") or "").strip() or None
    start = parse_espn_datetime(
        competition.get("date") or competition.get("startDate")
    )
    return EspnUpcomingFight(
        competition_id=competition_id,
        fighter_a_id=left_id,
        fighter_a_name=left_name,
        fighter_b_id=right_id,
        fighter_b_name=right_name,
        weight_class=weight,
        start=start,
    )


def parse_espn_scoreboard(payload: Mapping[str, Any]) -> tuple[EspnUpcomingEvent, ...]:
    """Return scheduled DWCS events; ignore Fight Night and completed weeks."""
    if not isinstance(payload, Mapping) or "events" not in payload:
        raise EspnSchemaError("ESPN scoreboard JSON must include an events array")
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        raise EspnSchemaError("ESPN scoreboard events must be an array")

    parsed: list[EspnUpcomingEvent] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise EspnSchemaError("ESPN scoreboard event must be an object")
        name = str(raw.get("name") or raw.get("shortName") or "").strip()
        if not is_dwcs_event_name(name):
            continue
        if not status_is_scheduled(raw.get("status")):
            continue
        espn_event_id = str(raw.get("id") or "").strip()
        start = parse_espn_datetime(raw.get("date"))
        if not espn_event_id or start is None:
            continue
        competitions = [
            dict(item) for item in (raw.get("competitions") or []) if isinstance(item, Mapping)
        ]
        fights = tuple(
            fight
            for fight in (_parse_fight(item) for item in competitions)
            if fight is not None
        )
        if not fights:
            continue
        parsed.append(
            EspnUpcomingEvent(
                espn_event_id=espn_event_id,
                name=name,
                start=start,
                location=_venue_location(competitions),
                series=series_for_event_name(name),
                fights=fights,
            )
        )
    return tuple(parsed)


__all__ = [
    "ESPN_IDENTITY_SOURCE",
    "EspnUpcomingEvent",
    "EspnUpcomingFight",
    "parse_espn_datetime",
    "parse_espn_scoreboard",
    "status_is_scheduled",
]
