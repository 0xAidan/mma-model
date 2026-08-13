"""Kelly staking with hard quarter-Kelly 1% bankroll cap (DWCS-204)."""

from __future__ import annotations

from typing import Final

from mma_model.value.ev import conditional_win_probability
from mma_model.value.odds import (
    american_to_decimal,
    validate_decimal_odds,
    validate_nonnegative_fraction,
    validate_probability,
)

QUARTER_KELLY_FRACTION: Final = 0.25
DEFAULT_BANKROLL_CAP_FRACTION: Final = 0.01  # 1% bankroll hard maximum
MAX_BANKROLL_CAP_FRACTION: Final = 0.01


def _validate_cap(cap: float, *, field: str = "cap") -> float:
    return validate_nonnegative_fraction(
        cap,
        field=field,
        maximum=MAX_BANKROLL_CAP_FRACTION,
    )


def kelly_fraction(model_prob: float, offered_decimal: float) -> float:
    """Full Kelly fraction of bankroll for a decimal price (may be negative)."""
    model_prob = validate_probability(model_prob, field="model_prob")
    offered_decimal = validate_decimal_odds(offered_decimal, field="offered_decimal")
    b = offered_decimal - 1.0
    if b <= 0.0:
        return 0.0
    q = 1.0 - model_prob
    return (model_prob * b - q) / b


def kelly_fraction_with_void(
    *,
    p_win: float,
    p_void: float,
    offered_decimal: float,
) -> float:
    """Full Kelly using conditional win/loss among non-void outcomes."""
    offered_decimal = validate_decimal_odds(offered_decimal, field="offered_decimal")
    conditional = conditional_win_probability(p_win, p_void)
    return kelly_fraction(conditional, offered_decimal)


def fractional_kelly(
    model_prob: float,
    offered_american: float,
    fraction: float = QUARTER_KELLY_FRACTION,
    cap: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Legacy American-odds fractional Kelly.

    ``fraction`` must be in ``[0, 1]``. ``cap`` must be in ``[0, 0.01]``.
    """
    fraction = validate_nonnegative_fraction(fraction, field="fraction", maximum=1.0)
    cap = _validate_cap(cap)
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
    """Production quarter-Kelly stake fraction (fraction fixed at 0.25).

    ``cap`` may be lowered but never raised above the hard 1% bankroll maximum.
    """
    cap = _validate_cap(cap)
    k = kelly_fraction(model_prob, offered_decimal) * QUARTER_KELLY_FRACTION
    if k < 0.0:
        return 0.0
    return min(k, cap)


def quarter_kelly_fraction_with_void(
    *,
    p_win: float,
    p_void: float,
    offered_decimal: float,
    cap: float = DEFAULT_BANKROLL_CAP_FRACTION,
) -> float:
    """Quarter-Kelly with void-aware conditional probabilities; cap remains 1%."""
    cap = _validate_cap(cap)
    k = kelly_fraction_with_void(
        p_win=p_win, p_void=p_void, offered_decimal=offered_decimal
    ) * QUARTER_KELLY_FRACTION
    if k < 0.0:
        return 0.0
    return min(k, cap)
