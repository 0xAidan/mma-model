"""Matchup feature vector = f(fighter A rolling, fighter B rolling)."""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.composites.rolling import RollingProfile


@dataclass
class MatchupFeatures:
    diff_sig_pm: float
    diff_acc: float
    diff_td_15: float
    diff_sub_15: float
    diff_ctrl_pm: float


def matchup_features(a: RollingProfile, b: RollingProfile) -> MatchupFeatures:
    return MatchupFeatures(
        diff_sig_pm=a.sig_str_landed_pm - b.sig_str_landed_pm,
        diff_acc=a.sig_str_acc - b.sig_str_acc,
        diff_td_15=a.td_avg_per_15 - b.td_avg_per_15,
        diff_sub_15=a.sub_att_per_15 - b.sub_att_per_15,
        diff_ctrl_pm=a.ctrl_per_min - b.ctrl_per_min,
    )
