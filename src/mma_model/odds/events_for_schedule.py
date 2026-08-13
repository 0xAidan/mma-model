"""Load DWCS event starts for odds scheduling / backfill (DWCS-205)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mma_model.dwcs.manifest import load_dwcs_event_manifest
from mma_model.odds.normalize import ensure_utc


def _parse_occurrence(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text), field="occurrence_timestamp")


def load_dwcs_schedule_events(*, from_year: int | None = None) -> list[dict[str, Any]]:
    """Return ``{event_id, card_id, event_start, name}`` from the frozen DWCS manifest."""
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
            }
        )
    rows.sort(key=lambda item: (item["event_start"], item["event_id"]))
    return rows


__all__ = ["load_dwcs_schedule_events"]
