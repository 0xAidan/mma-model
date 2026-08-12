"""Tests for public-first source policy contract loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mma_model.sources.policy import (
    CANONICAL_SOURCE_IDS,
    REQUIRED_QUALITY_TIER_IDS,
    REQUIRED_TIMESTAMP_QUALITY_IDS,
    SourcePolicyError,
    UnknownSourcePolicyError,
    load_source_policy,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "config" / "sources" / "source_policy_v1.json"


def _valid_policy_dict() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "source_policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_source_policy_public_first_mode() -> None:
    policy = load_source_policy(POLICY_PATH)
    assert policy.policy_mode == "public_first_hybrid_personal_project"
    assert policy.licensed_audit_status.decision_primary is None
    assert policy.licensed_audit_status.licensed_hard_blocker is True
    assert policy.gates_retained.dwcs_universe_cards == 89
    assert policy.gates_retained.dwcs_universe_bouts == 440
    assert policy.gates_retained.cross_source_reconciliation_min_where_comparable == 0.98
    assert policy.gates_retained.result_agreement_min == 0.99
    assert policy.gates_retained.future_row_leakage_failures_max == 0
    assert policy.gates_retained.mutable_current_as_historical_feature_failures_max == 0
    assert policy.identity_rules.same_name_auto_merge is False
    assert policy.deterministic_fallback_order[0] == "ufcstats_public"
    assert "opaque_precomputed_feature_csvs_as_training_inputs" in policy.roles[
        "ufcstats_public"
    ].forbidden


def test_canonical_source_ids_are_exhaustive_and_consistent() -> None:
    policy = load_source_policy()
    assert tuple(policy.source_ids) == CANONICAL_SOURCE_IDS
    assert set(policy.roles.keys()) == set(CANONICAL_SOURCE_IDS)
    assert set(policy.kill_criteria.keys()).issubset(set(CANONICAL_SOURCE_IDS))
    assert set(policy.deterministic_fallback_order).issubset(set(CANONICAL_SOURCE_IDS))
    for source_id in policy.deterministic_fallback_order:
        assert source_id in policy.roles
    for source_id in policy.kill_criteria:
        assert source_id in policy.roles
    for required_id in (
        "ufcstats_public",
        "mma_ai_bootstrap",
        "tapology_public",
        "sherdog_public",
        "combat_registry",
        "wikidata",
        "bestfightodds_archive",
        "the_odds_api",
        "sportsdataio",
        "balldontlie",
        "explicit_missing",
    ):
        assert required_id in policy.source_ids
        assert required_id in policy.roles


def test_no_alias_source_ids_in_committed_policy() -> None:
    blob = POLICY_PATH.read_text(encoding="utf-8")
    for alias in (
        "ufcstats_direct",
        "ufcstats_direct_snapshots",
        "mma_ai_raw_normalized_bootstrap_after_reconciliation",
        "tapology_public_regional",
        "sherdog_selective_secondary",
        "combat_registry_and_commission_overrides",
        "combat_registry_public",
        "sportsdataio_current_key",
        "sportsdataio_validation_only",
        "balldontlie_validation_only",
        "explicit_missing_with_quality_tier",
    ):
        assert alias not in blob


def test_observation_metadata_names_are_required() -> None:
    policy = load_source_policy()
    meta = policy.observation_metadata
    assert meta.required_timestamp_fields == (
        "observed_at",
        "source_published_at",
        "source_updated_at",
        "effective_at",
        "proxy_published_at",
    )
    assert meta.required_quality_fields == (
        "timestamp_quality",
        "timestamp_quality_source",
        "quality_tier",
    )
    assert meta.required_raw_fields == ("payload_hash", "raw_ref")
    assert set(meta.timestamp_quality_values) == set(REQUIRED_TIMESTAMP_QUALITY_IDS)
    assert set(meta.quality_tier_values) == set(REQUIRED_QUALITY_TIER_IDS)
    assert set(policy.quality_tiers.keys()) == set(REQUIRED_QUALITY_TIER_IDS)


def test_dwcs_102_persistence_requirements_are_documented() -> None:
    policy = load_source_policy()
    req = policy.dwcs_102_persistence
    assert req.migration_id == "0006_observation_pit_metadata"
    assert "raw_observations" in req.table_columns
    cols = req.table_columns["raw_observations"]
    for name in (
        "observed_at",
        "source_published_at",
        "source_updated_at",
        "effective_at",
        "proxy_published_at",
        "timestamp_quality",
        "timestamp_quality_source",
        "quality_tier",
        "attributes_json",
        "payload_hash",
        "raw_ref",
    ):
        assert name in cols
    assert "source_published_at" in req.source_observation_record_fields
    assert "proxy_published_at" in req.source_observation_record_fields
    assert "round_trip_silver_vs_gold_quality_tier" in req.required_tests
    assert req.implement_in_this_pr is False


def test_unknown_policy_mode_hard_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["policy_mode"] = "licensed_only"
    with pytest.raises(UnknownSourcePolicyError):
        load_source_policy(_write_policy(tmp_path, payload))


def test_unknown_source_id_in_fallback_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["deterministic_fallback_order"] = list(payload["deterministic_fallback_order"]) + [
        "ufcstats_direct_snapshots"
    ]
    with pytest.raises(SourcePolicyError, match="unknown source id"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_missing_required_role_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    del payload["roles"]["wikidata"]
    with pytest.raises(SourcePolicyError, match="roles"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_kill_criteria_unknown_source_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["kill_criteria"]["not_a_real_source"] = ["x"]
    with pytest.raises(SourcePolicyError, match="kill_criteria"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_quality_tier_typo_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["quality_tiers"]["sliver"] = payload["quality_tiers"].pop("silver")
    with pytest.raises(SourcePolicyError, match="quality_tier"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_timestamp_quality_missing_value_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["observation_metadata"]["timestamp_quality_values"] = [
        v
        for v in payload["observation_metadata"]["timestamp_quality_values"]
        if v != "publication_proxy"
    ]
    with pytest.raises(SourcePolicyError, match="timestamp_quality"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_nested_pit_clock_typo_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["pit_timestamps"]["separate_clocks"] = [
        "acquisition_time",
        "source_publication_or_update_time",
        "fact_effective_time",
        "documented_proxxy_time",
    ]
    with pytest.raises(SourcePolicyError, match="separate_clocks"):
        load_source_policy(_write_policy(tmp_path, payload))


def test_malformed_gate_threshold_fails(tmp_path: Path) -> None:
    payload = _valid_policy_dict()
    payload["gates_retained"]["result_agreement_min"] = "high"
    with pytest.raises((SourcePolicyError, ValidationError, TypeError, ValueError)):
        load_source_policy(_write_policy(tmp_path, payload))


def test_loaded_policy_is_deeply_immutable() -> None:
    policy = load_source_policy()
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        policy.policy_mode = "licensed_only"  # type: ignore[misc]
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        policy.gates_retained.dwcs_universe_bouts = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.deterministic_fallback_order[0] = "balldontlie"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        policy.source_ids.append("extra")  # type: ignore[attr-defined]
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        policy.roles["ufcstats_public"].forbidden = ("x",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.quality_tiers["gold"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        policy.kill_criteria["ufcstats_public"] = ("mutated",)  # type: ignore[index]
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        policy.observation_metadata.required_timestamp_fields = ("observed_at",)  # type: ignore[misc]
    with pytest.raises((TypeError, ValidationError, AttributeError)):
        policy.pit_timestamps.separate_clocks = ("acquisition_time",)  # type: ignore[misc]


def test_roles_mapping_is_not_mutable_dict() -> None:
    policy = load_source_policy()
    assert not isinstance(policy.roles, dict)
    with pytest.raises(TypeError):
        policy.roles["ufcstats_public"] = policy.roles["balldontlie"]  # type: ignore[index]


def test_design_plan_and_policy_paths_exist() -> None:
    assert (
        ROOT / "docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md"
    ).is_file()
    assert (
        ROOT / "docs/superpowers/plans/2026-08-12-public-first-mma-history.md"
    ).is_file()
    assert (ROOT / "docs/research/phase1-public-first-roadmap.md").is_file()
    policy = load_source_policy()
    assert (ROOT / policy.design_spec).is_file()
    assert (ROOT / policy.implementation_plan).is_file()


def test_spec_and_plan_document_four_clock_persistence_gap() -> None:
    spec = (
        ROOT / "docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md"
    ).read_text(encoding="utf-8")
    plan = (
        ROOT / "docs/superpowers/plans/2026-08-12-public-first-mma-history.md"
    ).read_text(encoding="utf-8")
    for text in (spec, plan):
        assert "proxy_published_at" in text
        assert "source_published_at" in text
        assert "timestamp_quality" in text
        assert "attributes_json" in text
        assert "0006_observation_pit_metadata" in text
        assert "round-trip" in text.lower() or "round_trip" in text


def test_plan_task1_does_not_recreate_policy_loader() -> None:
    plan = (
        ROOT / "docs/superpowers/plans/2026-08-12-public-first-mma-history.md"
    ).read_text(encoding="utf-8")
    assert "Create: `src/mma_model/sources/policy.py`" not in plan
    assert "ImportError` for `mma_model.sources.policy`" not in plan
    assert "ModuleNotFoundError` or `ImportError` for `mma_model.sources.policy`" not in plan
    assert "already merged" in plan.lower() or "already present" in plan.lower()
    assert "pit_proxy_v1.json" in plan
    assert "http_politeness_v1.json" in plan
    assert "0006_observation_pit_metadata" in plan
    assert "mma_model.sources.pit_proxy" in plan


def test_spec_and_plan_have_no_placeholders() -> None:
    for rel in (
        "docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md",
        "docs/superpowers/plans/2026-08-12-public-first-mma-history.md",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "TODO:" not in text
        assert "TBD:" not in text
        assert "FIXME" not in text
        assert "implement later" not in text.lower()
        assert "fill in details" not in text.lower()


def test_scorecard_approved_roles_use_canonical_ids() -> None:
    scorecard = json.loads(
        (ROOT / "output/research/stats-source-scorecard.json").read_text(encoding="utf-8")
    )
    approved = scorecard["prohibited_sources"]["approved_labeled_public_roles"]
    assert set(approved).issubset(set(CANONICAL_SOURCE_IDS))
    assert "combat_registry_public" not in approved
    assert "combat_registry" in approved
    assert "ufcstats_public" in approved
