"""Threshold, EV, CLV, profit, ROI, staking, and priced-metric tests (DWCS-204)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mma_model.domain.markets import MarketFamily
from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import (
    IneligiblePriceError,
    InvalidOddsError,
    InvalidProbabilityError,
    SelectionMismatchError,
    UnpricedMetricsError,
)
from mma_model.value.ev import (
    CLV_UNIT,
    closing_ev,
    expected_value,
    flat_unit_profit,
    same_selection_probability_clv,
    unsafe_same_line_probability_clv,
)
from mma_model.value.evidence import (
    ClosingPriceEvidence,
    ManualBindingSource,
    ManualBoutBindingAssertion,
    ManualObservedPriceEvidence,
    PriceObservationRole,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    SelectionPriceObservation,
    ValueSelectionContext,
)
from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    MAX_BANKROLL_CAP_FRACTION,
    fractional_kelly,
    quarter_kelly_fraction,
)
from mma_model.value.portfolio import stake_amount
from mma_model.value.priced import (
    MODEL_PROBABILITY_UNIT,
    ROI_UNIT,
    MetricsUnavailableReason,
    PricedValueRequest,
    compute_priced_value_metrics,
)
from mma_model.value.thresholds import compute_value_price_thresholds

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=2)
BOUT_A = "bout-a"
BOUT_B = "bout-b"


def _binding(
    bout_id: str = BOUT_A,
    *,
    at: datetime = T0,
) -> ManualBoutBindingAssertion:
    return ManualBoutBindingAssertion(
        bout_id=bout_id,
        asserted_at=at,
        asserted_by="tester",
        source=ManualBindingSource.USER_ASSERTION,
        note="test binding",
    )


def _ctx(bout_id: str = BOUT_A) -> ValueSelectionContext:
    return ValueSelectionContext(
        bout_id=bout_id,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        event_id="event-1",
    )


def _manual(
    price: float = 2.20,
    *,
    at: datetime = T0,
    bout_id: str | None = BOUT_A,
    role: PriceObservationRole = PriceObservationRole.OPENING,
    bookmaker_key: str = "manual_book",
    region: str = "us",
) -> ManualObservedPriceEvidence:
    binding = None if bout_id is None else _binding(bout_id, at=at)
    return ManualObservedPriceEvidence(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        automated=False,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        selection_identity="moneyline:fighter_a",
        price_decimal=price,
        lifecycle="available",
        observed_at=at,
        bookmaker_key=bookmaker_key,
        region=region,
        bout_binding=binding,
        price_role=role,
    )


def _quote(
    *,
    quote_id: int = 1,
    price: float = 2.20,
    bout_id: str | None = BOUT_A,
    at: datetime = T0,
    role: PriceObservationRole = PriceObservationRole.OPENING,
    bookmaker_key: str = "ref_book",
    region: str = "us",
) -> ProviderQuoteEvidence:
    return ProviderQuoteEvidence(
        quote_id=quote_id,
        provider="the_odds_api",
        bookmaker_key=bookmaker_key,
        region=region,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        selection_identity="moneyline:fighter_a",
        price_decimal=price,
        availability="available",
        observed_at=at,
        bout_id=bout_id,
        dedupe_key="dedupe-1",
        external_event_id="ext-1",
        price_role=role,
    )


def _elig(
    *,
    quote_id: int = 1,
    eligible: bool = True,
    reason: str = "none",
    bout_id: str | None = BOUT_A,
    evaluated_at: datetime = T0,
    availability: str = "available",
) -> QuoteEligibilityEvidence:
    return QuoteEligibilityEvidence(
        quote_id=quote_id,
        eligible=eligible,
        selection_identity="moneyline:fighter_a",
        resolved_bout_id=bout_id,
        reason=reason,
        evaluated_at=evaluated_at,
        quote_availability_at_decision=availability,
        quote_freshness_at=evaluated_at,
        lifecycle_state_at_decision="active",
        decision_version="odds_decision_v1",
    )


def _close_manual(
    price: float = 2.00,
    *,
    at: datetime = T1,
    bout_id: str = BOUT_A,
    bookmaker_key: str = "manual_book",
    region: str = "us",
    allow_cross_book: bool = False,
) -> ClosingPriceEvidence:
    return ClosingPriceEvidence(
        manual_evidence=_manual(
            price,
            at=at,
            bout_id=bout_id,
            role=PriceObservationRole.CLOSING,
            bookmaker_key=bookmaker_key,
            region=region,
        ),
        allow_cross_book=allow_cross_book,
    )


def _close_provider(
    price: float = 2.00,
    *,
    at: datetime = T1,
    bout_id: str = BOUT_A,
    quote_id: int = 9,
    bookmaker_key: str = "ref_book",
    region: str = "us",
    allow_cross_book: bool = False,
) -> ClosingPriceEvidence:
    return ClosingPriceEvidence(
        quote_evidence=_quote(
            quote_id=quote_id,
            price=price,
            bout_id=bout_id,
            at=at,
            role=PriceObservationRole.CLOSING,
            bookmaker_key=bookmaker_key,
            region=region,
        ),
        eligibility_evidence=_elig(
            quote_id=quote_id,
            eligible=True,
            bout_id=bout_id,
            evaluated_at=at,
        ),
        closing_cutoff=at,
        allow_cross_book=allow_cross_book,
    )


def test_thresholds_match_pinned_contract_and_exact_round_override() -> None:
    ml = compute_value_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    assert ml.fair_decimal == pytest.approx(2.0)
    assert ml.break_even_decimal == pytest.approx(2.5)
    assert ml.actionable_decimal == pytest.approx(2.5)
    assert ml.actionable_american == pytest.approx(150.0)

    exact = compute_value_price_thresholds(0.20, 0.18, family=MarketFamily.EXACT_ROUND)
    assert exact.actionable_ev_target == pytest.approx(0.10)
    assert exact.actionable_decimal == pytest.approx(1.0 / 0.18)


def test_threshold_rejects_p_equals_one() -> None:
    with pytest.raises(InvalidProbabilityError):
        compute_value_price_thresholds(1.0, 0.9, family=MarketFamily.MONEYLINE)


def test_zero_edge_ev_and_positive_edge() -> None:
    assert expected_value(0.5, 2.0) == pytest.approx(0.0)
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)
    assert closing_ev(0.55, 1.90) == pytest.approx(0.55 * 1.90 - 1.0)


def test_same_selection_clv_requires_strictly_later_close() -> None:
    opening = SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        bout_id=BOUT_A,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        market_selection_identity="moneyline:fighter_a",
        value_selection_identity=f"{BOUT_A}|moneyline:fighter_a",
        price_decimal=2.20,
        observed_at=T0,
        lifecycle_or_availability="available",
        price_role=PriceObservationRole.OPENING,
    )
    closing = SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        bout_id=BOUT_A,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        market_selection_identity="moneyline:fighter_a",
        value_selection_identity=f"{BOUT_A}|moneyline:fighter_a",
        price_decimal=2.00,
        observed_at=T1,
        lifecycle_or_availability="available",
        price_role=PriceObservationRole.CLOSING,
    )
    clv = same_selection_probability_clv(opening=opening, closing=closing)
    assert clv == pytest.approx(
        unsafe_same_line_probability_clv(bet_decimal=2.20, close_decimal=2.00)
    )
    same_ts = SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        bout_id=BOUT_A,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        market_selection_identity="moneyline:fighter_a",
        value_selection_identity=f"{BOUT_A}|moneyline:fighter_a",
        price_decimal=2.00,
        observed_at=T0,
        lifecycle_or_availability="available",
        price_role=PriceObservationRole.CLOSING,
    )
    with pytest.raises(SelectionMismatchError, match="strictly greater"):
        same_selection_probability_clv(opening=opening, closing=same_ts)


def test_push_void_profit_and_roi_zero() -> None:
    assert flat_unit_profit(settlement=SettlementResult.PUSH, offered_decimal=2.0) == 0.0
    assert flat_unit_profit(settlement=SettlementResult.VOID, offered_decimal=2.0) == 0.0
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(2.5),
            settlement=SettlementResult.PUSH,
        )
    )
    assert row.flat_unit_profit == 0.0
    assert row.realized_roi == 0.0
    assert row.realized_roi_unit == ROI_UNIT


def test_quarter_kelly_hard_cap_and_rejects_over_cap() -> None:
    stake = quarter_kelly_fraction(0.90, 3.0)
    assert stake <= MAX_BANKROLL_CAP_FRACTION
    with pytest.raises(InvalidOddsError):
        quarter_kelly_fraction(0.90, 3.0, cap=0.05)
    with pytest.raises(InvalidOddsError):
        fractional_kelly(0.55, -110, fraction=1.5, cap=0.01)
    assert stake_amount(stake_fraction=0.5, bankroll=1000.0) == pytest.approx(10.0)


def test_unpriced_cannot_produce_metrics_or_roi() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.UNPRICED_TARGET
    assert row.expected_value is None
    assert row.realized_roi is None
    assert row.probability_clv is None
    with pytest.raises(UnpricedMetricsError):
        row.require_available()


def test_unbound_manual_price_cannot_produce_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(bout_id=None),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.UNBOUND_MANUAL_PRICE
    assert row.realized_roi is None


def test_cross_bout_manual_price_rejected() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(BOUT_A),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(bout_id=BOUT_B),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.CONTEXT_MISMATCH


def test_cross_bout_provider_eligibility_rejected() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(BOUT_A),
            valuation_cutoff=T0,
            product_eligible=True,
            quote_evidence=_quote(bout_id=BOUT_B),
            eligibility_evidence=_elig(eligible=True, bout_id=BOUT_B),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.CONTEXT_MISMATCH


def test_match_gate_alone_insufficient_for_provider_quote() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            quote_evidence=_quote(),
            eligibility_evidence=_elig(
                eligible=False,
                reason="unmatched",
                bout_id=None,
                availability="unknown",
            ),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.MATCH_GATE_ONLY


def test_eligible_true_requires_resolved_bout_and_reason_none() -> None:
    with pytest.raises(IneligiblePriceError, match="resolved_bout_id"):
        QuoteEligibilityEvidence(
            quote_id=1,
            eligible=True,
            selection_identity="moneyline:fighter_a",
            resolved_bout_id=None,
            reason="none",
            evaluated_at=T0,
            quote_availability_at_decision="available",
        )
    with pytest.raises(IneligiblePriceError, match="reason='none'"):
        QuoteEligibilityEvidence(
            quote_id=1,
            eligible=True,
            selection_identity="moneyline:fighter_a",
            resolved_bout_id=BOUT_A,
            reason="stale",
            evaluated_at=T0,
            quote_availability_at_decision="available",
        )
    with pytest.raises(IneligiblePriceError, match="must not use reason='none'"):
        QuoteEligibilityEvidence(
            quote_id=1,
            eligible=False,
            selection_identity="moneyline:fighter_a",
            resolved_bout_id=None,
            reason="none",
            evaluated_at=T0,
            quote_availability_at_decision="unknown",
        )


def test_provider_close_requires_eligibility_not_naked_dto() -> None:
    with pytest.raises(IneligiblePriceError, match="QuoteEligibilityEvidence"):
        ClosingPriceEvidence(
            quote_evidence=_quote(role=PriceObservationRole.CLOSING),
            eligibility_evidence=None,
            closing_cutoff=T1,
        )


def test_eligible_provider_quote_emits_priced_metrics_and_roi() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            quote_evidence=_quote(price=2.20),
            eligibility_evidence=_elig(eligible=True),
            closing_evidence=_close_provider(2.00),
            settlement=SettlementResult.WIN,
        )
    )
    assert row.available is True
    assert row.target_value_selection_identity == f"{BOUT_A}|moneyline:fighter_a"
    assert row.expected_value == pytest.approx(0.55 * 2.20 - 1.0)
    assert row.closing_ev == pytest.approx(0.55 * 2.00 - 1.0)
    assert row.probability_clv is not None and row.probability_clv > 0
    assert row.probability_clv_unit == CLV_UNIT
    assert row.flat_unit_profit == pytest.approx(1.20)
    assert row.realized_roi == pytest.approx(row.flat_unit_profit)
    assert row.realized_roi_unit == ROI_UNIT
    assert row.model_probability_unit == MODEL_PROBABILITY_UNIT
    assert row.stake_fraction is not None
    assert row.stake_fraction <= DEFAULT_BANKROLL_CAP_FRACTION
    assert row.opening_provenance is not None
    assert row.opening_provenance.source_kind == "provider_quote"
    assert row.opening_provenance.eligibility_evaluated_at == T0.isoformat()
    assert row.closing_provenance is not None
    assert row.closing_provenance.bookmaker_key == "ref_book"
    assert row.closing_provenance.cross_book_closing is False
    assert row.bankroll_cap_fraction == DEFAULT_BANKROLL_CAP_FRACTION


def test_user_observed_priced_path_and_missing_close() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.50,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(2.20),
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.10)
    assert row.closing_ev is None
    assert row.closing_ev_reason is MetricsUnavailableReason.MISSING_CLOSING_PRICE
    assert row.probability_clv is None
    assert row.realized_roi is None
    assert row.realized_roi_reason is MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    assert row.opening_provenance is not None
    assert row.opening_provenance.manual_binding_source == "user_assertion"


def test_same_timestamp_close_suppresses_clv() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(2.20, at=T0),
            closing_evidence=_close_manual(2.00, at=T0),
        )
    )
    assert row.available is True
    assert row.expected_value is not None
    assert row.probability_clv is None
    assert row.closing_ev is None
    assert row.probability_clv_reason is MetricsUnavailableReason.NON_CLOSING_SAME_TIMESTAMP


def test_unresolved_preserves_ev_clv_stake_suppresses_profit_and_roi() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            manual_evidence=_manual(2.20),
            closing_evidence=_close_manual(2.00),
            settlement=SettlementResult.UNRESOLVED,
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.55 * 2.20 - 1.0)
    assert row.probability_clv is not None
    assert row.closing_ev is not None
    assert row.stake_fraction is not None
    assert row.flat_unit_profit is None
    assert row.realized_roi is None
    assert row.flat_unit_profit_reason is MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    assert row.realized_roi_reason is MetricsUnavailableReason.UNRESOLVED_SETTLEMENT


def test_value_selection_context_is_fight_unique() -> None:
    a = _ctx(BOUT_A)
    b = _ctx(BOUT_B)
    assert a.market_selection_identity == b.market_selection_identity
    assert a.value_selection_identity != b.value_selection_identity
    assert a.value_selection_identity.startswith(f"{BOUT_A}|")


def test_stale_eligibility_after_lock_rejected() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T1,
            product_eligible=True,
            quote_evidence=_quote(at=T0),
            # Decision evaluated at older opening time, not at valuation cutoff.
            eligibility_evidence=_elig(eligible=True, evaluated_at=T0),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.ELIGIBILITY_CUTOFF_MISMATCH


def test_locked_eligibility_at_cutoff_blocks_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T1,
            product_eligible=True,
            quote_evidence=_quote(at=T0),
            eligibility_evidence=_elig(
                eligible=False,
                reason="locked",
                bout_id=None,
                evaluated_at=T1,
                availability="available",
            ),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE


def test_replaced_and_review_blocked_eligibility_rejected() -> None:
    for reason in ("replaced", "review_blocked", "unknown_availability"):
        row = compute_priced_value_metrics(
            PricedValueRequest(
                model_prob=0.55,
                target_context=_ctx(),
                valuation_cutoff=T0,
                product_eligible=True,
                quote_evidence=_quote(),
                eligibility_evidence=_elig(
                    eligible=False,
                    reason=reason,
                    bout_id=None,
                    availability="unknown",
                ),
            )
        )
        assert row.available is False
        assert row.reason is MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE


def test_tampered_decision_identity_rejected() -> None:
    with pytest.raises(IneligiblePriceError, match="decision_identity"):
        QuoteEligibilityEvidence(
            quote_id=1,
            eligible=True,
            selection_identity="moneyline:fighter_a",
            resolved_bout_id=BOUT_A,
            reason="none",
            evaluated_at=T0,
            quote_availability_at_decision="available",
            decision_identity="elig_v1:deadbeef",
        )


def test_cross_book_close_disallowed_by_default() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            quote_evidence=_quote(bookmaker_key="ref_book"),
            eligibility_evidence=_elig(eligible=True),
            closing_evidence=_close_provider(
                2.00,
                bookmaker_key="other_book",
                allow_cross_book=False,
            ),
        )
    )
    assert row.available is True
    assert row.probability_clv is None
    assert (
        row.probability_clv_reason
        is MetricsUnavailableReason.CROSS_BOOK_CLOSING_DISALLOWED
    )


def test_cross_book_close_allowed_when_policy_explicit() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T0,
            product_eligible=True,
            quote_evidence=_quote(bookmaker_key="ref_book"),
            eligibility_evidence=_elig(eligible=True),
            closing_evidence=_close_provider(
                2.00,
                bookmaker_key="other_book",
                allow_cross_book=True,
            ),
        )
    )
    assert row.available is True
    assert row.probability_clv is not None
    assert row.closing_provenance is not None
    assert row.closing_provenance.cross_book_closing is True
    assert row.closing_provenance.bookmaker_key == "other_book"


def test_opening_before_closing_required_for_clv() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            target_context=_ctx(),
            valuation_cutoff=T1,
            product_eligible=True,
            quote_evidence=_quote(at=T1, bout_id=BOUT_A),
            eligibility_evidence=_elig(eligible=True, evaluated_at=T1),
            closing_evidence=_close_provider(2.00, at=T0),
        )
    )
    assert row.available is True
    assert row.probability_clv is None
    assert row.probability_clv_reason is MetricsUnavailableReason.EVIDENCE_MISMATCH
