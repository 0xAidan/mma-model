"""Expected value vs implied probability from American odds."""

from __future__ import annotations


def american_to_implied_prob(american: float) -> float:
    if american > 0:
        return 100.0 / (american + 100.0)
    aa = abs(american)
    return aa / (aa + 100.0)


def decimal_to_implied_prob(decimal_odds: float) -> float:
    if decimal_odds <= 1:
        return 1.0
    return 1.0 / decimal_odds


def ev_vs_fair(model_prob: float, offered_american: float) -> float:
    """Simple EV per 1 unit staked: model_prob * profit_if_win - (1-model_prob)."""
    if offered_american > 0:
        profit = offered_american / 100.0
    else:
        profit = 100.0 / abs(offered_american)
    return model_prob * profit - (1.0 - model_prob)
