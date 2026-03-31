"""Build supervised dataset: matchup features from point-in-time rolling stats."""

from __future__ import annotations

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.composites.rolling import rolling_profile_before_fight
from mma_model.db.models import Event, Fight
from mma_model.features.matchup import matchup_features


FEATURE_NAMES = (
    "diff_sig_pm",
    "diff_acc",
    "diff_td_15",
    "diff_sub_15",
    "diff_ctrl_pm",
)


def build_training_arrays(
    session: Session,
    *,
    min_prior_fights: int = 1,
    last_n: int = 5,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Return X (n, 5), y (n,), fight_ids for fights with detail + winner."""
    q = (
        select(Fight, Event)
        .join(Event, Fight.event_id == Event.id)
        .where(Fight.detail_ingested.is_(True))
        .where(Fight.winner_id.isnot(None))
        .order_by(Event.event_date)
    )
    rows_out: list[list[float]] = []
    labels: list[int] = []
    fight_ids: list[str] = []

    for fight, _ev in session.execute(q).all():
        ra = rolling_profile_before_fight(session, fight.fighter_a_id, fight.id, last_n=last_n)
        rb = rolling_profile_before_fight(session, fight.fighter_b_id, fight.id, last_n=last_n)
        if ra is None or rb is None:
            continue
        if ra.fights_count < min_prior_fights or rb.fights_count < min_prior_fights:
            continue
        m = matchup_features(ra, rb)
        rows_out.append(
            [m.diff_sig_pm, m.diff_acc, m.diff_td_15, m.diff_sub_15, m.diff_ctrl_pm]
        )
        y = 1 if fight.winner_id == fight.fighter_a_id else 0
        labels.append(y)
        fight_ids.append(fight.id)

    if not rows_out:
        return np.zeros((0, 5)), np.zeros((0,)), []
    return np.asarray(rows_out, dtype=np.float64), np.asarray(labels, dtype=np.int64), fight_ids
