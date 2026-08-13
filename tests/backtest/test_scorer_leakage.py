"""Walk-forward label cutoff, sealed 2025, and real-scorer future invariance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mma_model.backtest.engine import run_walk_forward
from mma_model.backtest.gates import HoldoutTrainError, assert_holdout_not_in_train
from mma_model.backtest.walk_forward_scorer import SnapshotWalkForwardScorer
from mma_model.features.snapshot import SnapshotBout, SnapshotEvent, SnapshotResultVersion
from mma_model.labels.outcomes import WinnerSide
from mma_model.modeling.baselines import protocol_training_universe
from mma_model.modeling.splits import group_cards, protocol_fixture_cards
from tests.backtest.helpers import CONTRACT, make_card


def test_ufc_310_season_metadata_blocks_holdout_train() -> None:
    with pytest.raises(HoldoutTrainError):
        assert_holdout_not_in_train(
            ("ufc-310",),
            event_seasons={"ufc-310": 2025},
        )


def test_training_label_uses_post_adjudication_correction_after_cutoff() -> None:
    cards, snapshot, _odds = protocol_training_universe()
    bout = snapshot.bout_by_id("2017-a")
    assert bout is not None
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id="2017-a",
            version_kind="current",
            revision=2,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            winner_fighter_id=bout.fighter_b_id,
            result_type="decisive",
            method="U-DEC",
            ending_round=3,
            time_str="5:00",
            effective_at=datetime(2019, 1, 15, tzinfo=UTC),
            observed_at=datetime(2019, 1, 15, tzinfo=UTC),
        )
    )
    scorer = SnapshotWalkForwardScorer(
        snapshot=snapshot,
        eval_event_ids=frozenset(card.event_id for card in cards),
        contract=CONTRACT,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
    )
    groups = group_cards(cards, CONTRACT)
    later = next(group for group in groups if group.event_id == "dev-2023")
    samples = scorer._train_samples(later)
    corrected = next(sample for sample in samples if sample.sample_id == "2017-a")
    assert corrected.binary_winner is WinnerSide.B
    early = next(group for group in groups if group.event_id == "brazil-2018")
    early_samples = scorer._train_samples(early)
    if any(sample.sample_id == "2017-a" for sample in early_samples):
        night = next(sample for sample in early_samples if sample.sample_id == "2017-a")
        assert night.binary_winner is WinnerSide.A


def test_two_sealed_2025_cards_do_not_use_first_outcome() -> None:
    cards, snapshot, _odds = protocol_training_universe()
    start = datetime(2025, 12, 6, 2, 0, tzinfo=UTC)
    snapshot.events.append(
        SnapshotEvent(
            event_id="ufc-310",
            scheduled_start_at=start,
            event_date=start.date(),
            series="dwcs",
            name="ufc-310",
        )
    )
    snapshot.bouts.append(
        SnapshotBout(
            bout_id="ufc-310-a",
            event_id="ufc-310",
            fighter_a_id="v1",
            fighter_b_id="v2",
            scheduled_rounds=3,
            status="completed",
        )
    )
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id="ufc-310-a",
            version_kind="event_night",
            revision=1,
            fighter_a_id="v1",
            fighter_b_id="v2",
            winner_fighter_id="v2",
            result_type="decisive",
            method="U-DEC",
            ending_round=3,
            time_str="5:00",
            effective_at=start,
            observed_at=start,
        )
    )
    extra = make_card("ufc-310", start, ("ufc-310-a",))
    all_cards = tuple(cards) + (extra,)
    scorer = SnapshotWalkForwardScorer(
        snapshot=snapshot,
        eval_event_ids=frozenset(card.event_id for card in all_cards),
        contract=CONTRACT,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
    )
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=all_cards,
        scorer=scorer,
        sealed_holdout=True,
        require_target_cards=False,
        bootstrap_replicates=4,
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    hold = [row for row in payload["attempts"] if row["season"] == 2025]
    assert len(hold) == 2
    ids = {row["event_id"] for row in hold}
    assert "ufc-310" in ids
    assert "hold-2025" in ids
    for row in hold:
        pred = row.get("prediction")
        if pred is None:
            continue
        assert "ufc-310" not in pred["train_event_ids"]
        assert "hold-2025" not in pred["train_event_ids"]
    first = next(row for row in hold if row["event_id"] == "hold-2025")
    second = next(row for row in hold if row["event_id"] == "ufc-310")
    if first.get("prediction") and second.get("prediction"):
        assert first["prediction"]["calibrator_hash"] == second["prediction"]["calibrator_hash"]


def test_real_scorer_future_event_does_not_change_prior_card_hashes() -> None:
    cards, snapshot, _odds = protocol_training_universe()
    early = tuple(card for card in cards if card.scheduled_start_at.year < 2024)
    stamp = datetime(2026, 2, 1, tzinfo=UTC)
    first = run_walk_forward(
        contract=CONTRACT,
        cards=early,
        scorer=SnapshotWalkForwardScorer(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in early),
            contract=CONTRACT,
            bootstrap_replicates=4,
            bootstrap_seed=306001,
        ),
        require_target_cards=False,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
        generated_at=stamp,
    )
    future_start = datetime(2023, 12, 1, 2, 0, tzinfo=UTC)
    snapshot.events.append(
        SnapshotEvent(
            event_id="future-2023b",
            scheduled_start_at=future_start,
            event_date=future_start.date(),
            series="dwcs",
            name="future-2023b",
        )
    )
    snapshot.bouts.append(
        SnapshotBout(
            bout_id="future-a",
            event_id="future-2023b",
            fighter_a_id="v1",
            fighter_b_id="n1",
            scheduled_rounds=3,
            status="completed",
        )
    )
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id="future-a",
            version_kind="event_night",
            revision=1,
            fighter_a_id="v1",
            fighter_b_id="n1",
            winner_fighter_id="v1",
            result_type="decisive",
            method="KO/TKO",
            ending_round=1,
            time_str="1:00",
            effective_at=future_start,
            observed_at=future_start,
        )
    )
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id="2017-a",
            version_kind="current",
            revision=9,
            fighter_a_id="v1",
            fighter_b_id="n1",
            winner_fighter_id="n1",
            result_type="decisive",
            method="U-DEC",
            ending_round=3,
            time_str="5:00",
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            observed_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    future_card = make_card("future-2023b", future_start, ("future-a",))
    full = early + (future_card,)
    second = run_walk_forward(
        contract=CONTRACT,
        cards=full,
        scorer=SnapshotWalkForwardScorer(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in full),
            contract=CONTRACT,
            bootstrap_replicates=4,
            bootstrap_seed=306001,
        ),
        require_target_cards=False,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
        generated_at=stamp,
    )
    for event_id in ("dev-2017", "brazil-2018"):
        assert first["card_output_hashes"][event_id] == second["card_output_hashes"][event_id]


def test_same_generated_at_is_byte_identical() -> None:
    cards = protocol_fixture_cards()[:3]
    _all_cards, snapshot, _odds = protocol_training_universe()
    stamp = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    first = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=SnapshotWalkForwardScorer(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in cards),
            contract=CONTRACT,
            bootstrap_replicates=4,
            bootstrap_seed=306001,
        ),
        require_target_cards=False,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
        generated_at=stamp,
    )
    second = run_walk_forward(
        contract=CONTRACT,
        cards=cards,
        scorer=SnapshotWalkForwardScorer(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in cards),
            contract=CONTRACT,
            bootstrap_replicates=4,
            bootstrap_seed=306001,
        ),
        require_target_cards=False,
        bootstrap_replicates=4,
        bootstrap_seed=306001,
        generated_at=stamp,
    )
    assert first["content_hash"] == second["content_hash"]
    assert first["card_output_hashes"] == second["card_output_hashes"]
