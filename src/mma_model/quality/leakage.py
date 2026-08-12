"""Future-row, same-card, and mutable-current leakage checks (DWCS-106)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutResultVersion,
    CanonicalBout,
    CanonicalEvent,
    FighterProfileObservation,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.db.tables.provenance import IngestRun, RawObservation
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.schema import sha256_canonical
from mma_model.sources.policy import load_source_policy

FeatureFn = Callable[[datetime], Any]


class FutureRowLeakageError(AssertionError):
    """Raised when a later mutation changes a past cutoff classification or hash."""


def snapshot_for_cutoff(
    session: Session,
    *,
    cutoff: datetime,
    series: str = "dwcs",
    exclude_event_id: str | None = None,
) -> dict[str, Any]:
    report = compute_coverage_report(
        series=series,
        session=session,
        policy=load_source_policy(),
        as_of=cutoff,
        exclude_event_id=exclude_event_id,
    )
    return {
        "report_hash": report.report_hash,
        "core_tiers": dict(report.core_tiers),
        "bout_tiers": [(row.bout_id, row.overall_tier) for row in report.bouts],
        "as_of": report.as_of,
    }


def assert_future_row_invariance(
    feature_fn: FeatureFn,
    earlier_cutoff: datetime,
    later_mutation: Callable[[], None],
) -> None:
    before = feature_fn(earlier_cutoff)
    later_mutation()
    after = feature_fn(earlier_cutoff)
    if before != after:
        raise FutureRowLeakageError(
            "future-row mutation changed past coverage classification or hash"
        )


def coverage_hash_at(
    session: Session, cutoff: datetime, *, exclude_event_id: str | None = None
) -> str:
    snap = snapshot_for_cutoff(session, cutoff=cutoff, exclude_event_id=exclude_event_id)
    return sha256_canonical(snap)


def append_future_bout(
    session: Session,
    *,
    event_id: str,
    bout_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
    effective_at: datetime,
) -> None:
    if session.get(CanonicalEvent, event_id) is None:
        session.add(
            CanonicalEvent(
                id=event_id,
                name="Future non-DWCS card",
                series="future_card",
                status="scheduled",
                scheduled_start_at=effective_at,
                event_date=effective_at.date(),
            )
        )
        session.flush()
    if session.get(CanonicalBout, bout_id) is None:
        session.add(
            CanonicalBout(
                id=bout_id,
                event_id=event_id,
                fighter_a_id=fighter_a_id,
                fighter_b_id=fighter_b_id,
                status="completed",
            )
        )
        session.flush()
    session.add(
        BoutResultVersion(
            bout_id=bout_id,
            version_kind="event_night",
            revision=1,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            winner_fighter_id=fighter_a_id,
            result_type="decisive",
            effective_at=effective_at,
            observed_at=effective_at,
        )
    )


def append_correction(
    session: Session,
    *,
    bout_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
    effective_at: datetime,
    result_type: str = "no_contest",
    observed_at: datetime | None = None,
) -> None:
    existing = list(
        session.scalars(
            select(BoutResultVersion).where(
                BoutResultVersion.bout_id == bout_id,
                BoutResultVersion.version_kind == "current",
            )
        ).all()
    )
    revision = max((row.revision for row in existing), default=0) + 1
    seen_at = observed_at if observed_at is not None else effective_at
    session.add(
        BoutResultVersion(
            bout_id=bout_id,
            version_kind="current",
            revision=revision,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            winner_fighter_id=None,
            result_type=result_type,
            effective_at=effective_at,
            observed_at=seen_at,
        )
    )


def append_mutable_profile(
    session: Session,
    *,
    fighter_id: str,
    observed_at: datetime,
) -> None:
    session.add(
        FighterProfileObservation(
            fighter_id=fighter_id,
            attribute="record_wins",
            value_num=99.0,
            source="mutable_current",
            effective_at=observed_at,
            observed_at=observed_at,
        )
    )


def append_source_failure(
    session: Session,
    *,
    source: str,
    reason: str,
    observed_at: datetime,
) -> None:
    session.add(
        HistorySourceFailure(
            source=source,
            reason=reason,
            scope="future",
            subject="future-row",
            evidence_json="{}",
            observed_at=observed_at,
        )
    )


def append_future_observation(
    session: Session,
    *,
    bout_id: str,
    source: str,
    effective_at: datetime,
    result_type: str = "draw",
    timestamp_quality: str = "direct_source_timestamp",
    ingest_run_id: str | None = None,
) -> None:
    run_id = ingest_run_id
    if run_id is None:
        run = IngestRun(
            source=source,
            stream="history",
            scope="future-leak",
            status="succeeded",
        )
        session.add(run)
        session.flush()
        run_id = run.id
    session.add(
        RawObservation(
            ingest_run_id=run_id,
            source=source,
            stream="history",
            scope="future-leak",
            checkpoint_version="v-future",
            external_id=f"future-obs-{bout_id}-{source}",
            entity_kind="bout_result",
            observed_at=effective_at,
            effective_at=effective_at,
            source_published_at=effective_at,
            proxy_published_at=effective_at,
            timestamp_quality=timestamp_quality,
            quality_tier="gold",
            payload_hash="c" * 64,
            raw_ref=None,
            subject_id=bout_id,
            version_kind="event_night",
            attributes_json=('{"result_type":"%s","winner_fighter_id":null}' % result_type),
        )
    )


def append_future_history_bout(
    session: Session,
    *,
    fighter_id: str,
    effective_at: datetime,
    external_bout_id: str = "future-hist-1",
) -> None:
    session.add(
        HistorySourceBout(
            source="tapology_public",
            stream="fighter_history",
            external_bout_id=external_bout_id,
            fighter_source="tapology_public",
            fighter_external_id="future-fighter",
            fighter_name="Future Fighter",
            fighter_canonical_id=fighter_id,
            opponent_name="Future Opp",
            event_name="Future Card",
            classification="professional",
            result="win",
            version_kind="event_night",
            revision=1,
            observed_at=effective_at,
            effective_at=effective_at,
            payload_hash="d" * 64,
            identity_status="linked",
            is_current_record=1,
            observation_origin="live_public",
        )
    )


def card_cutoff(scheduled_start: datetime, *, minutes_before: int = 60) -> datetime:
    if scheduled_start.tzinfo is None:
        scheduled_start = scheduled_start.replace(tzinfo=timezone.utc)
    return scheduled_start - timedelta(minutes=minutes_before)


def same_card_feature_fn(session: Session, *, event_id: str) -> FeatureFn:
    def _fn(cutoff: datetime) -> dict[str, Any]:
        return snapshot_for_cutoff(session, cutoff=cutoff, exclude_event_id=event_id)

    return _fn
