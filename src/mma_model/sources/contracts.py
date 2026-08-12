"""Source-neutral observation contracts for ingest adapters (DWCS-101).

Adapters must return these records and never write ORM tables directly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field


class DetailLevel(StrEnum):
    SUMMARY = "summary"
    PARTIAL = "partial"
    VERIFIED = "verified"


DETAIL_LEVEL_RANK: Mapping[DetailLevel, int] = {
    DetailLevel.SUMMARY: 0,
    DetailLevel.PARTIAL: 1,
    DetailLevel.VERIFIED: 2,
}


class SourceObservationRecord(BaseModel):
    """Typed source-neutral observation carrying provenance timestamps and refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    stream: str
    external_id: str
    entity_kind: str
    observed_at: datetime
    effective_at: datetime
    source_updated_at: datetime | None = None
    payload_hash: str = Field(min_length=64, max_length=64)
    raw_ref: str | None = None
    detail_level: DetailLevel = DetailLevel.PARTIAL
    version_kind: str | None = None
    schema_version: str = "1"
    subject_id: str | None = None
    attributes: Mapping[str, Any] = Field(default_factory=dict)
