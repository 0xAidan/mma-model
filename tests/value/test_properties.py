"""Property-style tests for DWCS-204 value math invariants."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily
from mma_model.value.devig import proportional_devig
from mma_model.value.errors import InvalidOddsError
from mma_model.value.ev import expected_value
from mma_model.value.kelly import quarter_kelly_fraction
from mma_model.value.odds import american_to_decimal, decimal_to_american
from mma_model.value.thresholds import compute_value_price_thresholds


@pytest.mark.parametrize(
    "american",
    [-400, -250, -110, 100, 110, 150, 250, 500],
)
def test_decimal_american_round_trip(american: int) -> None:
    decimal = american_to_decimal(american)
    back = decimal_to_american(decimal)
    assert back == pytest.approx(float(american), rel=1e-9, abs=1e-9)
    assert american_to_decimal(back) == pytest.approx(decimal, rel=1e-9, abs=1e-12)


def test_even_money_american_minus_100_maps_to_plus_100() -> None:
    """Decimal 2.0 is conventionally rendered as +100, not -100."""
    assert american_to_decimal(-100) == pytest.approx(2.0)
    assert decimal_to_american(2.0) == pytest.approx(100.0)


@pytest.mark.parametrize("decimal", [1.01, 1.25, 1.5, 1.91, 2.0, 2.5, 3.4, 10.0])
def test_american_decimal_round_trip_from_decimal(decimal: float) -> None:
    american = decimal_to_american(decimal)
    assert american_to_decimal(american) == pytest.approx(decimal, rel=1e-9, abs=1e-12)


def test_implied_prob_monotonic_in_decimal() -> None:
    prev = None
    for decimal in [1.1, 1.5, 2.0, 3.0, 5.0, 11.0]:
        implied = 1.0 / decimal
        if prev is not None:
            assert implied < prev
        prev = implied


def test_ev_monotonic_in_offered_price() -> None:
    model_prob = 0.45
    prev = None
    for decimal in [1.5, 1.8, 2.0, 2.5, 3.0]:
        ev = expected_value(model_prob, decimal)
        if prev is not None:
            assert ev > prev
        prev = ev


def test_threshold_ordering_property_grid() -> None:
    for p50 in [0.2, 0.35, 0.5, 0.65]:
        for p25 in [p50 * 0.8, p50 * 0.95, p50]:
            t = compute_value_price_thresholds(
                p50, p25, family=MarketFamily.MONEYLINE
            )
            assert t.fair_decimal <= t.actionable_decimal + 1e-12
            assert t.actionable_decimal <= t.strong_value_decimal + 1e-12


def test_zero_edge_property() -> None:
    for p in [0.2, 0.333333, 0.5, 0.7]:
        assert expected_value(p, 1.0 / p) == pytest.approx(0.0, abs=1e-12)


def test_complete_devig_sum_property() -> None:
    grids = [
        [1.8, 2.1],
        [2.0, 3.0, 6.0],
        [1.5, 2.5, 4.0, 8.0],
    ]
    for odds in grids:
        result = proportional_devig(odds)
        assert sum(result.fair_probs) == pytest.approx(1.0, abs=1e-12)


def test_stake_never_exceeds_cap_property() -> None:
    for p in [0.55, 0.7, 0.9, 0.99]:
        for decimal in [1.5, 2.0, 5.0, 20.0]:
            try:
                stake = quarter_kelly_fraction(p, decimal, cap=0.01)
            except InvalidOddsError:
                continue
            assert 0.0 <= stake <= 0.01 + 1e-15
