"""Scalar composite scores (strike / grapple / pace / momentum) from rolling profiles."""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.composites.rolling import RollingProfile


@dataclass
class PillarScores:
    strike: float
    grapple: float
    pace: float
    momentum: float


def pillars_from_rolling(r: RollingProfile, prev: RollingProfile | None) -> PillarScores:
    """Heuristic composites — replace with learned weights later."""
    strike = 0.5 * r.sig_str_landed_pm + 0.5 * (r.sig_str_acc * 100.0)
    grapple = 2.0 * r.td_avg_per_15 + 1.5 * r.sub_att_per_15 + 0.01 * r.ctrl_per_min
    pace = r.sig_str_landed_pm + r.td_avg_per_15
    mom = 0.0
    if prev and prev.fights_count > 0:
        mom = (strike - (0.5 * prev.sig_str_landed_pm + 0.5 * (prev.sig_str_acc * 100.0))) * 0.1
    return PillarScores(strike=strike, grapple=grapple, pace=pace, momentum=mom)
