"""Event-block bootstrap grouping and reproducibility."""

from __future__ import annotations

from collections import Counter

import numpy as np

from mma_model.backtest.metrics import (
    DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    PricedBet,
    bootstrap_betting_intervals,
    event_blocks,
    resample_event_blocks,
)
from mma_model.markets.settlement import SettlementResult
from mma_model.value.ev import expected_value


def _bet(event_id: str, bout_id: str, profit_side: str) -> PricedBet:
    settlement = SettlementResult.WIN if profit_side == "a" else SettlementResult.LOSS
    return PricedBet(
        event_id=event_id,
        bout_id=bout_id,
        season=2017,
        series_variant="standard",
        market_family="moneyline",
        outcome_key="fighter_a",
        source_kind="provider_quote",
        provider="the_odds_api",
        bookmaker_key="ref_book",
        model_prob=0.6,
        offered_decimal=2.1,
        settlement=settlement,
        is_proxy_timestamp=False,
        is_pre_policy_candidate=True,
        probability_clv=0.02,
        closing_ev=0.1,
        expected_value=expected_value(0.6, 2.1),
    )


def test_same_event_fights_always_resample_together() -> None:
    bets = (
        _bet("card-a", "a1", "a"),
        _bet("card-a", "a2", "b"),
        _bet("card-b", "b1", "a"),
    )
    blocks = event_blocks(bets)
    assert set(blocks["card-a"]) == {bets[0], bets[1]}
    rng = np.random.default_rng(DEFAULT_BACKTEST_BOOTSTRAP_SEED)
    for _ in range(40):
        drawn = resample_event_blocks(blocks, rng=rng)
        counts = Counter(bet.bout_id for bet in drawn)
        assert counts["a1"] == counts["a2"]


def test_bootstrap_is_reproducible_with_pinned_seed() -> None:
    bets = (
        _bet("card-a", "a1", "a"),
        _bet("card-a", "a2", "b"),
        _bet("card-b", "b1", "a"),
        _bet("card-c", "c1", "b"),
    )
    first = bootstrap_betting_intervals(bets, seed=306001, replicates=40)
    second = bootstrap_betting_intervals(bets, seed=306001, replicates=40)
    assert first == second
    assert first["bootstrap_unit"] == "event_block"
    assert first["event_count"] == 3
    assert first["seed"] == 306001
    assert first["n_replicates"] == 40
    assert "n_rejected" in first
    third = bootstrap_betting_intervals(bets, seed=306002, replicates=40)
    assert third["intervals"] != first["intervals"]
    levels = [item["level"] for item in first["intervals"]["flat_1_unit_roi"]]
    assert levels == [0.9, 0.95]


def test_bootstrap_omits_missing_clv_instead_of_coercing_zero() -> None:
    with_clv = _bet("card-a", "a1", "a")
    missing = PricedBet(
        event_id="card-b",
        bout_id="b1",
        season=2017,
        series_variant="standard",
        market_family="moneyline",
        outcome_key="fighter_a",
        source_kind="provider_quote",
        provider="the_odds_api",
        bookmaker_key="ref_book",
        model_prob=0.6,
        offered_decimal=2.1,
        settlement=SettlementResult.WIN,
        is_proxy_timestamp=False,
        is_pre_policy_candidate=True,
        probability_clv=None,
        closing_ev=None,
        expected_value=expected_value(0.6, 2.1),
    )
    payload = bootstrap_betting_intervals(
        (with_clv, missing),
        seed=306001,
        replicates=30,
    )
    assert payload["clv_n_missing"] > 0
    assert payload["clv_n_replicates"] <= payload["n_replicates"]
    clv_intervals = payload["intervals"]["clv"]
    assert clv_intervals[0]["n_missing"] == payload["clv_n_missing"]
    assert all(band["n_replicates"] == payload["clv_n_replicates"] for band in clv_intervals)
