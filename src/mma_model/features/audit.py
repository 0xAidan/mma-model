"""CLI future-invariance audit for cutoff-aware features (DWCS-301)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from mma_model.features.as_of import AsOfCutoff, FeatureCutoffError, cutoff_for_event
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotBout,
    SnapshotEvent,
    SnapshotHistoryBout,
    SnapshotProfileObservation,
    SnapshotResultVersion,
    SnapshotStatObservation,
    snapshot_from_session,
)
from mma_model.quality.leakage import assert_future_row_invariance

FIGHTER_A = "fighter-a"
FIGHTER_B = "fighter-b"
FIGHTER_C = "fighter-c"
FIGHTER_D = "fighter-d"
EVENT_PRIOR = "event-prior"
EVENT_CARD = "event-card"
EVENT_FUTURE = "event-future"
BOUT_PRIOR = "bout-prior"
BOUT_MAIN = "bout-main"
BOUT_COMAIN = "bout-comain"
BOUT_FUTURE = "bout-future"

PRIOR_START = datetime(2018, 6, 1, 2, 0, tzinfo=UTC)
CARD_START = datetime(2019, 6, 1, 2, 0, tzinfo=UTC)
FUTURE_START = datetime(2021, 6, 1, 2, 0, tzinfo=UTC)


def build_audit_fixture() -> tuple[FeatureSnapshot, AsOfCutoff, str, str, str]:
    """Small two-card history plus a same-card undercard bout."""
    snapshot = FeatureSnapshot(
        events=[
            SnapshotEvent(
                event_id=EVENT_PRIOR,
                scheduled_start_at=PRIOR_START,
                event_date=PRIOR_START.date(),
                series="dwcs",
                name="Prior card",
            ),
            SnapshotEvent(
                event_id=EVENT_CARD,
                scheduled_start_at=CARD_START,
                event_date=CARD_START.date(),
                series="dwcs",
                name="Target card",
            ),
        ],
        bouts=[
            SnapshotBout(
                bout_id=BOUT_PRIOR,
                event_id=EVENT_PRIOR,
                fighter_a_id=FIGHTER_A,
                fighter_b_id=FIGHTER_C,
                scheduled_rounds=3,
                weight_class="lightweight",
                status="completed",
            ),
            SnapshotBout(
                bout_id=BOUT_MAIN,
                event_id=EVENT_CARD,
                fighter_a_id=FIGHTER_A,
                fighter_b_id=FIGHTER_B,
                scheduled_rounds=3,
                weight_class="lightweight",
                status="scheduled",
            ),
            SnapshotBout(
                bout_id=BOUT_COMAIN,
                event_id=EVENT_CARD,
                fighter_a_id=FIGHTER_C,
                fighter_b_id=FIGHTER_D,
                scheduled_rounds=3,
                weight_class="lightweight",
                status="scheduled",
            ),
        ],
        result_versions=[
            SnapshotResultVersion(
                bout_id=BOUT_PRIOR,
                version_kind="event_night",
                revision=1,
                fighter_a_id=FIGHTER_A,
                fighter_b_id=FIGHTER_C,
                winner_fighter_id=FIGHTER_A,
                result_type="decisive",
                method="KO/TKO",
                ending_round=1,
                time_str="1:10",
                effective_at=PRIOR_START + timedelta(hours=3),
                observed_at=PRIOR_START + timedelta(hours=3),
            ),
        ],
        profiles=[
            SnapshotProfileObservation(
                fighter_id=FIGHTER_A,
                attribute="reach",
                value_text=None,
                value_num=70.0,
                value_date=None,
                source="ufcstats_public",
                effective_at=PRIOR_START - timedelta(days=30),
                observed_at=PRIOR_START - timedelta(days=30),
            ),
        ],
        stats=[
            SnapshotStatObservation(
                fighter_id=FIGHTER_A,
                bout_id=BOUT_PRIOR,
                stat_key="sig_str_landed",
                value_num=10.0,
                effective_at=PRIOR_START + timedelta(hours=3),
                observed_at=PRIOR_START + timedelta(hours=3),
            ),
            SnapshotStatObservation(
                fighter_id=FIGHTER_A,
                bout_id=BOUT_PRIOR,
                stat_key="sig_str_attempted",
                value_num=20.0,
                effective_at=PRIOR_START + timedelta(hours=3),
                observed_at=PRIOR_START + timedelta(hours=3),
            ),
            SnapshotStatObservation(
                fighter_id=FIGHTER_C,
                bout_id=BOUT_PRIOR,
                stat_key="sig_str_landed",
                value_num=2.0,
                effective_at=PRIOR_START + timedelta(hours=3),
                observed_at=PRIOR_START + timedelta(hours=3),
            ),
        ],
        history_bouts=[
            SnapshotHistoryBout(
                fighter_id=FIGHTER_B,
                opponent_id="regional-opp",
                event_date=(PRIOR_START - timedelta(days=400)).date(),
                event_name="Regional show",
                classification="professional",
                result="win",
                method="SUB",
                ending_round=2,
                time_str="2:00",
                elapsed_seconds=420,
                scheduled_rounds=3,
                promotion="regional",
                version_kind="event_night",
                revision=1,
                effective_at=PRIOR_START - timedelta(days=400),
                observed_at=PRIOR_START - timedelta(days=400),
                bout_status="completed",
                external_bout_id="hist-b-1",
            ),
        ],
    )
    event = snapshot.event_by_id(EVENT_CARD)
    assert event is not None
    cutoff = cutoff_for_event(event)
    return snapshot, cutoff, BOUT_MAIN, FIGHTER_A, FIGHTER_B


def _append_future_facts(
    snapshot: FeatureSnapshot,
    *,
    fighter_a_id: str,
    fighter_b_id: str,
) -> None:
    snapshot.events.append(
        SnapshotEvent(
            event_id=EVENT_FUTURE,
            scheduled_start_at=FUTURE_START,
            event_date=FUTURE_START.date(),
            series="dwcs",
            name="Future card",
        )
    )
    snapshot.bouts.append(
        SnapshotBout(
            bout_id=BOUT_FUTURE,
            event_id=EVENT_FUTURE,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            scheduled_rounds=3,
            status="completed",
        )
    )
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id=BOUT_FUTURE,
            version_kind="event_night",
            revision=1,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            winner_fighter_id=fighter_b_id,
            result_type="decisive",
            method="SUB",
            ending_round=1,
            time_str="0:30",
            effective_at=FUTURE_START + timedelta(hours=3),
            observed_at=FUTURE_START + timedelta(hours=3),
        )
    )
    snapshot.profiles.append(
        SnapshotProfileObservation(
            fighter_id=fighter_a_id,
            attribute="reach",
            value_text=None,
            value_num=99.0,
            value_date=None,
            source="mutable_current",
            effective_at=FUTURE_START,
            observed_at=FUTURE_START,
        )
    )
    snapshot.history_bouts.append(
        SnapshotHistoryBout(
            fighter_id=fighter_b_id,
            opponent_id="future-opp",
            event_date=FUTURE_START.date(),
            event_name="Future regional",
            classification="professional",
            result="win",
            method="KO/TKO",
            ending_round=1,
            time_str="1:00",
            elapsed_seconds=60,
            scheduled_rounds=3,
            promotion="regional",
            version_kind="event_night",
            revision=1,
            effective_at=FUTURE_START,
            observed_at=FUTURE_START,
            bout_status="completed",
            external_bout_id="hist-future",
        )
    )


def run_future_invariance(
    snapshot: FeatureSnapshot,
    cutoff: AsOfCutoff,
    *,
    bout_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
) -> None:
    builder = FeatureBuilder(snapshot)

    def feature_fn(_cutoff_time: datetime) -> bytes:
        return builder.build(
            fighter_a_id,
            fighter_b_id,
            cutoff,
            bout_id=bout_id,
            use_cache=False,
        ).row_bytes

    assert_future_row_invariance(
        feature_fn,
        cutoff.cutoff,
        lambda: _append_future_facts(
            snapshot, fighter_a_id=fighter_a_id, fighter_b_id=fighter_b_id
        ),
    )


def run_features_audit(
    *,
    series: str,
    future_invariance: bool,
    session: Session | None = None,
) -> None:
    if series != "dwcs":
        raise ValueError(f"unsupported series: {series!r}")
    if not future_invariance:
        raise ValueError("features audit requires --future-invariance")
    if session is not None:
        snapshot = snapshot_from_session(session)
        target = _first_auditable_bout(snapshot)
        if target is None:
            raise ValueError(
                "no eligible bouts with a constructible cutoff in the provided database"
            )
        bout, cutoff = target
        bout_id = bout.bout_id
        fighter_a_id = bout.fighter_a_id
        fighter_b_id = bout.fighter_b_id
    else:
        snapshot, cutoff, bout_id, fighter_a_id, fighter_b_id = build_audit_fixture()
    run_future_invariance(
        snapshot,
        cutoff,
        bout_id=bout_id,
        fighter_a_id=fighter_a_id,
        fighter_b_id=fighter_b_id,
    )


def _first_auditable_bout(
    snapshot: FeatureSnapshot,
) -> tuple[SnapshotBout, AsOfCutoff] | None:
    for event in snapshot.events:
        try:
            cutoff = cutoff_for_event(event)
        except FeatureCutoffError:
            continue
        bouts = snapshot.bouts_for_event(event.event_id)
        if not bouts:
            continue
        return bouts[0], cutoff
    return None
