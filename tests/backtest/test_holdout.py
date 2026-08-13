"""Locked 2025 is not accessed without --sealed-holdout and never trains."""

from __future__ import annotations

from mma_model.backtest.engine import PrecomputedScorer, ProtocolWalkForwardScorer, run_walk_forward
from mma_model.modeling.splits import protocol_fixture_cards
from tests.backtest.helpers import CONTRACT, scores_for_small_universe, small_universe


def test_holdout_locked_without_sealed_flag() -> None:
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=small_universe(),
        scorer=PrecomputedScorer(scores_for_small_universe(include_holdout=True)),
        sealed_holdout=False,
        require_target_cards=False,
        bootstrap_replicates=6,
    )
    hold = [row for row in payload["attempts"] if row["season"] == 2025]
    assert hold
    assert all(row["exclusion_reason"] == "locked_not_accessed" for row in hold)
    assert all(row["prediction"] is None for row in hold)
    assert payload["holdout"]["holdout_accessed"] is False
    assert payload["holdout"]["train_includes_2025"] is False
    selection = payload["metrics"]["all_dwcs"]["selection"]
    assert selection["locked_not_accessed"]["numerator"] == len(hold)
    assert selection["attempted"]["numerator"] == payload["n_attempts"]


def test_sealed_holdout_scores_2025_but_never_trains_on_it() -> None:
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=small_universe(),
        scorer=PrecomputedScorer(scores_for_small_universe(include_holdout=True)),
        sealed_holdout=True,
        require_target_cards=False,
        bootstrap_replicates=6,
    )
    hold = [row for row in payload["attempts"] if row["season"] == 2025]
    assert hold
    assert all(row["status"] == "predicted" for row in hold)
    assert payload["holdout"]["sealed_holdout"] is True
    assert payload["holdout"]["holdout_accessed"] is True
    assert payload["holdout"]["train_includes_2025"] is False
    for row in payload["attempts"]:
        pred = row.get("prediction")
        if pred is None:
            continue
        train_ids = pred["train_event_ids"]
        assert "hold-2025" not in train_ids
        assert all(item != "hold-2025" for item in train_ids)


def test_protocol_pipeline_never_trains_on_2025() -> None:
    cards = protocol_fixture_cards()
    scorer = ProtocolWalkForwardScorer(CONTRACT)
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        sealed_holdout=True,
        require_target_cards=False,
        bootstrap_replicates=8,
    )
    for row in payload["attempts"]:
        pred = row.get("prediction")
        if pred is None:
            continue
        assert "hold-2025" not in pred["train_event_ids"]
        assert all(item != "hold-2025" for item in pred["train_event_ids"])
    hold = [row for row in payload["attempts"] if row["bout_id"] == "2025-a"]
    assert len(hold) == 1
    assert hold[0]["status"] in {"predicted", "unavailable"}
    if hold[0]["status"] == "predicted":
        assert hold[0]["prediction"]["train_event_ids"]
        assert "hold-2025" not in hold[0]["prediction"]["train_event_ids"]
