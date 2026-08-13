"""Priced-only ROI/CLV/Kelly/drawdown/losing-run with hand calculations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.backtest.metrics import (
    PricedBet,
    betting_metrics,
    flat_equity_path,
    kelly_bankroll_path,
    longest_losing_run,
)
from mma_model.markets.settlement import SettlementResult
from mma_model.value.ev import expected_value, flat_unit_profit, unsafe_same_line_probability_clv
from mma_model.value.kelly import quarter_kelly_fraction
from tests.backtest.helpers import (
    CONTRACT,
    decisive_facts,
    later_dev_card,
    make_prediction,
    make_quote,
    make_score,
    two_bout_dev_card,
)

START = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)
P = 0.62
P25 = 0.55
OFFERED = 2.20
CLOSE = 2.00


def test_hand_calculated_priced_metrics() -> None:
    quotes = (
        make_quote(
            "2017-a",
            observed_at=START - timedelta(minutes=90),
            price=OFFERED,
            close_price=CLOSE,
            quote_id=1,
        ),
        make_quote(
            "2017-b",
            observed_at=START - timedelta(minutes=90),
            price=OFFERED,
            close_price=CLOSE,
            quote_id=2,
        ),
    )
    scorer = PrecomputedScorer(
        {
            "dev-2017": make_score(
                "dev-2017",
                (
                    make_prediction("2017-a", "dev-2017", p_a=P, p25=P25, estimator_hash="e"),
                    make_prediction("2017-b", "dev-2017", p_a=P, p25=P25, estimator_hash="e"),
                ),
                estimator_hash="e",
            )
        }
    )
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=scorer,
        quotes=quotes,
        settlement_facts={"2017-a": decisive_facts("a"), "2017-b": decisive_facts("b")},
        require_target_cards=False,
        bootstrap_replicates=12,
    )
    a_row = next(
        item
        for row in payload["attempts"]
        if row["bout_id"] == "2017-a"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    b_row = next(
        item
        for row in payload["attempts"]
        if row["bout_id"] == "2017-b"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    assert a_row["expected_value"] == expected_value(P, OFFERED)
    assert a_row["flat_unit_profit"] == flat_unit_profit(
        settlement=SettlementResult.WIN, offered_decimal=OFFERED
    )
    assert b_row["flat_unit_profit"] == flat_unit_profit(
        settlement=SettlementResult.LOSS, offered_decimal=OFFERED
    )
    assert a_row["probability_clv"] == unsafe_same_line_probability_clv(
        bet_decimal=OFFERED, close_decimal=CLOSE
    )
    assert a_row["quarter_kelly_fraction"] == quarter_kelly_fraction(P, OFFERED)
    assert a_row["pre_policy_candidate"] is True
    betting = payload["metrics"]["all_dwcs"]["betting"]
    win = flat_unit_profit(settlement=SettlementResult.WIN, offered_decimal=OFFERED)
    loss = flat_unit_profit(settlement=SettlementResult.LOSS, offered_decimal=OFFERED)
    assert betting["flat_1_unit_roi"]["numerator"] == win + loss
    assert betting["flat_1_unit_roi"]["denominator"] == 2
    assert betting["flat_1_unit_roi"]["value"] == (win + loss) / 2.0
    assert betting["turnover"]["value"] == 2.0
    assert betting["n_threshold_only"]["scope"] == "threshold_only"


def test_kelly_drawdown_and_losing_run_match_hand_path() -> None:
    bets = (
        PricedBet(
            event_id="e1",
            bout_id="b1",
            season=2017,
            series_variant="standard",
            market_family="moneyline",
            outcome_key="fighter_a",
            source_kind="provider_quote",
            provider="the_odds_api",
            bookmaker_key="ref_book",
            model_prob=P,
            offered_decimal=OFFERED,
            settlement=SettlementResult.LOSS,
            is_proxy_timestamp=False,
            is_pre_policy_candidate=True,
            probability_clv=0.02,
            closing_ev=0.1,
            expected_value=expected_value(P, OFFERED),
        ),
        PricedBet(
            event_id="e1",
            bout_id="b2",
            season=2017,
            series_variant="standard",
            market_family="moneyline",
            outcome_key="fighter_a",
            source_kind="provider_quote",
            provider="the_odds_api",
            bookmaker_key="ref_book",
            model_prob=P,
            offered_decimal=OFFERED,
            settlement=SettlementResult.LOSS,
            is_proxy_timestamp=False,
            is_pre_policy_candidate=True,
            probability_clv=0.01,
            closing_ev=0.05,
            expected_value=expected_value(P, OFFERED),
        ),
        PricedBet(
            event_id="e2",
            bout_id="b3",
            season=2018,
            series_variant="standard",
            market_family="moneyline",
            outcome_key="fighter_a",
            source_kind="provider_quote",
            provider="the_odds_api",
            bookmaker_key="ref_book",
            model_prob=P,
            offered_decimal=OFFERED,
            settlement=SettlementResult.WIN,
            is_proxy_timestamp=False,
            is_pre_policy_candidate=True,
            probability_clv=0.03,
            closing_ev=0.2,
            expected_value=expected_value(P, OFFERED),
        ),
    )
    _path, kelly_dd = kelly_bankroll_path(bets)
    _flat, flat_dd = flat_equity_path(bets)
    totals = betting_metrics(bets, n_threshold_only=4)
    assert longest_losing_run(bets) == 2
    assert totals.longest_losing_run.value == 2
    assert totals.maximum_drawdown.value == kelly_dd
    assert flat_dd == 2.0
    assert totals.n_threshold_only.numerator == 4
    assert totals.n_threshold_only.scope == "threshold_only"
    assert totals.n_priced.scope == "priced_only"
    bankroll = 1.0
    for bet in bets:
        stake = quarter_kelly_fraction(bet.model_prob, bet.offered_decimal) * bankroll
        if bet.settlement is SettlementResult.WIN:
            bankroll += stake * (bet.offered_decimal - 1.0)
        else:
            bankroll -= stake
    assert abs(_path[-1] - bankroll) < 1e-12


def test_unpriced_denominator_stays_separate() -> None:
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=PrecomputedScorer(
            {
                "dev-2017": make_score(
                    "dev-2017",
                    (
                        make_prediction("2017-a", "dev-2017", estimator_hash="e"),
                        make_prediction("2017-b", "dev-2017", estimator_hash="e"),
                    ),
                    estimator_hash="e",
                )
            }
        ),
        quotes=(),
        settlement_facts={"2017-a": decisive_facts("a")},
        require_target_cards=False,
        bootstrap_replicates=4,
    )
    betting = payload["metrics"]["all_dwcs"]["betting"]
    selection = payload["metrics"]["all_dwcs"]["selection"]
    assert selection["priced"]["numerator"] == 0
    assert selection["threshold_only"]["numerator"] >= 1
    assert betting["n_priced"]["numerator"] == 0
    assert betting["flat_1_unit_roi"]["value"] is None
    assert betting["mean_probability_clv"]["value"] is None
