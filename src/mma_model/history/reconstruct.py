"""Point-in-time pre-fight reconstruction from prior effective bouts (DWCS-105)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.history import HistoryReconstruction, HistorySourceBout
from mma_model.history.constants import (
    RECONSTRUCTION_VERSION,
    RESULT_PRECEDENCE,
    UNRESOLVED_IDENTITY_STATUSES,
)
from mma_model.history.models import PreFightRecord

COMPLETED_STATUSES = frozenset({"completed"})
COUNTED_RESULTS = frozenset({"win", "loss", "draw", "nc"})


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat SQLite-naive timestamps as UTC so PIT comparisons stay valid."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bout_dedupe_key(row: HistorySourceBout) -> str:
    date_part = row.event_date.isoformat() if row.event_date is not None else "undated"
    opponent = (row.opponent_canonical_id or row.opponent_external_id or row.opponent_name or "")
    event = row.event_external_id or (row.event_name or "")
    return f"{date_part}|{opponent.strip().casefold()}|{event.strip().casefold()}"


def _precedence_rank(source: str) -> int:
    try:
        return RESULT_PRECEDENCE.index(source)
    except ValueError:
        return len(RESULT_PRECEDENCE) + 1


def _publication_time(row: HistorySourceBout) -> datetime | None:
    """Best allowed publication/source/proxy clock for PIT visibility."""
    return row.source_published_at or row.source_updated_at or row.proxy_published_at


def _select_visible_bouts(
    session: Session,
    *,
    fighter_id: str,
    cutoff: datetime,
) -> list[HistorySourceBout]:
    rows = session.scalars(
        select(HistorySourceBout).where(
            HistorySourceBout.fighter_canonical_id == fighter_id,
            HistorySourceBout.is_current_record == 0,
        )
    ).all()
    visible: list[HistorySourceBout] = []
    for row in rows:
        if row.identity_status in UNRESOLVED_IDENTITY_STATUSES:
            continue
        effective_at = _as_utc(row.effective_at)
        observed_at = _as_utc(row.observed_at)
        if effective_at is None or observed_at is None:
            continue
        if effective_at >= cutoff:
            continue
        if observed_at > cutoff:
            continue
        published = _as_utc(_publication_time(row))
        if published is not None and published > cutoff:
            continue
        visible.append(row)
    return visible


def _dedupe_for_reconstruction(
    rows: Sequence[HistorySourceBout],
) -> list[HistorySourceBout]:
    """Keep one row per bout key using source precedence; later revisions win."""
    chosen: dict[str, HistorySourceBout] = {}
    for row in sorted(
        rows,
        key=lambda r: (
            _bout_dedupe_key(r),
            _precedence_rank(r.source),
            r.version_kind != "event_night",
            -int(r.revision),
            r.source,
            r.external_bout_id,
        ),
    ):
        key = _bout_dedupe_key(row)
        if key not in chosen:
            chosen[key] = row
            continue
        current = chosen[key]
        better_source = _precedence_rank(row.source) < _precedence_rank(current.source)
        same_source_newer = (
            row.source == current.source
            and (
                (row.version_kind == current.version_kind and row.revision > current.revision)
                or (row.version_kind == "current" and current.version_kind == "event_night")
            )
        )
        if better_source or same_source_newer:
            chosen[key] = row
    return [chosen[key] for key in sorted(chosen)]


def reconstruct_pre_fight_record(
    *,
    fighter_id: str,
    cutoff: datetime,
    session: Session,
) -> PreFightRecord:
    """Rebuild W/L/D/NC and experience from prior effective bouts only.

    Current mutable profile records are ignored. Undated bouts cannot be proven
    to precede the cutoff and are excluded. Unknown classification stays
    unknown and is never coerced into professional/amateur counts.
    """
    cutoff_utc = _require_utc(cutoff, "cutoff")
    visible = _select_visible_bouts(session, fighter_id=fighter_id, cutoff=cutoff_utc)
    unresolved_rows = session.scalars(
        select(HistorySourceBout).where(
            HistorySourceBout.fighter_canonical_id == fighter_id,
            HistorySourceBout.is_current_record == 0,
            HistorySourceBout.identity_status.in_(tuple(UNRESOLVED_IDENTITY_STATUSES)),
        )
    ).all()
    blocked_n = 0
    for row in unresolved_rows:
        effective_at = _as_utc(row.effective_at)
        observed_at = _as_utc(row.observed_at)
        if effective_at is None or observed_at is None:
            continue
        if effective_at < cutoff_utc and observed_at <= cutoff_utc:
            blocked_n += 1

    undated = [row for row in visible if row.event_date is None]
    dated = [row for row in visible if row.event_date is not None]
    selected = _dedupe_for_reconstruction(dated)

    wins = losses = draws = ncs = unknown_results = 0
    pro = amateur = unknown_class = 0
    experience = 0
    cancelled_n = 0
    known_seconds = 0
    minutes_unknown = False

    for row in selected:
        if row.bout_status == "cancelled" or row.result == "cancelled":
            cancelled_n += 1
            continue
        if row.bout_status not in COMPLETED_STATUSES and row.bout_status != "replacement":
            continue
        experience += 1
        if row.classification == "professional":
            pro += 1
        elif row.classification == "amateur":
            amateur += 1
        else:
            unknown_class += 1
        if row.result == "win":
            wins += 1
        elif row.result == "loss":
            losses += 1
        elif row.result == "draw":
            draws += 1
        elif row.result == "nc":
            ncs += 1
        else:
            unknown_results += 1
        if row.elapsed_seconds is None:
            minutes_unknown = True
        else:
            known_seconds += int(row.elapsed_seconds)

    known_minutes: float | None
    if experience == 0:
        known_minutes = 0.0 if not minutes_unknown else None
        minutes_unknown = False
    elif minutes_unknown:
        known_minutes = None
    else:
        known_minutes = known_seconds / 60.0

    return PreFightRecord(
        fighter_id=fighter_id,
        cutoff=cutoff_utc,
        reconstruction_version=RECONSTRUCTION_VERSION,
        wins=wins,
        losses=losses,
        draws=draws,
        no_contests=ncs,
        professional_bouts=pro,
        amateur_bouts=amateur,
        unknown_class_bouts=unknown_class,
        experience_bouts=experience,
        known_minutes=known_minutes,
        minutes_unknown=minutes_unknown,
        undated_excluded=len(undated),
        cancelled_excluded=cancelled_n,
        blocked_identity_excluded=blocked_n,
        used_current_record=False,
        unknown_results=unknown_results,
        left_truncated=any(int(row.left_truncated or 0) == 1 for row in selected),
    )


def persist_reconstruction(
    session: Session,
    record: PreFightRecord,
) -> HistoryReconstruction:
    payload = record.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    cutoff_utc = _require_utc(record.cutoff, "cutoff")
    existing_rows = session.scalars(
        select(HistoryReconstruction).where(
            HistoryReconstruction.fighter_canonical_id == record.fighter_id,
            HistoryReconstruction.reconstruction_version == record.reconstruction_version,
        )
    ).all()
    existing = next(
        (row for row in existing_rows if _as_utc(row.cutoff) == cutoff_utc),
        None,
    )
    if existing is not None:
        if existing.payload_hash != digest:
            raise RuntimeError(
                "future_row_invariance_failure: reconstruction payload changed "
                f"for fighter={record.fighter_id} cutoff={record.cutoff.isoformat()}"
            )
        return existing
    row = HistoryReconstruction(
        fighter_canonical_id=record.fighter_id,
        cutoff=record.cutoff,
        reconstruction_version=record.reconstruction_version,
        payload_json=blob,
        payload_hash=digest,
    )
    session.add(row)
    session.flush()
    return row


def record_to_dict(record: PreFightRecord) -> dict[str, Any]:
    return record.model_dump(mode="json")
