"""Tests for the frozen DWCS evaluation contract (DWCS-001)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mma_model.evaluation.contract import (
    CONTRACT_ID,
    EXPECTED_CONTRACT_VERSION,
    EXPECTED_SCHEMA_VERSION,
    ContractHashMismatch,
    ContractSchemaMismatch,
    ContractValidationError,
    ContractVersionMismatch,
    actionable_decimal_price,
    compute_contract_hash,
    contract_path,
    fair_decimal_odds,
    load_evaluation_contract,
    mutable_fact_allowed_at_cutoff,
    strong_value_decimal_price,
)


@pytest.fixture
def contract():
    return load_evaluation_contract()


def test_contract_file_exists_at_canonical_path():
    path = contract_path()
    assert path.is_file()
    assert path.name == "dwcs_v1.json"
    assert path.parent.name == "evaluation"


def test_valid_load_returns_expected_identity(contract):
    assert contract.schema_version == EXPECTED_SCHEMA_VERSION
    assert contract.contract_id == CONTRACT_ID
    assert contract.contract_version == EXPECTED_CONTRACT_VERSION
    assert len(contract.content_hash) == 64


def test_holdout_2025_is_locked(contract):
    assert contract.splits.holdout.seasons == [2025]
    assert contract.splits.holdout.locked is True
    assert contract.splits.validation.seasons == [2024]
    assert contract.splits.validation.locked is False
    assert contract.splits.development.seasons == [2017, 2018, 2019, 2020, 2021, 2022, 2023]
    assert contract.splits.grouping == "event_card"
    assert contract.splits.outer_fold == "rolling_origin_one_card_at_a_time"
    assert contract.splits.target_cards == 89


def test_universe_counts_and_brazil_sensitivity(contract):
    assert contract.universe.all_dwcs.cards == 89
    assert contract.universe.all_dwcs.bouts == 440
    assert contract.universe.standard_only.cards == 86
    assert contract.universe.standard_only.bouts == 425
    assert contract.universe.brazil.cards == 3
    assert contract.universe.brazil.bouts == 15
    assert contract.universe.brazil.series_variant == "dwcs_brazil"
    assert contract.sensitivity.report_universes == ["all_dwcs", "standard_only"]


def test_prediction_cutoff_and_card_identity(contract):
    pit = contract.point_in_time
    assert pit.prediction_cutoff_minutes_before_scheduled_start == 60
    assert pit.identical_cutoff_per_card is True
    assert pit.mutable_fact_rules.effective_at_strictly_before_cutoff is True
    assert pit.mutable_fact_rules.observed_at_at_or_before_cutoff is True
    assert pit.forbid_same_card_results is True
    assert pit.forbid_post_cutoff_odds_snapshots is True


def test_mutable_fact_point_in_time_invariants():
    cutoff = datetime(2025, 8, 12, 1, 0, 0)
    assert mutable_fact_allowed_at_cutoff(
        effective_at=cutoff - timedelta(minutes=1),
        observed_at=cutoff,
        cutoff=cutoff,
    )
    assert not mutable_fact_allowed_at_cutoff(
        effective_at=cutoff,
        observed_at=cutoff,
        cutoff=cutoff,
    )
    assert not mutable_fact_allowed_at_cutoff(
        effective_at=cutoff - timedelta(minutes=1),
        observed_at=cutoff + timedelta(seconds=1),
        cutoff=cutoff,
    )


def test_price_policy_and_priced_only_betting_metrics(contract):
    assert contract.price_policy.bookmaker_odds_optional_enrichment is True
    assert contract.price_policy.missing_bet365_does_not_block_core_guidance is True
    assert contract.price_policy.exact_ev_roi_clv_require_timestamped_price is True
    assert (
        contract.price_policy.price_target_only_rows_never_receive_synthetic_betting_performance
        is True
    )
    assert contract.metrics.price_target_rows_never_receive_synthetic_betting_performance is True
    assert contract.metrics.priced_rows_require == "timestamped_observed_or_user_recorded_price"
    assert "flat_1_unit_roi" in contract.metrics.betting_priced_only
    assert "clv" in contract.metrics.betting_priced_only
    assert "joint_log_loss" in contract.metrics.outcome
    assert "accuracy_descriptive_only" in contract.metrics.outcome


def test_recommendation_thresholds(contract):
    rec = contract.recommendation
    assert rec.actionable_ev_target == 0.05
    assert rec.strong_value_ev_target == 0.1
    assert rec.confirmed_value_min_prob_ev_positive == 0.7
    assert rec.exact_round_actionable_ev_target == 0.1
    assert rec.exact_round_min_prob_ev_positive == 0.75
    assert rec.max_confirmed_value_markets_per_matchup == 1
    assert rec.actionable_decimal_price == "max(1 / p25, 1.05 / p50)"
    assert rec.strong_value_decimal_price == "max(1 / p25, 1.10 / p50)"
    assert set(rec.classifications) == {"confirmed_value", "price_target", "no_bet"}


def test_actionable_and_strong_value_price_formulas_match_contract():
    p50 = 0.55
    p25 = 0.48
    assert fair_decimal_odds(p50) == pytest.approx(1.0 / p50)
    assert actionable_decimal_price(p50, p25) == pytest.approx(max(1.0 / p25, 1.05 / p50))
    assert strong_value_decimal_price(p50, p25) == pytest.approx(max(1.0 / p25, 1.10 / p50))
    # When p25 is much lower, conservative break-even dominates.
    low_p25 = 0.30
    assert actionable_decimal_price(p50, low_p25) == pytest.approx(1.0 / low_p25)


def test_confidence_intervals_contract(contract):
    assert contract.confidence_intervals.bootstrap_refits == 200
    assert contract.confidence_intervals.bootstrap_unit == "event_block"
    assert contract.confidence_intervals.levels == [0.9, 0.95]


def test_contract_is_immutable(contract):
    with pytest.raises(Exception):
        contract.contract_version = "9.9.9"  # type: ignore[misc]
    with pytest.raises(Exception):
        contract.splits.holdout.locked = False  # type: ignore[misc]


def test_normal_load_does_not_mutate_contract_file(tmp_path: Path):
    src = contract_path()
    copy = tmp_path / "dwcs_v1.json"
    original = src.read_bytes()
    copy.write_bytes(original)
    loaded = load_evaluation_contract(path=copy)
    assert loaded.contract_version == EXPECTED_CONTRACT_VERSION
    assert copy.read_bytes() == original


def test_schema_mismatch_hard_fails(tmp_path: Path):
    payload = json.loads(contract_path().read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    path = tmp_path / "bad_schema.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractSchemaMismatch):
        load_evaluation_contract(path=path)


def test_version_mismatch_hard_fails(tmp_path: Path):
    payload = json.loads(contract_path().read_text(encoding="utf-8"))
    payload["contract_version"] = "0.0.0-wrong"
    path = tmp_path / "bad_version.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractVersionMismatch):
        load_evaluation_contract(path=path)


def test_hash_mismatch_on_tampering_hard_fails(tmp_path: Path):
    payload = json.loads(contract_path().read_text(encoding="utf-8"))
    good_hash = compute_contract_hash(payload)
    payload["description"] = "tampered"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractHashMismatch):
        load_evaluation_contract(path=path, expected_hash=good_hash)


def test_invalid_payload_hard_fails(tmp_path: Path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": 1, "contract_id": CONTRACT_ID}), encoding="utf-8")
    with pytest.raises((ContractVersionMismatch, ContractValidationError)):
        load_evaluation_contract(path=path)


def test_content_hash_is_stable_for_canonical_payload():
    payload = json.loads(contract_path().read_text(encoding="utf-8"))
    assert compute_contract_hash(payload) == compute_contract_hash(payload)
    loaded = load_evaluation_contract(expected_hash=compute_contract_hash(payload))
    assert loaded.content_hash == compute_contract_hash(payload)
