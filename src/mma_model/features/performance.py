"""UFC/DWCS in-cage rates using actual elapsed seconds (DWCS-301).

Per-15-minute rates divide by 900 seconds of *actual* elapsed time, never an
assumed 15-minute fight. A bout enters a rate only when that stat key was
observed. Zero landed with positive attempts is a valid zero; absent keys are
missing. Invalid/missing elapsed or schedule excludes the bout from denominators.
"""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.features.as_of import AsOfCutoff, observation_admitted
from mma_model.features.duration import elapsed_seconds_for_rates
from mma_model.features.snapshot import (
    STAT_CTRL_SECONDS,
    STAT_SIG_STR_ATTEMPTED,
    STAT_SIG_STR_LANDED,
    STAT_SUB_ATT,
    STAT_TD_ATTEMPTED,
    STAT_TD_LANDED,
    FeatureSnapshot,
    SnapshotBout,
    SnapshotStatObservation,
)
from mma_model.features.spec import safe_diff

SECONDS_PER_MINUTE = 60.0
SECONDS_PER_15 = 900.0


@dataclass
class _RateAcc:
    numer: float = 0.0
    elapsed: float = 0.0
    attempts: float = 0.0
    n: int = 0


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
    return elapsed_seconds_for_rates(
        ending_round=chosen.ending_round,
        time_str=chosen.time_str,
        scheduled_rounds=bout.scheduled_rounds,
    )


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


def _add_count(_acc: _RateAcc, *, value: float, elapsed: float) -> None:
    _acc.numer += value
    _acc.elapsed += elapsed
    _acc.n += 1


def _add_accuracy(_acc: _RateAcc, *, landed: float, attempted: float, elapsed: float) -> None:
    _acc.numer += landed
    _acc.attempts += attempted
    _acc.elapsed += elapsed
    _acc.n += 1


def _per_min(acc: _RateAcc) -> tuple[float, float]:
    if acc.n == 0 or acc.elapsed <= 0:
        return 0.0, 1.0
    return acc.numer / (acc.elapsed / SECONDS_PER_MINUTE), 0.0


def _per_15(acc: _RateAcc) -> tuple[float, float]:
    if acc.n == 0 or acc.elapsed <= 0:
        return 0.0, 1.0
    return acc.numer / (acc.elapsed / SECONDS_PER_15), 0.0


def _accuracy(acc: _RateAcc) -> tuple[float, float]:
    if acc.n == 0 or acc.attempts <= 0:
        return 0.0, 1.0
    return acc.numer / acc.attempts, 0.0


