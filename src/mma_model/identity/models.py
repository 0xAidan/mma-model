"""Identity evidence and review candidate models (DWCS-104)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

ResolveKind = Literal["linked", "created", "queued", "blocked"]
ReviewStatus = Literal["pending", "approved", "rejected", "reversed"]
ReviewDecision = Literal["approve", "reject"]


def dump_evidence_json(evidence: Mapping[str, Any]) -> str:
    """Serialize evidence as a JSON object; reject NaN/non-serializable values."""
    try:
        payload = dict(evidence)
        blob = json.dumps(payload, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed evidence: {exc}") from exc
    parsed = json.loads(blob)
    if not isinstance(parsed, dict):
        raise ValueError("malformed evidence: must be a JSON object")
    return blob


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

    @field_validator("evidence")
    @classmethod
    def evidence_must_be_json_object(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        dump_evidence_json(value)
        return value


class ResolveResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ResolveKind
    canonical_id: str | None
    review_id: str | None
    evidence_id: str
    rule_id: str
    resolver_version: str
    reversible: bool = True
