"""Series / lifecycle / result classification for DWCS manifest rows."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

SERIES_VARIANTS = frozenset({"standard", "brazil"})
BOUT_STATUSES = frozenset({"occurred", "completed", "cancelled", "canceled", "replacement"})
RESULT_CLASSES = frozenset({"decisive", "draw", "no_contest"})
VERSION_STATES = frozenset(
    {"assumed_equal_to_current", "reversed_to_no_contest", "unchanged"}
)
CANCELLATION_KINDS = frozenset({"cancelled", "canceled", "replacement"})


class BoutCategory(StrEnum):
    """Exactly one terminal category per classified input row."""

    COMPLETED_STANDARD = "completed_standard"
    COMPLETED_BRAZIL = "completed_brazil"
    CANCELLED = "cancelled"
    REPLACEMENT = "replacement"
    PROVIDER_ENRICHMENT_UNMAPPED = "provider_enrichment_unmapped"
    PROVIDER_ENRICHMENT_BLOCKED = "provider_enrichment_blocked"
    MISMATCH_LEDGER_GAP = "mismatch_ledger_gap"
    CONFLICT = "conflict"


class SeriesVariant(StrEnum):
    STANDARD = "standard"
    BRAZIL = "brazil"


class BoutLifecycle(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REPLACEMENT = "replacement"


class ResultClass(StrEnum):
    DECISIVE = "decisive"
    DRAW = "draw"
    NO_CONTEST = "no_contest"


class ProviderEnrichmentState(StrEnum):
    UNMAPPED = "unmapped"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


class ClassificationError(ValueError):
    """Malformed or unknown enum values in a manifest row."""


@dataclass(frozen=True)
class BoutClassification:
    category: BoutCategory
    series_variant: SeriesVariant
    lifecycle: BoutLifecycle
    event_night_result: ResultClass
    current_result: ResultClass
    version_state: str
    provider_enrichment: ProviderEnrichmentState
    ufcstats_bout_id: str | None
    notes: tuple[str, ...] = ()


def _require_enum(value: object, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ClassificationError(f"unknown or malformed {field}: {value!r}")
    return value


def _result_class(payload: Mapping[str, Any] | None, field: str) -> ResultClass:
    if not isinstance(payload, Mapping):
        raise ClassificationError(f"missing {field}")
    raw = payload.get("class")
    return ResultClass(_require_enum(raw, RESULT_CLASSES, f"{field}.class"))


def classify_bout(
    row: Mapping[str, Any],
    *,
    provider_blocked: bool = True,
) -> BoutClassification:
    """Classify a bout manifest row into exactly one terminal category.

    Manifest occurred/completed rows are completed_standard/brazil. Provider
    enrichment (UFCStats) is classified separately as unmapped/blocked when IDs
    are null or access is blocked — that state is reported alongside, but the
    terminal universe category for an occurred bout remains completed_*.
    """
    series = SeriesVariant(
        _require_enum(row.get("series_variant"), SERIES_VARIANTS, "series_variant")
    )
    status_raw = _require_enum(
        str(row.get("status") or "").strip().lower(),
        BOUT_STATUSES,
        "status",
    )
    version_state = _require_enum(
        row.get("version_state"), VERSION_STATES, "version_state"
    )
    event_night = _result_class(row.get("event_night_result"), "event_night_result")
    current = _result_class(row.get("current_result"), "current_result")

    ufcstats_id = row.get("ufcstats_bout_id")
    if ufcstats_id is not None and not isinstance(ufcstats_id, str):
        raise ClassificationError(f"malformed ufcstats_bout_id: {ufcstats_id!r}")
    if isinstance(ufcstats_id, str) and not ufcstats_id.strip():
        ufcstats_id = None

    if status_raw in {"cancelled", "canceled"}:
        lifecycle = BoutLifecycle.CANCELLED
        category = BoutCategory.CANCELLED
    elif status_raw == "replacement":
        lifecycle = BoutLifecycle.REPLACEMENT
        category = BoutCategory.REPLACEMENT
    else:
        lifecycle = BoutLifecycle.COMPLETED
        category = (
            BoutCategory.COMPLETED_BRAZIL
            if series is SeriesVariant.BRAZIL
            else BoutCategory.COMPLETED_STANDARD
        )

    if ufcstats_id:
        provider = ProviderEnrichmentState.RESOLVED
    elif provider_blocked:
        provider = ProviderEnrichmentState.BLOCKED
    else:
        provider = ProviderEnrichmentState.UNMAPPED

    notes: list[str] = []
    if ufcstats_id is None:
        notes.append("ufcstats_bout_id_unmapped")
    if provider_blocked and ufcstats_id is None:
        notes.append("ufcstats_public_access_blocked_or_unmapped")

    return BoutClassification(
        category=category,
        series_variant=series,
        lifecycle=lifecycle,
        event_night_result=event_night,
        current_result=current,
        version_state=version_state,
        provider_enrichment=provider,
        ufcstats_bout_id=ufcstats_id,
        notes=tuple(notes),
    )


def classify_event_cancellation(entry: Mapping[str, Any]) -> BoutCategory:
    """Classify a cancellation/replacement ledger entry into one category."""
    kind = str(entry.get("kind") or entry.get("status") or "").strip().lower()
    if kind in {"cancelled", "canceled"}:
        return BoutCategory.CANCELLED
    if kind == "replacement":
        return BoutCategory.REPLACEMENT
    raise ClassificationError(f"unknown cancellation kind: {kind!r}")


def classify_mismatch_gap(gap: Mapping[str, Any]) -> BoutCategory:
    """Every open mismatch gap is categorized (never silently dropped)."""
    severity = str(gap.get("severity") or "").strip().lower()
    path = str(gap.get("path") or "").strip().lower()
    if not path:
        raise ClassificationError("mismatch gap missing path")
    if severity in {"incomplete_not_done", "mismatch", "open"} or path:
        return BoutCategory.MISMATCH_LEDGER_GAP
    raise ClassificationError(f"uncategorized mismatch gap: {gap!r}")
