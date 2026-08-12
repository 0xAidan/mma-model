"""Classify frozen DWCS bouts into exactly one overall core tier."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from mma_model.quality.constants import (
    DIRECT_TIMESTAMP_QUALITIES,
    INDEPENDENT_AGREEMENT_SOURCES,
    TIMESTAMP_QUALITY_RANK,
    TIER_RANK,
    VALIDATION_ONLY_SOURCES,
    QualityTier,
    ResultClassName,
    SourceClass,
    TimestampQuality,
)
from mma_model.quality.models import BoutCoverageRow

RESULT_CLASSES = frozenset({"decisive", "draw", "no_contest"})
VALID_TIERS = frozenset(TIER_RANK)
MUTABLE_SOURCES = frozenset({"mutable_current", "current_mutable_profile"})


def parse_iso_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def visibility_clock(
    *,
    timestamp_quality: str | None,
    source_published_at: datetime | None,
    source_updated_at: datetime | None,
    proxy_published_at: datetime | None,
    observed_at: datetime | None,
    effective_at: datetime | None,
    adjudicated_at: datetime | None = None,
) -> datetime | None:
    """Semantic visibility time. Acquisition observed_at is provenance only."""
    quality = str(timestamp_quality or "unknown")
    if quality in DIRECT_TIMESTAMP_QUALITIES:
        return parse_iso_datetime(
            source_published_at or source_updated_at or adjudicated_at or effective_at
        )
    if quality == "publication_proxy":
        return parse_iso_datetime(proxy_published_at)
    return parse_iso_datetime(observed_at)


def observation_visible(
    *,
    effective_at: datetime | None,
    observed_at: datetime | None,
    proxy_published_at: datetime | None,
    timestamp_quality: str | None,
    version_kind: str | None,
    is_mutable_current: bool,
    cutoff: datetime | None,
    event_id: str | None = None,
    exclude_event_id: str | None = None,
    source_published_at: datetime | None = None,
    source_updated_at: datetime | None = None,
    adjudicated_at: datetime | None = None,
    source: str | None = None,
) -> bool:
    del version_kind
    if is_mutable_current or (source or "") in MUTABLE_SOURCES:
        return False
    if exclude_event_id is not None and event_id == exclude_event_id:
        return False
    if cutoff is None:
        return True
    cutoff = parse_iso_datetime(cutoff)
    if cutoff is None:
        return True
    effective = parse_iso_datetime(effective_at)
    if effective is None or not (effective < cutoff):
        return False
    visible_at = visibility_clock(
        timestamp_quality=timestamp_quality,
        source_published_at=source_published_at,
        source_updated_at=source_updated_at,
        proxy_published_at=proxy_published_at,
        observed_at=observed_at,
        effective_at=effective,
        adjudicated_at=adjudicated_at,
    )
    if visible_at is None:
        return False
    return visible_at <= cutoff


def result_version_visible(
    *,
    effective_at: datetime | None,
    cutoff: datetime | None,
    event_id: str | None = None,
    exclude_event_id: str | None = None,
) -> bool:
    """Canonical result versions use effective_at as the publication clock."""
    if exclude_event_id is not None and event_id == exclude_event_id:
        return False
    if cutoff is None:
        return True
    cutoff = parse_iso_datetime(cutoff)
    if cutoff is None:
        return True
    effective = parse_iso_datetime(effective_at)
    if effective is None or not (effective < cutoff):
        return False
    return effective <= cutoff


def normalize_tier(value: object) -> QualityTier:
    text = str(value or "").strip()
    if text in VALID_TIERS:
        return text  # type: ignore[return-value]
    return "conflict"


def normalize_result(value: object) -> ResultClassName:
    text = str(value or "").strip()
    if text in RESULT_CLASSES:
        return text  # type: ignore[return-value]
    if not text:
        return "missing"
    return "missing"


def normalize_timestamp_quality(value: object) -> TimestampQuality:
    text = str(value or "").strip()
    if text in TIMESTAMP_QUALITY_RANK:
        return text  # type: ignore[return-value]
    return "unknown"


def result_key(row: Mapping[str, Any]) -> tuple[str, str]:
    result = str(row.get("result_type") or "")
    winner = str(row.get("winner_fighter_id") or "") if result == "decisive" else ""
    return (result, winner)


def _kind_conflict(rows: list[Mapping[str, Any]]) -> bool:
    results = {str(row.get("result_type") or "") for row in rows if row.get("result_type")}
    if len(results) > 1:
        return True
    winners = {
        str(row.get("winner_fighter_id") or "")
        for row in rows
        if row.get("result_type") == "decisive"
    }
    return len(winners) > 1


def classify_source_bout(observations: list[Mapping[str, Any]]) -> QualityTier:
    """Per-source tier. Independent agreement is an overall-only upgrade."""
    if not observations:
        return "missing"
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    has_direct = False
    for row in observations:
        if str(row.get("entity_kind") or "") == "conflict":
            return "conflict"
        if normalize_tier(row.get("quality_tier")) == "conflict":
            return "conflict"
        kind = str(row.get("version_kind") or "")
        by_kind.setdefault(kind, []).append(row)
        quality = normalize_timestamp_quality(row.get("timestamp_quality"))
        if quality in DIRECT_TIMESTAMP_QUALITIES:
            has_direct = True
    for rows in by_kind.values():
        if _kind_conflict(rows):
            return "conflict"
    if has_direct:
        return "gold"
    return "bronze"


def _independent_sources(source_tiers: Mapping[str, QualityTier]) -> list[str]:
    return [
        source
        for source, tier in source_tiers.items()
        if source in INDEPENDENT_AGREEMENT_SOURCES
        and source not in VALIDATION_ONLY_SOURCES
        and tier != "missing"
    ]


def classify_overall_bout(
    *,
    source_tiers: Mapping[str, QualityTier],
    core_observations: list[Mapping[str, Any]],
    source_observations: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> tuple[QualityTier, SourceClass, tuple[str, ...]]:
    notes: list[str] = []
    grouped = dict(source_observations or {})
    if not grouped and core_observations:
        for row in core_observations:
            grouped.setdefault(str(row.get("source") or ""), []).append(row)

    independent = [
        source
        for source in sorted(grouped)
        if source in INDEPENDENT_AGREEMENT_SOURCES
        and source not in VALIDATION_ONLY_SOURCES
        and grouped[source]
    ]
    by_kind: dict[str, dict[str, tuple[str, str]]] = {}
    has_direct = False
    for source in independent:
        for row in grouped[source]:
            if str(row.get("entity_kind") or "") == "conflict":
                return "conflict", "internal_manifest", ("independent_disagreement",)
            if normalize_tier(row.get("quality_tier")) == "conflict":
                return "conflict", "internal_manifest", ("independent_disagreement",)
            kind = str(row.get("version_kind") or "")
            key = result_key(row)
            if not key[0]:
                continue
            prior = by_kind.setdefault(kind, {})
            if source in prior and prior[source] != key:
                return "conflict", "internal_manifest", ("source_internal_disagreement",)
            prior[source] = key
            if normalize_timestamp_quality(row.get("timestamp_quality")) in DIRECT_TIMESTAMP_QUALITIES:
                has_direct = True
        if _kind_conflict(list(grouped[source])):
            return "conflict", "internal_manifest", ("source_internal_disagreement",)

    for _kind, per_source in by_kind.items():
        keys = {item for item in per_source.values() if item[0]}
        if len(keys) > 1:
            return "conflict", "internal_manifest", ("independent_disagreement",)

    agreeing = _independent_sources(source_tiers)
    if not agreeing and not independent:
        if source_tiers.get("dwcs_manifest") and source_tiers["dwcs_manifest"] != "missing":
            return "bronze", "internal_manifest", ("single_source_manifest",)
        return "missing", "internal_manifest", tuple(notes)

    source_class: SourceClass = "internal_manifest"
    if any(source != "dwcs_manifest" for source in independent):
        source_class = "public_extraction"

    if has_direct:
        notes.append("direct_or_revision_timestamp")
        return "gold", source_class, tuple(notes)
    if len(independent) >= 2:
        notes.append("independent_proxy_agreement")
        return "silver", source_class, tuple(notes)
    notes.append("single_source_retrospective_or_proxy")
    return "bronze", source_class, tuple(notes)


def select_best_observation(observations: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Deterministic best observation: timestamp quality, then source, hash, id."""
    if not observations:
        return None
    return min(
        observations,
        key=lambda row: (
            -TIMESTAMP_QUALITY_RANK.get(normalize_timestamp_quality(row.get("timestamp_quality")), 0),
            -TIER_RANK.get(normalize_tier(row.get("quality_tier")), 0),
            str(row.get("source") or ""),
            str(row.get("payload_hash") or ""),
            str(row.get("external_id") or ""),
            int(row.get("id") or 0),
        ),
    )


def build_bout_row(
    *,
    bout_id: str,
    event_id: str,
    season: int,
    series_variant: str,
    overall_tier: QualityTier,
    event_night_result: ResultClassName,
    current_result: ResultClassName,
    timestamp_quality: str,
    source_class: SourceClass,
    notes: tuple[str, ...],
) -> BoutCoverageRow:
    variant: str = series_variant if series_variant in {"standard", "brazil"} else "standard"
    return BoutCoverageRow(
        bout_id=bout_id,
        event_id=event_id,
        season=season,
        series_variant=variant,  # type: ignore[arg-type]
        overall_tier=overall_tier,
        event_night_result=event_night_result,
        current_result=current_result,
        timestamp_quality=normalize_timestamp_quality(timestamp_quality),
        source_class=source_class,
        notes=notes,
    )
