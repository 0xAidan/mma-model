"""Void-aware exact EV and conditional semantics labels."""

from __future__ import annotations

from dataclasses import replace

from mma_model.domain.markets import RecommendationState
from mma_model.recommend.policy import (
    EV_LABEL_CONDITIONAL,
    EV_LABEL_EXACT,
    EV_LABEL_EXHAUSTIVE,
    P25_EV_ZERO_EPS,
    NoBetReason,
    ProbabilitySemantics,
    _price_gate_reasons,
    evaluate_selection,
    observed_ev,
    ranking_p25_ev,
    render_thresholds,
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


def test_inconsistent_void_components_cannot_confirm() -> None:
    decision = evaluate_selection(
        make_candidate(
            p50=0.50,
            p25=0.40,
            quote=eligible_quote(2.60),
            probability_semantics=ProbabilitySemantics.CONDITIONAL_NONVOID,
            p_win_unconditional=0.40,
            p_void=0.04,
        ),
        POLICY,
    )
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.MALFORMED_CANDIDATE
    assert decision.offered_decimal is None


def test_exhaustive_with_void_is_not_relabeled_conditional() -> None:
    candidate = make_candidate(
        p50=0.50,
        p25=0.40,
        quote=eligible_quote(2.60),
        probability_semantics=ProbabilitySemantics.EXHAUSTIVE,
        p_win_unconditional=None,
        p_void=0.04,
    )
    median, label = observed_ev(candidate, probability=0.50, offered_decimal=2.60)
    assert label == EV_LABEL_EXHAUSTIVE
    assert median == expected_value(0.50, 2.60)
    decision = evaluate_selection(candidate, POLICY)
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.MALFORMED_CANDIDATE


def test_ranking_p25_ev_stays_conditional_when_median_is_exact() -> None:
    candidate = make_candidate(
        p50=0.50,
        p25=0.40,
        quote=eligible_quote(2.60),
        probability_semantics=ProbabilitySemantics.CONDITIONAL_NONVOID,
        p_win_unconditional=0.48,
        p_void=0.04,
    )
    assert ranking_p25_ev(candidate, 2.60) == expected_value(0.40, 2.60)
    median, label = observed_ev(candidate, probability=0.50, offered_decimal=2.60)
    assert label == EV_LABEL_EXACT
    assert median == expected_value_with_void(p_win=0.48, p_void=0.04, offered_decimal=2.60)
    assert median != ranking_p25_ev(candidate, 2.60)


def test_p25_ev_boundary_epsilon_counts_as_zero() -> None:
    candidate = make_candidate(quote=eligible_quote(2.50), p50=0.50, p25=0.40)
    rendered = render_thresholds(0.50, 0.40, family=candidate.family)
    passing = _price_gate_reasons(
        candidate, POLICY, rendered, p25_ev=-P25_EV_ZERO_EPS
    )
    assert NoBetReason.P25_EV_NONPOSITIVE not in passing
    failing = _price_gate_reasons(
        replace(candidate, p25=0.40),
        POLICY,
        rendered,
        p25_ev=-(P25_EV_ZERO_EPS * 10),
    )
    assert NoBetReason.P25_EV_NONPOSITIVE in failing
