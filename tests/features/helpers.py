"""Shared snapshot builders for DWCS-301 feature tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

from mma_model.features.as_of import cutoff_for_event
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotBout,
    SnapshotEvent,
    SnapshotHistoryBout,
    SnapshotProfileObservation,
    SnapshotResultVersion,
    SnapshotStatObservation,
)

UTC_TZ = UTC


def dt(year: int, month: int, day: int, hour: int = 2) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC_TZ)


def add_event(
    snapshot: FeatureSnapshot,
    event_id: str,
    start: datetime,
    *,
    name: str = "card",
) -> SnapshotEvent:
    event = SnapshotEvent(
        event_id=event_id,
        scheduled_start_at=start,
        event_date=start.date(),
        series="dwcs",
        name=name,
    )
    snapshot.events.append(event)
    return event


def add_bout(
    snapshot: FeatureSnapshot,
    bout_id: str,
    event_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
    *,
    scheduled_rounds: int | None = 3,
    weight_class: str | None = "lightweight",
) -> SnapshotBout:
    bout = SnapshotBout(
        bout_id=bout_id,
        event_id=event_id,
        fighter_a_id=fighter_a_id,
        fighter_b_id=fighter_b_id,
        scheduled_rounds=scheduled_rounds,
        weight_class=weight_class,
        status="completed",
    )
    snapshot.bouts.append(bout)
    return bout


def add_result(
    snapshot: FeatureSnapshot,
    bout: SnapshotBout,
    *,
    winner_id: str | None,
    method: str | None,
    result_type: str = "decisive",
    ending_round: int | None = 3,
    time_str: str | None = "5:00",
    effective_at: datetime,
    observed_at: datetime | None = None,
    version_kind: str = "event_night",
    revision: int = 1,
) -> SnapshotResultVersion:
    row = SnapshotResultVersion(
        bout_id=bout.bout_id,
        version_kind=version_kind,
        revision=revision,
        fighter_a_id=bout.fighter_a_id,
        fighter_b_id=bout.fighter_b_id,
        winner_fighter_id=winner_id,
        result_type=result_type,
        method=method,
        ending_round=ending_round,
        time_str=time_str,
        effective_at=effective_at,
        observed_at=observed_at if observed_at is not None else effective_at,
    )
    snapshot.result_versions.append(row)
    return row


def add_stat(
    snapshot: FeatureSnapshot,
    *,
    fighter_id: str,
    bout_id: str,
    key: str,
    value: float,
    at: datetime,
) -> None:
    snapshot.stats.append(
        SnapshotStatObservation(
            fighter_id=fighter_id,
            bout_id=bout_id,
            stat_key=key,
            value_num=value,
            effective_at=at,
            observed_at=at,
        )
    )


def add_strike_stats(
    snapshot: FeatureSnapshot,
    *,
    fighter_id: str,
    bout_id: str,
    landed: float,
    attempted: float,
    at: datetime,
    td_landed: float | None = None,
    td_attempted: float | None = None,
    sub_att: float | None = None,
    ctrl_seconds: float | None = None,
    opp_id: str | None = None,
    opp_landed: float | None = None,
    opp_td_landed: float | None = None,
    opp_td_attempted: float | None = None,
) -> None:
    add_stat(snapshot, fighter_id=fighter_id, bout_id=bout_id, key="sig_str_landed", value=landed, at=at)
    add_stat(
        snapshot,
        fighter_id=fighter_id,
        bout_id=bout_id,
        key="sig_str_attempted",
        value=attempted,
        at=at,
    )
    if td_landed is not None:
        add_stat(snapshot, fighter_id=fighter_id, bout_id=bout_id, key="td_landed", value=td_landed, at=at)
    if td_attempted is not None:
        add_stat(
            snapshot, fighter_id=fighter_id, bout_id=bout_id, key="td_attempted", value=td_attempted, at=at
        )
    if sub_att is not None:
        add_stat(snapshot, fighter_id=fighter_id, bout_id=bout_id, key="sub_att", value=sub_att, at=at)
    if ctrl_seconds is not None:
        add_stat(
            snapshot, fighter_id=fighter_id, bout_id=bout_id, key="ctrl_seconds", value=ctrl_seconds, at=at
        )
    if opp_id is not None and opp_landed is not None:
        add_stat(
            snapshot,
            fighter_id=opp_id,
            bout_id=bout_id,
            key="sig_str_landed",
            value=opp_landed,
            at=at,
        )
        add_stat(
            snapshot,
            fighter_id=opp_id,
            bout_id=bout_id,
            key="sig_str_attempted",
            value=opp_landed,
            at=at,
        )
    if opp_id is not None and opp_td_landed is not None:
        add_stat(
            snapshot, fighter_id=opp_id, bout_id=bout_id, key="td_landed", value=opp_td_landed, at=at
        )
    if opp_id is not None and opp_td_attempted is not None:
        add_stat(
            snapshot,
            fighter_id=opp_id,
            bout_id=bout_id,
            key="td_attempted",
            value=opp_td_attempted,
            at=at,
        )


def add_profile(
    snapshot: FeatureSnapshot,
    fighter_id: str,
    attribute: str,
    *,
    at: datetime,
    value_num: float | None = None,
    value_text: str | None = None,
    value_date: date | None = None,
) -> None:
    snapshot.profiles.append(
        SnapshotProfileObservation(
            fighter_id=fighter_id,
            attribute=attribute,
            value_text=value_text,
            value_num=value_num,
            value_date=value_date,
            source="ufcstats_public",
            effective_at=at,
            observed_at=at,
        )
    )


def add_history(
    snapshot: FeatureSnapshot,
    *,
    fighter_id: str,
    opponent_id: str | None,
    at: datetime,
    result: str = "win",
    method: str | None = "U-DEC",
    classification: str = "professional",
    external_bout_id: str = "hist-1",
    scheduled_rounds: int | None = 3,
    ending_round: int | None = 3,
    time_str: str | None = "5:00",
    version_kind: str = "event_night",
    revision: int = 1,
    event_date: date | None = None,
) -> None:
    snapshot.history_bouts.append(
        SnapshotHistoryBout(
            fighter_id=fighter_id,
            opponent_id=opponent_id,
            event_date=event_date if event_date is not None else at.date(),
            event_name="Regional",
            classification=classification,
            result=result,
            method=method,
            ending_round=ending_round,
            time_str=time_str,
            elapsed_seconds=None,
            scheduled_rounds=scheduled_rounds,
            promotion="regional",
            version_kind=version_kind,
            revision=revision,
            effective_at=at,
            observed_at=at,
            bout_status="completed",
            external_bout_id=external_bout_id,
        )
    )


def named(values: tuple[float, ...], names: tuple[str, ...]) -> dict[str, float]:
    return dict(zip(names, values, strict=True))


def cutoff_of(snapshot: FeatureSnapshot, event_id: str):
    event = snapshot.event_by_id(event_id)
    assert event is not None
    return cutoff_for_event(event)
