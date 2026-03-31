"""Point-in-time rolling aggregates from fight_fighter_stats + fights + events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.models import Event, Fight, FightFighterStats


@dataclass
class RollingProfile:
    fighter_id: str
    fights_count: int
    sig_str_landed_pm: float
    sig_str_acc: float
    td_avg_per_15: float
    sub_att_per_15: float
    ctrl_per_min: float


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def rolling_profile_before_fight(
    session: Session,
    fighter_id: str,
    before_fight_id: str,
    last_n: int = 5,
) -> Optional[RollingProfile]:
    bf = session.get(Fight, before_fight_id)
    if bf is None:
        return None
    bev = session.get(Event, bf.event_id)
    if bev is None or bev.event_date is None:
        return None

    q = (
        select(FightFighterStats, Fight, Event)
        .join(Fight, FightFighterStats.fight_id == Fight.id)
        .join(Event, Fight.event_id == Event.id)
        .where(FightFighterStats.fighter_id == fighter_id)
        .where(Event.event_date < bev.event_date)
        .order_by(Event.event_date.desc())
        .limit(last_n)
    )
    rows = session.execute(q).all()
    if not rows:
        return RollingProfile(fighter_id, 0, 0.0, 0.0, 0.0, 0.0, 0.0)

    total_minutes = 0.0
    sig_l = 0
    sig_a = 0
    td_l = 0
    sub = 0
    ctrl = 0
    for st, _fight, _ev in rows:
        total_minutes += 15.0
        sig_l += st.sig_str_landed
        sig_a += st.sig_str_attempted
        td_l += st.td_landed
        sub += st.sub_att
        ctrl += st.ctrl_seconds

    fc = len(rows)
    sig_pm = sig_l / (total_minutes / 15.0) if total_minutes else 0.0
    acc = _safe_div(sig_l, sig_a) if sig_a else 0.0
    td_15 = td_l / (total_minutes / 15.0) if total_minutes else 0.0
    sub_15 = sub / (total_minutes / 15.0) if total_minutes else 0.0
    ctrl_pm = ctrl / total_minutes / 60.0 if total_minutes else 0.0
    return RollingProfile(fighter_id, fc, sig_pm, acc, td_15, sub_15, ctrl_pm)
