"""Frozen recommendation policy packaging and contract mirroring (DWCS-307)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract
from mma_model.recommend.policy import (
    EXPECTED_POLICY_VERSION,
    PINNED_POLICY_HASH,
    PolicyContractDriftError,
    PolicyHashMismatch,
    PolicyValidationError,
    PolicyVersionMismatch,
    compute_policy_hash,
    load_recommendation_policy,
    package_policy_resource_path,
    visible_policy_path,
)


def test_visible_config_matches_packaged_bytes() -> None:
    packaged = package_policy_resource_path()
    visible = visible_policy_path()
    assert packaged.is_file()
    assert visible.is_file()
    assert packaged.read_bytes() == visible.read_bytes()
    assert EXPECTED_POLICY_VERSION == "1.0.0"


def test_pinned_digest_matches_packaged_payload() -> None:
    policy = load_recommendation_policy()
    assert policy.content_hash == PINNED_POLICY_HASH
    assert len(PINNED_POLICY_HASH) == 64
    assert policy.evaluation_contract_hash == PINNED_CONTRACT_HASH


def test_policy_mirrors_evaluation_contract() -> None:
    policy = load_recommendation_policy()
    contract = load_evaluation_contract()
    rec = contract.recommendation
    mirrors = policy.mirrors_evaluation_contract
    assert mirrors.max_confirmed_value_markets_per_matchup == 1
    assert mirrors.rank_confirmed_by is rec.rank_confirmed_by
    assert mirrors.actionable_ev_target == rec.actionable_ev_target
    assert mirrors.strong_value_ev_target == rec.strong_value_ev_target
    assert mirrors.confirmed_value_min_prob_ev_positive == rec.confirmed_value_min_prob_ev_positive
    assert mirrors.exact_round_min_prob_ev_positive == rec.exact_round_min_prob_ev_positive
    assert policy.family_is_qualified(policy.market_priority[0])


def test_content_hash_mismatch_fail_closed(tmp_path: Path) -> None:
    packaged = package_policy_resource_path()
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["description"] = "tampered"
    tampered = tmp_path / "policy.yaml"
    tampered.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PolicyHashMismatch):
        load_recommendation_policy(path=tampered)
    digest = compute_policy_hash(payload)
    assert digest != PINNED_POLICY_HASH


def test_version_mismatch_fail_closed(tmp_path: Path) -> None:
    packaged = package_policy_resource_path()
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["policy_version"] = "9.9.9"
    tampered = tmp_path / "policy.yaml"
    tampered.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(PolicyVersionMismatch):
        load_recommendation_policy(path=tampered, enforce_pinned_digest=False)


def test_contract_drift_fail_closed(tmp_path: Path) -> None:
    packaged = package_policy_resource_path()
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["mirrors_evaluation_contract"]["actionable_ev_target"] = 0.99
    tampered = tmp_path / "policy.yaml"
    tampered.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises((PolicyContractDriftError, PolicyValidationError)):
        load_recommendation_policy(path=tampered, enforce_pinned_digest=False)
