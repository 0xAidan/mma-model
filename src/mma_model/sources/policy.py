"""Load and validate the DWCS source-policy contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

ALLOWED_POLICY_MODES = frozenset({"public_first_hybrid_personal_project"})


class UnknownSourcePolicyError(ValueError):
    """Raised when policy_mode is missing or not in the allowlist."""


class GatesRetained(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dwcs_universe_cards: int
    dwcs_universe_bouts: int
    every_exclusion_categorized: bool
    cross_source_reconciliation_min_where_comparable: float
    result_agreement_min: float
    unresolved_evaluated_or_upcoming_identity_conflicts_max: int
    future_row_leakage_failures_max: int
    mutable_current_as_historical_feature_failures_max: int
    weakening_forbidden: bool
    policy_change_permits_only: str


class LicensedAuditStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_primary: str | None
    licensed_hard_blocker: bool
    scorecard_path: str
    preserved_evidence: tuple[str, ...] = ()
    rule: str


class IdentityRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exact_source_ids_first: bool
    wikidata_crosswalk_first: bool
    fuzzy_or_transliteration: str
    same_name_auto_merge: bool


class SourcePolicy(BaseModel):
    """Versioned source-policy contract for Phase 1 ingest decisions."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    contract_id: str
    contract_version: str
    effective_date: str
    ticket: str
    decision_recorded_by: str
    policy_mode: Literal["public_first_hybrid_personal_project"]
    supersedes: Mapping[str, Any]
    licensed_audit_status: LicensedAuditStatus
    gates_retained: GatesRetained
    roles: Mapping[str, Any]
    identity_rules: IdentityRules
    access_controls: Mapping[str, Any]
    pit_timestamps: Mapping[str, Any]
    quality_tiers: Mapping[str, str]
    kill_criteria: Mapping[str, Any]
    deterministic_fallback_order: tuple[str, ...]
    phase1_tickets: tuple[str, ...]
    design_spec: str
    implementation_plan: str


def default_source_policy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "sources" / "source_policy_v1.json"


def load_source_policy(path: Path | None = None) -> SourcePolicy:
    """Load the pinned source-policy JSON and hard-fail on unknown modes."""
    policy_path = path or default_source_policy_path()
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    mode = raw.get("policy_mode")
    if mode not in ALLOWED_POLICY_MODES:
        raise UnknownSourcePolicyError(
            f"unsupported policy_mode={mode!r}; allowed={sorted(ALLOWED_POLICY_MODES)}"
        )
    return SourcePolicy.model_validate(raw)
