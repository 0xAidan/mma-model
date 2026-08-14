"""Load and validate the DWCS source-policy contract (deeply immutable)."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class UnknownSourcePolicyError(ValueError):
    """Raised when policy_mode is missing or not in the allowlist."""


class SourcePolicyError(ValueError):
    """Raised when nested policy structure drifts or fails closed validation."""


class SourceId(StrEnum):
    UFCSTATS_PUBLIC = "ufcstats_public"
    ESPN_PUBLIC = "espn_public"
    MMA_AI_BOOTSTRAP = "mma_ai_bootstrap"
    DWCS_MANIFEST = "dwcs_manifest"
    TAPOLOGY_PUBLIC = "tapology_public"
    SHERDOG_PUBLIC = "sherdog_public"
    COMBAT_REGISTRY = "combat_registry"
    WIKIDATA = "wikidata"
    BESTFIGHTODDS_ARCHIVE = "bestfightodds_archive"
    THE_ODDS_API = "the_odds_api"
    SPORTSDATAIO = "sportsdataio"
    BALLDONTLIE = "balldontlie"
    EXPLICIT_MISSING = "explicit_missing"


class QualityTierId(StrEnum):
    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"
    MISSING = "missing"
    CONFLICT = "conflict"


class TimestampQualityId(StrEnum):
    DIRECT_SOURCE_TIMESTAMP = "direct_source_timestamp"
    REVISION_SNAPSHOT = "revision_snapshot"
    PUBLICATION_PROXY = "publication_proxy"
    UNKNOWN = "unknown"


class PitClockId(StrEnum):
    ACQUISITION_TIME = "acquisition_time"
    SOURCE_PUBLICATION_OR_UPDATE_TIME = "source_publication_or_update_time"
    FACT_EFFECTIVE_TIME = "fact_effective_time"
    DOCUMENTED_PROXY_TIME = "documented_proxy_time"


CANONICAL_SOURCE_IDS: tuple[str, ...] = tuple(member.value for member in SourceId)
REQUIRED_QUALITY_TIER_IDS: tuple[str, ...] = tuple(member.value for member in QualityTierId)
REQUIRED_TIMESTAMP_QUALITY_IDS: tuple[str, ...] = tuple(
    member.value for member in TimestampQualityId
)
REQUIRED_PIT_CLOCK_IDS: tuple[str, ...] = tuple(member.value for member in PitClockId)
ALLOWED_POLICY_MODES = frozenset({"public_first_hybrid_personal_project"})

REQUIRED_TIMESTAMP_FIELDS: tuple[str, ...] = (
    "observed_at",
    "source_published_at",
    "source_updated_at",
    "effective_at",
    "proxy_published_at",
)
REQUIRED_QUALITY_FIELDS: tuple[str, ...] = (
    "timestamp_quality",
    "timestamp_quality_source",
    "quality_tier",
)
REQUIRED_RAW_FIELDS: tuple[str, ...] = ("payload_hash", "raw_ref")
REQUIRED_RESERVED_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "observed_at",
    "source_published_at",
    "source_updated_at",
    "effective_at",
    "proxy_published_at",
    "timestamp_quality",
    "timestamp_quality_source",
    "quality_tier",
    "payload_hash",
    "raw_ref",
    "raw_blob_absent",
    "detail_level",
    "source",
    "stream",
    "external_id",
    "entity_kind",
    "version_kind",
    "schema_version",
    "subject_id",
)


def _freeze_str_map(values: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(values))


def _freeze_str_seq_map(values: Mapping[str, Sequence[str]]) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(items) for key, items in values.items()})


class GatesRetained(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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

    @field_validator(
        "cross_source_reconciliation_min_where_comparable",
        "result_agreement_min",
        mode="after",
    )
    @classmethod
    def _rate_in_unit_interval(cls, value: float) -> float:
        if not 0.0 <= float(value) <= 1.0:
            raise SourcePolicyError(f"gate rate out of range: {value!r}")
        return float(value)


class LicensedAuditStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_primary: str | None
    licensed_hard_blocker: bool
    scorecard_path: str
    preserved_evidence: tuple[str, ...] = ()
    rule: str


class IdentityRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    exact_source_ids_first: bool
    wikidata_crosswalk_first: bool
    fuzzy_or_transliteration: str
    same_name_auto_merge: bool


class SourceRoleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    acquisition: str | None = None
    bootstrap: str | None = None
    condition: str | None = None
    surfaces: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    limitations: str | None = None
    odds_vs_outcome_model: str | None = None


class AccessControls(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    never_bypass: tuple[str, ...]
    public_extraction_required: tuple[str, ...]


class PublicationProxyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_for: str
    requires: str
    example_rule: str
    forbidden_for: str


class PitTimestamps(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    never_backdate_observed_at: bool
    separate_clocks: tuple[str, ...]
    publication_proxy: PublicationProxyRule
    prefer_wayback_or_revision_snapshots: bool
    preserve_capture_timestamp_and_hash: bool

    @model_validator(mode="after")
    def _validate_clocks(self) -> PitTimestamps:
        if tuple(self.separate_clocks) != REQUIRED_PIT_CLOCK_IDS:
            raise SourcePolicyError(
                "pit_timestamps.separate_clocks must equal "
                f"{list(REQUIRED_PIT_CLOCK_IDS)}; got {list(self.separate_clocks)}"
            )
        return self


class ObservationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_timestamp_fields: tuple[str, ...]
    required_quality_fields: tuple[str, ...]
    required_raw_fields: tuple[str, ...]
    timestamp_quality_values: tuple[str, ...]
    quality_tier_values: tuple[str, ...]
    reserved_attribute_keys: tuple[str, ...]
    attributes_json_rule: str

    @model_validator(mode="after")
    def _validate_required_sets(self) -> ObservationMetadata:
        if tuple(self.required_timestamp_fields) != REQUIRED_TIMESTAMP_FIELDS:
            raise SourcePolicyError(
                "observation_metadata.required_timestamp_fields drift"
            )
        if tuple(self.required_quality_fields) != REQUIRED_QUALITY_FIELDS:
            raise SourcePolicyError("observation_metadata.required_quality_fields drift")
        if tuple(self.required_raw_fields) != REQUIRED_RAW_FIELDS:
            raise SourcePolicyError("observation_metadata.required_raw_fields drift")
        if set(self.timestamp_quality_values) != set(REQUIRED_TIMESTAMP_QUALITY_IDS):
            raise SourcePolicyError(
                "observation_metadata.timestamp_quality_values must equal "
                f"{list(REQUIRED_TIMESTAMP_QUALITY_IDS)}"
            )
        if set(self.quality_tier_values) != set(REQUIRED_QUALITY_TIER_IDS):
            raise SourcePolicyError(
                "observation_metadata.quality_tier_values must equal "
                f"{list(REQUIRED_QUALITY_TIER_IDS)}"
            )
        if set(self.reserved_attribute_keys) != set(REQUIRED_RESERVED_ATTRIBUTE_KEYS):
            raise SourcePolicyError(
                "observation_metadata.reserved_attribute_keys must equal "
                f"{list(REQUIRED_RESERVED_ATTRIBUTE_KEYS)}"
            )
        for key in (
            *REQUIRED_TIMESTAMP_FIELDS,
            *REQUIRED_QUALITY_FIELDS,
            *REQUIRED_RAW_FIELDS,
        ):
            if key not in self.reserved_attribute_keys:
                raise SourcePolicyError(
                    f"reserved_attribute_keys missing contract field {key!r}"
                )
        if self.attributes_json_rule != (
            "source_specific_non_contract_metadata_only_never_shadow_reserved_keys"
        ):
            raise SourcePolicyError("observation_metadata.attributes_json_rule drift")
        return self


class Dwcs102Persistence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    implement_in_this_pr: bool
    migration_id: str
    table_columns: Mapping[str, tuple[str, ...]]
    source_observation_record_fields: tuple[str, ...]
    repository_requirements: tuple[str, ...]
    required_tests: tuple[str, ...]
    gap_note: str

    @field_validator("table_columns", mode="before")
    @classmethod
    def _freeze_table_columns(cls, value: object) -> Mapping[str, tuple[str, ...]]:
        if not isinstance(value, Mapping):
            raise SourcePolicyError("dwcs_102_persistence.table_columns must be a mapping")
        return _freeze_str_seq_map(value)  # type: ignore[arg-type]


class Supersedes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase1_production_rights_rule: str
    note: str


class SourcePolicy(BaseModel):
    """Versioned source-policy contract for Phase 1 ingest decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    contract_id: str
    contract_version: str
    effective_date: str
    ticket: str
    decision_recorded_by: str
    policy_mode: Literal["public_first_hybrid_personal_project"]
    source_ids: tuple[str, ...]
    supersedes: Supersedes
    licensed_audit_status: LicensedAuditStatus
    gates_retained: GatesRetained
    roles: Mapping[str, SourceRoleSpec]
    identity_rules: IdentityRules
    access_controls: AccessControls
    pit_timestamps: PitTimestamps
    observation_metadata: ObservationMetadata
    dwcs_102_persistence: Dwcs102Persistence
    quality_tiers: Mapping[str, str]
    kill_criteria: Mapping[str, tuple[str, ...]]
    deterministic_fallback_order: tuple[str, ...]
    phase1_tickets: tuple[str, ...]
    design_spec: str
    implementation_plan: str

    @field_validator("roles", mode="before")
    @classmethod
    def _parse_roles(cls, value: object) -> Mapping[str, SourceRoleSpec]:
        if not isinstance(value, Mapping):
            raise SourcePolicyError("roles must be a mapping")
        parsed = {
            str(key): SourceRoleSpec.model_validate(spec) for key, spec in value.items()
        }
        return MappingProxyType(parsed)

    @field_validator("quality_tiers", mode="before")
    @classmethod
    def _parse_quality_tiers(cls, value: object) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise SourcePolicyError("quality_tiers must be a mapping")
        return _freeze_str_map({str(k): str(v) for k, v in value.items()})

    @field_validator("kill_criteria", mode="before")
    @classmethod
    def _parse_kill_criteria(cls, value: object) -> Mapping[str, tuple[str, ...]]:
        if not isinstance(value, Mapping):
            raise SourcePolicyError("kill_criteria must be a mapping")
        return _freeze_str_seq_map(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _validate_cross_references(self) -> SourcePolicy:
        if tuple(self.source_ids) != CANONICAL_SOURCE_IDS:
            raise SourcePolicyError(
                "source_ids must equal canonical exhaustive set "
                f"{list(CANONICAL_SOURCE_IDS)}; got {list(self.source_ids)}"
            )
        role_keys = set(self.roles.keys())
        if role_keys != set(CANONICAL_SOURCE_IDS):
            raise SourcePolicyError(
                f"roles keys must equal source_ids; missing={sorted(set(CANONICAL_SOURCE_IDS) - role_keys)} "
                f"extra={sorted(role_keys - set(CANONICAL_SOURCE_IDS))}"
            )
        if set(self.quality_tiers.keys()) != set(REQUIRED_QUALITY_TIER_IDS):
            raise SourcePolicyError(
                "quality_tiers keys must equal "
                f"{list(REQUIRED_QUALITY_TIER_IDS)}; got {sorted(self.quality_tiers)}"
            )
        unknown_kill = set(self.kill_criteria.keys()) - set(CANONICAL_SOURCE_IDS)
        if unknown_kill:
            raise SourcePolicyError(
                f"kill_criteria contains unknown source id(s): {sorted(unknown_kill)}"
            )
        unknown_fallback = [
            source_id
            for source_id in self.deterministic_fallback_order
            if source_id not in set(CANONICAL_SOURCE_IDS)
        ]
        if unknown_fallback:
            raise SourcePolicyError(
                f"deterministic_fallback_order contains unknown source id(s): {unknown_fallback}"
            )
        if self.deterministic_fallback_order[0] != SourceId.UFCSTATS_PUBLIC.value:
            raise SourcePolicyError(
                "deterministic_fallback_order must start with ufcstats_public"
            )
        if self.deterministic_fallback_order[-1] != SourceId.EXPLICIT_MISSING.value:
            raise SourcePolicyError(
                "deterministic_fallback_order must end with explicit_missing"
            )
        if SourceId.DWCS_MANIFEST.value in self.deterministic_fallback_order:
            raise SourcePolicyError(
                "dwcs_manifest is a frozen internal universe/result seed and must "
                "not appear in deterministic_fallback_order"
            )
        if len(set(self.deterministic_fallback_order)) != len(
            self.deterministic_fallback_order
        ):
            raise SourcePolicyError("deterministic_fallback_order contains duplicates")
        if self.dwcs_102_persistence.implement_in_this_pr:
            raise SourcePolicyError(
                "dwcs_102_persistence.implement_in_this_pr must remain false in this policy PR"
            )
        raw_cols = self.dwcs_102_persistence.table_columns.get("raw_observations")
        if raw_cols is None:
            raise SourcePolicyError(
                "dwcs_102_persistence.table_columns must include raw_observations"
            )
        for required in (
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
            if required not in raw_cols:
                raise SourcePolicyError(
                    f"dwcs_102_persistence.raw_observations missing column {required!r}"
                )
        # Re-wrap collections after validation so callers never receive mutable dicts.
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))
        object.__setattr__(
            self, "quality_tiers", MappingProxyType(dict(self.quality_tiers))
        )
        object.__setattr__(
            self,
            "kill_criteria",
            MappingProxyType(
                {key: tuple(values) for key, values in self.kill_criteria.items()}
            ),
        )
        object.__setattr__(
            self,
            "dwcs_102_persistence",
            self.dwcs_102_persistence.model_copy(
                update={
                    "table_columns": MappingProxyType(
                        {
                            key: tuple(values)
                            for key, values in self.dwcs_102_persistence.table_columns.items()
                        }
                    )
                }
            ),
        )
        return self


def default_source_policy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "sources" / "source_policy_v1.json"


def load_source_policy(path: Path | None = None) -> SourcePolicy:
    """Load the pinned source-policy JSON and hard-fail on unknown/malformed modes."""
    policy_path = path or default_source_policy_path()
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourcePolicyError(f"invalid source policy JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SourcePolicyError("source policy root must be an object")
    mode = raw.get("policy_mode")
    if mode not in ALLOWED_POLICY_MODES:
        raise UnknownSourcePolicyError(
            f"unsupported policy_mode={mode!r}; allowed={sorted(ALLOWED_POLICY_MODES)}"
        )
    try:
        return SourcePolicy.model_validate(raw)
    except SourcePolicyError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed for any nested drift
        raise SourcePolicyError(str(exc)) from exc
