"""Classify frozen DWCS bouts into exactly one overall core tier."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping

from mma_model.quality.constants import (
    DERIVED_SOURCE_DEPENDENCY,
    DIRECT_TIMESTAMP_QUALITIES,
    INDEPENDENT_AGREEMENT_SOURCES,
    SOURCE_FAMILY_BY_ID,
    TIMESTAMP_QUALITY_RANK,
    TIER_RANK,
    QualityTier,
    ResultClassName,
    SourceClass,
    TimestampQuality,
)
from mma_model.quality.models import BoutCoverageRow

RESULT_CLASSES = frozenset({"decisive", "draw", "no_contest"})
VALID_TIERS = frozenset(TIER_RANK)
MUTABLE_SOURCES = frozenset({"mutable_current", "current_mutable_profile"})
FACT_ENTITY_KINDS = frozenset({"bout_result", ""})
METADATA_GAP_ENTITY_KINDS = frozenset({"conflict"})


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


def source_family(source: str) -> str:
    if source in DERIVED_SOURCE_DEPENDENCY:
        return SOURCE_FAMILY_BY_ID.get(DERIVED_SOURCE_DEPENDENCY[source], source)
    return SOURCE_FAMILY_BY_ID.get(source, source)


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
    timestamp_quality: str | None = None,
    source_published_at: datetime | None = None,
    source_updated_at: datetime | None = None,
    proxy_published_at: datetime | None = None,
    observed_at: datetime | None = None,
    adjudicated_at: datetime | None = None,
) -> bool:
    """Result versions need effective_at and an allowed visibility clock."""
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
        timestamp_quality=timestamp_quality or "unknown",
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


def fingerprint_in_scope(
    *,
    cutoff: datetime | None,
    effective_at: datetime | None,
    observed_at: datetime | None,
) -> bool:
    """Include a row in the global semantic DB fingerprint.

    Coverage visibility is a separate filter and must not run first. Mutable
    current rows are included when ``cutoff`` is None. Cutoff reports drop rows
    whose clocks are entirely in the future.
    """
    if cutoff is None:
        return True
    cutoff = parse_iso_datetime(cutoff)
    if cutoff is None:
        return True
    effective = parse_iso_datetime(effective_at)
    observed = parse_iso_datetime(observed_at)
    if effective is not None and effective < cutoff:
        return True
    if observed is not None and observed <= cutoff:
        return True
    if effective is None and observed is None:
        return True
    return False


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


def _is_fact_row(row: Mapping[str, Any]) -> bool:
    kind = str(row.get("entity_kind") or "bout_result")
    if kind in METADATA_GAP_ENTITY_KINDS:
        return False
    return kind in FACT_ENTITY_KINDS


def _fact_rows(observations: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in observations if _is_fact_row(row)]


def classify_source_bout(observations: list[Mapping[str, Any]]) -> QualityTier:
    """Per-source tier. Independent agreement is an overall-only upgrade."""
    facts = _fact_rows(observations)
    if not facts:
        return "missing"
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    has_direct = False
    for row in facts:
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

    family_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for source, rows in grouped.items():
        if source not in INDEPENDENT_AGREEMENT_SOURCES:
            continue
        facts = _fact_rows(list(rows))
        if not facts:
            continue
        family_rows[source_family(source)].extend(facts)

    by_kind: dict[str, dict[str, tuple[str, str]]] = {}
    has_direct = False
    for family, rows in family_rows.items():
        kind_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            if normalize_tier(row.get("quality_tier")) == "conflict":
                return "conflict", "internal_manifest", ("independent_disagreement",)
            kind = str(row.get("version_kind") or "")
            kind_groups[kind].append(row)
            if (
                normalize_timestamp_quality(row.get("timestamp_quality"))
                in DIRECT_TIMESTAMP_QUALITIES
            ):
                has_direct = True
        for kind, kind_rows in kind_groups.items():
            if _kind_conflict(kind_rows):
                return "conflict", "internal_manifest", ("source_internal_disagreement",)
            keys = {result_key(row) for row in kind_rows if result_key(row)[0]}
            if not keys:
                continue
            key = next(iter(keys))
            prior = by_kind.setdefault(kind, {})
            prior[family] = key

    for _kind, per_family in by_kind.items():
        keys = {item for item in per_family.values() if item[0]}
        if len(keys) > 1:
            return "conflict", "internal_manifest", ("independent_disagreement",)

    families = sorted(family_rows)
    if not families:
        if source_tiers.get("dwcs_manifest") and source_tiers["dwcs_manifest"] != "missing":
            return "bronze", "internal_manifest", ("single_source_manifest",)
        return "missing", "internal_manifest", tuple(notes)

    source_class: SourceClass = "internal_manifest"
    if any(family != "dwcs_manifest" for family in families):
        source_class = "public_extraction"

    if has_direct:
        notes.append("direct_or_revision_timestamp")
        return "gold", source_class, tuple(notes)
    if len(families) >= 2:
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
            -TIMESTAMP_QUALITY_RANK.get(
                normalize_timestamp_quality(row.get("timestamp_quality")), 0
            ),
            -TIER_RANK.get(normalize_tier(row.get("quality_tier")), 0),
            str(row.get("source") or ""),
            str(row.get("payload_hash") or ""),
            str(row.get("external_id") or ""),
            int(row.get("id") or 0),
        ),
    )


def _clocks_match(left: object, right: object) -> bool:
    parsed_left = parse_iso_datetime(left if isinstance(left, (datetime, str)) else None)
    parsed_right = parse_iso_datetime(right if isinstance(right, (datetime, str)) else None)
    if parsed_left is None or parsed_right is None:
        return False
    return parsed_left == parsed_right


def attach_result_version_clocks(
    versions: list[Mapping[str, Any]],
    observations: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Correlate each result version to a matching raw observation's clocks."""
    by_key: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in observations:
        if not _is_fact_row(row):
            continue
        key = (str(row.get("subject_id") or ""), str(row.get("version_kind") or ""))
        by_key[key].append(row)
    attached: list[dict[str, Any]] = []
    for row in versions:
        clocks = dict(row)
        key = (str(row.get("bout_id") or ""), str(row.get("version_kind") or ""))
        candidates = [
            item
            for item in by_key.get(key, [])
            if _clocks_match(item.get("observed_at"), row.get("observed_at"))
            or (
                _clocks_match(item.get("effective_at"), row.get("effective_at"))
                and str(item.get("result_type") or "") == str(row.get("result_type") or "")
            )
        ]
        best = select_best_observation(candidates)
        if best is not None:
            clocks["timestamp_quality"] = best.get("timestamp_quality")
            clocks["proxy_published_at"] = best.get("proxy_published_at")
            clocks["source_published_at"] = best.get("source_published_at")
            clocks["source_updated_at"] = best.get("source_updated_at")
        else:
            clocks["timestamp_quality"] = "unknown"
        attached.append(clocks)
    return attached


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
