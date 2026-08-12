"""Inventory counts, identity, PIT, raw-ref, and source-status for DWCS-106."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutParticipant,
    BoutResultVersion,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.db.tables.provenance import IngestRun, RawObservation, SourceCheckpoint
from mma_model.history.coverage import REGIONAL_SOURCES, _probe_coverage_status
from mma_model.identity.audit import build_identity_audit
from mma_model.quality.classify import observation_visible, parse_iso_datetime
from mma_model.quality.constants import (
    KILL_REASONS,
    PHASE1_BOUT_SOURCES,
    REGIONAL_LIVE_PROBE_PATH,
    SCHEMA_DRIFT_REASONS,
    SOURCE_CLASS_BY_ID,
    UFCSTATS_FROZEN_AUDIT_PATH,
    VALIDATION_ONLY_SOURCES,
    SourceClass,
    SourceStatus,
)
from mma_model.quality.models import (
    CheckpointRunState,
    IdentityCoverage,
    PitCoverage,
    RawRefIntegrity,
    SourceCoverageRow,
)
from mma_model.sources.policy import SourceId


def _event_series_clause(series: str):
    if series == "dwcs":
        return or_(
            CanonicalEvent.series == "dwcs",
            CanonicalEvent.series.startswith("dwcs_"),
        )
    return CanonicalEvent.series == series


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def db_inventory(session: Session, *, series: str) -> dict[str, int]:
    event_filter = _event_series_clause(series)
    event_ids = list(session.scalars(select(CanonicalEvent.id).where(event_filter)).all())
    bout_ids = (
        list(
            session.scalars(
                select(CanonicalBout.id).where(CanonicalBout.event_id.in_(event_ids))
            ).all()
        )
        if event_ids
        else []
    )
    fighter_ids = (
        list(
            session.scalars(
                select(BoutParticipant.fighter_id)
                .where(BoutParticipant.bout_id.in_(bout_ids))
                .distinct()
            ).all()
        )
        if bout_ids
        else []
    )
    result_versions = (
        int(
            session.scalar(
                select(func.count())
                .select_from(BoutResultVersion)
                .where(BoutResultVersion.bout_id.in_(bout_ids))
            )
            or 0
        )
        if bout_ids
        else 0
    )
    provenance = int(session.scalar(select(func.count()).select_from(RawObservation)) or 0)
    return {
        "events": len(event_ids),
        "bouts": len(bout_ids),
        "fighters": len(set(fighter_ids)),
        "result_versions": result_versions,
        "provenance": provenance,
        "canonical_fighters_all": int(
            session.scalar(select(func.count()).select_from(CanonicalFighter)) or 0
        ),
    }


def load_raw_observations(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(RawObservation)).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        attrs: dict[str, Any] = {}
        if row.attributes_json:
            try:
                loaded = json.loads(row.attributes_json)
            except json.JSONDecodeError:
                loaded = {"malformed_attributes": True}
            if isinstance(loaded, dict):
                attrs = loaded
        out.append(
            {
                "id": row.id,
                "source": row.source,
                "stream": row.stream,
                "external_id": row.external_id,
                "entity_kind": row.entity_kind,
                "subject_id": row.subject_id,
                "quality_tier": row.quality_tier,
                "timestamp_quality": row.timestamp_quality,
                "payload_hash": row.payload_hash,
                "raw_ref": row.raw_ref,
                "observed_at": row.observed_at,
                "effective_at": row.effective_at,
                "proxy_published_at": row.proxy_published_at,
                "version_kind": row.version_kind,
                "result_type": attrs.get("result_type"),
                "winner_fighter_id": attrs.get("winner_fighter_id"),
                "method": attrs.get("method"),
                "ending_round": attrs.get("ending_round"),
                "time_str": attrs.get("time_str"),
                "malformed_attributes": bool(attrs.get("malformed_attributes")),
            }
        )
    return out


def load_result_versions(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(BoutResultVersion)).all()
    return [
        {
            "bout_id": row.bout_id,
            "version_kind": row.version_kind,
            "revision": row.revision,
            "result_type": row.result_type,
            "winner_fighter_id": row.winner_fighter_id,
            "method": row.method,
            "ending_round": row.ending_round,
            "time_str": row.time_str,
            "effective_at": row.effective_at,
            "observed_at": row.observed_at,
        }
        for row in rows
    ]


def raw_ref_integrity(observations: list[Mapping[str, Any]]) -> RawRefIntegrity:
    dangling = 0
    absent = 0
    present = 0
    malformed = 0
    for row in observations:
        raw_ref = row.get("raw_ref")
        payload_hash = row.get("payload_hash")
        if row.get("malformed_attributes"):
            malformed += 1
        if raw_ref in (None, ""):
            absent += 1
            continue
        if not isinstance(raw_ref, str) or (payload_hash and raw_ref != payload_hash):
            dangling += 1
            malformed += 1
            continue
        present += 1
    return RawRefIntegrity(
        ok=dangling == 0 and malformed == 0,
        dangling_raw_refs=dangling,
        blob_absent_explicit=absent,
        blob_present=present,
        malformed=malformed,
    )


def checkpoint_run_state(session: Session) -> CheckpointRunState:
    runs = list(session.scalars(select(IngestRun)).all())
    checkpoints = list(session.scalars(select(SourceCheckpoint)).all())
    status_counts = Counter(row.status for row in runs)
    run_fps = tuple(
        sorted(
            [str(row.source), str(row.stream), str(row.scope), str(row.status)]
            for row in runs
        )
    )
    ck_fps = tuple(
        sorted(
            [str(row.source), str(row.stream), str(row.scope), str(row.version)]
            for row in checkpoints
        )
    )
    return CheckpointRunState(
        ingest_runs=len(runs),
        completed_runs=int(status_counts.get("completed") or 0),
        failed_runs=int(status_counts.get("failed") or 0),
        running_runs=int(status_counts.get("running") or 0),
        checkpoints=len(checkpoints),
        run_fingerprints=run_fps,
        checkpoint_fingerprints=ck_fps,
    )


def identity_coverage(session: Session, *, series: str) -> IdentityCoverage:
    audit = build_identity_audit(session, series=series)
    fixture = dict(audit.fixture_validation)
    fixture["never_live_coverage"] = True
    fixture["label"] = str(fixture.get("label") or "synthetic_explicit")
    return IdentityCoverage(
        scoped_pending=audit.pending_reviews,
        scoped_unresolved_conflicts=audit.unresolved_conflicts,
        unscoped_pending=audit.unscoped_pending,
        unscoped_approved=audit.unscoped_approved,
        unscoped_rejected=audit.unscoped_rejected,
        unscoped_pending_blocking=audit.unscoped_pending_blocking,
        unmatched=audit.pending_reviews,
        upcoming_blocks=audit.upcoming_blocks,
        fixture_validation=fixture,
    )


def pit_coverage(
    session: Session,
    observations: list[Mapping[str, Any]],
    *,
    missing_required_details: int,
    conflicting_outcomes: int,
    future_row_leakage_failures: int = 0,
) -> PitCoverage:
    ts_counts: Counter[str] = Counter()
    for row in observations:
        ts_counts[str(row.get("timestamp_quality") or "unknown")] += 1
    left_truncated = int(
        session.scalar(
            select(func.count())
            .select_from(HistorySourceBout)
            .where(HistorySourceBout.left_truncated == 1)
        )
        or 0
    )
    return PitCoverage(
        proxy_timestamps=int(ts_counts.get("publication_proxy") or 0),
        unknown_timestamps=int(ts_counts.get("unknown") or 0),
        direct_timestamps=int(ts_counts.get("direct_source_timestamp") or 0),
        revision_snapshots=int(ts_counts.get("revision_snapshot") or 0),
        left_truncated_histories=left_truncated,
        future_row_leakage_failures=future_row_leakage_failures,
        mutable_current_leakage_failures=0,
        conflicting_outcomes=conflicting_outcomes,
        missing_required_details=missing_required_details,
        # mutable current records are counted for reporting but are not leakage
        # unless used as historical features; that check lives in gates/leakage.
    )


def source_failures(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(HistorySourceFailure).order_by(
            HistorySourceFailure.source.asc(),
            HistorySourceFailure.reason.asc(),
            HistorySourceFailure.scope.asc(),
        )
    ).all()
    return [
        {
            "source": row.source,
            "reason": row.reason,
            "scope": row.scope,
            "subject": row.subject,
            "host": row.host,
            "path_category": row.path_category,
            "http_status": row.http_status,
            "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        }
        for row in rows
    ]


def _status_for_source(
    source: str,
    *,
    mapped: int,
    conflict: int,
    failures: list[Mapping[str, Any]],
    ufcstats_audit: Mapping[str, Any],
    regional_probes: Mapping[str, Any],
) -> tuple[SourceStatus, str | None]:
    if source in VALIDATION_ONLY_SOURCES:
        return "validation_only", "licensed_validation_only_not_live_coverage"
    src_failures = [row for row in failures if row.get("source") == source]
    for row in src_failures:
        reason = str(row.get("reason") or "")
        if reason in SCHEMA_DRIFT_REASONS:
            return "schema_drift", reason
        if reason in KILL_REASONS:
            return "source_killed", reason
        if reason:
            return "source_failed", reason
    if source == SourceId.UFCSTATS_PUBLIC.value:
        blocked = bool(ufcstats_audit.get("blocked"))
        reason = ufcstats_audit.get("block_reason")
        if blocked:
            return "source_killed", str(reason or "blocked")
        if ufcstats_audit:
            bouts = ufcstats_audit.get("bouts") or {}
            if int(bouts.get("present") or 0) == 0:
                return "unmeasured", "cloudflare_blocked_unmapped"
        else:
            return "unmeasured", "ufcstats_live_unmeasured"
    probes = regional_probes.get("probes") if isinstance(regional_probes.get("probes"), dict) else regional_probes
    probe = (probes or {}).get(source) if isinstance(probes, dict) else None
    if isinstance(probe, dict):
        block_reason = probe.get("block_reason")
        result = str(probe.get("result") or "")
        if result == "BLOCKED" or block_reason:
            reason = str(block_reason or "blocked")
            if reason in SCHEMA_DRIFT_REASONS:
                return "schema_drift", reason
            return "source_killed", reason
        if source == SourceId.SHERDOG_PUBLIC.value and result == "OK":
            return "accessibility_only", "listing_only_not_fighter_history"
    if mapped > 0 or conflict > 0:
        return "present", None
    if source == SourceId.DWCS_MANIFEST.value:
        return "unmeasured", "manifest_not_ingested"
    if source == SourceId.EXPLICIT_MISSING.value:
        return "unmeasured", "explicit_missing_unused"
    return "unmeasured", "no_observations"


def source_coverage_rows(
    *,
    source_bout_tiers: Mapping[str, Mapping[str, str]],
    failures: list[Mapping[str, Any]],
) -> list[SourceCoverageRow]:
    ufcstats_audit = load_json_object(UFCSTATS_FROZEN_AUDIT_PATH)
    regional_probes = load_json_object(REGIONAL_LIVE_PROBE_PATH)
    rows: list[SourceCoverageRow] = []
    for source in PHASE1_BOUT_SOURCES:
        tiers = source_bout_tiers.get(source) or {}
        counts: Counter[str] = Counter(tiers.values())
        mapped = (
            int(counts.get("gold") or 0)
            + int(counts.get("silver") or 0)
            + int(counts.get("bronze") or 0)
        )
        missing = int(counts.get("missing") or 0)
        conflict = int(counts.get("conflict") or 0)
        status, reason = _status_for_source(
            source,
            mapped=mapped,
            conflict=conflict,
            failures=failures,
            ufcstats_audit=ufcstats_audit,
            regional_probes=regional_probes,
        )
        source_class: SourceClass = SOURCE_CLASS_BY_ID.get(source, "public_extraction")
        validation_only = source in VALIDATION_ONLY_SOURCES
        rows.append(
            SourceCoverageRow(
                source=source,
                source_class=source_class,
                status=status,
                reason=reason,
                mapped_bouts=mapped,
                missing_bouts=missing,
                conflict_bouts=conflict,
                gold=int(counts.get("gold") or 0),
                silver=int(counts.get("silver") or 0),
                bronze=int(counts.get("bronze") or 0),
                validation_only=validation_only,
                never_live_coverage=validation_only or status in {
                    "unmeasured",
                    "accessibility_only",
                    "source_killed",
                    "schema_drift",
                },
            )
        )
    return rows


def regional_live_payload(
    session: Session, *, as_of: datetime | None = None
) -> dict[str, Any]:
    """Read-only regional live vs fixture split. Fixture counts never fill live gates."""
    probes = load_json_object(REGIONAL_LIVE_PROBE_PATH)
    probe_rows = probes.get("probes") if isinstance(probes.get("probes"), dict) else probes
    live_map: dict[str, dict[str, Any]] = {}
    for source in REGIONAL_SOURCES:
        probe = dict((probe_rows or {}).get(source) or {})
        live_map[source] = {
            "status": _probe_coverage_status(probe) if probe else "unmeasured",
            "result": probe.get("result"),
            "reason": probe.get("block_reason") or probe.get("reason"),
            "http_status": probe.get("http_status"),
            "path_category": probe.get("path_category"),
        }
    rows = list(session.scalars(select(HistorySourceBout)).all())
    if as_of is not None:
        rows = [
            row
            for row in rows
            if parse_iso_datetime(row.observed_at) is not None
            and parse_iso_datetime(row.observed_at) <= as_of
        ]
    live_pro = [
        row
        for row in rows
        if row.observation_origin == "live_public" and row.classification == "professional"
    ]
    live_am = [
        row
        for row in rows
        if row.observation_origin == "live_public"
        and row.classification == "amateur"
        and row.regulated_us == "true"
    ]
    fixture_pro = [
        row
        for row in rows
        if row.observation_origin == "synthetic_fixture"
        and row.classification == "professional"
    ]
    fixture_am = [
        row
        for row in rows
        if row.observation_origin == "synthetic_fixture"
        and row.classification == "amateur"
        and row.regulated_us == "true"
    ]
    return {
        "evidence_class": "live_source_coverage" if live_pro or live_am else "fixture_validation",
        "probe_evidence_source": "frozen",
        "professional_n": len(live_pro),
        "professional_found": len(live_pro),
        "professional_source_failed": 0,
        "amateur_n": len(live_am),
        "amateur_found": len(live_am),
        "amateur_source_failed": 0,
        "pre_fight_agreement_n": 0,
        "pre_fight_agreement_d": 0,
        "pre_fight_agreement_rate": None,
        "live_source_coverage": live_map,
        "fixture_professional_n": len(fixture_pro),
        "fixture_professional_found": len(fixture_pro),
        "fixture_amateur_n": len(fixture_am),
        "fixture_amateur_found": len(fixture_am),
        "never_live_coverage_fixture": True,
        "left_truncated": sum(1 for row in rows if row.left_truncated == 1),
        "unresolved_identities": 0,
        "future_invariance_failures": 0,
    }


def group_observations_by_source_bout(
    observations: list[Mapping[str, Any]],
    *,
    bout_ids_by_subject: Mapping[str, str],
    cutoff: datetime | None,
    exclude_event_id: str | None,
    event_id_by_bout: Mapping[str, str],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in observations:
        subject = str(row.get("subject_id") or "")
        bout_id = bout_ids_by_subject.get(subject, subject)
        if not bout_id:
            continue
        source = str(row.get("source") or "")
        if source not in PHASE1_BOUT_SOURCES:
            continue
        visible = observation_visible(
            effective_at=parse_iso_datetime(row.get("effective_at")),  # type: ignore[arg-type]
            observed_at=parse_iso_datetime(row.get("observed_at")),  # type: ignore[arg-type]
            proxy_published_at=parse_iso_datetime(row.get("proxy_published_at")),  # type: ignore[arg-type]
            timestamp_quality=str(row.get("timestamp_quality") or ""),
            version_kind=str(row.get("version_kind") or "") or None,
            is_mutable_current=False,
            cutoff=cutoff,
            event_id=event_id_by_bout.get(bout_id),
            exclude_event_id=exclude_event_id,
        )
        if not visible:
            continue
        grouped[source][bout_id].append(dict(row))
    return grouped
