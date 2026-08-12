"""Manifest-first DWCS history sync into an explicit disposable database."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.tables.core import (
    BoutParticipant,
    BoutResultVersion,
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    EventSourceId,
    FighterSourceId,
)
from mma_model.dwcs.classification import (
    BoutCategory,
    BoutClassification,
    classify_bout,
    classify_event_cancellation,
    classify_mismatch_gap,
)
from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id, canonical_fighter_id
from mma_model.dwcs.manifest import (
    DEFAULT_BOUTS_PATH,
    DEFAULT_EVENTS_PATH,
    DEFAULT_EXPECTED_PATH,
    DEFAULT_MISMATCHES_PATH,
    DwcsBoutManifestRow,
    DwcsEventManifestRow,
    ManifestValidationError,
    load_dwcs_bout_manifest,
    load_dwcs_event_manifest,
    load_dwcs_mismatch_ledger,
    validate_expected_universe,
)
from mma_model.dwcs.winners import WinnerValidationError, resolve_version_winner
from mma_model.ingest.repository import IngestRepository
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.policy import SourceId
from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter

FailAt = str | None  # after_canonical | after_raw_observations | during_result_versions

SessionFactory = Callable[[], Session]
SOURCE_DWCS_MANIFEST = SourceId.DWCS_MANIFEST.value
SOURCE_ESPN = "espn"
CHECKPOINT_VERSION = "dwcs_manifest_v1"
STREAM_HISTORY = "history"
DEFAULT_SCHEDULED_ROUNDS = 3


@dataclass(frozen=True)
class SyncHistoryReport:
    cards: int
    bouts: int
    through_year: int
    dry_run: bool
    categories: Mapping[str, int]
    event_night_results: Mapping[str, int]
    current_results: Mapping[str, int]
    series_variants: Mapping[str, int]
    provider_enrichment: Mapping[str, int]
    inserted_observations: int
    skipped_identical: int
    conflicts: int
    batches_committed: int
    batches_failed: int
    canonical_events: int
    canonical_bouts: int
    canonical_fighters: int
    result_versions: int
    mismatch_gaps_categorized: int
    cancellations_replacements_categorized: int
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards": self.cards,
            "bouts": self.bouts,
            "through_year": self.through_year,
            "dry_run": self.dry_run,
            "categories": dict(self.categories),
            "event_night_results": dict(self.event_night_results),
            "current_results": dict(self.current_results),
            "series_variants": dict(self.series_variants),
            "provider_enrichment": dict(self.provider_enrichment),
            "inserted_observations": self.inserted_observations,
            "skipped_identical": self.skipped_identical,
            "conflicts": self.conflicts,
            "batches_committed": self.batches_committed,
            "batches_failed": self.batches_failed,
            "canonical_events": self.canonical_events,
            "canonical_bouts": self.canonical_bouts,
            "canonical_fighters": self.canonical_fighters,
            "result_versions": self.result_versions,
            "mismatch_gaps_categorized": self.mismatch_gaps_categorized,
            "cancellations_replacements_categorized": (
                self.cancellations_replacements_categorized
            ),
            "notes": list(self.notes),
        }

    def human_summary(self) -> str:
        return (
            f"cards={self.cards} bouts={self.bouts} "
            f"through={self.through_year} dry_run={self.dry_run} "
            f"conflicts={self.conflicts} "
            f"provider={dict(self.provider_enrichment)}"
        )


@dataclass
class _SyncState:
    category_counts: Counter[str] = field(default_factory=Counter)
    event_night_counts: Counter[str] = field(default_factory=Counter)
    current_counts: Counter[str] = field(default_factory=Counter)
    series_counts: Counter[str] = field(default_factory=Counter)
    provider_counts: Counter[str] = field(default_factory=Counter)
    inserted: int = 0
    skipped: int = 0
    conflicts: int = 0
    batches_committed: int = 0
    batches_failed: int = 0
    notes: list[str] = field(default_factory=list)


def _parse_ts(value: str | None) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _payload_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ordered_participants(
    row: DwcsBoutManifestRow,
) -> tuple[Any, Any]:
    # Deterministic corner assignment: ascending espn athlete id → A, other → B.
    parts = sorted(row.participants, key=lambda p: p.espn_athlete_id)
    return parts[0], parts[1]


def _participant_maps(row: DwcsBoutManifestRow) -> list[dict[str, Any]]:
    return [
        {
            "espn_athlete_id": p.espn_athlete_id,
            "current_winner_flag": p.current_winner_flag,
            "display_name": p.display_name,
        }
        for p in row.participants
    ]


def _ensure_fighter(
    session: Session,
    *,
    espn_athlete_id: str,
    display_name: str,
) -> str:
    fighter_id = canonical_fighter_id(espn_athlete_id)
    existing = session.get(CanonicalFighter, fighter_id)
    if existing is None:
        session.add(CanonicalFighter(id=fighter_id, display_name=display_name))
    mapping = session.scalars(
        select(FighterSourceId).where(
            FighterSourceId.source == SOURCE_ESPN,
            FighterSourceId.external_id == espn_athlete_id,
        )
    ).first()
    if mapping is None:
        session.add(
            FighterSourceId(
                fighter_id=fighter_id,
                source=SOURCE_ESPN,
                external_id=espn_athlete_id,
            )
        )
    elif mapping.fighter_id != fighter_id:
        raise ManifestValidationError(
            f"espn fighter id {espn_athlete_id} already mapped to {mapping.fighter_id}"
        )
    return fighter_id


def _ensure_event(session: Session, event: DwcsEventManifestRow) -> str:
    event_uuid = canonical_event_id(event.espn_event_id)
    occurred = _parse_ts(event.occurrence_timestamp)
    event_date: date | None = occurred.date() if occurred else None
    existing = session.get(CanonicalEvent, event_uuid)
    if existing is None:
        status = "completed" if event.status in {"completed", "occurred"} else event.status
        session.add(
            CanonicalEvent(
                id=event_uuid,
                name=event.name,
                series=f"dwcs_{event.series_variant}",
                status=status,
                scheduled_start_at=occurred,
                event_date=event_date,
                location=_format_venue(event.venue),
            )
        )
        session.flush()
    mapping = session.scalars(
        select(EventSourceId).where(
            EventSourceId.source == SOURCE_ESPN,
            EventSourceId.external_id == event.espn_event_id,
        )
    ).first()
    if mapping is None:
        session.add(
            EventSourceId(
                event_id=event_uuid,
                source=SOURCE_ESPN,
                external_id=event.espn_event_id,
            )
        )
    elif mapping.event_id != event_uuid:
        raise ManifestValidationError(
            f"espn event id {event.espn_event_id} already mapped to {mapping.event_id}"
        )
    return event_uuid


def _format_venue(venue: Mapping[str, Any] | None) -> str | None:
    if not venue:
        return None
    parts = [
        str(venue.get(key)).strip()
        for key in ("name", "city", "state", "country")
        if venue.get(key)
    ]
    return ", ".join(parts) if parts else None


def _ensure_bout(
    session: Session,
    *,
    row: DwcsBoutManifestRow,
    event_uuid: str,
    fighter_a_id: str,
    fighter_b_id: str,
) -> str:
    bout_uuid = canonical_bout_id(row.espn_competition_id)
    if fighter_a_id == fighter_b_id:
        raise ManifestValidationError("distinct fighters required")
    existing = session.get(CanonicalBout, bout_uuid)
    if existing is None:
        status = "completed" if row.status in {"occurred", "completed"} else row.status
        session.add(
            CanonicalBout(
                id=bout_uuid,
                event_id=event_uuid,
                fighter_a_id=fighter_a_id,
                fighter_b_id=fighter_b_id,
                scheduled_rounds=DEFAULT_SCHEDULED_ROUNDS,
                weight_class=row.weight_class,
                status=status,
            )
        )
        session.flush()
        session.add(
            BoutParticipant(bout_id=bout_uuid, fighter_id=fighter_a_id, corner="a")
        )
        session.add(
            BoutParticipant(bout_id=bout_uuid, fighter_id=fighter_b_id, corner="b")
        )
        session.flush()
    mapping = session.scalars(
        select(BoutSourceId).where(
            BoutSourceId.source == SOURCE_ESPN,
            BoutSourceId.external_id == row.espn_competition_id,
        )
    ).first()
    if mapping is None:
        session.add(
            BoutSourceId(
                bout_id=bout_uuid,
                source=SOURCE_ESPN,
                external_id=row.espn_competition_id,
            )
        )
    elif mapping.bout_id != bout_uuid:
        raise ManifestValidationError(
            f"espn competition id {row.espn_competition_id} already mapped to "
            f"{mapping.bout_id}"
        )
    return bout_uuid


def _build_result_observation(
    *,
    row: DwcsBoutManifestRow,
    bout_uuid: str,
    version_kind: str,
    fighter_a_id: str,
    fighter_b_id: str,
    winner_fighter_id: str | None,
    result_type: str,
    effective_at: datetime,
    observed_at: datetime,
    proxy_published_at: datetime | None,
    classification: BoutClassification,
    duration_status: str,
) -> SourceObservationRecord:
    payload = {
        "bout_id": row.bout_id,
        "espn_competition_id": row.espn_competition_id,
        "version_kind": version_kind,
        "result_type": result_type,
        "winner_fighter_id": winner_fighter_id,
        "fighter_a_id": fighter_a_id,
        "fighter_b_id": fighter_b_id,
        "version_state": row.version_state,
        "manifest_version": row.manifest_version,
    }
    digest = _payload_hash(payload)
    attrs: dict[str, Any] = {
        "fighter_a_id": fighter_a_id,
        "fighter_b_id": fighter_b_id,
        "winner_fighter_id": winner_fighter_id,
        "result_type": result_type,
        "method": None,
        "ending_round": None,
        "time_str": None,
        "elapsed_seconds_status": duration_status,
        "series_variant": row.series_variant,
        "version_state": row.version_state,
        "category": classification.category.value,
        "provider_enrichment": classification.provider_enrichment.value,
        "espn_competition_id": row.espn_competition_id,
        "espn_event_id": row.espn_event_id,
        "manifest_bout_id": row.bout_id,
    }
    quality_tier = "silver" if proxy_published_at is not None else "bronze"
    timestamp_quality = (
        "publication_proxy" if proxy_published_at is not None else "unknown"
    )
    return SourceObservationRecord(
        source=SOURCE_DWCS_MANIFEST,
        stream=STREAM_HISTORY,
        external_id=f"{row.espn_competition_id}:{version_kind}",
        entity_kind="bout_result",
        observed_at=observed_at,
        effective_at=effective_at,
        source_published_at=None,
        source_updated_at=None,
        proxy_published_at=proxy_published_at,
        timestamp_quality=timestamp_quality,
        timestamp_quality_source=SOURCE_DWCS_MANIFEST,
        quality_tier=quality_tier,
        payload_hash=digest,
        raw_ref=None,
        raw_blob_absent=True,
        detail_level=DetailLevel.SUMMARY,
        version_kind=version_kind,
        schema_version="1",
        subject_id=bout_uuid,
        attributes=attrs,
    )


def _conflict_observation(
    *,
    bout_uuid: str,
    espn_competition_id: str,
    evidence: Mapping[str, Any],
    observed_at: datetime,
    effective_at: datetime,
) -> SourceObservationRecord:
    payload = {
        "kind": "participant_or_result_disagreement",
        "espn_competition_id": espn_competition_id,
        "evidence": dict(evidence),
    }
    digest = _payload_hash(payload)
    return SourceObservationRecord(
        source=SOURCE_DWCS_MANIFEST,
        stream="conflicts",
        external_id=f"{espn_competition_id}:conflict:{digest[:16]}",
        entity_kind="conflict",
        observed_at=observed_at,
        effective_at=effective_at,
        source_published_at=None,
        source_updated_at=None,
        proxy_published_at=None,
        timestamp_quality="unknown",
        timestamp_quality_source=SOURCE_DWCS_MANIFEST,
        quality_tier="conflict",
        payload_hash=digest,
        raw_ref=None,
        raw_blob_absent=True,
        detail_level=DetailLevel.SUMMARY,
        version_kind=None,
        schema_version="1",
        subject_id=bout_uuid,
        attributes={
            "conflict_kind": "participant_or_result_disagreement",
            "evidence": dict(evidence),
            "espn_competition_id": espn_competition_id,
        },
    )


def detect_provider_disagreement(
    *,
    row: DwcsBoutManifestRow,
    provider_participant_espn_ids: Sequence[str] | None,
    provider_result_class: str | None,
) -> Mapping[str, Any] | None:
    """Fail closed: any participant/result disagreement becomes conflict evidence."""
    if provider_participant_espn_ids is None and provider_result_class is None:
        return None
    manifest_ids = sorted(p.espn_athlete_id for p in row.participants)
    evidence: dict[str, Any] = {}
    if provider_participant_espn_ids is not None:
        provider_ids = sorted(str(x) for x in provider_participant_espn_ids)
        if provider_ids != manifest_ids:
            evidence["participant_disagreement"] = {
                "manifest": manifest_ids,
                "provider": provider_ids,
            }
    if provider_result_class is not None:
        current = row.current_result.class_
        if provider_result_class != current:
            evidence["result_disagreement"] = {
                "manifest_current": current,
                "provider": provider_result_class,
            }
    return evidence or None


def sync_dwcs_history(
    *,
    through_year: int,
    repo: IngestRepository,
    session_factory: sessionmaker[Session] | SessionFactory,
    adapter: UfcstatsPublicAdapter | None = None,
    events_path: Path | None = None,
    bouts_path: Path | None = None,
    mismatches_path: Path | None = None,
    expected_universe_path: Path | None = None,
    dry_run: bool = False,
    observed_at: datetime | None = None,
    provider_blocked: bool = True,
    provider_overlays: Mapping[str, Mapping[str, Any]] | None = None,
    fail_after_batches: int | None = None,
    fail_at: FailAt = None,
    fail_on_batch: int | None = None,
) -> SyncHistoryReport:
    """Ingest frozen DWCS manifests before any provider facts.

    Adapter/provider facts are optional overlays. When UFCStats IDs are unmapped
    or public access is blocked, enrichment is classified unresolved/blocked
    without requiring a live adapter or inventing mappings.
    """
    if through_year < 2017:
        raise ManifestValidationError(f"through_year too early: {through_year}")

    events = load_dwcs_event_manifest(events_path or DEFAULT_EVENTS_PATH)
    bouts = load_dwcs_bout_manifest(bouts_path or DEFAULT_BOUTS_PATH)
    validate_expected_universe(
        events=events,
        bouts=bouts,
        expected_path=expected_universe_path or DEFAULT_EXPECTED_PATH,
    )
    ledger = load_dwcs_mismatch_ledger(mismatches_path or DEFAULT_MISMATCHES_PATH)

    # Filter by through_year after full-universe validation.
    events = [e for e in events if e.calendar_year <= through_year]
    bouts = [b for b in bouts if b.calendar_year <= through_year]
    events = sorted(events, key=lambda e: (e.calendar_year, e.espn_event_id))
    bouts = sorted(bouts, key=lambda b: (b.calendar_year, b.espn_competition_id))

    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware UTC")
    observed = observed.astimezone(timezone.utc)

    state = _SyncState()

    # Categorize mismatch ledger gaps (never silent).
    for gap in ledger.open_gaps:
        cat = classify_mismatch_gap(gap)
        state.category_counts[cat.value] += 1

    cancellation_count = 0
    for event in events:
        for entry in event.cancellations_replacements:
            cat = classify_event_cancellation(entry)
            state.category_counts[cat.value] += 1
            cancellation_count += 1

    def _classify_bout_row(bout: DwcsBoutManifestRow) -> BoutClassification:
        classification = classify_bout(
            bout.model_dump(by_alias=True),
            provider_blocked=provider_blocked,
        )
        state.category_counts[classification.category.value] += 1
        state.series_counts[classification.series_variant.value] += 1
        state.event_night_counts[classification.event_night_result.value] += 1
        state.current_counts[classification.current_result.value] += 1
        state.provider_counts[classification.provider_enrichment.value] += 1
        return classification

    if dry_run:
        for bout in bouts:
            _classify_bout_row(bout)
        if adapter is not None:
            state.notes.append("adapter_ignored_in_dry_run_no_network")
        return SyncHistoryReport(
            cards=len(events),
            bouts=len(bouts),
            through_year=through_year,
            dry_run=True,
            categories=dict(state.category_counts),
            event_night_results=dict(state.event_night_counts),
            current_results=dict(state.current_counts),
            series_variants=dict(state.series_counts),
            provider_enrichment=dict(state.provider_counts),
            inserted_observations=0,
            skipped_identical=0,
            conflicts=0,
            batches_committed=0,
            batches_failed=0,
            canonical_events=0,
            canonical_bouts=0,
            canonical_fighters=0,
            result_versions=0,
            mismatch_gaps_categorized=len(ledger.open_gaps),
            cancellations_replacements_categorized=cancellation_count,
            notes=tuple(state.notes),
        )

    run = repo.start_run(
        source=SOURCE_DWCS_MANIFEST,
        stream=STREAM_HISTORY,
        scope=f"dwcs:{through_year}",
    )

    bouts_by_event: dict[str, list[DwcsBoutManifestRow]] = {}
    for bout in bouts:
        bouts_by_event.setdefault(bout.espn_event_id, []).append(bout)

    try:
        for batch_idx, event in enumerate(events, start=1):
            if fail_after_batches is not None and batch_idx > fail_after_batches:
                state.batches_failed += 1
                raise RuntimeError("injected_batch_failure")

            event_bouts = bouts_by_event.get(event.espn_event_id, [])
            observations: list[SourceObservationRecord] = []
            inject_here = fail_on_batch is not None and batch_idx == fail_on_batch

            with session_factory() as session:
                repo.begin_owned_batch(session)
                try:
                    event_uuid = _ensure_event(session, event)
                    for bout in event_bouts:
                        classification = _classify_bout_row(bout)
                        duration = derive_elapsed_seconds(
                            ending_round=None,
                            time_str=None,
                            scheduled_rounds=DEFAULT_SCHEDULED_ROUNDS,
                        )
                        if duration.status is not DurationStatus.MISSING:
                            raise ManifestValidationError(
                                "manifest rows must not invent duration detail"
                            )

                        part_a, part_b = _ordered_participants(bout)
                        fighter_a_id = _ensure_fighter(
                            session,
                            espn_athlete_id=part_a.espn_athlete_id,
                            display_name=part_a.display_name,
                        )
                        fighter_b_id = _ensure_fighter(
                            session,
                            espn_athlete_id=part_b.espn_athlete_id,
                            display_name=part_b.display_name,
                        )
                        session.flush()
                        bout_uuid = _ensure_bout(
                            session,
                            row=bout,
                            event_uuid=event_uuid,
                            fighter_a_id=fighter_a_id,
                            fighter_b_id=fighter_b_id,
                        )

                        effective_night = _parse_ts(bout.occurrence_timestamp)
                        if effective_night is None:
                            raise ManifestValidationError(
                                f"missing occurrence_timestamp for {bout.bout_id}"
                            )
                        proxy_published_at = effective_night + timedelta(days=1)
                        fighter_by_espn = {
                            part_a.espn_athlete_id: fighter_a_id,
                            part_b.espn_athlete_id: fighter_b_id,
                        }
                        participant_maps = _participant_maps(bout)

                        en_winner_id: str | None = None
                        cur_winner_id: str | None = None
                        try:
                            en_res = resolve_version_winner(
                                version_kind="event_night",
                                result_class=bout.event_night_result.class_,
                                winner_espn_athlete_id=(
                                    bout.event_night_result.winner_espn_athlete_id
                                ),
                                participants=participant_maps,
                                fighter_id_by_espn=fighter_by_espn,
                            )
                            en_winner_id = en_res.winner_fighter_id
                            cur_res = resolve_version_winner(
                                version_kind="current",
                                result_class=bout.current_result.class_,
                                winner_espn_athlete_id=(
                                    bout.current_result.winner_espn_athlete_id
                                ),
                                participants=participant_maps,
                                fighter_id_by_espn=fighter_by_espn,
                            )
                            cur_winner_id = cur_res.winner_fighter_id
                        except WinnerValidationError as winner_exc:
                            state.conflicts += 1
                            state.category_counts[BoutCategory.CONFLICT.value] += 1
                            observations.append(
                                _conflict_observation(
                                    bout_uuid=bout_uuid,
                                    espn_competition_id=bout.espn_competition_id,
                                    evidence=winner_exc.evidence,
                                    observed_at=observed,
                                    effective_at=effective_night,
                                )
                            )
                            continue

                        current_effective = effective_night
                        if bout.version_state == "reversed_to_no_contest":
                            current_effective = (
                                _parse_ts(bout.occurrence_end_timestamp)
                                or effective_night
                            )

                        observations.append(
                            _build_result_observation(
                                row=bout,
                                bout_uuid=bout_uuid,
                                version_kind="event_night",
                                fighter_a_id=fighter_a_id,
                                fighter_b_id=fighter_b_id,
                                winner_fighter_id=en_winner_id,
                                result_type=bout.event_night_result.class_,
                                effective_at=effective_night,
                                observed_at=observed,
                                proxy_published_at=proxy_published_at,
                                classification=classification,
                                duration_status=duration.status.value,
                            )
                        )
                        observations.append(
                            _build_result_observation(
                                row=bout,
                                bout_uuid=bout_uuid,
                                version_kind="current",
                                fighter_a_id=fighter_a_id,
                                fighter_b_id=fighter_b_id,
                                winner_fighter_id=cur_winner_id,
                                result_type=bout.current_result.class_,
                                effective_at=current_effective,
                                observed_at=observed,
                                proxy_published_at=proxy_published_at,
                                classification=classification,
                                duration_status=duration.status.value,
                            )
                        )

                        overlay = (provider_overlays or {}).get(bout.espn_competition_id)
                        if overlay:
                            evidence = detect_provider_disagreement(
                                row=bout,
                                provider_participant_espn_ids=overlay.get(
                                    "participant_espn_ids"
                                ),
                                provider_result_class=overlay.get("result_class"),
                            )
                            if evidence:
                                state.conflicts += 1
                                state.category_counts[BoutCategory.CONFLICT.value] += 1
                                observations.append(
                                    _conflict_observation(
                                        bout_uuid=bout_uuid,
                                        espn_competition_id=bout.espn_competition_id,
                                        evidence=evidence,
                                        observed_at=observed,
                                        effective_at=effective_night,
                                    )
                                )

                    if inject_here and fail_at == "after_canonical":
                        raise RuntimeError("injected_failure_after_canonical")

                    def _after_raw(obs: SourceObservationRecord) -> None:
                        if inject_here and fail_at == "after_raw_observations":
                            raise RuntimeError("injected_failure_after_raw_observations")

                    def _before_result(obs: SourceObservationRecord) -> None:
                        if inject_here and fail_at == "during_result_versions":
                            raise RuntimeError("injected_failure_during_result_versions")

                    # Single transaction: canonical entities + observations +
                    # result versions + checkpoint. No nested independent commits.
                    result = repo.apply_batch(
                        session,
                        run_id=run.id,
                        observations=observations,
                        checkpoint_token=f"event:{event.espn_event_id}",
                        checkpoint_version=CHECKPOINT_VERSION,
                        on_after_raw_observation=_after_raw if inject_here else None,
                        on_before_result_version=_before_result if inject_here else None,
                    )
                    session.commit()
                    state.inserted += result.inserted
                    state.skipped += result.skipped_identical
                    state.batches_committed += 1
                except Exception:
                    session.rollback()
                    if inject_here and fail_at is not None:
                        state.batches_failed += 1
                        raise
                    raise
                finally:
                    repo.end_owned_batch(session)

        if adapter is not None:
            state.notes.append(
                "adapter_present_but_ufcstats_ids_unmapped_or_blocked;"
                "provider_enrichment_classified_without_live_fetch"
            )

        repo.finish_run(run.id, status="succeeded")
    except Exception as exc:
        repo.finish_run(
            run.id,
            status="failed",
            error_class=type(exc).__name__,
            error_message=str(exc)[:500],
        )
        if fail_after_batches is None and fail_at is None:
            raise
        state.notes.append(f"partial_failure_preserved_prior_batches:{exc}")

    with session_factory() as session:
        canonical_events = list(session.scalars(select(CanonicalEvent)).all())
        canonical_bouts = list(session.scalars(select(CanonicalBout)).all())
        canonical_fighters = list(session.scalars(select(CanonicalFighter)).all())
        result_versions = list(session.scalars(select(BoutResultVersion)).all())

    return SyncHistoryReport(
        cards=len(events),
        bouts=len(bouts),
        through_year=through_year,
        dry_run=False,
        categories=dict(state.category_counts),
        event_night_results=dict(state.event_night_counts),
        current_results=dict(state.current_counts),
        series_variants=dict(state.series_counts),
        provider_enrichment=dict(state.provider_counts),
        inserted_observations=state.inserted,
        skipped_identical=state.skipped,
        conflicts=state.conflicts,
        batches_committed=state.batches_committed,
        batches_failed=state.batches_failed,
        canonical_events=len(canonical_events),
        canonical_bouts=len(canonical_bouts),
        canonical_fighters=len(canonical_fighters),
        result_versions=len(result_versions),
        mismatch_gaps_categorized=len(ledger.open_gaps),
        cancellations_replacements_categorized=cancellation_count,
        notes=tuple(state.notes),
    )


# Re-export for callers
__all__ = [
    "SyncHistoryReport",
    "detect_provider_disagreement",
    "sync_dwcs_history",
]
