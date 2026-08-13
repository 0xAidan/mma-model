"""Cutoff-aware career, finish, and activity features (DWCS-301).

Uses ``training_label`` as-of cutoff, never current Wikipedia records.
Rates are Laplace-smoothed so 0/0 is not a confident zero. Missing elapsed
time is excluded from minute/round totals rather than assumed as 15:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Never

from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.features.as_of import AsOfCutoff, event_start_datetime, observation_admitted
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotHistoryBout,
    SnapshotResultVersion,
    to_label_version,
)
from mma_model.labels.outcomes import (
    MethodLabel,
    OutcomeLabel,
    ResultClass,
    WinnerSide,
    label_from_facts,
    training_label,
)

LAPLACE_ALPHA = 1.0
ACTIVITY_WINDOW = timedelta(days=365)


@dataclass(frozen=True)
class FighterBoutView:
    fighter_id: str
    opponent_id: str | None
    event_id: str | None
    start_at: datetime | None
    label: OutcomeLabel
    elapsed_seconds: int | None
    ending_round: int | None
    is_ufc_dwcs: bool
    classification: str
    fighter_is_a: bool


def _elapsed_seconds(
    *,
    ending_round: int | None,
    time_str: str | None,
    scheduled_rounds: int | None,
    stored: int | None = None,
) -> int | None:
    if stored is not None and stored > 0:
        return stored
    if ending_round is None or time_str is None or not str(time_str).strip():
        return None
    schedule = scheduled_rounds
    if schedule is None or schedule < 1:
        schedule = 5 if ending_round > 3 else 3
    derived = derive_elapsed_seconds(
        ending_round=ending_round,
        time_str=time_str,
        scheduled_rounds=schedule,
    )
    if derived.status is DurationStatus.VALID:
        return derived.elapsed_seconds
    return None


def _select_version(
    versions: list[SnapshotResultVersion],
    cutoff: AsOfCutoff,
    *,
    bout_event_id: str,
) -> SnapshotResultVersion | None:
    eligible = [
        row
        for row in versions
        if observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
            bout_event_id=bout_event_id,
        )
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row.effective_at, row.observed_at, row.revision))


def _fighter_won(label: OutcomeLabel, *, fighter_is_a: bool) -> bool | None:
    if label.result_class is not ResultClass.DECISIVE or label.binary_winner is None:
        return None
    if label.binary_winner is WinnerSide.A:
        return fighter_is_a
    if label.binary_winner is WinnerSide.B:
        return not fighter_is_a
    never_side: Never = label.binary_winner
    raise ValueError(f"unhandled winner side: {never_side!r}")


def _canonical_views(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
) -> list[FighterBoutView]:
    views: list[FighterBoutView] = []
    for bout in snapshot.bouts:
        if fighter_id not in {bout.fighter_a_id, bout.fighter_b_id}:
            continue
        versions = [row for row in snapshot.result_versions if row.bout_id == bout.bout_id]
        chosen = _select_version(versions, cutoff, bout_event_id=bout.event_id)
        if chosen is None:
            continue
        label = training_label([to_label_version(row) for row in versions], cutoff.cutoff)
        if label.result_class in {ResultClass.PENDING, ResultClass.UNKNOWN}:
            continue
        event = snapshot.event_by_id(bout.event_id)
        start = None
        if event is not None:
            start = event_start_datetime(
                scheduled_start_at=event.scheduled_start_at,
                event_date=event.event_date,
            )
        fighter_is_a = fighter_id == bout.fighter_a_id
        opponent_id = bout.fighter_b_id if fighter_is_a else bout.fighter_a_id
        views.append(
            FighterBoutView(
                fighter_id=fighter_id,
                opponent_id=opponent_id,
                event_id=bout.event_id,
                start_at=start,
                label=label,
                elapsed_seconds=_elapsed_seconds(
                    ending_round=chosen.ending_round,
                    time_str=chosen.time_str,
                    scheduled_rounds=bout.scheduled_rounds,
                ),
                ending_round=chosen.ending_round,
                is_ufc_dwcs=True,
                classification="professional",
                fighter_is_a=fighter_is_a,
            )
        )
    return views


def _history_label(row: SnapshotHistoryBout) -> OutcomeLabel | None:
    if row.bout_status == "cancelled" or row.result in {"cancelled", "unknown"}:
        return None
    result_class: ResultClass | None
    winner_side: WinnerSide | None
    if row.result == "win":
        result_class = ResultClass.DECISIVE
        winner_side = WinnerSide.A
    elif row.result == "loss":
        result_class = ResultClass.DECISIVE
        winner_side = WinnerSide.B
    elif row.result == "draw":
        result_class = ResultClass.DRAW
        winner_side = None
    elif row.result in {"nc", "no_contest"}:
        result_class = ResultClass.NO_CONTEST
        winner_side = None
    else:
        return None
    return label_from_facts(
        method_raw=row.method,
        result_class=result_class,
        winner_side=winner_side,
        source_version_kind=None,
    )


def _history_views(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
) -> list[FighterBoutView]:
    target = snapshot.event_by_id(cutoff.event_id)
    event_date = target.event_date if target is not None else None
    on_card = {
        bout.fighter_a_id for bout in snapshot.bouts_for_event(cutoff.event_id)
    } | {bout.fighter_b_id for bout in snapshot.bouts_for_event(cutoff.event_id)}
    latest: dict[str, SnapshotHistoryBout] = {}
    for row in snapshot.history_bouts:
        if row.fighter_id != fighter_id:
            continue
        if not observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
        ):
            continue
        if event_date is not None and row.event_date == event_date and fighter_id in on_card:
            continue
        key = row.external_bout_id or f"{row.effective_at.isoformat()}:{row.revision}"
        previous = latest.get(key)
        if previous is None or (row.effective_at, row.observed_at, row.revision) > (
            previous.effective_at,
            previous.observed_at,
            previous.revision,
        ):
            latest[key] = row

    views: list[FighterBoutView] = []
    for row in latest.values():
        label = _history_label(row)
        if label is None:
            continue
        start = event_start_datetime(
            scheduled_start_at=None, event_date=row.event_date
        ) or row.effective_at
        views.append(
            FighterBoutView(
                fighter_id=fighter_id,
                opponent_id=row.opponent_id,
                event_id=None,
                start_at=start,
                label=label,
                elapsed_seconds=_elapsed_seconds(
                    ending_round=row.ending_round,
                    time_str=row.time_str,
                    scheduled_rounds=row.scheduled_rounds,
                    stored=row.elapsed_seconds,
                ),
                ending_round=row.ending_round,
                is_ufc_dwcs=False,
                classification=row.classification,
                fighter_is_a=True,
            )
        )
    return views


def _dedupe(views: list[FighterBoutView]) -> list[FighterBoutView]:
    """Prefer canonical UFC/DWCS rows when a regional bout matches date+opponent."""
    canonical_keys: set[tuple[str | None, datetime | None]] = set()
    for view in views:
        if view.is_ufc_dwcs and view.start_at is not None:
            day = view.start_at.date()
            canonical_keys.add((view.opponent_id, datetime(day.year, day.month, day.day)))
    kept: list[FighterBoutView] = []
    for view in views:
        if not view.is_ufc_dwcs and view.start_at is not None:
            day = view.start_at.date()
            key = (view.opponent_id, datetime(day.year, day.month, day.day))
            if key in canonical_keys:
                continue
        kept.append(view)
    return kept


def prior_bouts_for_fighter(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
) -> tuple[FighterBoutView, ...]:
    views = _canonical_views(snapshot, fighter_id, cutoff) + _history_views(
        snapshot, fighter_id, cutoff
    )
    return tuple(_dedupe(views))


def _smoothed_rate(count: int, n: int) -> tuple[float, float]:
    """Laplace/Beta: (k + α) / (n + 2α). missing=1 iff n==0."""
    denom = n + 2.0 * LAPLACE_ALPHA
    value = (count + LAPLACE_ALPHA) / denom
    missing = 1.0 if n == 0 else 0.0
    return value, missing


def _is_finish_method(method: MethodLabel | None) -> bool:
    if method is None:
        return False
    if method is MethodLabel.KO_TKO:
        return True
    if method is MethodLabel.SUBMISSION:
        return True
    if method in {
        MethodLabel.DECISION,
        MethodLabel.OTHER_STOPPAGE,
        MethodLabel.TECHNICAL_DECISION,
        MethodLabel.TECHNICAL_DRAW,
    }:
        return False
    never_method: Never = method
    raise ValueError(f"unhandled method label: {never_method!r}")


def _method_is(label: OutcomeLabel, method: MethodLabel) -> bool:
    return label.method is method


def career_features_for_fighter(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
    *,
    prefix: str,
) -> dict[str, float]:
    bouts = prior_bouts_for_fighter(snapshot, fighter_id, cutoff)
    n = len(bouts)
    minutes = 0.0
    rounds = 0.0
    activity = 0
    window_start = cutoff.cutoff - ACTIVITY_WINDOW
    last_start: datetime | None = None
    ko_wins = sub_wins = dec_wins = 0
    ko_losses = sub_losses = dec_losses = 0
    finish_elapsed: list[int] = []
    pro = amateur = ufc = regional = 0
    method_n = 0

    for bout in bouts:
        if bout.elapsed_seconds is not None:
            minutes += bout.elapsed_seconds / 60.0
        if bout.ending_round is not None and bout.ending_round > 0:
            rounds += float(bout.ending_round)
        if bout.start_at is not None:
            if last_start is None or bout.start_at > last_start:
                last_start = bout.start_at
            if bout.start_at >= window_start:
                activity += 1
        if bout.classification == "amateur":
            amateur += 1
        else:
            pro += 1
        if bout.is_ufc_dwcs:
            ufc += 1
        else:
            regional += 1

        won = _fighter_won(bout.label, fighter_is_a=bout.fighter_is_a)

        if bout.label.result_class is ResultClass.DECISIVE:
            method_n += 1
            if won is True:
                if _method_is(bout.label, MethodLabel.KO_TKO):
                    ko_wins += 1
                elif _method_is(bout.label, MethodLabel.SUBMISSION):
                    sub_wins += 1
                elif _method_is(bout.label, MethodLabel.DECISION) or _method_is(
                    bout.label, MethodLabel.TECHNICAL_DECISION
                ):
                    dec_wins += 1
                if _is_finish_method(bout.label.method) and bout.elapsed_seconds is not None:
                    finish_elapsed.append(bout.elapsed_seconds)
            elif won is False:
                if _method_is(bout.label, MethodLabel.KO_TKO):
                    ko_losses += 1
                elif _method_is(bout.label, MethodLabel.SUBMISSION):
                    sub_losses += 1
                elif _method_is(bout.label, MethodLabel.DECISION) or _method_is(
                    bout.label, MethodLabel.TECHNICAL_DECISION
                ):
                    dec_losses += 1

    layoff_missing = 1.0 if last_start is None else 0.0
    layoff_days = 0.0 if last_start is None else float((cutoff.cutoff - last_start).days)
    if n == 0:
        ufc_share, ufc_share_missing = 0.0, 1.0
    else:
        ufc_share, ufc_share_missing = ufc / n, 0.0

    ko_win, ko_win_m = _smoothed_rate(ko_wins, method_n)
    sub_win, sub_win_m = _smoothed_rate(sub_wins, method_n)
    dec_win, dec_win_m = _smoothed_rate(dec_wins, method_n)
    ko_loss, ko_loss_m = _smoothed_rate(ko_losses, method_n)
    sub_loss, sub_loss_m = _smoothed_rate(sub_losses, method_n)
    dec_loss, dec_loss_m = _smoothed_rate(dec_losses, method_n)
    finish_mean = (
        sum(finish_elapsed) / len(finish_elapsed) if finish_elapsed else 0.0
    )
    finish_missing = 1.0 if not finish_elapsed else 0.0

    return {
        f"prior_fights_{prefix}": float(n),
        f"prior_minutes_{prefix}": minutes,
        f"prior_rounds_{prefix}": rounds,
        f"layoff_days_{prefix}": layoff_days,
        f"layoff_days_missing_{prefix}": layoff_missing,
        f"activity_365d_{prefix}": float(activity),
        f"debut_{prefix}": 1.0 if n == 0 else 0.0,
        f"pro_bouts_{prefix}": float(pro),
        f"amateur_bouts_{prefix}": float(amateur),
        f"ufc_dwcs_bouts_{prefix}": float(ufc),
        f"regional_bouts_{prefix}": float(regional),
        f"ko_win_rate_{prefix}": ko_win,
        f"ko_win_rate_missing_{prefix}": ko_win_m,
        f"sub_win_rate_{prefix}": sub_win,
        f"sub_win_rate_missing_{prefix}": sub_win_m,
        f"dec_win_rate_{prefix}": dec_win,
        f"dec_win_rate_missing_{prefix}": dec_win_m,
        f"ko_loss_rate_{prefix}": ko_loss,
        f"ko_loss_rate_missing_{prefix}": ko_loss_m,
        f"sub_loss_rate_{prefix}": sub_loss,
        f"sub_loss_rate_missing_{prefix}": sub_loss_m,
        f"dec_loss_rate_{prefix}": dec_loss,
        f"dec_loss_rate_missing_{prefix}": dec_loss_m,
        f"finish_elapsed_mean_{prefix}": finish_mean,
        f"finish_elapsed_mean_missing_{prefix}": finish_missing,
        f"ufc_dwcs_share_{prefix}": ufc_share,
        f"ufc_dwcs_share_missing_{prefix}": ufc_share_missing,
    }


def matchup_career(
    snapshot: FeatureSnapshot,
    fighter_a_id: str,
    fighter_b_id: str,
    cutoff: AsOfCutoff,
) -> dict[str, float]:
    return {
        **career_features_for_fighter(snapshot, fighter_a_id, cutoff, prefix="a"),
        **career_features_for_fighter(snapshot, fighter_b_id, cutoff, prefix="b"),
    }
