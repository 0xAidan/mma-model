"""Kelly staking with quarter-Kelly and bankroll cap (DWCS-204)."""

from __future__ import annotations

from typing import Final

from mma_model.value.odds import (
    american_to_decimal,
    validate_decimal_odds,
    validate_probability,
)

QUARTER_KELLY_FRACTION: Final = 0.25
DEFAULT_BANKROLL_CAP_FRACTION: Final = 0.01  # 1% bankroll


def kelly_fraction(model_prob: float, offered_decimal: float) -> float:
    """Full Kelly fraction of bankroll for a decimal price (may be negative)."""
    model_prob = validate_probability(model_prob, field="model_prob")
    offered_decimal = validate_decimal_odds(offered_decimal, field="offered_decimal")
    b = offered_decimal - 1.0
    if b <= 0.0:
        return 0.0
    q = 1.0 - model_prob
    return (model_prob * b - q) / b


def fractional_kelly(
    model_prob: float,
    offered_american: float,
    fraction: float = QUARTER_KELLY_FRACTION,
    cap: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Legacy American-odds fractional Kelly with a non-negative cap."""
    if fraction < 0.0:
        raise ValueError("fraction must be non-negative")
    if cap < 0.0:
        raise ValueError("cap must be non-negative")
    decimal = american_to_decimal(offered_american)
    k = kelly_fraction(model_prob, decimal) * fraction
    if k < 0.0:
        return 0.0
    return min(k, cap)


def quarter_kelly_fraction(
    model_prob: float,
    offered_decimal: float,
    *,
    cap: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Quarter-Kelly stake fraction, capped at ``cap`` of bankroll (default 1%)."""
    if cap < 0.0:
        raise ValueError("cap must be non-negative")
    k = kelly_fraction(model_prob, offered_decimal) * QUARTER_KELLY_FRACTION
    if k < 0.0:
        return 0.0
    return min(k, cap)
