"""Validated decimal / American odds and probability conversions (DWCS-204).

Internal math keeps full float precision. Rounding helpers exist only for
display / persistence boundaries.
"""

from __future__ import annotations

from typing import Final

from mma_model.value.errors import InvalidOddsError, InvalidProbabilityError

VALUE_MATH_METHOD: Final = "dwcs_value_math"
VALUE_MATH_VERSION: Final = "1.0.0"

DISPLAY_DECIMAL_PLACES: Final = 6
DISPLAY_AMERICAN_PLACES: Final = 2
DISPLAY_PROBABILITY_PLACES: Final = 6


def validate_probability(
    value: float,
    *,
    field: str = "probability",
    allow_one: bool = False,
) -> float:
    """Require a usable probability in (0, 1) or optionally (0, 1]."""
    try:
        prob = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidProbabilityError(f"{field} must be a number") from exc
    if allow_one:
        if not 0.0 < prob <= 1.0:
            raise InvalidProbabilityError(f"{field} must be in (0, 1]")
    elif not 0.0 < prob < 1.0:
        raise InvalidProbabilityError(f"{field} must be in (0, 1)")
    return prob


def validate_decimal_odds(value: float, *, field: str = "decimal_odds") -> float:
    """Require decimal odds strictly greater than 1.0."""
    try:
        decimal = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOddsError(f"{field} must be a number") from exc
    if decimal <= 1.0:
        raise InvalidOddsError(f"{field} must be > 1 (got {value!r})")
    if decimal != decimal:  # NaN
        raise InvalidOddsError(f"{field} must be finite")
    if decimal == float("inf"):
        raise InvalidOddsError(f"{field} must be finite")
    return decimal


def validate_american_odds(value: float, *, field: str = "american_odds") -> float:
    """Require conventional American odds: <= -100 or >= +100.

    Zero American odds are rejected as ambiguous. Values in (-100, 100)
    excluding the conventional ±100 boundaries are invalid American quotes
    (provider decimal pass-through belongs in odds.normalize only).
    """
    try:
        american = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidOddsError(f"{field} must be a number") from exc
    if american != american or american in (float("inf"), float("-inf")):
        raise InvalidOddsError(f"{field} must be finite")
    if american == 0.0:
        raise InvalidOddsError(f"{field} cannot be 0 (ambiguous American odds)")
    if -100.0 < american < 100.0:
        raise InvalidOddsError(
            f"{field} must be <= -100 or >= +100 (got {value!r})"
        )
    return american


def american_to_decimal(american: float) -> float:
    """Convert validated American odds to decimal odds (no display rounding)."""
    american = validate_american_odds(american)
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def decimal_to_american(decimal_odds: float) -> float:
    """Convert validated decimal odds to American odds (no display rounding)."""
    decimal_odds = validate_decimal_odds(decimal_odds)
    if decimal_odds >= 2.0:
        return (decimal_odds - 1.0) * 100.0
    return -100.0 / (decimal_odds - 1.0)


def decimal_to_implied_prob(decimal_odds: float) -> float:
    """Book implied probability from decimal odds: 1 / decimal."""
    decimal_odds = validate_decimal_odds(decimal_odds)
    return 1.0 / decimal_odds


def american_to_implied_prob(american: float) -> float:
    """Book implied probability from American odds."""
    return decimal_to_implied_prob(american_to_decimal(american))


def probability_to_decimal(probability: float, *, allow_one: bool = False) -> float:
    """Break-even decimal odds from a probability: 1 / p."""
    probability = validate_probability(probability, allow_one=allow_one)
    return 1.0 / probability


def round_decimal_for_display(decimal_odds: float) -> float:
    """Display / persistence rounding for decimal odds only."""
    return round(validate_decimal_odds(decimal_odds), DISPLAY_DECIMAL_PLACES)


def round_american_for_display(american: float) -> float:
    """Display rounding for American odds only."""
    return round(validate_american_odds(american), DISPLAY_AMERICAN_PLACES)


def round_probability_for_display(probability: float, *, allow_one: bool = True) -> float:
    """Display rounding for probabilities only."""
    return round(
        validate_probability(probability, allow_one=allow_one),
        DISPLAY_PROBABILITY_PLACES,
    )
