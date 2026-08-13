"""Sportsbook-agnostic threshold rendering and exact confirmation boundaries."""

from __future__ import annotations

from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.markets.price_targets import compute_price_thresholds, decimal_to_american
from mma_model.recommend.policy import (
    GateId,
    NoBetReason,
    _price_gate_reasons,
    evaluate_selection,
    format_american_or_better,
    render_thresholds,
)
from mma_model.value.odds import american_to_decimal
from tests.recommend.helpers import POLICY, eligible_quote, make_candidate


def test_unpriced_qualified_row_has_exact_thresholds() -> None:
    decision = evaluate_selection(make_candidate(quote=None, p50=0.50, p25=0.40), POLICY)
    assert decision.classification is RecommendationState.PRICE_TARGET
    assert decision.thresholds is not None
    expected = compute_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    assert decision.thresholds.fair_decimal == expected.fair_decimal
    assert decision.thresholds.actionable_decimal == expected.actionable_decimal
    assert decision.thresholds.strong_value_decimal == expected.strong_value_decimal
    assert decision.thresholds.fair_american == decimal_to_american(expected.fair_decimal)
    assert decision.thresholds.actionable_or_better.endswith(" or better")
    assert decision.thresholds.strong_value_or_better.endswith(" or better")
    payload = decision.as_dict()
    assert payload["median_ev"] is None
    assert payload["roi"] is None
    assert payload["clv"] is None
    assert payload["profit"] is None
    assert payload["offered_decimal"] is None
    assert payload["is_best_available_market"] is False


def test_american_or_better_negative_and_positive_round_trip() -> None:
    favorite = render_thresholds(0.70, 0.65, family=MarketFamily.MONEYLINE)
    assert favorite.actionable_american < 0
    assert favorite.actionable_or_better.endswith(" or better")
    assert american_to_decimal(favorite.actionable_american) == favorite.actionable_decimal
    dog = render_thresholds(0.30, 0.25, family=MarketFamily.MONEYLINE)
    assert dog.actionable_american > 0
    assert dog.actionable_or_better.startswith("+")
    assert american_to_decimal(dog.actionable_american) == dog.actionable_decimal
    assert format_american_or_better(150.0) == "+150 or better"
    assert format_american_or_better(-150.0) == "-150 or better"


def test_exact_actionable_and_p25_ev_zero_boundaries_pass() -> None:
    decision = evaluate_selection(
        make_candidate(
            quote=eligible_quote(2.50),
            p50=0.50,
            p25=0.40,
            prob_ev_positive=0.70,
        ),
        POLICY,
    )
    assert decision.classification is RecommendationState.CONFIRMED_VALUE
    assert decision.p25_ev == 0.0


def test_below_actionable_and_p_ev_and_p25_ev_fail_exactly() -> None:
    below = evaluate_selection(
        make_candidate(quote=eligible_quote(2.49), p50=0.50, p25=0.40, prob_ev_positive=0.90),
        POLICY,
    )
    assert below.classification is RecommendationState.NO_BET
    assert NoBetReason.BELOW_ACTIONABLE in below.reasons
    low_p = evaluate_selection(
        make_candidate(quote=eligible_quote(2.50), p50=0.50, p25=0.40, prob_ev_positive=0.699999),
        POLICY,
    )
    assert NoBetReason.PROB_EV_POSITIVE_LOW in low_p.reasons
    neg = evaluate_selection(
        make_candidate(quote=eligible_quote(2.00), p50=0.50, p25=0.40, prob_ev_positive=0.90),
        POLICY,
    )
    assert NoBetReason.P25_EV_NONPOSITIVE in neg.reasons


def test_exact_round_requires_075_prob_ev_positive() -> None:
    """Boundary helper only. Default policy still treats exact-round as experimental."""
    rendered = render_thresholds(0.20, 0.18, family=MarketFamily.EXACT_ROUND)
    passing = make_candidate(
        family=MarketFamily.EXACT_ROUND,
        outcome=OutcomeKey.ROUND_1,
        p50=0.20,
        p25=0.18,
        quote=eligible_quote(6.0),
        prob_ev_positive=0.75,
    )
    assert NoBetReason.PROB_EV_POSITIVE_LOW not in _price_gate_reasons(
        passing, POLICY, rendered, p25_ev=0.08
    )
    low = make_candidate(
        family=MarketFamily.EXACT_ROUND,
        outcome=OutcomeKey.ROUND_1,
        p50=0.20,
        p25=0.18,
        quote=eligible_quote(6.0),
        prob_ev_positive=0.749,
    )
    assert NoBetReason.PROB_EV_POSITIVE_LOW in _price_gate_reasons(
        low, POLICY, rendered, p25_ev=0.08
    )


def test_nonproduction_and_missing_p_ev_cannot_confirm() -> None:
    short = evaluate_selection(
        make_candidate(
            quote=eligible_quote(2.60),
            bootstrap_successful_count=12,
            prob_ev_positive=0.99,
        ),
        POLICY,
    )
    assert NoBetReason.NONPRODUCTION_UNCERTAINTY in short.reasons
    assert GateId.PRICE not in {item.gate for item in short.gate_trace.results}
    assert short.offered_decimal is None
    missing = evaluate_selection(
        make_candidate(quote=eligible_quote(2.60), prob_ev_positive=None),
        POLICY,
    )
    assert NoBetReason.MISSING_PROB_EV_POSITIVE in missing.reasons
