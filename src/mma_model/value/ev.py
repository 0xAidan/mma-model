"""Expected value, CLV, closing EV, and flat-unit profit (DWCS-204)."""

from __future__ import annotations

from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import InvalidOddsError, InvalidProbabilityError
from mma_model.value.odds import (
    american_to_decimal,
    american_to_implied_prob,
    decimal_to_implied_prob,
    validate_decimal_odds,
    validate_probability,
)


def expected_value(model_prob: float, offered_decimal: float) -> float:
    """Exact EV per 1 unit staked: ``model_prob * offered_decimal - 1``."""
    model_prob = validate_probability(model_prob, field="model_prob")
    offered_decimal = validate_decimal_odds(offered_decimal, field="offered_decimal")
    return model_prob * offered_decimal - 1.0


def closing_ev(model_prob: float, closing_decimal: float) -> float:
    """EV versus the same-line closing decimal price."""
    return expected_value(model_prob, closing_decimal)


def same_line_probability_clv(
    *,
    bet_decimal: float,
    close_decimal: float,
) -> float:
    """Same-line probability CLV: ``implied(close) - implied(bet)``.

    Positive means the bet price beat the close (lower implied at bet time).
    """
    bet_implied = decimal_to_implied_prob(bet_decimal)
    close_implied = decimal_to_implied_prob(close_decimal)
    return close_implied - bet_implied


def flat_unit_profit(
    *,
    settlement: SettlementResult,
    offered_decimal: float,
) -> float:
    """Realized profit for a flat 1-unit stake.

    Push and void are exactly zero. Unresolved cannot produce realized profit.
    """
    if settlement is SettlementResult.UNRESOLVED:
        raise InvalidOddsError("unresolved settlements cannot produce realized profit")
    offered_decimal = validate_decimal_odds(offered_decimal, field="offered_decimal")
    if settlement is SettlementResult.WIN:
        return offered_decimal - 1.0
    if settlement is SettlementResult.LOSS:
        return -1.0
    if settlement is SettlementResult.PUSH or settlement is SettlementResult.VOID:
        return 0.0
    never: SettlementResult = settlement
    raise InvalidOddsError(f"unsupported settlement result: {never!r}")


def compute_exact_ev(model_prob: float, offered_decimal: float) -> float:
    """Compatibility alias used by DWCS-202 price guidance."""
    return expected_value(model_prob, offered_decimal)


def ev_vs_fair(model_prob: float, offered_american: float) -> float:
    """Legacy American-odds EV helper; converts then uses decimal EV."""
    try:
        return expected_value(model_prob, american_to_decimal(offered_american))
    except (InvalidOddsError, InvalidProbabilityError):
        raise


# Re-export for legacy tests / callers.
__all__ = [
    "american_to_implied_prob",
    "closing_ev",
    "compute_exact_ev",
    "ev_vs_fair",
    "expected_value",
    "flat_unit_profit",
    "same_line_probability_clv",
]
