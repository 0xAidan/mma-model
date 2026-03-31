"""Orchestrate sync from ufcstats.com into the local DB."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from mma_model.config import feature_flags, profile
from mma_model.db.models import Event, Fight, Fighter, FightFighterStats
from mma_model.ufcstats.client import UFCStatsClient, fetch_completed_events_page, fetch_url
from mma_model.ufcstats.parsers import parse_completed_events, parse_event_fights, parse_fight_totals


def upsert_fighter(session: Session, fid: str, name: str) -> None:
    row = session.get(Fighter, fid)
    if row is None:
        session.add(Fighter(id=fid, name=name))
    else:
        row.name = name


def sync_pipeline(session: Session, client: UFCStatsClient, profile_name: str = "default") -> dict:
    flags = {**feature_flags(), **profile(profile_name)}
    max_events = int(flags.get("sync_max_events_per_run", 5))
    ingest_details = bool(flags.get("ingest_fight_details", True))

    html = fetch_completed_events_page(client)
    events = parse_completed_events(html)[:max_events]
    stats = {"events": 0, "fights": 0, "fight_details": 0}

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
            upsert_fighter(session, f.fighter_a_id, f.fighter_a_name)
            upsert_fighter(session, f.fighter_b_id, f.fighter_b_name)
            session.merge(
                Fight(
                    id=f.fight_id,
                    event_id=ev.ufcstats_id,
                    fighter_a_id=f.fighter_a_id,
                    fighter_b_id=f.fighter_b_id,
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
                    fight = session.get(Fight, f.fight_id)
                    if fight:
                        fight.detail_ingested = True
                    stats["fight_details"] += 1

    return stats
