"""Outcome metrics and availability denominators reconcile."""

from __future__ import annotations

from datetime import UTC, datetime

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.modeling.metrics import binary_nll
from tests.backtest.helpers import (
    CONTRACT,
    decisive_facts,
    later_dev_card,
    make_prediction,
    make_quote,
    make_score,
    scores_for_small_universe,
    small_universe,
    two_bout_dev_card,
)


def test_outcome_and_availability_denominators_reconcile() -> None:
    cards = small_universe()
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=PrecomputedScorer(scores_for_small_universe()),
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
    selection = payload["metrics"]["all_dwcs"]["selection"]
    attempted = selection["attempted"]["denominator"]
    parts = (
        selection["predicted"]["numerator"]
        + selection["abstained"]["numerator"]
        + selection["unavailable"]["numerator"]
        + selection["excluded"]["numerator"]
    )
    # locked_not_accessed is a subset of excluded
    assert attempted == payload["n_attempts"]
    assert parts == attempted
    assert selection["locked_not_accessed"]["numerator"] == 1
    outcome = payload["metrics"]["all_dwcs"]["outcome"]
    assert outcome["n_predicted"] == selection["predicted"]["numerator"]
    assert outcome["market_log_loss"]["denominator"] == outcome["n_scored_decisive"]
    assert outcome["accuracy_descriptive_only"]["denominator"] == outcome["n_scored_decisive"]
    y = [1, 0, 1, 1, 0]
    p = [0.61, 0.58, 0.57, 0.66, 0.54]
    assert abs(outcome["market_log_loss"]["value"] - binary_nll(y, p)) < 1e-9
    brazil = payload["metrics"]["brazil"]["selection"]
    standard = payload["metrics"]["standard_only"]["selection"]
    assert (
        brazil["attempted"]["numerator"] + standard["attempted"]["numerator"]
        == selection["attempted"]["numerator"]
    )


def test_every_metric_has_numerator_and_denominator() -> None:
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
        quotes=(
            make_quote(
                "2017-a",
                observed_at=datetime(2017, 7, 11, 17, 30, tzinfo=UTC),
                quote_id=1,
            ),
        ),
        settlement_facts={"2017-a": decisive_facts("a")},
        require_target_cards=False,
        bootstrap_replicates=4,
    )
    betting = payload["metrics"]["all_dwcs"]["betting"]
    for key, row in betting.items():
        assert "numerator" in row, key
        assert "denominator" in row, key
        assert "scope" in row, key
        assert row["scope"] in {"priced_only", "threshold_only", "qualifying_priced"}
