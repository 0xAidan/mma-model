"""Standard/Brazil/year/market/source breakdowns reconcile to the top level."""

from __future__ import annotations

from datetime import UTC, datetime

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.backtest.metrics import AttemptRow, PricedBet, assert_breakdowns_reconcile
from mma_model.markets.settlement import SettlementResult
from tests.backtest.helpers import (
    CONTRACT,
    decisive_facts,
    make_quote,
    scores_for_small_universe,
    small_universe,
)


def test_universe_year_market_source_slices_reconcile() -> None:
    start_2017 = datetime(2017, 7, 11, 17, 30, tzinfo=UTC)
    start_2018 = datetime(2018, 8, 10, 23, 30, tzinfo=UTC)
    quotes = (
        make_quote("2017-a", observed_at=start_2017, quote_id=1, provider="the_odds_api"),
        make_quote(
            "br-a",
            observed_at=start_2018,
            quote_id=2,
            provider="manual",
            source_kind="user_observed",
        ),
    )
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=small_universe(),
        scorer=PrecomputedScorer(scores_for_small_universe()),
        quotes=quotes,
        settlement_facts={
            "2017-a": decisive_facts("a"),
            "2017-b": decisive_facts("b"),
            "br-a": decisive_facts("a"),
            "2023-a": decisive_facts("a"),
            "2024-a": decisive_facts("b"),
        },
        require_target_cards=False,
        bootstrap_replicates=8,
    )
    all_n = payload["metrics"]["all_dwcs"]["selection"]["attempted"]["numerator"]
    std_n = payload["metrics"]["standard_only"]["selection"]["attempted"]["numerator"]
    br_n = payload["metrics"]["brazil"]["selection"]["attempted"]["numerator"]
    assert std_n + br_n == all_n
    years = payload["breakdowns"]["years"]
    year_sum = sum(int(block["n_attempts"]) for block in years.values())
    assert year_sum == all_n
    assert payload["universe"]["brazil_bouts"] == br_n
    sources = payload["breakdowns"]["sources"]
    assert sources
    market_n = payload["breakdowns"]["markets"]["moneyline"]["betting"]["n_priced"]["numerator"]
    assert market_n == payload["metrics"]["all_dwcs"]["betting"]["n_priced"]["numerator"]


def test_assert_breakdowns_reconcile_helper() -> None:
    attempts = (
        AttemptRow(
            event_id="e",
            bout_id="b1",
            season=2017,
            series_variant="standard",
            status="predicted",
            exclusion_reason=None,
            predicted=True,
            abstained=False,
            unavailable=False,
            excluded=False,
            locked_not_accessed=False,
            priced=True,
            threshold_only=False,
            pre_policy_candidate=True,
            markets_available=("moneyline",),
            markets_unavailable=(),
            n_priced_selections=1,
            n_threshold_selections=0,
            priced_market_families=("moneyline",),
            threshold_market_families=(),
        ),
        AttemptRow(
            event_id="e2",
            bout_id="b2",
            season=2018,
            series_variant="brazil",
            status="predicted",
            exclusion_reason=None,
            predicted=True,
            abstained=False,
            unavailable=False,
            excluded=False,
            locked_not_accessed=False,
            priced=False,
            threshold_only=True,
            pre_policy_candidate=False,
            markets_available=("moneyline",),
            markets_unavailable=(),
            n_priced_selections=0,
            n_threshold_selections=1,
            priced_market_families=(),
            threshold_market_families=("moneyline",),
        ),
    )
    bets = (
        PricedBet(
            event_id="e",
            bout_id="b1",
            season=2017,
            series_variant="standard",
            market_family="moneyline",
            outcome_key="fighter_a",
            source_kind="provider_quote",
            provider="x",
            bookmaker_key="y",
            model_prob=0.6,
            offered_decimal=2.0,
            settlement=SettlementResult.WIN,
            is_proxy_timestamp=False,
            is_pre_policy_candidate=True,
            probability_clv=0.01,
            closing_ev=0.1,
            expected_value=0.2,
        ),
    )
    assert_breakdowns_reconcile(attempts=attempts, bets=bets)
