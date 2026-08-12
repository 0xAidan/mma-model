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
    wins: int
    losses: int
    draws: int
    no_contests: int
    professional_bouts: int
    amateur_bouts: int
    unknown_class_bouts: int
    experience_bouts: int
    known_minutes: float | None
    minutes_unknown: bool
    undated_excluded: int
    cancelled_excluded: int
    blocked_identity_excluded: int
    used_current_record: bool = False
    unknown_results: int = 0
    left_truncated: bool = False

    def comparable_tuple(self) -> tuple[int, int, int, int]:
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
