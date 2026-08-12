"""Frozen DWCS-106 coverage and gate models."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from mma_model.quality.constants import (
    COVERAGE_CONTRACT_ID,
    COVERAGE_CONTRACT_VERSION,
    COVERAGE_SCHEMA_VERSION,
    COVERAGE_TICKET,
    EvidenceOrigin,
    GateStatus,
    QualityTier,
    ResultClassName,
    SeriesVariant,
    SourceClass,
    SourceStatus,
    TimestampQuality,
)


class BoutCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bout_id: str
    event_id: str
    season: int
    series_variant: SeriesVariant
    overall_tier: QualityTier
    event_night_result: ResultClassName
    current_result: ResultClassName
    timestamp_quality: TimestampQuality
    source_class: SourceClass
    notes: tuple[str, ...] = ()


class DimensionCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    missing: int = 0
    conflict: int = 0
    total: int = 0


class SourceCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    source_class: SourceClass
    status: SourceStatus
    reason: str | None = None
    mapped_bouts: int = 0
    missing_bouts: int = 0
    conflict_bouts: int = 0
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    validation_only: bool = False
    never_live_coverage: bool = False
    evidence_origin: EvidenceOrigin = "none"
    evidence_hash: str | None = None
    evidence_observed_at: str | None = None


class FieldCoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    present: int
    missing: int
    unknown: int
    denominator: int
    status: Literal["measured", "unmeasured", "insufficient_sample"]


class GateAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    segment: str
    status: GateStatus
    blocking: bool
    numerator: int | None = None
    denominator: int | None = None
    threshold: float | None = None
    reason: str | None = None


class LicensedStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_primary: None = None
    licensed_primary_unselected: bool = True
    licensed_adoption_not_selected: bool = True
    licensed_hard_blocker: bool = True
    phase1_global_blocker: bool = False
    note: str = "historical Phase 0 evidence only; never a Phase 1 pipeline stop"


class IdentityCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scoped_pending: int = 0
    scoped_unresolved_conflicts: int = 0
    unscoped_pending: int = 0
    unscoped_approved: int = 0
    unscoped_rejected: int = 0
    unscoped_pending_blocking: bool = False
    unmatched: int = 0
    unmatched_source_identities: int = 0
    upcoming_blocks: int = 0
    fixture_validation: dict[str, Any] = Field(default_factory=dict)


class PitCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proxy_timestamps: int = 0
    unknown_timestamps: int = 0
    direct_timestamps: int = 0
    revision_snapshots: int = 0
    left_truncated_histories: int = 0
    future_row_leakage_failures: int = 0
    future_row_leakage_checks_executed: int = 0
    future_row_leakage_evidence_hash: str = ""
    mutable_current_leakage_failures: int = 0
    mutable_current_leakage_checks_executed: int = 0
    mutable_current_leakage_evidence_hash: str = ""
    mutable_current_rows_examined: int = 0
    mutable_current_applicable_rows: int = 0
    mutable_current_synthetic_guard_checks: int = 0
    mutable_current_leakage_status: GateStatus = "not_applicable"
    conflicting_outcomes: int = 0
    missing_required_details: int = 0


class RawRefIntegrity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    dangling_raw_refs: int = 0
    blob_absent_explicit: int = 0
    blob_present: int = 0
    malformed: int = 0
    unverifiable: int = 0
    missing_blobs: int = 0
    corrupt_blobs: int = 0
    store_provided: bool = False


class CheckpointRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ingest_runs: int = 0
    succeeded_runs: int = 0
    completed_runs: int = 0
    failed_runs: int = 0
    running_runs: int = 0
    checkpoints: int = 0
    run_fingerprints: tuple[list[str], ...] = ()
    checkpoint_fingerprints: tuple[list[str], ...] = ()


class LaneCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cards: int
    bouts: int


class ResultLaneCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decisive: int
    draw: int
    no_contest: int


class ResultTransitionCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reversed_to_no_contest: int = 0
    both_lanes_no_contest: int = 0
    event_night_equals_current: int = 0


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = COVERAGE_SCHEMA_VERSION
    contract_id: str = COVERAGE_CONTRACT_ID
    contract_version: str = COVERAGE_CONTRACT_VERSION
    ticket: str = COVERAGE_TICKET
    series: str
    as_of: str | None = None
    report_hash: str
    config_hash: str
    db_hash: str
    policy_hash: str
    evaluation_contract_hash: str
    policy_mode: str
    licensed_status: LicensedStatus
    universe_cards: int
    universe_bouts: int
    standard: LaneCounts
    brazil: LaneCounts
    event_night: ResultLaneCounts
    current: ResultLaneCounts
    result_transitions: ResultTransitionCounts
    counts_events: int
    counts_bouts: int
    counts_fighters: int
    counts_result_versions: int
    counts_provenance: int
    core_tiers: dict[str, int]
    core_tier_sum: int
    bouts: tuple[BoutCoverageRow, ...]
    season_dimensions: tuple[DimensionCount, ...]
    source_class_dimensions: tuple[DimensionCount, ...]
    quality_tier_dimensions: tuple[DimensionCount, ...]
    source_rows: tuple[SourceCoverageRow, ...]
    field_rows: tuple[FieldCoverageRow, ...]
    identity: IdentityCoverage
    pit: PitCoverage
    raw_ref_integrity: RawRefIntegrity
    checkpoint_run_state: CheckpointRunState
    source_failures: tuple[dict[str, Any], ...]
    fixture_validation: dict[str, Any] = Field(default_factory=dict)
    regional_live: dict[str, Any] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()
    gates: tuple[GateAssessment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    exit_code: int
    blocker_codes: tuple[str, ...]
    passed_codes: tuple[str, ...]
    informational_codes: tuple[str, ...]
    gates: tuple[GateAssessment, ...]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def empty_tier_counts() -> dict[str, int]:
    return {"gold": 0, "silver": 0, "bronze": 0, "missing": 0, "conflict": 0}


def mapping_to_dimension(key: str, counts: Mapping[str, int]) -> DimensionCount:
    gold = int(counts.get("gold") or 0)
    silver = int(counts.get("silver") or 0)
    bronze = int(counts.get("bronze") or 0)
    missing = int(counts.get("missing") or 0)
    conflict = int(counts.get("conflict") or 0)
    return DimensionCount(
        key=key,
        gold=gold,
        silver=silver,
        bronze=bronze,
        missing=missing,
        conflict=conflict,
        total=gold + silver + bronze + missing + conflict,
    )
