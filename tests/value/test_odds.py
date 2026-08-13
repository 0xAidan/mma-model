"""Unit tests for validated odds conversions (DWCS-204)."""

from __future__ import annotations

import math

import pytest

from mma_model.value.errors import InvalidOddsError, InvalidProbabilityError
from mma_model.value.odds import (
    american_to_decimal,
    american_to_implied_prob,
    decimal_to_american,
    decimal_to_implied_prob,
    probability_to_decimal,
    round_decimal_for_display,
    validate_american_odds,
    validate_decimal_odds,
    validate_probability,
)


def test_rejects_zero_american_odds() -> None:
    with pytest.raises(InvalidOddsError, match="cannot be 0"):
        validate_american_odds(0)
    with pytest.raises(InvalidOddsError):
        american_to_decimal(0)


def test_rejects_ambiguous_short_american() -> None:
    with pytest.raises(InvalidOddsError):
        validate_american_odds(50)
    with pytest.raises(InvalidOddsError):
        validate_american_odds(-50)
    with pytest.raises(InvalidOddsError):
        validate_american_odds(1.74)


def test_rejects_impossible_decimal_probability_nan_inf_and_one() -> None:
    with pytest.raises(InvalidOddsError):
        validate_decimal_odds(1.0)
    with pytest.raises(InvalidOddsError):
        validate_decimal_odds(math.nan)
    with pytest.raises(InvalidOddsError):
        validate_decimal_odds(math.inf)
    with pytest.raises(InvalidProbabilityError):
        validate_probability(0.0)
    with pytest.raises(InvalidProbabilityError):
        validate_probability(1.0)
    with pytest.raises(InvalidProbabilityError):
        validate_probability(math.nan)
    with pytest.raises(InvalidProbabilityError):
        validate_probability(math.inf)
    with pytest.raises(InvalidProbabilityError):
        probability_to_decimal(1.0)


def test_american_decimal_round_trip_known_points() -> None:
    assert american_to_decimal(-150) == pytest.approx(1.0 + 100.0 / 150.0)
    assert american_to_decimal(150) == pytest.approx(2.5)
    assert american_to_implied_prob(-150) == pytest.approx(0.6)
    assert american_to_implied_prob(150) == pytest.approx(0.4)
    assert decimal_to_american(2.0) == pytest.approx(100.0)
    assert decimal_to_american(1.5) == pytest.approx(-200.0)
    assert decimal_to_implied_prob(2.0) == pytest.approx(0.5)
    assert probability_to_decimal(0.5) == pytest.approx(2.0)


def test_display_rounding_is_boundary_only() -> None:
    raw = american_to_decimal(-150)
    assert round_decimal_for_display(raw) == pytest.approx(1.666667)
