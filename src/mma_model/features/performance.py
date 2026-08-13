"""UFC/DWCS in-cage rates using actual elapsed seconds (DWCS-301).

Per-15-minute rates divide by 900 seconds of *actual* elapsed time, never an
assumed 15-minute fight. Zero landed with positive attempts is a valid zero;
no prior sample is missing. Bouts with invalid/missing elapsed seconds are
excluded from rate denominators.
"""

from __future__ import annotations

from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.features.as_of import AsOfCutoff, observation_admitted
from mma_model.features.snapshot import (
    STAT_CTRL_SECONDS,
    STAT_SIG_STR_ATTEMPTED,
    STAT_SIG_STR_LANDED,
    STAT_SUB_ATT,
    STAT_TD_LANDED,
    FeatureSnapshot,
    SnapshotBout,
    SnapshotStatObservation,
)

SECONDS_PER_MINUTE = 60.0
SECONDS_PER_15 = 900.0


def _elapsed_for_bout(bout: SnapshotBout, snapshot: FeatureSnapshot, cutoff: AsOfCutoff) -> int | None:
    versions = [row for row in snapshot.result_versions if row.bout_id == bout.bout_id]
    eligible = [
        row
        for row in versions
        if observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
            bout_event_id=bout.event_id,
        )
    ]
    if not eligible:
        return None
    chosen = max(eligible, key=lambda row: (row.effective_at, row.observed_at, row.revision))
    if chosen.ending_round is None or chosen.time_str is None:
        return None
    derived = derive_elapsed_seconds(
        ending_round=chosen.ending_round,
        time_str=chosen.time_str,
        scheduled_rounds=bout.scheduled_rounds,
    )
    if derived.status is DurationStatus.VALID:
        return derived.elapsed_seconds
    return None


def _latest_stats(
    snapshot: FeatureSnapshot,
    *,
    fighter_id: str,
    bout_id: str,
    cutoff: AsOfCutoff,
    bout_event_id: str,
) -> dict[str, float]:
    latest: dict[str, SnapshotStatObservation] = {}
    for row in snapshot.stats:
        if row.fighter_id != fighter_id or row.bout_id != bout_id:
            continue
        if not observation_admitted(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
            bout_event_id=bout_event_id,
        ):
            continue
        previous = latest.get(row.stat_key)
        if previous is None or (row.effective_at, row.observed_at) > (
            previous.effective_at,
            previous.observed_at,
        ):
            latest[row.stat_key] = row
    out: dict[str, float] = {}
    for key, row in latest.items():
        if row.value_num is not None:
            out[key] = float(row.value_num)
    return out


def _stat(values: dict[str, float], key: str) -> float:
    return float(values.get(key, 0.0))


def performance_features_for_fighter(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
    *,
    prefix: str,
) -> dict[str, float]:
    elapsed_total = 0.0
    sig_landed = 0.0
    sig_attempted = 0.0
    opp_sig_landed = 0.0
    td_landed = 0.0
    td_absorbed = 0.0
    sub_att = 0.0
    ctrl_seconds = 0.0
    sample_n = 0
    opp_sample_n = 0

    for bout in snapshot.bouts:
        if fighter_id not in {bout.fighter_a_id, bout.fighter_b_id}:
            continue
        elapsed = _elapsed_for_bout(bout, snapshot, cutoff)
        if elapsed is None or elapsed <= 0:
            continue
        own = _latest_stats(
            snapshot,
            fighter_id=fighter_id,
            bout_id=bout.bout_id,
            cutoff=cutoff,
            bout_event_id=bout.event_id,
        )
        if not own:
            continue
        opponent_id = bout.fighter_b_id if fighter_id == bout.fighter_a_id else bout.fighter_a_id
        opp = _latest_stats(
            snapshot,
            fighter_id=opponent_id,
            bout_id=bout.bout_id,
            cutoff=cutoff,
            bout_event_id=bout.event_id,
        )
        elapsed_total += float(elapsed)
        sig_landed += _stat(own, STAT_SIG_STR_LANDED)
        sig_attempted += _stat(own, STAT_SIG_STR_ATTEMPTED)
        td_landed += _stat(own, STAT_TD_LANDED)
        sub_att += _stat(own, STAT_SUB_ATT)
        ctrl_seconds += _stat(own, STAT_CTRL_SECONDS)
        sample_n += 1
        if opp:
            opp_sig_landed += _stat(opp, STAT_SIG_STR_LANDED)
            td_absorbed += _stat(opp, STAT_TD_LANDED)
            opp_sample_n += 1

    missing = 1.0 if sample_n == 0 or elapsed_total <= 0 else 0.0
    opp_missing = 1.0 if opp_sample_n == 0 or elapsed_total <= 0 else 0.0
    minutes = elapsed_total / SECONDS_PER_MINUTE if elapsed_total > 0 else 0.0
    per_15 = elapsed_total / SECONDS_PER_15 if elapsed_total > 0 else 0.0

    landed_pm = (sig_landed / minutes) if minutes > 0 else 0.0
    acc_missing = 1.0 if sig_attempted <= 0 else 0.0
    acc = (sig_landed / sig_attempted) if sig_attempted > 0 else 0.0
    opp_pm = (opp_sig_landed / minutes) if minutes > 0 and opp_sample_n else 0.0
    td_15 = (td_landed / per_15) if per_15 > 0 else 0.0
    td_abs_15 = (td_absorbed / per_15) if per_15 > 0 and opp_sample_n else 0.0
    sub_15 = (sub_att / per_15) if per_15 > 0 else 0.0
    ctrl_pm = (ctrl_seconds / minutes) if minutes > 0 else 0.0

    if missing == 1.0:
        landed_pm = acc = td_15 = sub_15 = ctrl_pm = 0.0
        acc_missing = 1.0
    if opp_missing == 1.0:
        opp_pm = td_abs_15 = 0.0

    return {
        f"sig_str_landed_pm_{prefix}": landed_pm,
        f"sig_str_acc_{prefix}": acc,
        f"sig_str_acc_missing_{prefix}": acc_missing,
        f"opp_sig_str_landed_pm_{prefix}": opp_pm,
        f"td_landed_per_15_{prefix}": td_15,
        f"td_absorbed_per_15_{prefix}": td_abs_15,
        f"sub_att_per_15_{prefix}": sub_15,
        f"ctrl_per_min_{prefix}": ctrl_pm,
        f"perf_missing_{prefix}": missing,
        f"opp_perf_missing_{prefix}": opp_missing,
    }


def matchup_performance(
    snapshot: FeatureSnapshot,
    fighter_a_id: str,
    fighter_b_id: str,
    cutoff: AsOfCutoff,
) -> dict[str, float]:
    a = performance_features_for_fighter(snapshot, fighter_a_id, cutoff, prefix="a")
    b = performance_features_for_fighter(snapshot, fighter_b_id, cutoff, prefix="b")
    merged = {**a, **b}
    merged["sig_str_landed_pm_diff"] = a["sig_str_landed_pm_a"] - b["sig_str_landed_pm_b"]
    merged["sig_str_acc_diff"] = a["sig_str_acc_a"] - b["sig_str_acc_b"]
    merged["td_landed_per_15_diff"] = a["td_landed_per_15_a"] - b["td_landed_per_15_b"]
    return merged
