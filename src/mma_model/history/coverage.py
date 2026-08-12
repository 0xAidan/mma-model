"""Coverage helpers: years filter, live source status, left-truncation (DWCS-105)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.history.constants import (
    SOURCE_COMBAT_REGISTRY,
    SOURCE_SHERDOG,
    SOURCE_TAPOLOGY,
)
from mma_model.history.identity import compute_identity_conflations

REGIONAL_SOURCES = (SOURCE_TAPOLOGY, SOURCE_SHERDOG, SOURCE_COMBAT_REGISTRY)
EXCLUDED_TRUNCATION_KINDS = frozenset({"current", "correction"})
EXCLUDED_TRUNCATION_STATUS = frozenset({"scheduled", "cancelled", "unknown"})


def bout_event_date(
    row: Mapping[str, Any],
    db_dates: Mapping[str, date | None],
) -> date | None:
    raw = row.get("event_date")
    if raw:
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        return date.fromisoformat(str(raw)[:10])
    bout_id = str(row.get("bout_id") or "")
    return db_dates.get(bout_id)


def year_in_range(event_date: date | None, years: range | None) -> bool:
    if years is None:
        return True
    if event_date is None:
        return False
    return event_date.year in years


def db_bout_dates(session: Session) -> dict[str, date | None]:
    rows = session.scalars(select(HistorySourceBout)).all()
    out: dict[str, date | None] = {}
    for row in rows:
        current = out.get(row.external_bout_id)
        if current is None and row.event_date is not None:
            out[row.external_bout_id] = row.event_date
    return out


def filter_sample_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    years: range | None,
    db_dates: Mapping[str, date | None],
    require_regulated_us: bool = False,
) -> list[dict[str, Any]]:
    eligible: list[dict[str, Any]] = []
    for row in rows:
        if require_regulated_us and str(row.get("regulated_us") or "") != "true":
            continue
        event_date = bout_event_date(row, db_dates)
        if not year_in_range(event_date, years):
            continue
        payload = dict(row)
        if event_date is not None:
            payload["event_date"] = event_date.isoformat()
        eligible.append(payload)
    return eligible


def live_source_coverage_map(
    session: Session,
    probes: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    failures = list(session.scalars(select(HistorySourceFailure)).all())
    failed_by_source: dict[str, list[HistorySourceFailure]] = {}
    for row in failures:
        failed_by_source.setdefault(row.source, []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for source in REGIONAL_SOURCES:
        probe = dict(probes.get(source) or {})
        result = probe.get("result")
        reason = probe.get("block_reason")
        db_fail = failed_by_source.get(source) or []
        if result == "BLOCKED" or reason in {"http_403", "login_wall", "http_redirect_refused"}:
            status = "source_killed"
        elif db_fail and any("kill" in (row.reason or "") or row.reason in {
            "http_403",
            "login_wall",
            "schema_drift",
        } for row in db_fail):
            status = "source_failed"
        elif result == "OK" and source == SOURCE_SHERDOG:
            status = "accessibility_only"
        elif result in {None, "NOT_RUN"}:
            status = "unmeasured"
        elif result == "OK":
            status = "unmeasured"
        else:
            status = "source_failed"
        if status == "source_killed" and source in {SOURCE_TAPOLOGY, SOURCE_COMBAT_REGISTRY}:
            # Frozen live probes: Tapology 403 and Combat login wall are killed.
            status = "source_killed"
        out[source] = {
            "status": status,
            "result": result,
            "reason": reason,
            "http_status": probe.get("http_status"),
            "path_category": probe.get("path_category"),
            "db_failure_reasons": tuple(sorted({row.reason for row in db_fail})),
        }
    return out


def evidence_class_for(session: Session) -> str:
    rows = list(session.scalars(select(HistorySourceBout)).all())
    if not rows:
        return "fixture_validation"
    origins = {getattr(row, "observation_origin", "unknown") or "unknown" for row in rows}
    live = "live_public" in origins
    synthetic = "synthetic_fixture" in origins
    if live and synthetic:
        return "mixed"
    if live:
        return "live_source_coverage"
    return "fixture_validation"


def left_truncated_history_count(session: Session) -> int:
    """Count truncated fighter histories/segments, not raw bout/version rows."""
    rows = list(session.scalars(select(HistorySourceBout)).all())
    histories: set[tuple[str, str]] = set()
    for row in rows:
        if int(row.left_truncated or 0) != 1:
            continue
        if row.version_kind in EXCLUDED_TRUNCATION_KINDS:
            continue
        if row.bout_status in EXCLUDED_TRUNCATION_STATUS:
            continue
        if row.result in {"unknown", "cancelled"}:
            continue
        if row.event_date is not None and row.event_date.year >= 2026:
            continue
        if getattr(row, "missing_reason", None) in {"invalid", "invalid_time"}:
            continue
        key = (row.source, row.fighter_canonical_id or row.fighter_external_id)
        histories.add(key)
    return len(histories)


def measured_identity_conflations(session: Session) -> int:
    return compute_identity_conflations(session)
