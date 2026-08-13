"""Appending future events/quotes/corrections must not mutate prior card hashes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from tests.backtest.helpers import (
    CONTRACT,
    brazil_card,
    decisive_facts,
    later_dev_card,
    make_prediction,
    make_quote,
    make_score,
    two_bout_dev_card,
    val_card,
)


def _scores() -> dict:
    return {
        "dev-2017": make_score(
            "dev-2017",
            (
                make_prediction("2017-a", "dev-2017", estimator_hash="e2017"),
                make_prediction("2017-b", "dev-2017", p_a=0.4, estimator_hash="e2017"),
            ),
            estimator_hash="e2017",
        ),
        "brazil-2018": make_score(
            "brazil-2018",
            (make_prediction("br-a", "brazil-2018", estimator_hash="e2018"),),
            estimator_hash="e2018",
        ),
        "dev-2023": make_score(
            "dev-2023",
            (make_prediction("2023-a", "dev-2023", estimator_hash="e2023"),),
            estimator_hash="e2023",
        ),
        "val-2024": make_score(
            "val-2024",
            (make_prediction("2024-a", "val-2024", estimator_hash="e2024"),),
            estimator_hash="e2024",
        ),
    }


def test_appending_future_card_leaves_prior_card_hashes_identical() -> None:
    early = (two_bout_dev_card(), brazil_card(), later_dev_card())
    full = early + (val_card(),)
    scorer = PrecomputedScorer(_scores())
    facts = {
        "2017-a": decisive_facts("a"),
        "2017-b": decisive_facts("b"),
        "br-a": decisive_facts("a"),
        "2023-a": decisive_facts("a"),
        "2024-a": decisive_facts("b"),
    }
    first = run_walk_forward(
        contract=CONTRACT,
        cards=early,
        scorer=scorer,
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=8,
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = run_walk_forward(
        contract=CONTRACT,
        cards=full,
        scorer=scorer,
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=8,
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for event_id in ("dev-2017", "brazil-2018", "dev-2023"):
        assert first["card_output_hashes"][event_id] == second["card_output_hashes"][event_id]
    assert first["hashes"]["data"] != second["hashes"]["data"]
    assert first["content_hash"] != second["content_hash"]


def test_appending_future_quote_does_not_change_prior_priced_rows() -> None:
    cards = (two_bout_dev_card(), later_dev_card())
    start_2017 = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)
    quotes_v1 = (
        make_quote(
            "2017-a",
            observed_at=start_2017 - timedelta(minutes=90),
            quote_id=1,
        ),
    )
    quotes_v2 = quotes_v1 + (
        make_quote(
            "2023-a",
            observed_at=datetime(2023, 8, 22, 0, 30, tzinfo=UTC),
            quote_id=2,
        ),
    )
    scorer = PrecomputedScorer(_scores())
    facts = {
        "2017-a": decisive_facts("a"),
        "2017-b": decisive_facts("b"),
        "2023-a": decisive_facts("a"),
    }
    first = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        quotes=quotes_v1,
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=6,
    )
    second = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        quotes=quotes_v2,
        settlement_facts=facts,
        require_target_cards=False,
        bootstrap_replicates=6,
    )
    assert first["card_output_hashes"]["dev-2017"] == second["card_output_hashes"]["dev-2017"]
    assert first["card_output_hashes"]["dev-2023"] != second["card_output_hashes"]["dev-2023"]
