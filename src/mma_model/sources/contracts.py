"""Source-neutral observation contracts for ingest adapters (DWCS-101/102).

Adapters must return these records and never write ORM tables directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mma_model.sources.policy import (
    REQUIRED_QUALITY_TIER_IDS,
    REQUIRED_TIMESTAMP_QUALITY_IDS,
)


class DetailLevel(StrEnum):
    SUMMARY = "summary"
    PARTIAL = "partial"
    VERIFIED = "verified"


DETAIL_LEVEL_RANK: Mapping[DetailLevel, int] = {
    DetailLevel.SUMMARY: 0,
    DetailLevel.PARTIAL: 1,
    DetailLevel.VERIFIED: 2,
}


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC (got naive datetime)")
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValueError(f"{field_name} must use UTC timezone")
    return value.astimezone(timezone.utc)


class SourceObservationRecord(BaseModel):
    """Typed source-neutral observation carrying provenance timestamps and refs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    stream: str
    external_id: str
    entity_kind: str
    observed_at: datetime
    effective_at: datetime
    source_published_at: datetime | None = None
    source_updated_at: datetime | None = None
    proxy_published_at: datetime | None = None
    timestamp_quality: str = "unknown"
    timestamp_quality_source: str | None = None
    quality_tier: str = "bronze"
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

    @field_validator(
        "observed_at",
        "effective_at",
        "source_published_at",
        "source_updated_at",
        "proxy_published_at",
        mode="after",
    )
    @classmethod
    def _validate_utc_fields(cls, value: datetime | None, info) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value, info.field_name)

    @model_validator(mode="after")
    def _validate_raw_ref_and_quality(self) -> SourceObservationRecord:
        if self.timestamp_quality not in REQUIRED_TIMESTAMP_QUALITY_IDS:
            raise ValueError(
                f"timestamp_quality must be one of {list(REQUIRED_TIMESTAMP_QUALITY_IDS)}"
            )
        if self.quality_tier not in REQUIRED_QUALITY_TIER_IDS:
            raise ValueError(
                f"quality_tier must be one of {list(REQUIRED_QUALITY_TIER_IDS)}"
            )
        if self.raw_blob_absent:
            if self.raw_ref is not None:
                raise ValueError("raw_blob_absent=True requires raw_ref=None")
        elif self.raw_ref is not None and self.raw_ref != self.payload_hash:
            raise ValueError("raw_ref must equal payload_hash when a blob is claimed")
        return self
