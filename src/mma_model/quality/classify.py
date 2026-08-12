"""Classify frozen DWCS bouts into exactly one overall core tier."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from mma_model.quality.constants import (
    CORE_OVERALL_SOURCES,
    TIER_RANK,
    QualityTier,
    ResultClassName,
    SourceClass,
)
from mma_model.quality.models import BoutCoverageRow

RESULT_CLASSES = frozenset({"decisive", "draw", "no_contest"})
VALID_TIERS = frozenset(TIER_RANK)


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
) -> bool:
    if is_mutable_current:
        return False
    if exclude_event_id is not None and event_id == exclude_event_id:
        return False
    if cutoff is None:
        return True
    effective_at = parse_iso_datetime(effective_at)
    observed_at = parse_iso_datetime(observed_at)
    proxy_published_at = parse_iso_datetime(proxy_published_at)
    cutoff = parse_iso_datetime(cutoff)
    if cutoff is None:
        return True
    if version_kind == "correction":
        return effective_at is not None and effective_at < cutoff
    if timestamp_quality == "publication_proxy" and proxy_published_at is not None:
        return proxy_published_at <= cutoff and (
            observed_at is None or observed_at <= cutoff
        )
    if effective_at is None or not (effective_at < cutoff):
        return False
    return observed_at is None or observed_at <= cutoff


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


def best_non_conflict_tier(tiers: list[QualityTier]) -> QualityTier:
    if not tiers:
        return "missing"
    if any(tier == "conflict" for tier in tiers):
        return "conflict"
    ranked = [tier for tier in tiers if tier != "missing"]
    if not ranked:
        return "missing"
    return max(ranked, key=lambda item: TIER_RANK[item])


def classify_source_bout(
    observations: list[Mapping[str, Any]],
) -> QualityTier:
    if not observations:
        return "missing"
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for row in observations:
        if str(row.get("entity_kind") or "") == "conflict":
            return "conflict"
        if normalize_tier(row.get("quality_tier")) == "conflict":
            return "conflict"
        kind = str(row.get("version_kind") or "")
        by_kind.setdefault(kind, []).append(row)
    tiers: list[QualityTier] = []
    for rows in by_kind.values():
        results = {
            str(row.get("result_type") or "")
            for row in rows
            if row.get("result_type")
        }
        if len(results) > 1:
            return "conflict"
        winners = {
            str(row.get("winner_fighter_id") or "")
            for row in rows
            if row.get("result_type") == "decisive"
        }
        if len(winners) > 1:
            return "conflict"
        tiers.extend(normalize_tier(row.get("quality_tier")) for row in rows)
    return best_non_conflict_tier(tiers)


def classify_overall_bout(
    *,
    source_tiers: Mapping[str, QualityTier],
    core_observations: list[Mapping[str, Any]],
) -> tuple[QualityTier, SourceClass, tuple[str, ...]]:
    notes: list[str] = []
    core_tiers = [
        source_tiers[source]
        for source in CORE_OVERALL_SOURCES
        if source in source_tiers
    ]
    if any(tier == "conflict" for tier in core_tiers):
        return "conflict", "internal_manifest", tuple(notes)
    if core_observations:
        overall = classify_source_bout(core_observations)
        source_class: SourceClass = "internal_manifest"
        if overall == "gold":
            source_class = "public_extraction"
        elif overall == "missing":
            source_class = "internal_manifest"
        return overall, source_class, tuple(notes)
    if source_tiers.get("dwcs_manifest") and source_tiers["dwcs_manifest"] != "missing":
        return source_tiers["dwcs_manifest"], "internal_manifest", tuple(notes)
    return "missing", "internal_manifest", tuple(notes)


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
        timestamp_quality=timestamp_quality or "unknown",
        source_class=source_class,
        notes=notes,
    )
