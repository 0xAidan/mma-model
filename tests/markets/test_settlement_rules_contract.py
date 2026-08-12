"""Settlement rules contract packaging, digest, and immutability (DWCS-200)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mma_model.markets.rules import (
    EXPECTED_CONTRACT_VERSION,
    PINNED_SETTLEMENT_HASH,
    SettlementRulesHashMismatch,
    compute_settlement_hash,
    default_settlement_rules,
    load_settlement_rules,
    package_settlement_resource_path,
    visible_settlement_path,
)


def test_visible_config_matches_packaged_bytes() -> None:
    packaged = package_settlement_resource_path()
    visible = visible_settlement_path()
    assert packaged.is_file()
    assert visible.is_file()
    assert packaged.read_bytes() == visible.read_bytes()
    assert EXPECTED_CONTRACT_VERSION == "1.3.0"


def test_pinned_digest_matches_packaged_payload() -> None:
    contract = load_settlement_rules()
    assert contract.content_hash == PINNED_SETTLEMENT_HASH
    assert len(PINNED_SETTLEMENT_HASH) == 64


def test_content_hash_mismatch_fail_closed(tmp_path: Path) -> None:
    packaged = package_settlement_resource_path()
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    # Silent semantic drift under the same version string.
    payload["rule_sets"]["mma_generic"]["moneyline"]["draw"] = "push"
    drifted = tmp_path / "settlement_drift.yaml"
    drifted.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    assert compute_settlement_hash(payload) != PINNED_SETTLEMENT_HASH
    with pytest.raises(SettlementRulesHashMismatch, match="content hash mismatch"):
        load_settlement_rules(drifted)


def test_rule_sets_mapping_is_immutable() -> None:
    contract = default_settlement_rules()
    with pytest.raises(TypeError):
        contract.rule_sets["mma_generic"] = contract.rule_sets["mma_generic"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del contract.rule_sets["mma_generic"]  # type: ignore[attr-defined]


def test_nested_rule_models_are_frozen() -> None:
    contract = default_settlement_rules()
    generic = contract.rule_sets["mma_generic"]
    with pytest.raises((ValidationError, TypeError, ValueError)):
        generic.moneyline.draw = "void"  # type: ignore[misc]
    with pytest.raises((ValidationError, TypeError, ValueError)):
        generic.totals.round_seconds = 1  # type: ignore[misc]
