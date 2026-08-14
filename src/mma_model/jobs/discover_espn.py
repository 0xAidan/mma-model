"""Persist upcoming DWCS cards from ESPN public JSON using canonical ESPN ids."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutParticipant,
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    EventSourceId,
    FighterSourceId,
)
from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id, canonical_fighter_id
from mma_model.jobs.discover_live import DiscoverResult
from mma_model.sources.espn_public.client import EspnPublicClient
from mma_model.sources.espn_public.errors import EspnSchemaError
from mma_model.sources.espn_public.parser import (
    ESPN_IDENTITY_SOURCE,
    EspnUpcomingEvent,
    parse_espn_scoreboard,
)
from mma_model.sources.http.block_signals import SourceBlockedError


def _get_or_create_fighter(
    session: Session,
    *,
    espn_athlete_id: str,
    display_name: str,
) -> tuple[CanonicalFighter, bool]:
    existing = session.scalar(
        select(FighterSourceId).where(
            FighterSourceId.source == ESPN_IDENTITY_SOURCE,
            FighterSourceId.external_id == espn_athlete_id,
        )
    )
    if existing is not None:
        fighter = session.get(CanonicalFighter, existing.fighter_id)
        if fighter is None:
            raise RuntimeError(f"dangling espn fighter source id {espn_athlete_id}")
        if display_name and fighter.display_name != display_name:
            fighter.display_name = display_name
        return fighter, False
    fighter = CanonicalFighter(
        id=canonical_fighter_id(espn_athlete_id),
        display_name=display_name.strip() or espn_athlete_id,
    )
    session.add(fighter)
    session.flush()
    session.add(
        FighterSourceId(
            fighter_id=fighter.id,
            source=ESPN_IDENTITY_SOURCE,
            external_id=espn_athlete_id,
        )
    )
    return fighter, True


def persist_espn_event(
    session: Session,
    event: EspnUpcomingEvent,
) -> tuple[CanonicalEvent, int, int]:
    """Insert or refresh one scheduled ESPN DWCS card."""
    event_uuid = canonical_event_id(event.espn_event_id)
    source_row = session.scalar(
        select(EventSourceId).where(
            EventSourceId.source == ESPN_IDENTITY_SOURCE,
            EventSourceId.external_id == event.espn_event_id,
        )
    )
    created_fighters = 0
    created_bouts = 0
    if source_row is not None:
        row = session.get(CanonicalEvent, source_row.event_id)
        if row is None:
            raise RuntimeError(f"dangling espn event source id {event.espn_event_id}")
        row.name = event.name
        row.series = event.series
        row.status = "scheduled"
        row.scheduled_start_at = event.start
        row.event_date = event.start.date()
        row.location = event.location or row.location
        canonical = row
    else:
        existing = session.get(CanonicalEvent, event_uuid)
        if existing is not None:
            existing.name = event.name
            existing.series = event.series
            existing.status = "scheduled"
            existing.scheduled_start_at = event.start
            existing.event_date = event.start.date()
            existing.location = event.location or existing.location
            canonical = existing
        else:
            canonical = CanonicalEvent(
                id=event_uuid,
                name=event.name,
                series=event.series,
                status="scheduled",
                scheduled_start_at=event.start,
                event_date=event.start.date(),
                location=event.location or None,
            )
            session.add(canonical)
            session.flush()
        session.add(
            EventSourceId(
                event_id=canonical.id,
                source=ESPN_IDENTITY_SOURCE,
                external_id=event.espn_event_id,
            )
        )

    for fight in event.fights:
        fa, fa_new = _get_or_create_fighter(
            session,
            espn_athlete_id=fight.fighter_a_id,
            display_name=fight.fighter_a_name,
        )
        fb, fb_new = _get_or_create_fighter(
            session,
            espn_athlete_id=fight.fighter_b_id,
            display_name=fight.fighter_b_name,
        )
        created_fighters += int(fa_new) + int(fb_new)
        bout_source = session.scalar(
            select(BoutSourceId).where(
                BoutSourceId.source == ESPN_IDENTITY_SOURCE,
                BoutSourceId.external_id == fight.competition_id,
            )
        )
        if bout_source is not None:
            continue
        bout = CanonicalBout(
            id=canonical_bout_id(fight.competition_id),
            event_id=canonical.id,
            fighter_a_id=fa.id,
            fighter_b_id=fb.id,
            status="scheduled",
            weight_class=fight.weight_class,
        )
        session.add(bout)
        session.flush()
        session.add(
            BoutSourceId(
                bout_id=bout.id,
                source=ESPN_IDENTITY_SOURCE,
                external_id=fight.competition_id,
            )
        )
        session.add(BoutParticipant(bout_id=bout.id, fighter_id=fa.id, corner="a"))
        session.add(BoutParticipant(bout_id=bout.id, fighter_id=fb.id, corner="b"))
        created_bouts += 1
    return canonical, created_bouts, created_fighters


def persist_from_espn_events(
    session: Session,
    events: Sequence[EspnUpcomingEvent],
) -> DiscoverResult:
    written = DiscoverResult(detail="persisted upcoming DWCS cards from ESPN")
    for event in events:
        row, bouts, fighters = persist_espn_event(session, event)
        written.events_written += 1
        written.bouts_written += bouts
        written.fighters_written += fighters
        written.event_ids.append(row.id)
    if not written.event_ids:
        written.detail = "no upcoming DWCS cards in ESPN scoreboard"
    return written


def fetch_espn_upcoming(
    *,
    cache_dir: Path,
    dates: str,
    robots_disallow: bool = False,
) -> tuple[EspnUpcomingEvent, ...]:
    client = EspnPublicClient(cache_dir=cache_dir, robots_disallow=robots_disallow)
    try:
        payload, _digest = client.fetch_scoreboard(dates=dates)
        return parse_espn_scoreboard(payload)
    except SourceBlockedError:
        raise
    finally:
        client.close()


def upcoming_from_espn_context(
    context: Mapping[str, Any],
    *,
    cache_dir: Path,
    as_of: datetime,
) -> tuple[EspnUpcomingEvent, ...]:
    """Fixture payload, pre-parsed events, or one live scoreboard GET."""
    injected = context.get("espn_events")
    if injected is not None:
        return tuple(injected)
    scoreboard = context.get("espn_scoreboard")
    if scoreboard is not None:
        if not isinstance(scoreboard, Mapping):
            raise EspnSchemaError("espn_scoreboard fixture must be an object")
        return parse_espn_scoreboard(scoreboard)
    dates = str(context.get("espn_dates") or as_of.year)
    return fetch_espn_upcoming(cache_dir=cache_dir, dates=dates)


__all__ = [
    "fetch_espn_upcoming",
    "persist_espn_event",
    "persist_from_espn_events",
    "upcoming_from_espn_context",
]
