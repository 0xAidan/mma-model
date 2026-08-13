"""Load DWCS events for odds scheduling / backfill (DWCS-205)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import CanonicalEvent
from mma_model.dwcs.manifest import load_dwcs_event_manifest
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.schedule import load_default_schedule_contract

_DWCS_SERIES = frozenset({"dwcs", "dwcs_brazil"})
_UPCOMING_EVENT_STATUS = frozenset({"scheduled", "upcoming"})
_COMPLETED_EVENT_STATUS = frozenset({"completed", "complete"})
_CANCELLED_EVENT_STATUS = frozenset({"cancelled", "canceled"})


def _db_utc(value: datetime, *, field: str) -> datetime:
    """Normalize DB datetimes; SQLite may strip tzinfo for aware writes."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return ensure_utc(value, field=field)


class LiveScheduleEventsError(RuntimeError):
    """Raised when live scheduling cannot determine upcoming events."""


def _parse_occurrence(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text), field="occurrence_timestamp")


def load_dwcs_schedule_events(*, from_year: int | None = None) -> list[dict[str, Any]]:
    """Historical backfill only: frozen completed DWCS manifest rows."""
    rows: list[dict[str, Any]] = []
    for event in load_dwcs_event_manifest():
        start = _parse_occurrence(event.occurrence_timestamp)
        if from_year is not None and start.year < int(from_year):
            continue
        rows.append(
            {
                "event_id": event.event_id,
                "card_id": event.event_id,
                "event_start": start,
                "name": event.name,
                "calendar_year": event.calendar_year,
                "status": "completed",
                "source": "frozen_manifest",
            }
        )
    rows.sort(key=lambda item: (item["event_start"], item["event_id"]))
    return rows


def load_upcoming_dwcs_events_from_db(
    session: Session,
    *,
    as_of: datetime,
    horizon: timedelta | None = None,
    series: str = "dwcs",
) -> list[dict[str, Any]]:
    """Canonical upcoming/scheduled DWCS events for live due/job paths.

    Uses DB card state (including replacements / late ``scheduled_start_at``
    corrections). Frozen manifest is intentionally not consulted here.

    Events with ``scheduled_start_at < as_of`` are excluded on purpose: cadence
    windows are half-open ending at event start, so post-start ticks are already
    a deterministic no-op. Late schedule moves must update the canonical row;
    there is no stale-manifest fallback.
    """
    stamp = ensure_utc(as_of, field="as_of")
    sched = load_default_schedule_contract()
    if series not in _DWCS_SERIES and series != sched.series:
        raise LiveScheduleEventsError(f"unsupported series for live odds: {series!r}")

    # Default horizon: odds window open from T-72h through event start, plus
    # events that begin within the far-window look-ahead from as_of.
    if horizon is None:
        far = max(w.offset_before_event_start_sec for w in sched.cadence_windows)
        horizon = timedelta(seconds=far)

    upper = stamp + horizon
    series_filter = tuple(_DWCS_SERIES)
    events = session.scalars(
        select(CanonicalEvent)
        .where(CanonicalEvent.series.in_(series_filter))
        .where(CanonicalEvent.status.in_(tuple(_UPCOMING_EVENT_STATUS)))
        .where(CanonicalEvent.scheduled_start_at.is_not(None))
        .where(CanonicalEvent.scheduled_start_at >= stamp)
        .where(CanonicalEvent.scheduled_start_at <= upper)
        .order_by(CanonicalEvent.scheduled_start_at.asc(), CanonicalEvent.id.asc())
    ).all()

    rows: list[dict[str, Any]] = []
    for event in events:
        start = _db_utc(event.scheduled_start_at, field="scheduled_start_at")  # type: ignore[arg-type]
        rows.append(
            {
                "event_id": event.id,
                "card_id": event.id,
                "event_start": start,
                "name": event.name,
                "status": event.status,
                "series": event.series,
                "source": "canonical_db",
            }
        )
    return rows


def classify_event_status_for_tests(status: str) -> str:
    """Normalize status labels for upcoming/completed/cancelled test fixtures."""
    value = str(status).strip().lower()
    if value in _UPCOMING_EVENT_STATUS:
        return "upcoming"
    if value in _COMPLETED_EVENT_STATUS:
        return "completed"
    if value in _CANCELLED_EVENT_STATUS:
        return "cancelled"
    raise ValueError(f"unsupported event status for schedule tests: {status!r}")


__all__ = [
    "LiveScheduleEventsError",
    "classify_event_status_for_tests",
    "load_dwcs_schedule_events",
    "load_upcoming_dwcs_events_from_db",
]
