"""Same-card bouts share one pre-card state; first result cannot leak."""

from __future__ import annotations

from copy import deepcopy

from mma_model.backtest.engine import (
    CardScore,
    PrecomputedScorer,
    SnapshotWalkForwardScorer,
    run_walk_forward,
)
from mma_model.modeling.baselines import protocol_training_universe
from mma_model.modeling.splits import FoldMetadata, group_cards, outer_folds
from tests.backtest.helpers import CONTRACT, make_prediction, make_score, small_universe


def test_two_bout_card_uses_one_estimator_hash() -> None:
    cards = small_universe()
    scores = {
        "dev-2017": make_score(
            "dev-2017",
            (
                make_prediction("2017-a", "dev-2017", estimator_hash="shared"),
                make_prediction("2017-b", "dev-2017", p_a=0.41, estimator_hash="shared"),
            ),
            estimator_hash="shared",
        )
    }
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=PrecomputedScorer(scores),
        require_target_cards=False,
        bootstrap_replicates=8,
    )
    pair = [row for row in payload["attempts"] if row["event_id"] == "dev-2017"]
    assert len(pair) == 2
    hashes = {row["card_estimator_hash"] for row in pair}
    assert hashes == {"shared"}
    assert pair[0]["prediction"]["estimator_hash"] == pair[1]["prediction"]["estimator_hash"]


def test_protocol_same_card_predictions_share_fitted_state() -> None:
    cards, snapshot, _odds = protocol_training_universe()
    scorer = SnapshotWalkForwardScorer(
        snapshot=snapshot,
        eval_event_ids=frozenset(card.event_id for card in cards),
        contract=CONTRACT,
    )
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=scorer,
        require_target_cards=False,
        bootstrap_replicates=8,
    )
    pair = [
        row
        for row in payload["attempts"]
        if row["bout_id"] in {"2017-a", "2017-b"} and row["status"] == "predicted"
    ]
    assert len(pair) == 2
    assert pair[0]["prediction"]["estimator_hash"] == pair[1]["prediction"]["estimator_hash"]
    assert pair[0]["prediction"]["train_event_ids"] == pair[1]["prediction"]["train_event_ids"]
    assert "dev-2017" not in pair[0]["prediction"]["train_event_ids"]


def test_flipping_first_bout_result_does_not_change_second_prediction() -> None:
    cards, snapshot, _odds = protocol_training_universe()
    groups = group_cards(cards)
    group = next(item for item in groups if item.event_id == "dev-2017")
    fold = next(
        item for item in outer_folds(cards, require_target_cards=False).folds
        if item.test_event_id == "dev-2017"
    )
    scorer = SnapshotWalkForwardScorer(
        snapshot=deepcopy(snapshot),
        eval_event_ids=frozenset(card.event_id for card in cards),
        contract=CONTRACT,
    )
    first = scorer.score_card(group, fold)
    p_b_first = next(item.p_fighter_a for item in first.predictions if item.bout_id == "2017-b")

    mutated = deepcopy(snapshot)
    for row in mutated.result_versions:
        if row.bout_id == "2017-a":
            object.__setattr__(row, "winner_fighter_id", "n1")
    scorer2 = SnapshotWalkForwardScorer(
        snapshot=mutated,
        eval_event_ids=frozenset(card.event_id for card in cards),
        contract=CONTRACT,
    )
    second = scorer2.score_card(group, fold)
    p_b_second = next(item.p_fighter_a for item in second.predictions if item.bout_id == "2017-b")
    assert p_b_first == p_b_second


def test_score_card_receives_all_bout_ids_together() -> None:
    seen: list[tuple[str, ...]] = []

    class Recorder:
        def score_card(self, group, fold: FoldMetadata) -> CardScore:
            seen.append(group.bout_ids)
            preds = tuple(
                make_prediction(bout_id, group.event_id, estimator_hash="one")
                for bout_id in group.bout_ids
            )
            return make_score(group.event_id, preds, estimator_hash="one")

    run_walk_forward(
        contract=CONTRACT,
        cards=(small_universe()[0],),
        scorer=Recorder(),
        require_target_cards=False,
        bootstrap_replicates=4,
    )
    assert seen == [("2017-a", "2017-b")]
