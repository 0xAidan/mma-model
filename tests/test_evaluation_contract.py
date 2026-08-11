"""Tests for the frozen DWCS evaluation contract (DWCS-001)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mma_model.evaluation.contract import (
    CONTRACT_ID,
    EXPECTED_CONTRACT_VERSION,
    EXPECTED_SCHEMA_VERSION,
    PINNED_CONTRACT_HASH,
    BettingMetric,
    BoundComparison,
    ContractHashMismatch,
    ContractSchemaMismatch,
    ContractValidationError,
    ContractVersionMismatch,
    OutcomeMetric,
    RecommendationClass,
    actionable_decimal_price,
    compute_contract_hash,
    contract_path,
    fair_decimal_odds,
    holdout_ucb_delta_log_loss_passes,
    load_evaluation_contract,
    mutable_fact_allowed_at_cutoff,
    package_contract_resource_path,
    strong_value_decimal_price,
    visible_contract_path,
)

# Literal pinned digest from the committed contract bytes. Do not derive this in-test
# from the payload under assertion; update only with an intentional contract bump.
PINNED_DIGEST_LITERAL = "af0ad518a6417ac7d67e5f56fe836ab58afe55d8ac70813bf6045307ea6fb2cf"


@pytest.fixture
def contract():
    return load_evaluation_contract()


def test_pinned_digest_constant_matches_literal():
    assert PINNED_CONTRACT_HASH == PINNED_DIGEST_LITERAL
    assert len(PINNED_DIGEST_LITERAL) == 64


def test_visible_config_path_matches_package_bytes():
    visible = visible_contract_path()
    packaged = package_contract_resource_path()
    assert visible.exists()
    assert packaged.exists()
    assert visible.resolve() == packaged.resolve()
    assert visible.read_bytes() == packaged.read_bytes()


def test_valid_default_load_verifies_pinned_digest(contract):
    assert contract.schema_version == EXPECTED_SCHEMA_VERSION
    assert contract.contract_id == CONTRACT_ID
    assert contract.contract_version == EXPECTED_CONTRACT_VERSION
    assert contract.content_hash == PINNED_DIGEST_LITERAL


def test_default_load_hard_fails_when_canonical_bytes_tampered(monkeypatch: pytest.MonkeyPatch):
    """Tampering packaged bytes under the same version must hard-fail via pinned digest."""
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    payload["description"] = "tampered under same version"

    def _tampered_payload() -> dict:
        return payload

    monkeypatch.setattr(
        "mma_model.evaluation.contract._read_package_payload",
        _tampered_payload,
    )
    with pytest.raises(ContractHashMismatch):
        load_evaluation_contract()


def test_holdout_2025_is_locked(contract):
    assert contract.splits.holdout.seasons == (2025,)
    assert contract.splits.holdout.locked is True
    assert contract.splits.validation.seasons == (2024,)
    assert contract.splits.validation.locked is False
    assert contract.splits.development.seasons == (2017, 2018, 2019, 2020, 2021, 2022, 2023)
    assert contract.splits.grouping.value == "event_card"
    assert contract.splits.outer_fold.value == "rolling_origin_one_card_at_a_time"
    assert contract.splits.target_cards == 89


def test_universe_counts_and_brazil_sensitivity(contract):
    assert contract.universe.all_dwcs.cards == 89
    assert contract.universe.all_dwcs.bouts == 440
    assert contract.universe.standard_only.cards == 86
    assert contract.universe.standard_only.bouts == 425
    assert contract.universe.brazil.cards == 3
    assert contract.universe.brazil.bouts == 15
    assert contract.universe.brazil.series_variant == "dwcs_brazil"
    assert [u.value for u in contract.sensitivity.report_universes] == [
        "all_dwcs",
        "standard_only",
    ]


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
    assert (
        contract.metrics.priced_rows_require.value
        == "timestamped_observed_or_user_recorded_price"
    )
    assert set(contract.metrics.betting_priced_only) == set(BettingMetric)
    assert BettingMetric.FLAT_1_UNIT_ROI in contract.metrics.betting_priced_only
    assert BettingMetric.CLV in contract.metrics.betting_priced_only
    assert OutcomeMetric.JOINT_LOG_LOSS in contract.metrics.outcome
    assert OutcomeMetric.ECE in contract.metrics.outcome
    assert OutcomeMetric.ACCURACY_DESCRIPTIVE_ONLY in contract.metrics.outcome


def test_recommendation_thresholds_and_exact_round(contract):
    rec = contract.recommendation
    assert rec.actionable_ev_target == 0.05
    assert rec.strong_value_ev_target == 0.1
    assert rec.confirmed_value_min_prob_ev_positive == 0.7
    assert rec.exact_round_actionable_ev_target == 0.1
    assert rec.exact_round_min_prob_ev_positive == 0.75
    assert rec.max_confirmed_value_markets_per_matchup == 1
    assert rec.actionable_decimal_price == "max(1 / p25, 1.05 / p50)"
    assert rec.strong_value_decimal_price == "max(1 / p25, 1.10 / p50)"
    assert set(rec.classifications) == set(RecommendationClass)


def test_actionable_and_strong_value_price_formulas_match_contract(contract):
    p50 = 0.55
    p25 = 0.48
    rec = contract.recommendation
    assert fair_decimal_odds(p50) == pytest.approx(1.0 / p50)
    assert actionable_decimal_price(p50, p25, ev_target=rec.actionable_ev_target) == pytest.approx(
        max(1.0 / p25, 1.05 / p50)
    )
    assert strong_value_decimal_price(
        p50, p25, ev_target=rec.strong_value_ev_target
    ) == pytest.approx(max(1.0 / p25, 1.10 / p50))
    low_p25 = 0.30
    assert actionable_decimal_price(p50, low_p25) == pytest.approx(1.0 / low_p25)


def test_event_block_interval_levels_are_unambiguous(contract):
    ci = contract.confidence_intervals
    assert ci.bootstrap_refits == 200
    assert ci.bootstrap_unit.value == "event_block"
    assert ci.probability_and_ev.bootstrap_unit.value == "event_block"
    assert ci.probability_and_ev.interval_levels == (0.9, 0.95)
    assert ci.betting_metrics.bootstrap_unit.value == "event_block"
    assert ci.betting_metrics.interval_levels == (0.9, 0.95)
    assert ci.betting_metrics.note is not None
    assert "event-block" in ci.betting_metrics.note.lower()


def test_strict_ucb_bound_is_strictly_below(contract):
    gate = contract.go_live_gates.moneyline.holdout_2025_event_block_90pct_ucb_delta_log_loss
    assert gate.comparison is BoundComparison.LT
    assert gate.strict_upper_bound == 0.02
    assert holdout_ucb_delta_log_loss_passes(0.019999, contract) is True
    assert holdout_ucb_delta_log_loss_passes(0.02, contract) is False
    assert holdout_ucb_delta_log_loss_passes(0.021, contract) is False
    assert contract.go_live_gates.moneyline.ece_max == 0.08


def test_contract_field_assignment_raises_validation_error(contract):
    with pytest.raises(ValidationError):
        contract.contract_version = "9.9.9"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        contract.splits.holdout.locked = False  # type: ignore[misc]


def test_nested_sequence_mutation_raises(contract):
    seasons = contract.splits.development.seasons
    assert isinstance(seasons, tuple)
    with pytest.raises(AttributeError):
        seasons.append(2026)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        seasons[0] = 2099  # type: ignore[index]
    metrics = contract.metrics.betting_priced_only
    with pytest.raises(AttributeError):
        metrics.append(BettingMetric.CLV)  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        metrics[0] = BettingMetric.CLV  # type: ignore[index]


def test_normal_load_does_not_mutate_contract_file():
    path = contract_path()
    original = path.read_bytes()
    loaded = load_evaluation_contract()
    assert loaded.content_hash == PINNED_DIGEST_LITERAL
    assert path.read_bytes() == original


def test_schema_mismatch_hard_fails(tmp_path: Path):
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    path = tmp_path / "bad_schema.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractSchemaMismatch):
        load_evaluation_contract(path=path, enforce_pinned_digest=False)


def test_version_mismatch_hard_fails(tmp_path: Path):
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    payload["contract_version"] = "0.0.0-wrong"
    path = tmp_path / "bad_version.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractVersionMismatch):
        load_evaluation_contract(path=path, enforce_pinned_digest=False)


def test_path_load_with_same_version_but_altered_content_fails_pinned_digest(tmp_path: Path):
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    payload["description"] = "altered"
    path = tmp_path / "altered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractHashMismatch):
        load_evaluation_contract(path=path)


def test_invalid_payload_hard_fails(tmp_path: Path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": 1, "contract_id": CONTRACT_ID}), encoding="utf-8")
    with pytest.raises((ContractVersionMismatch, ContractValidationError)):
        load_evaluation_contract(path=path, enforce_pinned_digest=False)


def test_protocol_reject_unlocked_holdout(tmp_path: Path):
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    payload["splits"]["holdout"]["locked"] = False
    # Keep hash check off so protocol validation is the failure mode under test.
    path = tmp_path / "unlocked_holdout.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractValidationError):
        load_evaluation_contract(path=path, enforce_pinned_digest=False)


def test_compute_hash_helper_matches_pinned_literal_for_committed_file():
    payload = json.loads(visible_contract_path().read_text(encoding="utf-8"))
    # Compare helper output to the literal constant, not to a second in-test hash.
    assert compute_contract_hash(payload) == PINNED_DIGEST_LITERAL
