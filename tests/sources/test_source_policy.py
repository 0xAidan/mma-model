"""Tests for public-first source policy contract loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from mma_model.sources.policy import (
    UnknownSourcePolicyError,
    load_source_policy,
)

ROOT = Path(__file__).resolve().parents[2]


def test_load_source_policy_public_first_mode() -> None:
    policy = load_source_policy(ROOT / "config" / "sources" / "source_policy_v1.json")
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
    assert policy.deterministic_fallback_order[0] == "ufcstats_direct_snapshots"
    assert "opaque_precomputed_feature_csvs_as_training_inputs" in policy.roles[
        "ufcstats_direct"
    ]["forbidden"]


def test_unknown_policy_mode_hard_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        (
            '{"schema_version":1,"contract_id":"x","contract_version":"1",'
            '"effective_date":"2026-08-12","ticket":"DWCS-003",'
            '"decision_recorded_by":"test","policy_mode":"licensed_only",'
            '"supersedes":{},"licensed_audit_status":{"decision_primary":null,'
            '"licensed_hard_blocker":true,"scorecard_path":"x","rule":"r"},'
            '"gates_retained":{"dwcs_universe_cards":89,"dwcs_universe_bouts":440,'
            '"every_exclusion_categorized":true,'
            '"cross_source_reconciliation_min_where_comparable":0.98,'
            '"result_agreement_min":0.99,'
            '"unresolved_evaluated_or_upcoming_identity_conflicts_max":0,'
            '"future_row_leakage_failures_max":0,'
            '"mutable_current_as_historical_feature_failures_max":0,'
            '"weakening_forbidden":true,"policy_change_permits_only":"x"},'
            '"roles":{},"identity_rules":{"exact_source_ids_first":true,'
            '"wikidata_crosswalk_first":true,"fuzzy_or_transliteration":"queue",'
            '"same_name_auto_merge":false},"access_controls":{},'
            '"pit_timestamps":{},"quality_tiers":{},"kill_criteria":{},'
            '"deterministic_fallback_order":[],"phase1_tickets":[],'
            '"design_spec":"x","implementation_plan":"y"}'
        ),
        encoding="utf-8",
    )
    with pytest.raises(UnknownSourcePolicyError):
        load_source_policy(bad)


def test_design_plan_and_policy_paths_exist() -> None:
    assert (ROOT / "docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md").is_file()
    assert (ROOT / "docs/superpowers/plans/2026-08-12-public-first-mma-history.md").is_file()
    assert (ROOT / "docs/research/phase1-public-first-roadmap.md").is_file()
    policy = load_source_policy()
    assert (ROOT / policy.design_spec).is_file()
    assert (ROOT / policy.implementation_plan).is_file()


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
