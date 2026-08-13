"""Threshold, EV, CLV, profit, staking, and priced-metric tests (DWCS-204)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mma_model.domain.markets import MarketFamily
from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import (
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
    ManualObservedPriceEvidence,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    SelectionPriceObservation,
)
from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    MAX_BANKROLL_CAP_FRACTION,
    fractional_kelly,
    quarter_kelly_fraction,
)
from mma_model.value.portfolio import stake_amount
from mma_model.value.priced import (
    MetricsUnavailableReason,
    PricedValueRequest,
    compute_priced_value_metrics,
)
from mma_model.value.thresholds import compute_value_price_thresholds

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=2)


def _manual(price: float = 2.20, *, at: datetime = T0) -> ManualObservedPriceEvidence:
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
        bookmaker_key="manual_book",
        region="us",
    )


def _quote(
    *,
    quote_id: int = 1,
    price: float = 2.20,
    bout_id: str | None = "bout-1",
) -> ProviderQuoteEvidence:
    return ProviderQuoteEvidence(
        quote_id=quote_id,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        selection_identity="moneyline:fighter_a",
        price_decimal=price,
        availability="available",
        observed_at=T0,
        bout_id=bout_id,
        bookmaker_key="ref_book",
        region="us",
    )


def _elig(
    *,
    quote_id: int = 1,
    eligible: bool = True,
    reason: str = "none",
    bout_id: str | None = "bout-1",
) -> QuoteEligibilityEvidence:
    return QuoteEligibilityEvidence(
        quote_id=quote_id,
        eligible=eligible,
        selection_identity="moneyline:fighter_a",
        resolved_bout_id=bout_id,
        reason=reason,
    )


def _close(price: float = 2.00, *, at: datetime = T1) -> SelectionPriceObservation:
    return SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        market_family="moneyline",
        outcome_key="fighter_a",
        line_point=None,
        selection_identity="moneyline:fighter_a",
        price_decimal=price,
        observed_at=at,
        lifecycle_or_availability="available",
    )


def test_thresholds_match_pinned_contract_and_exact_round_override() -> None:
    ml = compute_value_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    assert ml.fair_decimal == pytest.approx(2.0)
    assert ml.break_even_decimal == pytest.approx(2.5)
    assert ml.actionable_decimal == pytest.approx(2.5)
    assert ml.strong_value_decimal == pytest.approx(2.5)
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


def test_same_selection_clv_and_mismatch_rejection() -> None:
    opening = _close(2.20, at=T0)
    closing = _close(2.00, at=T1)
    clv = same_selection_probability_clv(opening=opening, closing=closing)
    assert clv == pytest.approx(unsafe_same_line_probability_clv(
        bet_decimal=2.20, close_decimal=2.00
    ))
    assert clv > 0
    bad_close = SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        market_family="moneyline",
        outcome_key="fighter_b",
        line_point=None,
        selection_identity="moneyline:fighter_b",
        price_decimal=2.00,
        observed_at=T1,
        lifecycle_or_availability="available",
    )
    with pytest.raises(SelectionMismatchError):
        same_selection_probability_clv(opening=opening, closing=bad_close)
    early_close = _close(2.00, at=T0 - timedelta(minutes=1))
    with pytest.raises(SelectionMismatchError):
        same_selection_probability_clv(opening=opening, closing=early_close)


def test_push_void_profit_zero_and_win_loss() -> None:
    assert flat_unit_profit(settlement=SettlementResult.PUSH, offered_decimal=2.0) == 0.0
    assert flat_unit_profit(settlement=SettlementResult.VOID, offered_decimal=2.0) == 0.0
    assert flat_unit_profit(settlement=SettlementResult.WIN, offered_decimal=2.5) == pytest.approx(
        1.5
    )
    assert flat_unit_profit(settlement=SettlementResult.LOSS, offered_decimal=2.5) == -1.0


def test_quarter_kelly_hard_cap_and_rejects_over_cap() -> None:
    stake = quarter_kelly_fraction(0.90, 3.0)
    assert stake <= MAX_BANKROLL_CAP_FRACTION
    with pytest.raises(InvalidOddsError):
        quarter_kelly_fraction(0.90, 3.0, cap=0.05)
    with pytest.raises(InvalidOddsError):
        fractional_kelly(0.55, -110, fraction=1.5, cap=0.01)
    assert stake_amount(stake_fraction=0.5, bankroll=1000.0) == pytest.approx(10.0)


def test_unpriced_cannot_produce_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(model_prob=0.55, product_eligible=True)
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.UNPRICED_TARGET
    assert row.expected_value is None
    assert row.probability_clv is None
    assert row.flat_unit_profit is None
    assert row.stake_fraction is None
    with pytest.raises(UnpricedMetricsError):
        row.require_available()


def test_booleans_alone_cannot_fabricate_provider_eligibility() -> None:
    # No quote/eligibility evidence → unpriced even if product_eligible.
    row = compute_priced_value_metrics(
        PricedValueRequest(model_prob=0.55, product_eligible=True)
    )
    assert row.available is False


def test_match_gate_alone_insufficient_for_provider_quote() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            product_eligible=True,
            quote_evidence=_quote(),
            eligibility_evidence=_elig(
                eligible=False,
                reason="unmatched",
                bout_id=None,
            ),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.MATCH_GATE_ONLY


def test_eligible_provider_quote_emits_priced_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            product_eligible=True,
            quote_evidence=_quote(price=2.20),
            eligibility_evidence=_elig(eligible=True),
            closing_observation=_close(2.00),
            settlement=SettlementResult.PUSH,
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.55 * 2.20 - 1.0)
    assert row.closing_ev == pytest.approx(0.55 * 2.00 - 1.0)
    assert row.probability_clv is not None and row.probability_clv > 0
    assert row.probability_clv_unit == CLV_UNIT
    assert row.flat_unit_profit == 0.0
    assert row.stake_fraction is not None
    assert row.stake_fraction <= DEFAULT_BANKROLL_CAP_FRACTION


def test_user_observed_priced_path() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.50,
            product_eligible=True,
            manual_evidence=_manual(2.20),
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.10)
    assert row.closing_ev is None
    assert row.closing_ev_reason is MetricsUnavailableReason.MISSING_CLOSING_PRICE
    assert row.probability_clv is None
    assert row.probability_clv_reason is MetricsUnavailableReason.MISSING_CLOSING_PRICE


def test_unresolved_preserves_ev_clv_stake_suppresses_profit_only() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            product_eligible=True,
            manual_evidence=_manual(2.20),
            closing_observation=_close(2.00),
            settlement=SettlementResult.UNRESOLVED,
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.55 * 2.20 - 1.0)
    assert row.probability_clv is not None
    assert row.closing_ev is not None
    assert row.stake_fraction is not None
    assert row.flat_unit_profit is None
    assert row.flat_unit_profit_reason is MetricsUnavailableReason.UNRESOLVED_SETTLEMENT


def test_quote_eligibility_identity_mismatch_blocks() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            product_eligible=True,
            quote_evidence=_quote(quote_id=1),
            eligibility_evidence=_elig(quote_id=2, eligible=True),
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.EVIDENCE_MISMATCH
