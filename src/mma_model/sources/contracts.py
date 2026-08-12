"""Source-neutral observation contracts for ingest adapters (DWCS-101).

Adapters must return these records and never write ORM tables directly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    # When True, raw_ref must be None (explicit blob absence). When False, a verified
    # content-addressed blob must exist for payload_hash before commit.
    raw_blob_absent: bool = False
    detail_level: DetailLevel = DetailLevel.PARTIAL
    version_kind: str | None = None
    schema_version: str = "1"
    subject_id: str | None = None
    attributes: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_raw_ref_contract(self) -> SourceObservationRecord:
        if self.raw_blob_absent:
            if self.raw_ref is not None:
                raise ValueError("raw_blob_absent=True requires raw_ref=None")
            return self
        if self.raw_ref is not None and self.raw_ref != self.payload_hash:
            raise ValueError("raw_ref must equal payload_hash when a blob is claimed")
        return self
