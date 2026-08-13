"""Fair / break-even / actionable / strong-value thresholds (DWCS-204).

Delegates formula evaluation to the pinned DWCS evaluation contract helpers so
price guidance stays consistent with DWCS-200 / DWCS-001.

Target constants are defined here (not imported from markets.price_targets) to
avoid an import cycle: markets.price_targets -> value.odds -> value.__init__ ->
value.thresholds -> markets.price_targets.
"""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.domain.markets import MarketFamily, PriceThresholdKind
from mma_model.evaluation.contract import (
    actionable_decimal_price,
    fair_decimal_odds,
    strong_value_decimal_price,
)
from mma_model.value.odds import (
    VALUE_MATH_METHOD,
    VALUE_MATH_VERSION,
    decimal_to_american,
    probability_to_decimal,
    validate_probability,
)

# Keep numeric identity with markets.price_targets / pinned evaluation contract.
STANDARD_ACTIONABLE_EV_TARGET = 0.05
STANDARD_STRONG_VALUE_EV_TARGET = 0.10
EXACT_ROUND_ACTIONABLE_EV_TARGET = 0.10


def actionable_ev_target_for_family(family: MarketFamily) -> float:
    if family is MarketFamily.EXACT_ROUND:
        return EXACT_ROUND_ACTIONABLE_EV_TARGET
    return STANDARD_ACTIONABLE_EV_TARGET


@dataclass(frozen=True)
class ValuePriceThresholds:
    """Sportsbook-agnostic thresholds with decimal and American forms."""

    p50: float
    p25: float
    family: MarketFamily
    fair_decimal: float
    break_even_decimal: float
    actionable_decimal: float
    strong_value_decimal: float
    fair_american: float
    break_even_american: float
    actionable_american: float
    strong_value_american: float
    actionable_ev_target: float
    strong_value_ev_target: float
    method: str
    version: str

    def as_decimal_mapping(self) -> dict[PriceThresholdKind, float]:
        return {
            PriceThresholdKind.FAIR: self.fair_decimal,
            PriceThresholdKind.ACTIONABLE: self.actionable_decimal,
            PriceThresholdKind.STRONG_VALUE: self.strong_value_decimal,
        }


def conservative_break_even_decimal(p25: float) -> float:
    """Conservative break-even decimal odds from p25: 1 / p25."""
    return probability_to_decimal(
        validate_probability(p25, field="p25", allow_one=True),
        allow_one=True,
    )


def compute_value_price_thresholds(
    p50: float,
    p25: float,
    *,
    family: MarketFamily,
) -> ValuePriceThresholds:
    """Fair, p25 break-even, 5% actionable, 10% strong-value (exact-round override)."""
    p50_v = validate_probability(p50, field="p50", allow_one=True)
    p25_v = validate_probability(p25, field="p25", allow_one=True)
    if p25_v > p50_v:
        raise ValueError("p25 must be <= p50 for conservative actionable thresholds")

    actionable_target = actionable_ev_target_for_family(family)
    strong_target = STANDARD_STRONG_VALUE_EV_TARGET
    if family is MarketFamily.EXACT_ROUND:
        strong_target = max(STANDARD_STRONG_VALUE_EV_TARGET, actionable_target)

    fair = fair_decimal_odds(p50_v)
    break_even = conservative_break_even_decimal(p25_v)
    actionable = actionable_decimal_price(p50_v, p25_v, ev_target=actionable_target)
    strong = strong_value_decimal_price(p50_v, p25_v, ev_target=strong_target)

    return ValuePriceThresholds(
        p50=p50_v,
        p25=p25_v,
        family=family,
        fair_decimal=fair,
        break_even_decimal=break_even,
        actionable_decimal=actionable,
        strong_value_decimal=strong,
        fair_american=decimal_to_american(fair),
        break_even_american=decimal_to_american(break_even),
        actionable_american=decimal_to_american(actionable),
        strong_value_american=decimal_to_american(strong),
        actionable_ev_target=actionable_target,
        strong_value_ev_target=strong_target,
        method=VALUE_MATH_METHOD,
        version=VALUE_MATH_VERSION,
    )


__all__ = [
    "EXACT_ROUND_ACTIONABLE_EV_TARGET",
    "STANDARD_ACTIONABLE_EV_TARGET",
    "STANDARD_STRONG_VALUE_EV_TARGET",
    "ValuePriceThresholds",
    "actionable_ev_target_for_family",
    "compute_value_price_thresholds",
    "conservative_break_even_decimal",
]
