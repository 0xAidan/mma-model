"""Sequential opponent-strength ratings frozen before each card (DWCS-301).

Glicko-lite with simultaneous same-card updates: ratings for card T use only
completed events strictly before T. Draws get a 0.5 update; NC / overturned /
pending do not update ratings. Debuts keep high RD and a missing flag.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Never

from mma_model.features.as_of import (
    AsOfCutoff,
    event_start_datetime,
    implied_event_start,
    observation_admitted,
)
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotEvent,
    SnapshotHistoryBout,
    SnapshotResultVersion,
    to_label_version,
)
from mma_model.labels.outcomes import (
    ResultClass,
    ResultVersion,
    WinnerSide,
    training_label,
)

INITIAL_RATING = 1500.0
INITIAL_RD = 350.0
MIN_RD = 50.0
Q = math.log(10.0) / 400.0


@dataclass(frozen=True)
class FighterStrength:
    fighter_id: str
    rating: float
    rating_sd: float
    prior_decisive_bouts: int
    missing: bool


@dataclass(frozen=True)
class _Game:
    opponent_id: str
    score: float  # 1.0 win, 0.0 loss, 0.5 draw


@dataclass(frozen=True)
class _Period:
    period_id: str
    start_at: datetime
    event_id: str | None
    games_by_fighter: dict[str, tuple[_Game, ...]]


def default_strength(fighter_id: str) -> FighterStrength:
    return FighterStrength(
        fighter_id=fighter_id,
        rating=INITIAL_RATING,
        rating_sd=INITIAL_RD,
        prior_decisive_bouts=0,
        missing=True,
    )


def _g_rd(rd: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * Q * Q * rd * rd / (math.pi * math.pi))


def _expected_score(rating: float, opp_rating: float, opp_rd: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, -_g_rd(opp_rd) * (rating - opp_rating) / 400.0))


def _glicko_update(
    rating: float,
    rd: float,
    games: tuple[tuple[float, float, float], ...],
) -> tuple[float, float]:
    if not games:
        return rating, rd
    d2_inv = 0.0
    delta = 0.0
    for opp_rating, opp_rd, score in games:
        g_val = _g_rd(opp_rd)
        expected = _expected_score(rating, opp_rating, opp_rd)
        d2_inv += g_val * g_val * expected * (1.0 - expected)
        delta += g_val * (score - expected)
    d2_inv *= Q * Q
    if d2_inv <= 0.0:
        return rating, rd
    d_sq = 1.0 / d2_inv
    rd_new = 1.0 / math.sqrt((1.0 / (rd * rd)) + (1.0 / d_sq))
    rd_new = max(rd_new, MIN_RD)
    rating_new = rating + Q * rd_new * rd_new * delta
    return rating_new, rd_new


def _select_version(
    versions: list[SnapshotResultVersion],
    cutoff: AsOfCutoff,
    *,
    bout_event_id: str,
) -> SnapshotResultVersion | None:
    eligible: list[SnapshotResultVersion] = []
    for row in versions:
        if not observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
            bout_event_id=bout_event_id,
        ):
            continue
        eligible.append(row)
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda row: (row.effective_at, row.observed_at, row.revision),
    )


def _label_versions(rows: list[SnapshotResultVersion]) -> list[ResultVersion]:
    return [to_label_version(row) for row in rows]


def _canonical_games(
    snapshot: FeatureSnapshot,
    event: SnapshotEvent,
    cutoff: AsOfCutoff,
) -> dict[str, list[_Game]]:
    games: dict[str, list[_Game]] = {}
    for bout in snapshot.bouts_for_event(event.event_id):
        versions = [row for row in snapshot.result_versions if row.bout_id == bout.bout_id]
        chosen = _select_version(versions, cutoff, bout_event_id=bout.event_id)
        if chosen is None:
            continue
        label = training_label(_label_versions(versions), cutoff.cutoff)
        _add_label_games(games, bout.fighter_a_id, bout.fighter_b_id, label.result_class, label.binary_winner)
    return games


def _add_label_games(
    games: dict[str, list[_Game]],
    fighter_a_id: str,
    fighter_b_id: str,
    result_class: ResultClass,
    binary_winner: WinnerSide | None,
) -> None:
    if result_class is ResultClass.DRAW:
        games.setdefault(fighter_a_id, []).append(_Game(fighter_b_id, 0.5))
        games.setdefault(fighter_b_id, []).append(_Game(fighter_a_id, 0.5))
        return
    if result_class is ResultClass.DECISIVE:
        if binary_winner is WinnerSide.A:
            games.setdefault(fighter_a_id, []).append(_Game(fighter_b_id, 1.0))
            games.setdefault(fighter_b_id, []).append(_Game(fighter_a_id, 0.0))
            return
        if binary_winner is WinnerSide.B:
            games.setdefault(fighter_b_id, []).append(_Game(fighter_a_id, 1.0))
            games.setdefault(fighter_a_id, []).append(_Game(fighter_b_id, 0.0))
            return
        return
    if result_class in {
        ResultClass.NO_CONTEST,
        ResultClass.OVERTURNED,
        ResultClass.PENDING,
        ResultClass.UNKNOWN,
    }:
        return
    never_class: Never = result_class
    raise ValueError(f"unhandled result class for ratings: {never_class!r}")


def _history_admitted(
    row: SnapshotHistoryBout,
    cutoff: AsOfCutoff,
    *,
    fighters_on_card: set[str],
    event_date,
) -> bool:
    if row.bout_status == "cancelled":
        return False
    if not observation_admitted(
        effective_at=row.effective_at,
        observed_at=row.observed_at,
        cutoff=cutoff,
    ):
        return False
    if (
        event_date is not None
        and row.event_date == event_date
        and row.fighter_id in fighters_on_card
    ):
        return False
    return True


def _history_periods(
    snapshot: FeatureSnapshot,
    cutoff: AsOfCutoff,
    *,
    fighters_on_card: set[str],
    event_date,
) -> list[_Period]:
    grouped: dict[tuple[str, str], list[SnapshotHistoryBout]] = {}
    for row in snapshot.history_bouts:
        if row.fighter_id is None:
            continue
        if not _history_admitted(
            row, cutoff, fighters_on_card=fighters_on_card, event_date=event_date
        ):
            continue
        date_key = row.event_date.isoformat() if row.event_date is not None else row.effective_at.date().isoformat()
        name_key = row.event_name or row.external_bout_id or "unknown"
        grouped.setdefault((date_key, name_key), []).append(row)

    periods: list[_Period] = []
    for (date_key, name_key), rows in grouped.items():
        latest_by_ext: dict[str, SnapshotHistoryBout] = {}
        for row in rows:
            key = row.external_bout_id or f"{row.fighter_id}:{row.effective_at.isoformat()}:{row.revision}"
            previous = latest_by_ext.get(key)
            if previous is None or (row.effective_at, row.observed_at, row.revision) > (
                previous.effective_at,
                previous.observed_at,
                previous.revision,
            ):
                latest_by_ext[key] = row
        games: dict[str, list[_Game]] = {}
        start_at = None
        for row in latest_by_ext.values():
            start = event_start_datetime(
                scheduled_start_at=None,
                event_date=row.event_date,
            ) or row.effective_at
            start_at = start if start_at is None else min(start_at, start)
            if row.opponent_id is None:
                continue
            if row.result == "draw":
                games.setdefault(row.fighter_id, []).append(_Game(row.opponent_id, 0.5))
                games.setdefault(row.opponent_id, []).append(_Game(row.fighter_id, 0.5))
                continue
            if row.result == "win":
                games.setdefault(row.fighter_id, []).append(_Game(row.opponent_id, 1.0))
                games.setdefault(row.opponent_id, []).append(_Game(row.fighter_id, 0.0))
                continue
            if row.result == "loss":
                games.setdefault(row.fighter_id, []).append(_Game(row.opponent_id, 0.0))
                games.setdefault(row.opponent_id, []).append(_Game(row.fighter_id, 1.0))
                continue
            # nc / unknown / cancelled: no rating update
        if start_at is None or not games:
            continue
        frozen = {fid: tuple(items) for fid, items in games.items()}
        periods.append(
            _Period(
                period_id=f"hist:{date_key}:{name_key}",
                start_at=start_at,
                event_id=None,
                games_by_fighter=frozen,
            )
        )
    return periods


def _canonical_periods(
    snapshot: FeatureSnapshot,
    cutoff: AsOfCutoff,
) -> list[_Period]:
    periods: list[_Period] = []
    for event in snapshot.events:
        if event.event_id == cutoff.event_id:
            continue
        start = event_start_datetime(
            scheduled_start_at=event.scheduled_start_at,
            event_date=event.event_date,
        )
        if start is None:
            continue
        games = _canonical_games(snapshot, event, cutoff)
        if not games:
            continue
        frozen = {fid: tuple(items) for fid, items in games.items()}
        periods.append(
            _Period(
                period_id=f"event:{event.event_id}",
                start_at=start,
                event_id=event.event_id,
                games_by_fighter=frozen,
            )
        )
    return periods


def _apply_period(
    states: dict[str, FighterStrength],
    period: _Period,
) -> dict[str, FighterStrength]:
    pre = dict(states)
    updated = dict(states)
    for fighter_id, games in period.games_by_fighter.items():
        current = pre.get(fighter_id, default_strength(fighter_id))
        packed: list[tuple[float, float, float]] = []
        decisive = 0
        for game in games:
            opp = pre.get(game.opponent_id, default_strength(game.opponent_id))
            packed.append((opp.rating, opp.rating_sd, game.score))
            if game.score != 0.5:
                decisive += 1
        rating, rd = _glicko_update(current.rating, current.rating_sd, tuple(packed))
        prior = current.prior_decisive_bouts + decisive
        updated[fighter_id] = FighterStrength(
            fighter_id=fighter_id,
            rating=rating,
            rating_sd=rd,
            prior_decisive_bouts=prior,
            missing=prior == 0,
        )
    return updated


def strengths_before_event(
    snapshot: FeatureSnapshot,
    cutoff: AsOfCutoff,
    *,
    fighter_ids: tuple[str, ...] | None = None,
) -> dict[str, FighterStrength]:
    """Pre-card ratings: history strictly before the cutoff event."""
    target_event = snapshot.event_by_id(cutoff.event_id)
    if target_event is None:
        target_start = implied_event_start(cutoff)
        event_date = None
        fighters_on_card: set[str] = set()
    else:
        target_start = event_start_datetime(
            scheduled_start_at=target_event.scheduled_start_at,
            event_date=target_event.event_date,
        ) or implied_event_start(cutoff)
        event_date = target_event.event_date
        fighters_on_card = {
            bout.fighter_a_id
            for bout in snapshot.bouts_for_event(cutoff.event_id)
        } | {
            bout.fighter_b_id
            for bout in snapshot.bouts_for_event(cutoff.event_id)
        }

    periods = _canonical_periods(snapshot, cutoff) + _history_periods(
        snapshot,
        cutoff,
        fighters_on_card=fighters_on_card,
        event_date=event_date,
    )
    periods = [p for p in periods if p.start_at < target_start]
    periods.sort(key=lambda p: (p.start_at, p.period_id))

    states: dict[str, FighterStrength] = {}
    for period in periods:
        states = _apply_period(states, period)

    if fighter_ids is None:
        return states
    return {fid: states.get(fid, default_strength(fid)) for fid in fighter_ids}


def matchup_strength(
    snapshot: FeatureSnapshot,
    fighter_a_id: str,
    fighter_b_id: str,
    cutoff: AsOfCutoff,
) -> dict[str, float]:
    states = strengths_before_event(
        snapshot, cutoff, fighter_ids=(fighter_a_id, fighter_b_id)
    )
    a = states[fighter_a_id]
    b = states[fighter_b_id]
    return {
        "rating_a": a.rating,
        "rating_b": b.rating,
        "rating_diff": a.rating - b.rating,
        "rating_sd_a": a.rating_sd,
        "rating_sd_b": b.rating_sd,
        "rating_sd_sum": a.rating_sd + b.rating_sd,
        "prior_decisive_bouts_a": float(a.prior_decisive_bouts),
        "prior_decisive_bouts_b": float(b.prior_decisive_bouts),
        "rating_missing_a": 1.0 if a.missing else 0.0,
        "rating_missing_b": 1.0 if b.missing else 0.0,
    }
