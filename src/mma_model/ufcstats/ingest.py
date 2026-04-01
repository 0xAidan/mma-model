"""Orchestrate sync from ufcstats.com into the local DB."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from mma_model.config import feature_flags, profile
from mma_model.db.models import Event, Fight, Fighter, FightFighterStats, IngestCursor
from mma_model.ufcstats.client import UFCStatsClient, fetch_completed_events_page, fetch_url
from mma_model.ufcstats.parsers import (
    EventRow,
    parse_completed_events,
    parse_event_fights,
    parse_fight_totals,
    parse_fight_winner_id,
)

CURSOR_COMPLETED_EVENTS = "ufcstats_completed_next_page"


def _cursor_next_page(session: Session, name: str) -> int:
    row = session.get(IngestCursor, name)
    return row.next_page if row is not None else 1


def _set_cursor_page(session: Session, name: str, next_page: int) -> None:
    session.merge(IngestCursor(cursor_name=name, next_page=next_page))


def _coerce_list_page(val: Any) -> int | str:
    if val is None or val == "all":
        return "all"
    if isinstance(val, int):
        return val
    s = str(val).strip().lower()
    if s == "all":
        return "all"
    return int(s)


def fetch_completed_events_for_sync(
    client: UFCStatsClient,
    *,
    mode: str,
    one_shot_page: int | str,
    start_page: int,
    pages_per_run: int,
) -> tuple[list[EventRow], dict[str, Any]]:
    """Load EventRow list for this sync. Paginated mode dedupes by event id and preserves order."""
    meta: dict[str, Any] = {"mode": mode}
    if mode == "one_shot":
        html = fetch_completed_events_page(client, page=_coerce_list_page(one_shot_page))
        events = parse_completed_events(html)
        meta["cursor_next_page"] = 1
        meta["pagination_exhausted"] = True
        return events, meta

    if mode != "paginated":
        raise ValueError(f"Unknown sync_event_list_mode: {mode!r}")

    batch: list[EventRow] = []
    seen: set[str] = set()
    reached_end = False
    next_cursor = start_page
    for p in range(start_page, start_page + pages_per_run):
        html = fetch_completed_events_page(client, page=p)
        rows = parse_completed_events(html)
        if not rows:
            reached_end = True
            next_cursor = 1
            meta["note"] = "empty_page_reached_reset_cursor"
            break
        for r in rows:
            if r.ufcstats_id not in seen:
                seen.add(r.ufcstats_id)
                batch.append(r)
        next_cursor = p + 1
    meta["cursor_next_page"] = next_cursor
    meta["pagination_exhausted"] = reached_end
    return batch, meta


def _canonical_fighter_pair(
    fighter_a_id: str,
    fighter_a_name: str,
    fighter_b_id: str,
    fighter_b_name: str,
) -> tuple[str, str, str, str]:
    """Sort by fighter id so fighter_a is stable; avoids winner-always-A from UFC page order."""
    if fighter_a_id <= fighter_b_id:
        return fighter_a_id, fighter_a_name, fighter_b_id, fighter_b_name
    return fighter_b_id, fighter_b_name, fighter_a_id, fighter_a_name


def upsert_fighter(session: Session, fid: str, name: str) -> None:
    row = session.get(Fighter, fid)
    if row is None:
        session.add(Fighter(id=fid, name=name))
    else:
        row.name = name


def sync_pipeline(
    session: Session,
    client: UFCStatsClient,
    profile_name: str = "default",
    *,
    resume: bool = False,
    reset_cursor: bool = False,
) -> dict:
    flags = {**feature_flags(), **profile(profile_name)}
    max_events = int(flags.get("sync_max_events_per_run", 5))
    ingest_details = bool(flags.get("ingest_fight_details", True))
    list_mode = str(flags.get("sync_event_list_mode", "one_shot"))
    one_shot_page = flags.get("sync_completed_list_page", "all")
    pages_per_run = int(flags.get("sync_pages_per_run", 10))

    if list_mode == "paginated":
        if reset_cursor:
            start_page = 1
        elif resume:
            start_page = _cursor_next_page(session, CURSOR_COMPLETED_EVENTS)
        else:
            start_page = 1
    else:
        start_page = 1

    events, ev_meta = fetch_completed_events_for_sync(
        client,
        mode=list_mode,
        one_shot_page=one_shot_page,
        start_page=start_page,
        pages_per_run=pages_per_run,
    )

    if max_events > 0:
        events = events[:max_events]

    if list_mode == "paginated":
        _set_cursor_page(session, CURSOR_COMPLETED_EVENTS, int(ev_meta["cursor_next_page"]))

    stats: dict[str, Any] = {
        "events": 0,
        "fights": 0,
        "fight_details": 0,
        "sync_event_list_mode": list_mode,
        "sync_events_considered": len(events),
        **{k: v for k, v in ev_meta.items() if k in ("cursor_next_page", "pagination_exhausted", "note")},
    }

    for ev in events:
        session.merge(
            Event(
                id=ev.ufcstats_id,
                name=ev.name,
                event_date=ev.date.date() if ev.date else None,
                location=ev.location or None,
                raw_url=ev.url,
            )
        )
        stats["events"] += 1

        event_html = fetch_url(client, ev.url)
        fights = parse_event_fights(event_html)
        for f in fights:
            fa_id, fa_name, fb_id, fb_name = _canonical_fighter_pair(
                f.fighter_a_id,
                f.fighter_a_name,
                f.fighter_b_id,
                f.fighter_b_name,
            )
            upsert_fighter(session, fa_id, fa_name)
            upsert_fighter(session, fb_id, fb_name)
            fight_row = session.merge(
                Fight(
                    id=f.fight_id,
                    event_id=ev.ufcstats_id,
                    fighter_a_id=fa_id,
                    fighter_b_id=fb_id,
                    winner_id=f.winner_id,
                    weight_class=f.weight_class or None,
                    method=f.method or None,
                    fight_round=f.fight_round,
                    time_str=f.time_str,
                    detail_ingested=False,
                )
            )
            stats["fights"] += 1

            if ingest_details:
                fh = fetch_url(client, f.fight_url)
                totals = parse_fight_totals(fh)
                if len(totals) >= 2:
                    session.execute(delete(FightFighterStats).where(FightFighterStats.fight_id == f.fight_id))
                    for t in totals:
                        session.add(
                            FightFighterStats(
                                fight_id=f.fight_id,
                                fighter_id=t.fighter_id,
                                kd=t.kd,
                                sig_str_landed=t.sig_str_landed,
                                sig_str_attempted=t.sig_str_attempted,
                                sig_str_pct=t.sig_str_pct,
                                total_str_landed=t.total_str_landed,
                                total_str_attempted=t.total_str_attempted,
                                td_landed=t.td_landed,
                                td_attempted=t.td_attempted,
                                td_pct=t.td_pct,
                                sub_att=t.sub_att,
                                rev=t.rev,
                                ctrl_seconds=t.ctrl_seconds,
                            )
                        )
                    fight_row.detail_ingested = True
                    wid = parse_fight_winner_id(fh)
                    if wid:
                        fight_row.winner_id = wid
                    stats["fight_details"] += 1

    return stats
