"""Identity evidence and review candidate models (DWCS-104)."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

ResolveKind = Literal["linked", "created", "queued", "blocked"]
ReviewStatus = Literal["pending", "approved", "rejected", "reversed"]
ReviewDecision = Literal["approve", "reject"]


class ReviewCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    external_id: str
    display_name: str
    normalized_name: str
    candidate_canonical_ids: tuple[str, ...] = ()
    rule_id: str
    evidence: Mapping[str, Any] = Field(default_factory=dict)
    wikidata_id: str | None = None
    dob: date | None = None
    bout_id: str | None = None
    bout_status: str | None = None
    prior_mapping_json: str | None = None


class ResolveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ResolveKind
    canonical_id: str | None
    review_id: str | None
    evidence_id: str
    rule_id: str
    resolver_version: str
    reversible: bool = True
