"""Typed history records (DWCS-105)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

Classification = Literal["professional", "amateur", "unknown"]
RegulatedUs = Literal["true", "false", "unknown"]
BoutResult = Literal["win", "loss", "draw", "nc", "unknown", "cancelled"]
IdentityStatus = Literal["linked", "queued", "blocked", "unresolved"]


class PreFightRecord(BaseModel):
    """Point-in-time reconstructed record from prior effective bouts only."""

    model_config = ConfigDict(frozen=True)

    fighter_id: str
    cutoff: datetime
    reconstruction_version: str
    wins: int | None
    losses: int | None
    draws: int | None
    no_contests: int | None
    professional_bouts: int | None
    amateur_bouts: int | None
    unknown_class_bouts: int | None
    experience_bouts: int | None
    known_minutes: float | None
    minutes_unknown: bool
    undated_excluded: int
    cancelled_excluded: int
    blocked_identity_excluded: int
    used_current_record: bool = False
    unknown_results: int = 0
    left_truncated: bool = False
    completeness: Literal["complete", "left_truncated", "unknown"] = "complete"
    visibility_unknown_excluded: int = 0
    date_precision_excluded: int = 0
    history_unknown: bool = False

    def comparable_tuple(self) -> tuple[int, int, int, int] | None:
        if self.completeness == "unknown" or self.history_unknown:
            return None
        if None in (self.wins, self.losses, self.draws, self.no_contests):
            return None
        return (self.wins, self.losses, self.draws, self.no_contests)


class CoverageSampleRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    bout_id: str
    classification: Classification
    regulated_us: RegulatedUs = "unknown"
    found: bool
    source_failed: bool = False
    source_failed_reason: str | None = None


class RegionalCoverageReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    professional_n: int
    professional_found: int
    professional_rate: float | None
    amateur_n: int
    amateur_found: int
    amateur_rate: float | None
    unknown_class_n: int
    source_failed: tuple[dict[str, Any], ...] = ()
    pre_fight_agreement_n: int = 0
    pre_fight_agreement_d: int = 0
    pre_fight_agreement_rate: float | None = None
    pre_fight_exclusions: tuple[str, ...] = ()
    pre_fight_unknown_n: int = 0
    future_invariance_failures: int = 0
    conflicts: int = 0
    identity_exact_links: int = 0
    identity_queued: int = 0
    identity_blocks: int = 0
    identity_conflations: int = 0
    professional_source_failed: int = 0
    professional_missing_unexplained: int = 0
    amateur_source_failed: int = 0
    amateur_missing_unexplained: int = 0
    left_truncated: int = 0
    unresolved_identities: int = 0
    pit_tiers: tuple[tuple[str, int], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    invariance_hash: str = ""
    report_hash: str = ""
    notes: tuple[str, ...] = ()
    evidence_class: Literal["fixture_validation", "live_source_coverage", "mixed"] = (
        "fixture_validation"
    )
    live_source_coverage: dict[str, dict[str, Any]] = Field(default_factory=dict)
    eligible_sample_bouts: tuple[dict[str, Any], ...] = ()
    fixture_professional_n: int = 0
    fixture_professional_found: int = 0
    fixture_amateur_n: int = 0
    fixture_amateur_found: int = 0
    probe_evidence_source: Literal["live", "frozen", "injected", "offline", "not_run"] = (
        "not_run"
    )
    probe_evidence: dict[str, Any] = Field(default_factory=dict)


class SourceKillEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    reason: str
    host: str | None = None
    path_category: str | None = None
    http_status: int | None = None
    robots: Mapping[str, Any] = Field(default_factory=dict)
    observed_at: datetime | None = None
    result: str = "BLOCKED"