def performance_features_for_fighter(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    cutoff: AsOfCutoff,
    *,
    prefix: str,
) -> dict[str, float]:
    strike_pm = _RateAcc()
    strike_acc = _RateAcc()
    opp_strike_pm = _RateAcc()
    td_landed = _RateAcc()
    td_att = _RateAcc()
    td_acc = _RateAcc()
    td_absorbed = _RateAcc()
    sub_att = _RateAcc()
    ctrl = _RateAcc()

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
        elapsed_f = float(elapsed)

        if STAT_SIG_STR_LANDED in own:
            _add_count(strike_pm, value=own[STAT_SIG_STR_LANDED], elapsed=elapsed_f)
        if STAT_SIG_STR_LANDED in own and STAT_SIG_STR_ATTEMPTED in own:
            _add_accuracy(
                strike_acc,
                landed=own[STAT_SIG_STR_LANDED],
                attempted=own[STAT_SIG_STR_ATTEMPTED],
                elapsed=elapsed_f,
            )
        if STAT_TD_LANDED in own:
            _add_count(td_landed, value=own[STAT_TD_LANDED], elapsed=elapsed_f)
        if STAT_TD_ATTEMPTED in own:
            _add_count(td_att, value=own[STAT_TD_ATTEMPTED], elapsed=elapsed_f)
        if STAT_TD_LANDED in own and STAT_TD_ATTEMPTED in own:
            _add_accuracy(
                td_acc,
                landed=own[STAT_TD_LANDED],
                attempted=own[STAT_TD_ATTEMPTED],
                elapsed=elapsed_f,
            )
        if STAT_SUB_ATT in own:
            _add_count(sub_att, value=own[STAT_SUB_ATT], elapsed=elapsed_f)
        if STAT_CTRL_SECONDS in own:
            _add_count(ctrl, value=own[STAT_CTRL_SECONDS], elapsed=elapsed_f)

        if STAT_SIG_STR_LANDED in opp:
            _add_count(opp_strike_pm, value=opp[STAT_SIG_STR_LANDED], elapsed=elapsed_f)
        if STAT_TD_ATTEMPTED in opp and STAT_TD_LANDED in opp:
            _add_count(td_absorbed, value=opp[STAT_TD_LANDED], elapsed=elapsed_f)

    landed_pm, landed_m = _per_min(strike_pm)
    acc, acc_m = _accuracy(strike_acc)
    opp_pm, opp_m = _per_min(opp_strike_pm)
    td_15, td_m = _per_15(td_landed)
    td_att_15, td_att_m = _per_15(td_att)
    td_accuracy, td_acc_m = _accuracy(td_acc)
    td_abs_15, td_abs_m = _per_15(td_absorbed)
    sub_15, sub_m = _per_15(sub_att)
    ctrl_pm, ctrl_m = _per_min(ctrl)

    any_own = (
        strike_pm.n
        + strike_acc.n
        + td_landed.n
        + td_att.n
        + sub_att.n
        + ctrl.n
    )
    any_opp = opp_strike_pm.n + td_absorbed.n
    return {
        f"sig_str_landed_pm_{prefix}": landed_pm,
        f"sig_str_landed_pm_missing_{prefix}": landed_m,
        f"sig_str_acc_{prefix}": acc,
        f"sig_str_acc_missing_{prefix}": acc_m,
        f"opp_sig_str_landed_pm_{prefix}": opp_pm,
        f"opp_sig_str_landed_pm_missing_{prefix}": opp_m,
        f"td_landed_per_15_{prefix}": td_15,
        f"td_landed_per_15_missing_{prefix}": td_m,
        f"td_acc_{prefix}": td_accuracy,
        f"td_acc_missing_{prefix}": td_acc_m,
        f"td_att_per_15_{prefix}": td_att_15,
        f"td_att_per_15_missing_{prefix}": td_att_m,
        f"td_absorbed_per_15_{prefix}": td_abs_15,
        f"td_absorbed_per_15_missing_{prefix}": td_abs_m,
        f"sub_att_per_15_{prefix}": sub_15,
        f"sub_att_per_15_missing_{prefix}": sub_m,
        f"ctrl_per_min_{prefix}": ctrl_pm,
        f"ctrl_per_min_missing_{prefix}": ctrl_m,
        f"perf_missing_{prefix}": 1.0 if any_own == 0 else 0.0,
        f"opp_perf_missing_{prefix}": 1.0 if any_opp == 0 else 0.0,
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
    merged["sig_str_landed_pm_diff"] = safe_diff(
        a["sig_str_landed_pm_a"],
        b["sig_str_landed_pm_b"],
        a["sig_str_landed_pm_missing_a"],
        b["sig_str_landed_pm_missing_b"],
    )
    merged["sig_str_acc_diff"] = safe_diff(
        a["sig_str_acc_a"],
        b["sig_str_acc_b"],
        a["sig_str_acc_missing_a"],
        b["sig_str_acc_missing_b"],
    )
    merged["td_landed_per_15_diff"] = safe_diff(
        a["td_landed_per_15_a"],
        b["td_landed_per_15_b"],
        a["td_landed_per_15_missing_a"],
        b["td_landed_per_15_missing_b"],
    )
    return merged
