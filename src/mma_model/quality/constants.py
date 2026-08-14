"""DWCS-106 coverage constants, source-class maps, and frozen evidence paths."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from mma_model.sources.policy import SourceId

QualityTier = Literal["gold", "silver", "bronze", "missing", "conflict"]
TimestampQuality = Literal[
    "direct_source_timestamp",
    "revision_snapshot",
    "publication_proxy",
    "unknown",
]
EvidenceOrigin = Literal["persisted", "frozen_fallback", "checkpoint", "none"]
SourceClass = Literal[
    "internal_manifest",
    "public_extraction",
    "public_dataset",
    "licensed_api",
    "official_record",
]
SourceStatus = Literal[
    "present",
    "source_killed",
    "source_failed",
    "accessibility_only",
    "schema_drift",
    "unmeasured",
    "validation_only",
]
GateStatus = Literal[
    "pass", "fail", "insufficient_sample", "unmeasured", "informational", "not_applicable"
]
SeriesVariant = Literal["standard", "brazil"]
ResultClassName = Literal["decisive", "draw", "no_contest", "missing"]

QUALITY_TIERS: tuple[QualityTier, ...] = (
    "gold",
    "silver",
    "bronze",
    "missing",
    "conflict",
)
SOURCE_CLASSES: tuple[SourceClass, ...] = (
    "internal_manifest",
    "public_extraction",
    "public_dataset",
    "licensed_api",
    "official_record",
)
TIER_RANK: dict[str, int] = {
    "missing": 0,
    "bronze": 1,
    "silver": 2,
    "gold": 3,
    "conflict": 4,
}
TIMESTAMP_QUALITY_RANK: dict[str, int] = {
    "unknown": 0,
    "publication_proxy": 1,
    "revision_snapshot": 2,
    "direct_source_timestamp": 3,
}
DIRECT_TIMESTAMP_QUALITIES = frozenset({"direct_source_timestamp", "revision_snapshot"})
TIMESTAMP_QUALITIES: tuple[TimestampQuality, ...] = (
    "direct_source_timestamp",
    "revision_snapshot",
    "publication_proxy",
    "unknown",
)
EVIDENCE_ORIGINS: tuple[EvidenceOrigin, ...] = (
    "persisted",
    "frozen_fallback",
    "checkpoint",
    "none",
)
SUCCEEDED_RUN_STATUSES = frozenset({"succeeded"})
COMPLETED_RUN_STATUSES = frozenset({"completed"})
FAILED_RUN_STATUSES = frozenset({"failed"})
RUNNING_RUN_STATUSES = frozenset({"running"})
CANONICAL_TERMINAL_OK_STATUSES = frozenset({"succeeded"})

COVERAGE_SCHEMA_VERSION = 1
COVERAGE_CONTRACT_ID = "dwcs_coverage"
COVERAGE_CONTRACT_VERSION = "1.0.0"
COVERAGE_TICKET = "DWCS-106"

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_STRICT_BLOCKERS = 2

LIVE_DB_URLS = frozenset(
    {
        "sqlite:///data/mma.db",
        "sqlite:///./data/mma.db",
    }
)

PHASE1_BOUT_SOURCES: tuple[str, ...] = (
    SourceId.DWCS_MANIFEST.value,
    SourceId.UFCSTATS_PUBLIC.value,
    SourceId.MMA_AI_BOOTSTRAP.value,
    SourceId.TAPOLOGY_PUBLIC.value,
    SourceId.SHERDOG_PUBLIC.value,
    SourceId.COMBAT_REGISTRY.value,
    SourceId.SPORTSDATAIO.value,
    SourceId.BALLDONTLIE.value,
    SourceId.EXPLICIT_MISSING.value,
)

SOURCE_CLASS_BY_ID: dict[str, SourceClass] = {
    SourceId.DWCS_MANIFEST.value: "internal_manifest",
    SourceId.UFCSTATS_PUBLIC.value: "public_extraction",
    SourceId.ESPN_PUBLIC.value: "public_dataset",
    SourceId.TAPOLOGY_PUBLIC.value: "public_extraction",
    SourceId.SHERDOG_PUBLIC.value: "public_extraction",
    SourceId.MMA_AI_BOOTSTRAP.value: "public_dataset",
    SourceId.WIKIDATA.value: "public_dataset",
    SourceId.SPORTSDATAIO.value: "licensed_api",
    SourceId.BALLDONTLIE.value: "licensed_api",
    SourceId.COMBAT_REGISTRY.value: "official_record",
    SourceId.EXPLICIT_MISSING.value: "public_dataset",
}

VALIDATION_ONLY_SOURCES = frozenset(
    {
        SourceId.SPORTSDATAIO.value,
        SourceId.BALLDONTLIE.value,
    }
)
SOURCE_FAMILY_BY_ID: dict[str, str] = {
    SourceId.DWCS_MANIFEST.value: "dwcs_manifest",
    SourceId.UFCSTATS_PUBLIC.value: "ufcstats",
    SourceId.ESPN_PUBLIC.value: "espn",
    SourceId.MMA_AI_BOOTSTRAP.value: "ufcstats",
    SourceId.TAPOLOGY_PUBLIC.value: "tapology",
    SourceId.SHERDOG_PUBLIC.value: "sherdog",
    SourceId.COMBAT_REGISTRY.value: "combat_registry",
    SourceId.SPORTSDATAIO.value: "sportsdataio",
    SourceId.BALLDONTLIE.value: "balldontlie",
    SourceId.EXPLICIT_MISSING.value: "explicit_missing",
}
DERIVED_SOURCE_DEPENDENCY: dict[str, str] = {
    SourceId.MMA_AI_BOOTSTRAP.value: SourceId.UFCSTATS_PUBLIC.value,
}
INDEPENDENT_AGREEMENT_SOURCES = frozenset(
    {
        SourceId.DWCS_MANIFEST.value,
        SourceId.UFCSTATS_PUBLIC.value,
        SourceId.MMA_AI_BOOTSTRAP.value,
        SourceId.TAPOLOGY_PUBLIC.value,
        SourceId.SHERDOG_PUBLIC.value,
        SourceId.COMBAT_REGISTRY.value,
        SourceId.SPORTSDATAIO.value,
        SourceId.BALLDONTLIE.value,
    }
)
GATE_RAW_UNVERIFIABLE = "raw_ref_unverifiable"

REQUIRED_RESULT_FIELDS: tuple[str, ...] = (
    "result_type",
    "winner_fighter_id",
    "method",
    "ending_round",
    "time_str",
    "quality_tier",
    "timestamp_quality",
    "payload_hash",
)

KILL_REASONS = frozenset(
    {
        "cloudflare_challenge",
        "http_403",
        "login_wall",
        "http_redirect_refused",
        "robots_disallow",
        "source_killed",
        "persistent_block",
    }
)
SCHEMA_DRIFT_REASONS = frozenset({"schema_drift", "parser_schema_drift"})

GATE_MANIFEST_REPRESENTATION = "manifest_representation"
GATE_CROSS_SOURCE_RECONCILIATION = "cross_source_reconciliation"
GATE_RESULT_AGREEMENT = "result_agreement"
GATE_IDENTITY_CONFLICT = "identity_conflict"
GATE_FUTURE_ROW_LEAKAGE = "future_row_leakage"
GATE_MUTABLE_CURRENT_LEAKAGE = "mutable_current_leakage"
GATE_REGIONAL_PROFESSIONAL = "regional_professional_sample"
GATE_REGIONAL_AMATEUR = "regional_amateur_sample"
GATE_PRE_FIGHT_AGREEMENT = "pre_fight_agreement"
GATE_UFCSTATS_LIVE = "ufcstats_public_live"
GATE_RAW_REF_INTEGRITY = "raw_ref_integrity"
GATE_CORE_DENOMINATOR = "core_denominator"
GATE_MISSING_REQUIRED_DETAILS = "missing_required_details"
GATE_LICENSED_PRIMARY = "licensed_primary_status"

LICENSED_NON_BLOCKER_CODES = frozenset(
    {
        "licensed_primary_unselected",
        "licensed_adoption_not_selected",
        "licensed_hard_blocker",
        "decision_primary_null",
        GATE_LICENSED_PRIMARY,
    }
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_SCHEMA_PATH = REPO_ROOT / "output" / "contracts" / "coverage.schema.json"
UFCSTATS_FROZEN_AUDIT_PATH = (
    REPO_ROOT / "output" / "research" / "ufcstats-public-audit-summary.json"
)
REGIONAL_LIVE_PROBE_PATH = REPO_ROOT / "config" / "history" / "live_probe_evidence_v1.json"
REGIONAL_SAMPLE_PATH = REPO_ROOT / "config" / "history" / "adjudicated_regional_sample_v1.json"
DEFAULT_LEAKAGE_CUTOFF = "2024-01-01T00:00:00+00:00"
DEFAULT_COVERAGE_DOC = REPO_ROOT / "docs" / "data" / "coverage-health.md"
DEFAULT_COVERAGE_SUMMARY = REPO_ROOT / "output" / "research" / "dwcs-106-coverage-summary.json"
