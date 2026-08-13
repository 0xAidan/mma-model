"""Integration: DWCS-204 value math with DWCS-200/202 guidance paths."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey
from mma_model.markets.price_targets import compute_price_thresholds, decimal_to_american
from mma_model.odds.manual_price import compute_exact_ev
from mma_model.odds.price_guidance import build_price_guidance
from mma_model.value.ev import expected_value
from mma_model.value.odds import american_to_decimal
from mma_model.value.odds import decimal_to_american as value_d2a
from mma_model.value.thresholds import compute_value_price_thresholds


def test_price_targets_delegate_conversions_to_value_math() -> None:
    assert decimal_to_american(2.5) == pytest.approx(value_d2a(2.5))
    assert american_to_decimal(-200) == pytest.approx(1.5)


def test_markets_and_value_thresholds_agree() -> None:
    markets = compute_price_thresholds(0.42, 0.38, family=MarketFamily.METHOD)
    value = compute_value_price_thresholds(0.42, 0.38, family=MarketFamily.METHOD)
    assert markets.fair_decimal == pytest.approx(value.fair_decimal)
    assert markets.actionable_decimal == pytest.approx(value.actionable_decimal)
    assert markets.strong_value_decimal == pytest.approx(value.strong_value_decimal)


def test_exact_ev_alias_matches_value_expected_value() -> None:
    assert compute_exact_ev(0.5, 2.2) == pytest.approx(expected_value(0.5, 2.2))


def test_unpriced_guidance_still_emits_thresholds_without_ev() -> None:
    row = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.48,
        gates_pass=True,
        observed=None,
    )
    assert row.thresholds is not None
    assert row.exact_ev is None
    assert row.exact_ev_available is False
