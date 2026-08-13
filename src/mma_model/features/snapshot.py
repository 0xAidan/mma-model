"""Typed in-memory PIT snapshot for cutoff-aware features (DWCS-301).

Tests and the builder operate on this snapshot so they do not need a full
production database. Optional SQLAlchemy loaders copy canonical tables into
the same types. Future rows can be appended in memory without mutating disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutResultVersion,
    CanonicalBout,
    CanonicalEvent,
    FighterProfileObservation,
    FighterStatObservation,
)
from mma_model.db.tables.history import HistorySourceBout
from mma_model.features.as_of import ensure_utc
from mma_model.labels.outcomes import ResultClass, ResultVersion, VersionKind, WinnerSide


STAT_SIG_STR_LANDED = "sig_str_landed"
STAT_SIG_STR_ATTEMPTED = "sig_str_attempted"
STAT_TD_LANDED = "td_landed"
STAT_TD_ATTEMPTED = "td_attempted"
STAT_SUB_ATT = "sub_att"
STAT_CTRL_SECONDS = "ctrl_seconds"

KNOWN_STAT_KEYS: frozenset[str] = frozenset(
    {
        STAT_SIG_STR_LANDED,
        STAT_SIG_STR_ATTEMPTED,
        STAT_TD_LANDED,
        STAT_TD_ATTEMPTED,
        STAT_SUB_ATT,
        STAT_CTRL_SECONDS,
    }
)


@dataclass(frozen=True)
class SnapshotEvent:
    event_id: str
    scheduled_start_at: datetime | None
    event_date: date | None
    series: str | None = None
    name: str = ""


@dataclass(frozen=True)
class SnapshotBout:
    bout_id: str
    event_id: str
    fighter_a_id: str
    fighter_b_id: str
    scheduled_rounds: int = 3
    weight_class: str | None = None
    status: str = "scheduled"


@dataclass(frozen=True)
class SnapshotResultVersion:
    bout_id: str
    version_kind: str
    revision: int
    fighter_a_id: str
    fighter_b_id: str
    winner_fighter_id: str | None
    result_type: str | None
    method: str | None
    ending_round: int | None
    time_str: str | None
    effective_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class SnapshotProfileObservation:
    fighter_id: str
    attribute: str
    value_text: str | None
    value_num: float | None
    value_date: date | None
    source: str
    effective_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class SnapshotStatObservation:
    fighter_id: str
    bout_id: str
    stat_key: str
    value_num: float | None
    effective_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class SnapshotHistoryBout:
    fighter_id: str
    opponent_id: str | None
    event_date: date | None
    event_name: str | None
    classification: str
    result: str
    method: str | None
    ending_round: int | None
    time_str: str | None
    elapsed_seconds: int | None
    scheduled_rounds: int | None
    promotion: str | None
    version_kind: str
    revision: int
    effective_at: datetime
    observed_at: datetime
    bout_status: str = "completed"
    external_bout_id: str = ""


@dataclass
class FeatureSnapshot:
    """Mutable bag of PIT facts. The builder never writes it; tests may append."""

    events: list[SnapshotEvent] = field(default_factory=list)
    bouts: list[SnapshotBout] = field(default_factory=list)
    result_versions: list[SnapshotResultVersion] = field(default_factory=list)
    profiles: list[SnapshotProfileObservation] = field(default_factory=list)
    stats: list[SnapshotStatObservation] = field(default_factory=list)
    history_bouts: list[SnapshotHistoryBout] = field(default_factory=list)

    def event_by_id(self, event_id: str) -> SnapshotEvent | None:
        for event in self.events:
            if event.event_id == event_id:
                return event
        return None

    def bout_by_id(self, bout_id: str) -> SnapshotBout | None:
        for bout in self.bouts:
            if bout.bout_id == bout_id:
                return bout
        return None

    def bouts_for_event(self, event_id: str) -> list[SnapshotBout]:
        return [bout for bout in self.bouts if bout.event_id == event_id]


def parse_version_kind(raw: str) -> VersionKind:
    if raw == VersionKind.EVENT_NIGHT.value:
        return VersionKind.EVENT_NIGHT
    if raw in {VersionKind.CURRENT.value, "correction"}:
        return VersionKind.CURRENT
    raise ValueError(f"unhandled result version_kind: {raw!r}")


def parse_result_class(raw: str | None) -> ResultClass | None:
    if raw is None or not str(raw).strip():
        return None
    key = str(raw).strip().lower()
    mapping = {
        "decisive": ResultClass.DECISIVE,
        "draw": ResultClass.DRAW,
        "no_contest": ResultClass.NO_CONTEST,
        "nc": ResultClass.NO_CONTEST,
        "overturned": ResultClass.OVERTURNED,
        "pending": ResultClass.PENDING,
        "unknown": ResultClass.UNKNOWN,
    }
    return mapping.get(key)


def winner_side_for(
    *,
    winner_fighter_id: str | None,
    fighter_a_id: str,
    fighter_b_id: str,
) -> WinnerSide | None:
    if winner_fighter_id is None:
        return None
    if winner_fighter_id == fighter_a_id:
        return WinnerSide.A
    if winner_fighter_id == fighter_b_id:
        return WinnerSide.B
    return None


def to_label_version(row: SnapshotResultVersion) -> ResultVersion:
    return ResultVersion(
        version_kind=parse_version_kind(row.version_kind),
        effective_at=ensure_utc(row.effective_at),
        observed_at=ensure_utc(row.observed_at),
        winner_side=winner_side_for(
            winner_fighter_id=row.winner_fighter_id,
            fighter_a_id=row.fighter_a_id,
            fighter_b_id=row.fighter_b_id,
        ),
        method_raw=row.method,
        result_class=parse_result_class(row.result_type),
        revision=row.revision,
    )


def snapshot_from_session(session: Session) -> FeatureSnapshot:
    """Copy canonical PIT tables into an in-memory snapshot (read-only)."""
    events = [
        SnapshotEvent(
            event_id=row.id,
            scheduled_start_at=row.scheduled_start_at,
            event_date=row.event_date,
            series=row.series,
            name=row.name,
        )
        for row in session.scalars(select(CanonicalEvent)).all()
    ]
    bouts = [
        SnapshotBout(
            bout_id=row.id,
            event_id=row.event_id,
            fighter_a_id=row.fighter_a_id,
            fighter_b_id=row.fighter_b_id,
            scheduled_rounds=int(row.scheduled_rounds or 3),
            weight_class=row.weight_class,
            status=row.status,
        )
        for row in session.scalars(select(CanonicalBout)).all()
    ]
    results = [
        SnapshotResultVersion(
            bout_id=row.bout_id,
            version_kind=row.version_kind,
            revision=int(row.revision),
            fighter_a_id=row.fighter_a_id,
            fighter_b_id=row.fighter_b_id,
            winner_fighter_id=row.winner_fighter_id,
            result_type=row.result_type,
            method=row.method,
            ending_round=row.ending_round,
            time_str=row.time_str,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
        for row in session.scalars(select(BoutResultVersion)).all()
    ]
    profiles = [
        SnapshotProfileObservation(
            fighter_id=row.fighter_id,
            attribute=row.attribute,
            value_text=row.value_text,
            value_num=row.value_num,
            value_date=row.value_date,
            source=row.source,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
        for row in session.scalars(select(FighterProfileObservation)).all()
    ]
    stats = [
        SnapshotStatObservation(
            fighter_id=row.fighter_id,
            bout_id=str(row.bout_id),
            stat_key=row.stat_key,
            value_num=row.value_num,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
        for row in session.scalars(select(FighterStatObservation)).all()
        if row.bout_id is not None
    ]
    history = [
        SnapshotHistoryBout(
            fighter_id=str(row.fighter_canonical_id),
            opponent_id=row.opponent_canonical_id,
            event_date=row.event_date,
            event_name=row.event_name,
            classification=row.classification,
            result=row.result,
            method=row.method,
            ending_round=row.ending_round,
            time_str=row.time_str,
            elapsed_seconds=row.elapsed_seconds,
            scheduled_rounds=row.scheduled_rounds,
            promotion=row.promotion,
            version_kind=row.version_kind,
            revision=int(row.revision),
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            bout_status=row.bout_status,
            external_bout_id=row.external_bout_id,
        )
        for row in session.scalars(select(HistorySourceBout)).all()
        if row.fighter_canonical_id is not None
    ]
    return FeatureSnapshot(
        events=events,
        bouts=bouts,
        result_versions=results,
        profiles=profiles,
        stats=stats,
        history_bouts=history,
    )
