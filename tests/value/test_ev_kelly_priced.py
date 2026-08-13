"""Threshold, EV, CLV, profit, and staking tests (DWCS-204)."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily
from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import UnpricedMetricsError
from mma_model.value.ev import (
    closing_ev,
    expected_value,
    flat_unit_profit,
    same_line_probability_clv,
)
from mma_model.value.kelly import DEFAULT_BANKROLL_CAP_FRACTION, quarter_kelly_fraction
from mma_model.value.portfolio import stake_amount
from mma_model.value.priced import (
    MetricsUnavailableReason,
    PricedValueRequest,
    PriceSourceKind,
    compute_priced_value_metrics,
)
from mma_model.value.thresholds import compute_value_price_thresholds


def test_thresholds_match_pinned_contract_and_exact_round_override() -> None:
    ml = compute_value_price_thresholds(0.50, 0.40, family=MarketFamily.MONEYLINE)
    assert ml.fair_decimal == pytest.approx(2.0)
    assert ml.break_even_decimal == pytest.approx(2.5)
    assert ml.actionable_decimal == pytest.approx(2.5)
    assert ml.strong_value_decimal == pytest.approx(2.5)
    assert ml.actionable_ev_target == pytest.approx(0.05)
    assert ml.actionable_american == pytest.approx(150.0)

    exact = compute_value_price_thresholds(0.20, 0.18, family=MarketFamily.EXACT_ROUND)
    assert exact.actionable_ev_target == pytest.approx(0.10)
    assert exact.actionable_decimal == pytest.approx(1.0 / 0.18)


def test_threshold_ordering_fair_le_actionable_le_strong() -> None:
    # Choose p25 close to p50 so EV targets dominate ordering.
    t = compute_value_price_thresholds(0.40, 0.39, family=MarketFamily.MONEYLINE)
    assert t.fair_decimal <= t.actionable_decimal <= t.strong_value_decimal


def test_zero_edge_ev_and_positive_edge() -> None:
    assert expected_value(0.5, 2.0) == pytest.approx(0.0)
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)
    assert closing_ev(0.55, 1.90) == pytest.approx(0.55 * 1.90 - 1.0)


def test_same_line_probability_clv_sign() -> None:
    # Bet at 2.20, close at 2.00 → beat the close (lower implied at bet).
    clv = same_line_probability_clv(bet_decimal=2.20, close_decimal=2.00)
    assert clv == pytest.approx(0.5 - (1.0 / 2.20))
    assert clv > 0


def test_push_void_profit_zero_and_win_loss() -> None:
    assert flat_unit_profit(settlement=SettlementResult.PUSH, offered_decimal=2.0) == 0.0
    assert flat_unit_profit(settlement=SettlementResult.VOID, offered_decimal=2.0) == 0.0
    assert flat_unit_profit(settlement=SettlementResult.WIN, offered_decimal=2.5) == pytest.approx(
        1.5
    )
    assert flat_unit_profit(settlement=SettlementResult.LOSS, offered_decimal=2.5) == -1.0


def test_quarter_kelly_capped_at_one_percent() -> None:
    # Huge edge would exceed 1% without the cap.
    stake = quarter_kelly_fraction(0.90, 3.0)
    assert stake <= DEFAULT_BANKROLL_CAP_FRACTION
    assert stake_amount(stake_fraction=0.5, bankroll=1000.0) == pytest.approx(10.0)


def test_unpriced_cannot_produce_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            source_kind=PriceSourceKind.UNPRICED,
            product_eligible=True,
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.UNPRICED_TARGET
    assert row.expected_value is None
    assert row.probability_clv is None
    assert row.flat_unit_profit is None
    assert row.stake_fraction is None
    with pytest.raises(UnpricedMetricsError):
        row.require_available()


def test_match_gate_alone_insufficient_for_provider_quote() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            source_kind=PriceSourceKind.PROVIDER_QUOTE,
            offered_decimal=2.10,
            has_timestamped_price=True,
            product_eligible=True,
            match_gate_ok=True,
            quote_eligible=False,
        )
    )
    assert row.available is False
    assert row.reason is MetricsUnavailableReason.MATCH_GATE_ONLY


def test_eligible_provider_quote_emits_priced_metrics() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.55,
            source_kind=PriceSourceKind.PROVIDER_QUOTE,
            offered_decimal=2.20,
            has_timestamped_price=True,
            product_eligible=True,
            quote_eligible=True,
            match_gate_ok=True,
            closing_decimal=2.00,
            settlement=SettlementResult.PUSH,
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.55 * 2.20 - 1.0)
    assert row.closing_ev == pytest.approx(0.55 * 2.00 - 1.0)
    assert row.probability_clv is not None and row.probability_clv > 0
    assert row.flat_unit_profit == 0.0
    assert row.stake_fraction is not None
    assert row.stake_fraction <= DEFAULT_BANKROLL_CAP_FRACTION


def test_user_observed_priced_path_without_quote_eligibility() -> None:
    row = compute_priced_value_metrics(
        PricedValueRequest(
            model_prob=0.50,
            source_kind=PriceSourceKind.USER_OBSERVED,
            offered_decimal=2.20,
            has_timestamped_price=True,
            product_eligible=True,
        )
    )
    assert row.available is True
    assert row.expected_value == pytest.approx(0.10)
