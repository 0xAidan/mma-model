"""Sportsbook-agnostic price-target contract tests (DWCS-200)."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, MarketMaturity, RecommendationState
from mma_model.markets.price_targets import (
    EXACT_ROUND_ACTIONABLE_EV_TARGET,
    STANDARD_ACTIONABLE_EV_TARGET,
    american_or_better_meets_threshold,
    classify_recommendation,
    compute_price_thresholds,
    decimal_to_american,
)


def test_thresholds_match_evaluation_formulas() -> None:
    # p50=0.50 → fair 2.0; p25=0.40 → 1/p25=2.5; 1.05/p50=2.1 → actionable 2.5
    # strong 1.10/p50=2.2 → max(2.5, 2.2)=2.5
    thresholds = compute_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    assert thresholds.fair_decimal == pytest.approx(2.0)
    assert thresholds.actionable_decimal == pytest.approx(2.5)
    assert thresholds.strong_value_decimal == pytest.approx(2.5)
    assert thresholds.actionable_ev_target == STANDARD_ACTIONABLE_EV_TARGET


def test_actionable_uses_target_ev_when_higher_than_p25() -> None:
    # p50=0.40, p25=0.38 → 1/p25≈2.631; 1.05/0.40=2.625 → actionable ≈2.631
    thresholds = compute_price_thresholds(0.40, 0.38, family=MarketFamily.MONEYLINE)
    assert thresholds.actionable_decimal == pytest.approx(1.0 / 0.38)
    # strong: 1.10/0.40=2.75 > 1/p25 → 2.75
    assert thresholds.strong_value_decimal == pytest.approx(2.75)


def test_exact_round_uses_10pct_actionable_target() -> None:
    thresholds = compute_price_thresholds(0.20, 0.18, family=MarketFamily.EXACT_ROUND)
    assert thresholds.actionable_ev_target == EXACT_ROUND_ACTIONABLE_EV_TARGET
    # 1/p25≈5.555; 1.10/p50=5.5 → actionable 5.555...
    assert thresholds.actionable_decimal == pytest.approx(1.0 / 0.18)


def test_price_targets_are_deterministic() -> None:
    a = compute_price_thresholds(0.55, 0.50, family=MarketFamily.METHOD)
    b = compute_price_thresholds(0.55, 0.50, family=MarketFamily.METHOD)
    assert a == b


def test_rejects_inverted_percentiles() -> None:
    with pytest.raises(ValueError, match="p25"):
        compute_price_thresholds(0.40, 0.50, family=MarketFamily.MONEYLINE)


def test_unpriced_qualified_selection_is_price_target() -> None:
    result = classify_recommendation(
        family=MarketFamily.MONEYLINE,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.50,
        gates_pass=True,
        offered_decimal=None,
    )
    assert result.state is RecommendationState.PRICE_TARGET
    assert result.thresholds is not None
    assert result.thresholds.fair_decimal == pytest.approx(1.0 / 0.55)


def test_confirmed_value_requires_offer_and_confidence() -> None:
    thresholds = compute_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    ok = classify_recommendation(
        family=MarketFamily.MONEYLINE,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.40,
        gates_pass=True,
        offered_decimal=thresholds.actionable_decimal,
        prob_ev_positive=0.80,
    )
    assert ok.state is RecommendationState.CONFIRMED_VALUE

    low_conf = classify_recommendation(
        family=MarketFamily.MONEYLINE,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.40,
        gates_pass=True,
        offered_decimal=thresholds.actionable_decimal,
        prob_ev_positive=0.50,
    )
    assert low_conf.state is RecommendationState.NO_BET


def test_experimental_family_cannot_emit_price_target() -> None:
    result = classify_recommendation(
        family=MarketFamily.METHOD,
        maturity=MarketMaturity.EXPERIMENTAL,
        p50=0.30,
        p25=0.25,
        gates_pass=True,
        offered_decimal=None,
    )
    assert result.state is RecommendationState.NO_BET
    assert "not qualified" in result.reason


def test_failed_gates_emit_no_bet() -> None:
    result = classify_recommendation(
        family=MarketFamily.MONEYLINE,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.50,
        gates_pass=False,
    )
    assert result.state is RecommendationState.NO_BET


def test_american_or_better_for_favorite_and_dog() -> None:
    # Threshold -150; offered -140 is better (shorter favorite liability / higher decimal)
    assert american_or_better_meets_threshold(
        offered_american=-140,
        threshold_american=-150,
    )
    assert not american_or_better_meets_threshold(
        offered_american=-160,
        threshold_american=-150,
    )
    # Threshold +150; offered +160 is better
    assert american_or_better_meets_threshold(
        offered_american=160,
        threshold_american=150,
    )
    assert not american_or_better_meets_threshold(
        offered_american=140,
        threshold_american=150,
    )


def test_decimal_to_american_roundtrip_shape() -> None:
    assert decimal_to_american(2.0) == pytest.approx(100.0)
    assert decimal_to_american(1.5) == pytest.approx(-200.0)
    assert decimal_to_american(3.0) == pytest.approx(200.0)
