"""Persist upcoming DWCS cards from UFCStats listings (no ESPN ids)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from mma_model.dwcs.ids import upcoming_bout_id, upcoming_event_id, upcoming_fighter_id
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.policy import SourceId
from mma_model.sources.ufcstats_public.parser import parse_event_details
from mma_model.ufcstats.parsers import EventRow, parse_completed_events

SOURCE = SourceId.UFCSTATS_PUBLIC.value
_DWCS_NAME = re.compile(
    r"contender\s+series|dana\s+white.?s\s+contender|\bdwcs\b",
    re.IGNORECASE,
)
_BRAZIL = re.compile(r"\bbrazil\b", re.IGNORECASE)
_INACTIVE_BOUTS = frozenset({"cancelled", "canceled", "replaced", "scratched", "completed"})


@dataclass(frozen=True)
class DiscoverEventPage:
    """Parsed UFCStats event-details page (or a test-injected equivalent)."""

    event_name: str
    date_text: str
    event_date: datetime | None
    location: str
    fights: tuple[Mapping[str, Any], ...]
    cancelled: bool = False


@dataclass
class DiscoverResult:
    events_written: int = 0
    bouts_written: int = 0
    fighters_written: int = 0
    event_ids: list[str] = field(default_factory=list)
    detail: str = ""


def is_dwcs_event_name(name: str) -> bool:
    return bool(_DWCS_NAME.search(name or ""))


def series_for_event_name(name: str) -> str:
    if _BRAZIL.search(name or ""):
        return "dwcs_brazil"
    return "dwcs"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _get_or_create_fighter(
    session: Session,
    *,
    ufcstats_id: str,
    display_name: str,
) -> tuple[CanonicalFighter, bool]:
    existing = session.scalar(
        select(FighterSourceId).where(
            FighterSourceId.source == SOURCE,
            FighterSourceId.external_id == ufcstats_id,
        )
    )
    if existing is not None:
        fighter = session.get(CanonicalFighter, existing.fighter_id)
        if fighter is None:
            raise RuntimeError(f"dangling fighter source id {ufcstats_id}")
        return fighter, False
    fighter = CanonicalFighter(
        id=upcoming_fighter_id(ufcstats_id),
        display_name=display_name.strip() or ufcstats_id,
    )
    session.add(fighter)
    session.flush()
    session.add(
        FighterSourceId(
            fighter_id=fighter.id,
            source=SOURCE,
            external_id=ufcstats_id,
        )
    )
    return fighter, True


def persist_upcoming_event(
    session: Session,
    *,
    ufcstats_event_id: str,
    listing_name: str,
    listing_date: datetime | None,
    listing_location: str,
    page: DiscoverEventPage,
) -> tuple[CanonicalEvent, int, int]:
    """Insert or refresh one upcoming DWCS card. Returns event, new bouts, new fighters."""
    if page.cancelled:
        raise ValueError(f"refusing cancelled UFCStats event {ufcstats_event_id}")
    start = page.event_date or listing_date
    if start is None:
        raise ValueError(f"upcoming event {ufcstats_event_id} has no parseable date")
    start = _aware(start)
    name = (page.event_name or listing_name).strip()
    if not name:
        raise ValueError(f"upcoming event {ufcstats_event_id} has no name")
    series = series_for_event_name(name)

    source_row = session.scalar(
        select(EventSourceId).where(
            EventSourceId.source == SOURCE,
            EventSourceId.external_id == ufcstats_event_id,
        )
    )
    created_fighters = 0
    created_bouts = 0
    if source_row is not None:
        event = session.get(CanonicalEvent, source_row.event_id)
        if event is None:
            raise RuntimeError(f"dangling event source id {ufcstats_event_id}")
        event.name = name
        event.series = series
        event.status = "scheduled"
        event.scheduled_start_at = start
        event.event_date = start.date()
        event.location = page.location or listing_location or event.location
    else:
        event = CanonicalEvent(
            id=upcoming_event_id(ufcstats_event_id),
            name=name,
            series=series,
            status="scheduled",
            scheduled_start_at=start,
            event_date=start.date(),
            location=page.location or listing_location or None,
        )
        session.add(event)
        session.flush()
        session.add(
            EventSourceId(
                event_id=event.id,
                source=SOURCE,
                external_id=ufcstats_event_id,
            )
        )

    for fight in page.fights:
        fight_id = str(fight.get("external_fight_id") or "").strip()
        fighter_a = dict(fight.get("fighter_a") or {})
        fighter_b = dict(fight.get("fighter_b") or {})
        fa_ext = str(fighter_a.get("id") or "").strip()
        fb_ext = str(fighter_b.get("id") or "").strip()
        if not fight_id or not fa_ext or not fb_ext or fa_ext == fb_ext:
            continue
        fa, fa_new = _get_or_create_fighter(
            session,
            ufcstats_id=fa_ext,
            display_name=str(fighter_a.get("name") or fa_ext),
        )
        fb, fb_new = _get_or_create_fighter(
            session,
            ufcstats_id=fb_ext,
            display_name=str(fighter_b.get("name") or fb_ext),
        )
        created_fighters += int(fa_new) + int(fb_new)
        bout_source = session.scalar(
            select(BoutSourceId).where(
                BoutSourceId.source == SOURCE,
                BoutSourceId.external_id == fight_id,
            )
        )
        if bout_source is not None:
            continue
        bout = CanonicalBout(
            id=upcoming_bout_id(fight_id),
            event_id=event.id,
            fighter_a_id=fa.id,
            fighter_b_id=fb.id,
            status="scheduled",
        )
        session.add(bout)
        session.flush()
        session.add(BoutSourceId(bout_id=bout.id, source=SOURCE, external_id=fight_id))
        session.add(BoutParticipant(bout_id=bout.id, fighter_id=fa.id, corner="a"))
        session.add(BoutParticipant(bout_id=bout.id, fighter_id=fb.id, corner="b"))
        created_bouts += 1
    return event, created_bouts, created_fighters


def active_bout_ids(session: Session, event_id: str) -> tuple[str, ...]:
    rows = session.scalars(
        select(CanonicalBout)
        .where(CanonicalBout.event_id == event_id)
        .order_by(CanonicalBout.id.asc())
    ).all()
    return tuple(
        row.id
        for row in rows
        if (row.status or "scheduled").strip().casefold() not in _INACTIVE_BOUTS
    )


def persist_from_listing(
    session: Session,
    *,
    listing: Sequence[EventRow],
    pages: Mapping[str, DiscoverEventPage],
) -> DiscoverResult:
    written = DiscoverResult(detail="persisted upcoming DWCS cards from listing")
    for row in listing:
        if not is_dwcs_event_name(row.name):
            continue
        page = pages.get(row.ufcstats_id)
        if page is None:
            continue
        if page.cancelled:
            continue
        event, bouts, fighters = persist_upcoming_event(
            session,
            ufcstats_event_id=row.ufcstats_id,
            listing_name=row.name,
            listing_date=row.date,
            listing_location=row.location,
            page=page,
        )
        written.events_written += 1
        written.bouts_written += bouts
        written.fighters_written += fighters
        written.event_ids.append(event.id)
    if not written.event_ids:
        written.detail = "no upcoming DWCS cards in listing"
    return written


def fetch_live_listing_and_pages(
    *,
    cache_dir: Path,
    robots_disallow: bool = False,
) -> tuple[list[EventRow], dict[str, DiscoverEventPage]]:
    """Live UFCStats upcoming listing + event-details for DWCS names only."""
    from mma_model.sources.ufcstats_public.client import UfcstatsPublicClient

    client = UfcstatsPublicClient(cache_dir=cache_dir, robots_disallow=robots_disallow)
    try:
        listing_html, _digest = client.fetch_upcoming_events_list()
        rows = [
            row for row in parse_completed_events(listing_html) if is_dwcs_event_name(row.name)
        ]
        pages: dict[str, DiscoverEventPage] = {}
        for row in rows:
            html, _page_digest = client.fetch_event_details(row.ufcstats_id)
            parsed = parse_event_details(html)
            event_date = parsed.get("event_date")
            if isinstance(event_date, datetime):
                event_date = _aware(event_date)
            else:
                event_date = None
            pages[row.ufcstats_id] = DiscoverEventPage(
                event_name=str(parsed.get("event_name") or row.name),
                date_text=str(parsed.get("date_text") or ""),
                event_date=event_date,
                location=str(parsed.get("location") or row.location or ""),
                fights=tuple(parsed.get("fights") or ()),
                cancelled=bool(parsed.get("cancelled_evidence")),
            )
        return rows, pages
    except SourceBlockedError:
        raise
    finally:
        client.close()


__all__ = [
    "DiscoverEventPage",
    "DiscoverResult",
    "active_bout_ids",
    "fetch_live_listing_and_pages",
    "is_dwcs_event_name",
    "persist_from_listing",
    "persist_upcoming_event",
    "series_for_event_name",
]
