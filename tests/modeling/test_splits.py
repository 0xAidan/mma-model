"""Event-grouped splits, holdout lock, and frozen evaluator (DWCS-302)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mma_model.backtest.contract import (
    PINNED_FEATURE_SPEC_HASH,
    PINNED_SPLITS_CONFIG_HASH,
    EvaluatorHashMismatchError,
    HashKind,
    verify_evaluator_hashes,
)
from mma_model.cli import main
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract
from mma_model.modeling.splits import (
    FoldRole,
    HoldoutLockedError,
    cards_from_manifest,
    inspect_folds,
    outer_folds,
    protocol_fixture_cards,
    sealed_holdout_folds,
    sensitivity_membership,
    tuning_folds,
    validation_folds,
    verify_fold_plan,
)
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK


@pytest.fixture
def cards():
    return protocol_fixture_cards()


@pytest.fixture
def contract():
    return load_evaluation_contract()


def _fold_by_event(plan, event_id: str):
    matches = [fold for fold in plan.folds if fold.test_event_id == event_id]
    assert len(matches) == 1
    return matches[0]


def test_same_event_bouts_excluded_from_training(cards) -> None:
    plan = outer_folds(cards)
    fold = _fold_by_event(plan, "dev-2017")
    assert fold.test_bout_ids == ("2017-a", "2017-b")
    assert "dev-2017" not in fold.train_event_ids
    later = _fold_by_event(plan, "brazil-2018")
    assert "dev-2017" in later.train_event_ids
    assert set(later.test_bout_ids).isdisjoint({"2017-a", "2017-b"})


def test_train_timestamps_strictly_before_test_cutoff(cards) -> None:
    plan = outer_folds(cards, allow_holdout=True)
    starts = {
        card.event_id: card.scheduled_start_at
        for card in cards
        if card.scheduled_start_at is not None
    }
    for fold in plan.folds:
        if fold.max_train_timestamp is not None:
            assert fold.max_train_timestamp < fold.cutoff
        for event_id in fold.train_event_ids:
            assert starts[event_id] < fold.cutoff
            assert event_id != fold.test_event_id


def test_holdout_cannot_be_selected_for_ordinary_tuning(cards) -> None:
    tuned = tuning_folds(cards)
    assert all(fold.role is FoldRole.DEVELOPMENT for fold in tuned.folds)
    assert all(fold.test_event_id != "hold-2025" for fold in tuned.folds)
    validated = validation_folds(cards)
    assert all(fold.role is FoldRole.VALIDATION for fold in validated.folds)
    assert all(fold.test_event_id != "hold-2025" for fold in validated.folds)
    default_outer = outer_folds(cards)
    assert all(fold.role is not FoldRole.HOLDOUT for fold in default_outer.folds)
    with pytest.raises(HoldoutLockedError, match="locked"):
        sealed_holdout_folds(cards, allow_holdout=False)
    sealed = sealed_holdout_folds(cards, allow_holdout=True)
    assert all(fold.role is FoldRole.HOLDOUT for fold in sealed.folds)
    assert all(fold.locked for fold in sealed.folds)
    assert {fold.test_event_id for fold in sealed.folds} == {"hold-2025"}


def test_brazil_sensitivity_membership_is_explicit(cards) -> None:
    in_all, in_standard = sensitivity_membership(SeriesVariant.BRAZIL)
    assert in_all is True
    assert in_standard is False
    std_all, std_only = sensitivity_membership(SeriesVariant.STANDARD)
    assert std_all is True
    assert std_only is True
    plan = outer_folds(cards)
    brazil = _fold_by_event(plan, "brazil-2018")
    assert brazil.series_variant is SeriesVariant.BRAZIL
    assert brazil.in_all_dwcs is True
    assert brazil.in_standard_only is False
    standard = _fold_by_event(plan, "dev-2017")
    assert standard.series_variant is SeriesVariant.STANDARD
    assert standard.in_all_dwcs is True
    assert standard.in_standard_only is True


def test_hash_mismatch_hard_fails(cards, contract) -> None:
    plan = outer_folds(cards, contract=contract)
    assert plan.contract_hash == PINNED_CONTRACT_HASH
    assert plan.feature_spec_hash == PINNED_FEATURE_SPEC_HASH
    assert plan.config_hash == PINNED_SPLITS_CONFIG_HASH
    verify_fold_plan(plan, cards, contract)

    with pytest.raises(EvaluatorHashMismatchError) as contract_exc:
        verify_fold_plan(replace(plan, contract_hash="0" * 64), cards, contract)
    assert contract_exc.value.kind is HashKind.CONTRACT

    with pytest.raises(EvaluatorHashMismatchError) as spec_exc:
        verify_fold_plan(replace(plan, feature_spec_hash="1" * 64), cards, contract)
    assert spec_exc.value.kind is HashKind.FEATURE_SPEC

    with pytest.raises(EvaluatorHashMismatchError) as data_exc:
        verify_fold_plan(replace(plan, data_hash="2" * 64), cards, contract)
    assert data_exc.value.kind is HashKind.DATA

    with pytest.raises(EvaluatorHashMismatchError) as config_exc:
        verify_fold_plan(replace(plan, config_hash="3" * 64), cards, contract)
    assert config_exc.value.kind is HashKind.CONFIG

    with pytest.raises(EvaluatorHashMismatchError):
        verify_evaluator_hashes(
            contract_hash=plan.contract_hash,
            feature_spec_hash=plan.feature_spec_hash,
            data_hash="deadbeef",
            config_hash=plan.config_hash,
            expected_data_hash=plan.data_hash,
            expected_config_hash=plan.config_hash,
        )


def test_inner_folds_never_include_2024_or_2025_labels(cards) -> None:
    inner = tuning_folds(cards)
    forbidden = {"val-2024", "hold-2025"}
    assert {fold.test_event_id for fold in inner.folds} == {
        "dev-2017",
        "brazil-2018",
        "dev-2023",
    }
    for fold in inner.folds:
        assert fold.role is FoldRole.DEVELOPMENT
        assert fold.test_event_id not in forbidden
        assert forbidden.isdisjoint(fold.train_event_ids)
        assert fold.cutoff.year <= 2023


def test_rolling_origin_later_development_trains_on_earlier(cards) -> None:
    plan = outer_folds(cards)
    first = _fold_by_event(plan, "dev-2017")
    brazil = _fold_by_event(plan, "brazil-2018")
    later = _fold_by_event(plan, "dev-2023")
    assert first.train_event_ids == ()
    assert first.max_train_timestamp is None
    assert brazil.train_event_ids == ("dev-2017",)
    assert later.train_event_ids == ("dev-2017", "brazil-2018")
    validation = _fold_by_event(plan, "val-2024")
    assert "dev-2023" in validation.train_event_ids
    assert "dev-2017" in validation.train_event_ids


def test_manifest_targets_89_cards(contract) -> None:
    cards = cards_from_manifest()
    plan = outer_folds(cards, allow_holdout=True, contract=contract)
    assert len(plan.folds) == contract.splits.target_cards
    assert len(plan.folds) == 89
    brazil = [fold for fold in plan.folds if fold.series_variant is SeriesVariant.BRAZIL]
    assert len(brazil) == contract.universe.brazil.cards
    assert all(fold.in_all_dwcs and not fold.in_standard_only for fold in brazil)
    holdout = [fold for fold in plan.folds if fold.role is FoldRole.HOLDOUT]
    assert holdout
    assert all(fold.locked for fold in holdout)
    default = outer_folds(cards, allow_holdout=False, contract=contract)
    assert all(fold.role is not FoldRole.HOLDOUT for fold in default.folds)
    assert len(default.folds) == 89 - len(holdout)


def test_inspect_folds_cli_refuses_live_db(capsys) -> None:
    code = main(
        [
            "evaluation",
            "inspect-folds",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert code == EXIT_INTERNAL
    assert "refusing" in capsys.readouterr().out


def test_inspect_folds_cli_default_omits_holdout(capsys) -> None:
    code = main(
        [
            "evaluation",
            "inspect-folds",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--json",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    roles = {fold["role"] for fold in payload["folds"]}
    assert "holdout" not in roles
    assert "development" in roles
    assert "validation" in roles
    assert payload["include_holdout"] is False
    ids = {fold["test_event_id"] for fold in payload["folds"]}
    assert "hold-2025" not in ids
    assert "dev-2017" in ids


def test_inspect_folds_cli_include_holdout_labels_locked(capsys) -> None:
    code = main(
        [
            "evaluation",
            "inspect-folds",
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--include-holdout",
            "--json",
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    holdout = [fold for fold in payload["folds"] if fold["role"] == "holdout"]
    assert holdout
    assert all(fold["locked"] is True for fold in holdout)
    assert all(fold["test_event_id"] == "hold-2025" for fold in holdout)


def test_inspect_folds_cli_contract_hash_mismatch_exits(tmp_path: Path, capsys) -> None:
    source = Path("src/mma_model/evaluation/dwcs_v1.json")
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["description"] = "tampered contract bytes"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    code = main(["evaluation", "inspect-folds", "--contract", str(path)])
    assert code == EXIT_INTERNAL
    out = capsys.readouterr().out.lower()
    assert "hash mismatch" in out or "contract error" in out


def test_inspect_folds_api_uses_contract_path(cards) -> None:
    plan = inspect_folds(
        contract_path=Path("config/evaluation/dwcs_v1.json"),
        cards=cards,
    )
    assert all(fold.role is not FoldRole.HOLDOUT for fold in plan.folds)
    assert plan.contract_hash == PINNED_CONTRACT_HASH


def test_identical_cutoff_shared_across_card_bouts(cards) -> None:
    plan = outer_folds(cards)
    fold = _fold_by_event(plan, "dev-2017")
    expected = datetime(2017, 7, 11, 18, 0, tzinfo=UTC)
    assert fold.cutoff == expected
    assert fold.test_bout_ids == ("2017-a", "2017-b")
