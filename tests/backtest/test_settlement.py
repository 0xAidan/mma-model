"""Event-night settlement, push/void, and current-result isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.markets.settlement import MarketSelection, SettlementResult, settle
from mma_model.value.ev import flat_unit_profit
from tests.backtest.helpers import (
    CONTRACT,
    cancelled_facts,
    decisive_facts,
    draw_facts,
    later_dev_card,
    make_prediction,
    make_quote,
    make_score,
    nc_facts,
    two_bout_dev_card,
)

START = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)


def _payload(facts, quotes=None):
    cards = (two_bout_dev_card(), later_dev_card())
    scorer = PrecomputedScorer(
        {
            "dev-2017": make_score(
                "dev-2017",
                (
                    make_prediction("2017-a", "dev-2017", estimator_hash="e"),
                    make_prediction("2017-b", "dev-2017", p_a=0.4, estimator_hash="e"),
                ),
                estimator_hash="e",
            )
        }
    )
    return run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        quotes=quotes or (),
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=6,
    )


def test_moneyline_uses_event_night_winner() -> None:
    quotes = (
        make_quote(
            "2017-a",
            observed_at=START - timedelta(minutes=90),
            price=2.20,
            quote_id=1,
        ),
    )
    payload = _payload({"2017-a": decisive_facts("a"), "2017-b": decisive_facts("b")}, quotes)
    priced = next(
        item
        for row in payload["attempts"]
        if row["bout_id"] == "2017-a"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    assert priced["settlement"] == SettlementResult.WIN.value
    assert priced["flat_unit_profit"] == flat_unit_profit(
        settlement=SettlementResult.WIN, offered_decimal=2.20
    )


def test_draw_and_void_profit_are_zero() -> None:
    quotes = (
        make_quote(
            "2017-a",
            observed_at=START - timedelta(minutes=90),
            price=2.20,
            quote_id=1,
        ),
        make_quote(
            "2017-b",
            observed_at=START - timedelta(minutes=90),
            price=2.20,
            quote_id=2,
        ),
    )
    payload = _payload({"2017-a": draw_facts(), "2017-b": nc_facts()}, quotes)
    profits = [
        item["flat_unit_profit"]
        for row in payload["attempts"]
        if row["event_id"] == "dev-2017"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    ]
    assert profits
    assert all(value == 0.0 for value in profits)
    for row in payload["attempts"]:
        if row["bout_id"] in {"2017-a", "2017-b"}:
            for item in row["priced_rows"]:
                if item["outcome_key"] == "fighter_a":
                    assert item["settlement"] in {
                        SettlementResult.VOID.value,
                        SettlementResult.PUSH.value,
                    }


def test_cancelled_bout_voids_moneyline() -> None:
    decision = settle(
        MarketSelection(family=MarketFamily.MONEYLINE, outcome=OutcomeKey.FIGHTER_A),
        cancelled_facts(),
    )
    assert decision.result is SettlementResult.VOID
    assert flat_unit_profit(settlement=decision.result, offered_decimal=1.90) == 0.0


def test_current_correction_is_not_used_for_grading() -> None:
    """Event-night A-win stays a win even if a later current NC exists only in comments."""
    quotes = (
        make_quote(
            "2017-a",
            observed_at=START - timedelta(minutes=90),
            price=2.00,
            quote_id=3,
        ),
    )
    payload = _payload({"2017-a": decisive_facts("a")}, quotes)
    priced = next(
        item
        for row in payload["attempts"]
        if row["bout_id"] == "2017-a"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    assert priced["settlement"] == SettlementResult.WIN.value
    nc_payload = _payload({"2017-a": nc_facts()}, quotes)
    nc_priced = next(
        item
        for row in nc_payload["attempts"]
        if row["bout_id"] == "2017-a"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    assert nc_priced["settlement"] == SettlementResult.VOID.value
    assert priced["flat_unit_profit"] != nc_priced["flat_unit_profit"]
