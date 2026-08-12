"""Sportsbook-agnostic fair / actionable / strong-value price targets (DWCS-200).

Exact bookmaker quotes are optional enrichment. These thresholds are the required
fallback and do not depend on any sportsbook feed.
"""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.domain.markets import (
    MarketFamily,
    MarketMaturity,
    PriceThresholdKind,
    RecommendationState,
)
from mma_model.evaluation.contract import (
    actionable_decimal_price,
    fair_decimal_odds,
    strong_value_decimal_price,
)

STANDARD_ACTIONABLE_EV_TARGET = 0.05
STANDARD_STRONG_VALUE_EV_TARGET = 0.10
EXACT_ROUND_ACTIONABLE_EV_TARGET = 0.10
CONFIRMED_VALUE_MIN_PROB_EV_POSITIVE = 0.70
EXACT_ROUND_MIN_PROB_EV_POSITIVE = 0.75


@dataclass(frozen=True)
class PriceThresholds:
    """Deterministic decimal thresholds from calibrated probabilities."""

    p50: float
    p25: float
    fair_decimal: float
    actionable_decimal: float
    strong_value_decimal: float
    actionable_ev_target: float
    strong_value_ev_target: float
    family: MarketFamily

    def as_mapping(self) -> dict[PriceThresholdKind, float]:
        return {
            PriceThresholdKind.FAIR: self.fair_decimal,
            PriceThresholdKind.ACTIONABLE: self.actionable_decimal,
            PriceThresholdKind.STRONG_VALUE: self.strong_value_decimal,
        }


@dataclass(frozen=True)
class RecommendationClassification:
    state: RecommendationState
    reason: str
    thresholds: PriceThresholds | None
    offered_decimal: float | None = None


def actionable_ev_target_for_family(family: MarketFamily) -> float:
    if family is MarketFamily.EXACT_ROUND:
        return EXACT_ROUND_ACTIONABLE_EV_TARGET
    return STANDARD_ACTIONABLE_EV_TARGET


def confirmed_value_min_prob_ev_positive(family: MarketFamily) -> float:
    if family is MarketFamily.EXACT_ROUND:
        return EXACT_ROUND_MIN_PROB_EV_POSITIVE
    return CONFIRMED_VALUE_MIN_PROB_EV_POSITIVE


def compute_price_thresholds(
    p50: float,
    p25: float,
    *,
    family: MarketFamily,
) -> PriceThresholds:
    """Compute fair / actionable / strong-value decimals (sportsbook-agnostic)."""
    if p25 > p50:
        raise ValueError("p25 must be <= p50 for conservative actionable thresholds")
    actionable_target = actionable_ev_target_for_family(family)
    strong_target = STANDARD_STRONG_VALUE_EV_TARGET
    # Exact-round actionable already uses the 10% target; strong-value stays at
    # least as demanding (same 10% floor, still max'd with p25 break-even).
    if family is MarketFamily.EXACT_ROUND:
        strong_target = max(STANDARD_STRONG_VALUE_EV_TARGET, actionable_target)
    return PriceThresholds(
        p50=p50,
        p25=p25,
        fair_decimal=fair_decimal_odds(p50),
        actionable_decimal=actionable_decimal_price(
            p50, p25, ev_target=actionable_target
        ),
        strong_value_decimal=strong_value_decimal_price(
            p50, p25, ev_target=strong_target
        ),
        actionable_ev_target=actionable_target,
        strong_value_ev_target=strong_target,
        family=family,
    )


def decimal_to_american(decimal_odds: float) -> float:
    """Convert decimal odds to American odds."""
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be > 1")
    if decimal_odds >= 2.0:
        return (decimal_odds - 1.0) * 100.0
    return -100.0 / (decimal_odds - 1.0)


def american_or_better_meets_threshold(
    *,
    offered_american: float,
    threshold_american: float,
) -> bool:
    """Higher decimal is better; American 'or better' respects sign."""
    # Convert both to decimal for a uniform comparison.
    offered_decimal = _american_to_decimal(offered_american)
    threshold_decimal = _american_to_decimal(threshold_american)
    return offered_decimal >= threshold_decimal


def _american_to_decimal(american: float) -> float:
    if american == 0:
        raise ValueError("american odds cannot be 0")
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def offered_meets_actionable(
    *,
    offered_decimal: float,
    thresholds: PriceThresholds,
) -> bool:
    if offered_decimal <= 1.0:
        raise ValueError("offered_decimal must be > 1")
    return offered_decimal >= thresholds.actionable_decimal


def classify_recommendation(
    *,
    family: MarketFamily,
    maturity: MarketMaturity,
    p50: float,
    p25: float,
    gates_pass: bool,
    offered_decimal: float | None = None,
    prob_ev_positive: float | None = None,
) -> RecommendationClassification:
    """Classify confirmed_value / price_target / no_bet without requiring a book.

    - Failed gates or non-qualified maturity → ``no_bet``
    - Timestamped offered price meeting actionable (+ EV confidence) → confirmed
    - Otherwise → ``price_target`` with sportsbook-agnostic thresholds
    """
    if not gates_pass or maturity is not MarketMaturity.QUALIFIED:
        reason = (
            "market family not qualified"
            if maturity is not MarketMaturity.QUALIFIED
            else "model/data/maturity gate failed"
        )
        return RecommendationClassification(
            state=RecommendationState.NO_BET,
            reason=reason,
            thresholds=None,
            offered_decimal=offered_decimal,
        )

    thresholds = compute_price_thresholds(p50, p25, family=family)
    if offered_decimal is None:
        return RecommendationClassification(
            state=RecommendationState.PRICE_TARGET,
            reason="no timestamped offered price; publish sportsbook-agnostic thresholds",
            thresholds=thresholds,
            offered_decimal=None,
        )

    min_prob = confirmed_value_min_prob_ev_positive(family)
    meets_price = offered_meets_actionable(
        offered_decimal=offered_decimal,
        thresholds=thresholds,
    )
    meets_confidence = (
        prob_ev_positive is not None and prob_ev_positive >= min_prob
    )
    if meets_price and meets_confidence:
        return RecommendationClassification(
            state=RecommendationState.CONFIRMED_VALUE,
            reason=(
                f"offered price meets actionable threshold and "
                f"P(EV>0)>={min_prob}"
            ),
            thresholds=thresholds,
            offered_decimal=offered_decimal,
        )
    if not meets_price:
        reason = "offered price below actionable threshold"
    else:
        reason = f"P(EV>0) below required {min_prob}"
    return RecommendationClassification(
        state=RecommendationState.NO_BET,
        reason=reason,
        thresholds=thresholds,
        offered_decimal=offered_decimal,
    )
