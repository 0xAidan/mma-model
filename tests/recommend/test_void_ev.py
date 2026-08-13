"""Void-aware exact EV and conditional semantics labels."""

from __future__ import annotations

from mma_model.recommend.policy import (
    EV_LABEL_CONDITIONAL,
    EV_LABEL_EXACT,
    ProbabilitySemantics,
    evaluate_selection,
    observed_ev,
)
from mma_model.value.ev import expected_value, expected_value_with_void
from tests.recommend.helpers import POLICY, eligible_quote, make_candidate


def test_void_aware_exact_ev_hand_check() -> None:
    candidate = make_candidate(
        p50=0.50,
        p25=0.40,
        quote=eligible_quote(2.60),
        probability_semantics=ProbabilitySemantics.CONDITIONAL_NONVOID,
        p_win_unconditional=0.48,
        p_void=0.04,
    )
    decision = evaluate_selection(candidate, POLICY)
    expected = expected_value_with_void(p_win=0.48, p_void=0.04, offered_decimal=2.60)
    assert decision.median_ev == expected
    assert decision.ev_semantics_label == EV_LABEL_EXACT
    ranking = expected_value(0.40, 2.60)
    assert decision.p25_ev == ranking


def test_missing_unconditional_components_label_conditional() -> None:
    candidate = make_candidate(
        p50=0.50,
        p25=0.40,
        quote=eligible_quote(2.60),
        probability_semantics=ProbabilitySemantics.CONDITIONAL_NONVOID,
        p_win_unconditional=None,
        p_void=None,
    )
    median, label = observed_ev(candidate, probability=0.50, offered_decimal=2.60)
    assert label == EV_LABEL_CONDITIONAL
    assert median == expected_value(0.50, 2.60)
    decision = evaluate_selection(candidate, POLICY)
    assert decision.ev_semantics_label == EV_LABEL_CONDITIONAL
    # p50 is already conditional; do not condition twice.
    assert decision.median_ev == expected_value(0.50, 2.60)
