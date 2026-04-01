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


def feature_row_and_label_for_fight(
    session: Session,
    fight: Fight,
    *,
    min_prior_fights: int = 1,
    last_n: int = 5,
) -> tuple[np.ndarray, int] | None:
    """Point-in-time feature vector and label (1 = fighter A wins). None if not eligible."""
    ra = rolling_profile_before_fight(session, fight.fighter_a_id, fight.id, last_n=last_n)
    rb = rolling_profile_before_fight(session, fight.fighter_b_id, fight.id, last_n=last_n)
    if ra is None or rb is None:
        return None
    if ra.fights_count < min_prior_fights or rb.fights_count < min_prior_fights:
        return None
    m = matchup_features(ra, rb)
    x = np.array(
        [m.diff_sig_pm, m.diff_acc, m.diff_td_15, m.diff_sub_15, m.diff_ctrl_pm],
        dtype=np.float64,
    )
    y = 1 if fight.winner_id == fight.fighter_a_id else 0
    return x, y


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
        .order_by(Event.event_date, Fight.id)
    )
    rows_out: list[list[float]] = []
    labels: list[int] = []
    fight_ids: list[str] = []

    for fight, _ev in session.execute(q).all():
        got = feature_row_and_label_for_fight(
            session, fight, min_prior_fights=min_prior_fights, last_n=last_n
        )
        if got is None:
            continue
        x, y = got
        rows_out.append(x.tolist())
        labels.append(y)
        fight_ids.append(fight.id)

    if not rows_out:
        return np.zeros((0, 5)), np.zeros((0,)), []
    return np.asarray(rows_out, dtype=np.float64), np.asarray(labels, dtype=np.int64), fight_ids


def build_training_arrays_for_fight_ids(
    session: Session,
    fight_ids: list[str],
    *,
    min_prior_fights: int = 1,
    last_n: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """Stack rows for the given fight ids (order preserved). Skips missing or ineligible fights."""
    xs: list[np.ndarray] = []
    ys: list[int] = []
    for fid in fight_ids:
        fight = session.get(Fight, fid)
        if fight is None:
            continue
        got = feature_row_and_label_for_fight(
            session, fight, min_prior_fights=min_prior_fights, last_n=last_n
        )
        if got is None:
            continue
        x, y = got
        xs.append(x)
        ys.append(y)
    if not xs:
        return np.zeros((0, 5)), np.zeros((0,))
    return np.stack(xs, axis=0), np.asarray(ys, dtype=np.int64)
