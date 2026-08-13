"""Pre-price gates cannot be overridden by a spectacular offered price."""

from __future__ import annotations

from datetime import UTC, datetime

from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey, RecommendationState
from mma_model.recommend.policy import (
    GateId,
    NoBetReason,
    SelectionDecision,
    coerce_candidate,
    evaluate_selection,
)
from tests.recommend.helpers import POLICY, eligible_quote, make_candidate

SPECTACULAR = eligible_quote(offered=50.0)


def test_identity_failure_ignores_spectacular_price() -> None:
    decision = evaluate_selection(
        make_candidate(identity_resolved=False, quote=SPECTACULAR),
        POLICY,
    )
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.IDENTITY_UNRESOLVED
    assert decision.offered_decimal is None
    assert decision.median_ev is None
    gates = [item.gate for item in decision.gate_trace.results]
    assert gates[0] is GateId.IDENTITY
    assert GateId.PRICE not in gates


def test_ambiguous_and_replacement_are_identity_gates() -> None:
    ambiguous = evaluate_selection(make_candidate(ambiguous=True, quote=SPECTACULAR), POLICY)
    assert ambiguous.primary_reason is NoBetReason.AMBIGUOUS_SELECTION
    replaced = evaluate_selection(make_candidate(replacement=True, quote=SPECTACULAR), POLICY)
    assert replaced.primary_reason is NoBetReason.REPLACEMENT


def test_data_quality_before_price() -> None:
    decision = evaluate_selection(
        make_candidate(data_quality_pass=False, quote=SPECTACULAR),
        POLICY,
    )
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.DATA_QUALITY
    assert GateId.PRICE not in {item.gate for item in decision.gate_trace.results}


def test_model_and_calibration_before_price() -> None:
    unqualified = evaluate_selection(
        make_candidate(model_qualified=False, quote=SPECTACULAR),
        POLICY,
    )
    assert unqualified.primary_reason is NoBetReason.MODEL_UNQUALIFIED
    uncalibrated = evaluate_selection(
        make_candidate(calibrated=False, quote=SPECTACULAR),
        POLICY,
    )
    assert uncalibrated.primary_reason is NoBetReason.UNCALIBRATED


def test_missing_p25_before_price() -> None:
    decision = evaluate_selection(
        make_candidate(p25=None, quote=SPECTACULAR),
        POLICY,
    )
    assert decision.primary_reason is NoBetReason.MISSING_P25
    assert decision.thresholds is None


def test_experimental_family_cannot_confirm_or_target() -> None:
    decision = evaluate_selection(
        make_candidate(
            family=MarketFamily.METHOD,
            outcome=OutcomeKey.KO_TKO,
            market_maturity=MarketMaturity.EXPERIMENTAL,
            quote=SPECTACULAR,
        ),
        POLICY,
    )
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.MARKET_EXPERIMENTAL


def test_stale_and_post_cutoff_cannot_confirm() -> None:
    stale = evaluate_selection(
        make_candidate(quote=eligible_quote(offered=50.0, stale=True, lifecycle="stale")),
        POLICY,
    )
    assert stale.classification is RecommendationState.NO_BET
    assert stale.primary_reason is NoBetReason.STALE_LINE
    assert stale.offered_decimal == 50.0
    late = evaluate_selection(
        make_candidate(
            quote=eligible_quote(
                offered=50.0,
                observed_at=datetime(2024, 8, 13, 2, 0, tzinfo=UTC),
            )
        ),
        POLICY,
    )
    assert late.primary_reason is NoBetReason.POST_CUTOFF


def test_suspended_replaced_ineligible_cannot_confirm() -> None:
    suspended = evaluate_selection(
        make_candidate(
            quote=eligible_quote(offered=50.0, suspended=True, availability="suspended")
        ),
        POLICY,
    )
    assert suspended.primary_reason is NoBetReason.SUSPENDED_LINE
    replaced = evaluate_selection(
        make_candidate(quote=eligible_quote(offered=50.0, replaced=True, lifecycle="replaced")),
        POLICY,
    )
    assert replaced.primary_reason is NoBetReason.REPLACED_LINE
    ineligible = evaluate_selection(
        make_candidate(quote=eligible_quote(offered=50.0, eligible=False)),
        POLICY,
    )
    assert ineligible.primary_reason is NoBetReason.INELIGIBLE_QUOTE


def test_malformed_candidate_is_typed_no_bet() -> None:
    decision = coerce_candidate({"event_id": "e", "bout_id": "b", "p50": 2.0}, POLICY)
    assert isinstance(decision, SelectionDecision)
    assert decision.classification is RecommendationState.NO_BET
    assert decision.primary_reason is NoBetReason.MALFORMED_CANDIDATE
