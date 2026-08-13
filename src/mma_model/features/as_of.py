"""Point-in-time cutoffs and observation admission (DWCS-301).

Official cutoff is scheduled start minus 60 minutes (evaluation contract).
All bouts on one event share one cutoff. Missing start times fail closed or
emit an explicit ``proxy_scheduled_start`` label — never a silent close.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Never, Protocol

from mma_model.evaluation.contract import (
    load_evaluation_contract,
    mutable_fact_allowed_at_cutoff,
)


class FeatureCutoffError(ValueError):
    """Invalid or missing cutoff construction."""


class CutoffMismatchError(FeatureCutoffError):
    """Two bouts on the same event would receive different cutoffs."""


class CutoffKind(StrEnum):
    SCHEDULED_MINUS_60M = "scheduled_minus_60m"
    PROXY_SCHEDULED_START = "proxy_scheduled_start"


@dataclass(frozen=True)
class AsOfCutoff:
    """Shared card-level prediction cutoff."""

    event_id: str
    cutoff: datetime
    cutoff_kind: CutoffKind

    def __post_init__(self) -> None:
        object.__setattr__(self, "cutoff", ensure_utc(self.cutoff))


class EventCutoffSource(Protocol):
    event_id: str
    scheduled_start_at: datetime | None
    event_date: date | None


def ensure_utc(value: datetime) -> datetime:
    """Require a datetime; naive values are treated as UTC (SQLite round-trip)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def prediction_cutoff_minutes() -> int:
    contract = load_evaluation_contract()
    return int(contract.point_in_time.prediction_cutoff_minutes_before_scheduled_start)


def _require_cutoff_kind(kind: CutoffKind) -> None:
    if kind is CutoffKind.SCHEDULED_MINUS_60M:
        return
    if kind is CutoffKind.PROXY_SCHEDULED_START:
        return
    never_kind: Never = kind
    raise FeatureCutoffError(f"unhandled cutoff kind: {never_kind!r}")


def event_start_datetime(
    *,
    scheduled_start_at: datetime | None,
    event_date: date | None,
) -> datetime | None:
    if scheduled_start_at is not None:
        return ensure_utc(scheduled_start_at)
    if event_date is not None:
        return datetime.combine(event_date, time.min, tzinfo=timezone.utc)
    return None


def implied_event_start(cutoff: AsOfCutoff) -> datetime:
    """Recover the event start implied by a constructed cutoff."""
    _require_cutoff_kind(cutoff.cutoff_kind)
    if cutoff.cutoff_kind is CutoffKind.SCHEDULED_MINUS_60M:
        return cutoff.cutoff + timedelta(minutes=prediction_cutoff_minutes())
    if cutoff.cutoff_kind is CutoffKind.PROXY_SCHEDULED_START:
        return cutoff.cutoff
    never_kind: Never = cutoff.cutoff_kind
    raise FeatureCutoffError(f"unhandled cutoff kind: {never_kind!r}")


def cutoff_for_event(
    event: EventCutoffSource,
    *,
    allow_proxy: bool = False,
) -> AsOfCutoff:
    """Build the contract cutoff, or a labeled proxy when start is missing."""
    if event.scheduled_start_at is not None:
        start = ensure_utc(event.scheduled_start_at)
        minutes = prediction_cutoff_minutes()
        return AsOfCutoff(
            event_id=event.event_id,
            cutoff=start - timedelta(minutes=minutes),
            cutoff_kind=CutoffKind.SCHEDULED_MINUS_60M,
        )
    if allow_proxy and event.event_date is not None:
        proxy_start = datetime.combine(event.event_date, time.min, tzinfo=timezone.utc)
        return AsOfCutoff(
            event_id=event.event_id,
            cutoff=proxy_start,
            cutoff_kind=CutoffKind.PROXY_SCHEDULED_START,
        )
    raise FeatureCutoffError(
        f"event {event.event_id} has no scheduled_start_at; "
        "refusing to invent a close (pass allow_proxy with event_date for a labeled proxy)"
    )


def observation_admitted(
    *,
    effective_at: datetime,
    observed_at: datetime,
    cutoff: AsOfCutoff,
    bout_event_id: str | None = None,
) -> bool:
    """True iff the fact is visible at cutoff and is not a same-card result."""
    if bout_event_id is not None and bout_event_id == cutoff.event_id:
        return False
    return mutable_fact_allowed_at_cutoff(
        effective_at=ensure_utc(effective_at),
        observed_at=ensure_utc(observed_at),
        cutoff=cutoff.cutoff,
    )


def assert_identical_event_cutoffs(cutoffs: Sequence[AsOfCutoff]) -> None:
    """Hard-fail when two bouts on the same event would get different cutoffs."""
    seen: dict[str, AsOfCutoff] = {}
    for item in cutoffs:
        previous = seen.get(item.event_id)
        if previous is not None and previous != item:
            raise CutoffMismatchError(
                f"bouts on event {item.event_id} received different cutoffs: "
                f"{previous!r} vs {item!r}"
            )
        seen[item.event_id] = item


class EventCutoffRegistry:
    """Tracks the single cutoff used for each event during a build session."""

    def __init__(self) -> None:
        self._by_event: dict[str, AsOfCutoff] = {}

    def register(self, cutoff: AsOfCutoff) -> None:
        previous = self._by_event.get(cutoff.event_id)
        if previous is not None and previous != cutoff:
            raise CutoffMismatchError(
                f"bouts on event {cutoff.event_id} received different cutoffs: "
                f"{previous!r} vs {cutoff!r}"
            )
        self._by_event[cutoff.event_id] = cutoff

    def get(self, event_id: str) -> AsOfCutoff | None:
        return self._by_event.get(event_id)
