"""Post-cutoff, stale, replacement, and ambiguous odds are excluded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.backtest.gates import THRESHOLD_SCOPE
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
CUTOFF = START - timedelta(minutes=60)


def _run(quotes):
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
    facts = {"2017-a": decisive_facts("a"), "2017-b": decisive_facts("b")}
    return run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        quotes=quotes,
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=6,
    )


def _bout(payload, bout_id: str):
    return next(row for row in payload["attempts"] if row["bout_id"] == bout_id)


def test_post_cutoff_quote_excluded() -> None:
    quotes = (
        make_quote("2017-a", observed_at=CUTOFF + timedelta(minutes=1), quote_id=1),
    )
    payload = _run(quotes)
    bout = _bout(payload, "2017-a")
    assert bout["priced_rows"] == []
    reasons = {row.get("quote_exclusion") for row in bout["threshold_only_rows"]}
    assert "post_cutoff_odds" in reasons
    assert all(row.get("expected_value") is None for row in bout["threshold_only_rows"])


def test_stale_and_replaced_quotes_excluded() -> None:
    quotes = (
        make_quote(
            "2017-a",
            observed_at=CUTOFF - timedelta(minutes=10),
            lifecycle="stale",
            quote_id=1,
        ),
        make_quote(
            "2017-b",
            observed_at=CUTOFF - timedelta(minutes=10),
            is_replacement=True,
            quote_id=2,
        ),
    )
    payload = _run(quotes)
    a = _bout(payload, "2017-a")
    b = _bout(payload, "2017-b")
    assert "stale_odds" in {row.get("quote_exclusion") for row in a["threshold_only_rows"]}
    assert "replaced_odds" in {row.get("quote_exclusion") for row in b["threshold_only_rows"]}
    assert a["priced_rows"] == []
    assert b["priced_rows"] == []


def test_ambiguous_same_timestamp_prices_excluded() -> None:
    quotes = (
        make_quote("2017-a", observed_at=CUTOFF - timedelta(minutes=10), price=2.10, quote_id=1),
        make_quote("2017-a", observed_at=CUTOFF - timedelta(minutes=10), price=1.80, quote_id=2),
    )
    payload = _run(quotes)
    bout = _bout(payload, "2017-a")
    assert "ambiguous_odds" in {row.get("quote_exclusion") for row in bout["threshold_only_rows"]}
    assert bout["priced_rows"] == []


def test_later_post_cutoff_quote_does_not_poison_earlier_valid_quote() -> None:
    quotes = (
        make_quote(
            "2017-a",
            observed_at=CUTOFF - timedelta(minutes=10),
            price=2.10,
            quote_id=1,
        ),
        make_quote(
            "2017-a",
            observed_at=CUTOFF + timedelta(minutes=5),
            price=9.99,
            quote_id=2,
        ),
    )
    payload = _run(quotes)
    bout = _bout(payload, "2017-a")
    priced = [row for row in bout["priced_rows"] if row["outcome_key"] == "fighter_a"]
    assert priced
    assert priced[0]["offered_decimal"] == 2.10
    assert priced[0]["later_ignored"] >= 1


def test_quote_observed_exactly_at_cutoff_is_eligible() -> None:
    quotes = (
        make_quote("2017-a", observed_at=CUTOFF, price=2.05, quote_id=3),
    )
    payload = _run(quotes)
    bout = _bout(payload, "2017-a")
    priced = [row for row in bout["priced_rows"] if row["outcome_key"] == "fighter_a"]
    assert priced
    assert priced[0]["offered_decimal"] == 2.05


def test_threshold_only_rows_have_no_synthetic_betting_fields() -> None:
    payload = _run(())
    bout = _bout(payload, "2017-a")
    assert bout["priced_rows"] == []
    for row in bout["threshold_only_rows"]:
        assert row["scope"] == THRESHOLD_SCOPE
        assert row["expected_value"] is None
        assert row["flat_unit_profit"] is None
        assert row["probability_clv"] is None
        assert row["quarter_kelly_fraction"] is None
        assert row["stake_fraction"] is None
        assert row["realized_roi"] is None
        assert row["turnover"] is None
    betting = payload["metrics"]["all_dwcs"]["betting"]
    assert betting["n_threshold_only"]["numerator"] >= 1
    assert betting["flat_1_unit_roi"]["value"] is None
