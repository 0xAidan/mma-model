"""Inventory counts, identity, PIT, raw-ref, and source-status for DWCS-106."""

from __future__ import annotations

import hashlib
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
    FighterProfileObservation,
    FighterSourceId,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.db.tables.identity import IdentityReviewQueue
from mma_model.db.tables.provenance import IngestRun, RawObservation, SourceCheckpoint
from mma_model.history.audit import load_adjudicated_sample
from mma_model.history.coverage import REGIONAL_SOURCES, _probe_coverage_status
from mma_model.identity.audit import build_identity_audit
from mma_model.ingest.raw_store import ContentAddressedRawStore, PayloadCorruptionError
from mma_model.quality.classify import (
    attach_result_version_clocks,
    fingerprint_in_scope,
    observation_visible,
    parse_iso_datetime,
    result_version_visible,
)
from mma_model.quality.constants import (
    COMPLETED_RUN_STATUSES,
    FAILED_RUN_STATUSES,
    KILL_REASONS,
    PHASE1_BOUT_SOURCES,
    REGIONAL_LIVE_PROBE_PATH,
    RUNNING_RUN_STATUSES,
    SCHEMA_DRIFT_REASONS,
    SOURCE_CLASS_BY_ID,
    SUCCEEDED_RUN_STATUSES,
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
from mma_model.quality.schema import sha256_canonical
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


def visible_fighter_count(session: Session, visible_bout_ids: list[str]) -> int:
    """Distinct canonical fighters in visible bouts, independent of insert order."""
    if not visible_bout_ids:
        return 0
    ids: set[str] = set()
    bouts = session.scalars(
        select(CanonicalBout).where(CanonicalBout.id.in_(visible_bout_ids))
    ).all()
    for bout in bouts:
        ids.add(str(bout.fighter_a_id))
        ids.add(str(bout.fighter_b_id))
    parts = session.scalars(
        select(BoutParticipant.fighter_id).where(BoutParticipant.bout_id.in_(visible_bout_ids))
    ).all()
    ids.update(str(item) for item in parts)
    return len(ids)


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
                "source_published_at": row.source_published_at,
                "source_updated_at": row.source_updated_at,
                "proxy_published_at": row.proxy_published_at,
                "version_kind": row.version_kind,
                "raw_blob_absent": row.raw_ref in (None, ""),
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


def raw_ref_integrity(
    observations: list[Mapping[str, Any]],
    *,
    raw_store: ContentAddressedRawStore | None = None,
) -> RawRefIntegrity:
    dangling = 0
    absent = 0
    present = 0
    malformed = 0
    unverifiable = 0
    missing_blobs = 0
    corrupt_blobs = 0
    for row in observations:
        raw_ref = row.get("raw_ref")
        payload_hash = row.get("payload_hash")
        if row.get("malformed_attributes"):
            malformed += 1
        if raw_ref in (None, "") or row.get("raw_blob_absent"):
            absent += 1
            continue
        if not isinstance(raw_ref, str) or (payload_hash and raw_ref != payload_hash):
            dangling += 1
            malformed += 1
            continue
        if raw_store is None:
            unverifiable += 1
            continue
        if not raw_store.exists(str(raw_ref)):
            missing_blobs += 1
            continue
        try:
            raw_store.verify(str(raw_ref))
            data = raw_store.get(str(raw_ref))
        except (PayloadCorruptionError, OSError, ValueError):
            corrupt_blobs += 1
            continue
        if hashlib.sha256(data).hexdigest() != str(payload_hash or raw_ref):
            corrupt_blobs += 1
            continue
        present += 1
    ok = (
        dangling == 0
        and malformed == 0
        and unverifiable == 0
        and missing_blobs == 0
        and corrupt_blobs == 0
    )
    return RawRefIntegrity(
        ok=ok,
        dangling_raw_refs=dangling,
        blob_absent_explicit=absent,
        blob_present=present,
        malformed=malformed,
        unverifiable=unverifiable,
        missing_blobs=missing_blobs,
        corrupt_blobs=corrupt_blobs,
        store_provided=raw_store is not None,
    )


def checkpoint_run_state(session: Session, *, cutoff: datetime | None = None) -> CheckpointRunState:
    runs = [
        row
        for row in session.scalars(select(IngestRun)).all()
        if cutoff is None
        or parse_iso_datetime(row.started_at) is None
        or parse_iso_datetime(row.started_at) < cutoff  # type: ignore[operator]
    ]
    checkpoints = list(session.scalars(select(SourceCheckpoint)).all())
    run_fps = tuple(
        sorted([str(row.source), str(row.stream), str(row.scope), str(row.status)] for row in runs)
    )
    ck_fps = tuple(
        sorted(
            [str(row.source), str(row.stream), str(row.scope), str(row.version)]
            for row in checkpoints
        )
    )
    return CheckpointRunState(
        ingest_runs=len(runs),
        succeeded_runs=int(sum(1 for row in runs if row.status in SUCCEEDED_RUN_STATUSES)),
        completed_runs=int(sum(1 for row in runs if row.status in COMPLETED_RUN_STATUSES)),
        failed_runs=int(sum(1 for row in runs if row.status in FAILED_RUN_STATUSES)),
        running_runs=int(sum(1 for row in runs if row.status in RUNNING_RUN_STATUSES)),
        checkpoints=len(checkpoints),
        run_fingerprints=run_fps,
        checkpoint_fingerprints=ck_fps,
    )


def _unmatched_source_identities(session: Session) -> int:
    mapped = {
        (str(row.source), str(row.external_id))
        for row in session.scalars(select(FighterSourceId)).all()
    }
    unmatched: set[tuple[str, str]] = set()
    for row in session.scalars(select(IdentityReviewQueue)).all():
        key = (str(row.source), str(row.external_id))
        if key not in mapped and row.decision_canonical_id in (None, ""):
            unmatched.add(key)
    for row in session.scalars(select(HistorySourceBout)).all():
        key = (str(row.fighter_source or row.source), str(row.fighter_external_id))
        if row.fighter_canonical_id in (None, "") and key[1] and key not in mapped:
            unmatched.add(key)
    return len(unmatched)


def identity_coverage(session: Session, *, series: str) -> IdentityCoverage:
    audit = build_identity_audit(session, series=series)
    fixture = dict(audit.fixture_validation)
    fixture["never_live_coverage"] = True
    fixture["label"] = str(fixture.get("label") or "synthetic_explicit")
    unmatched = _unmatched_source_identities(session)
    return IdentityCoverage(
        scoped_pending=audit.pending_reviews,
        scoped_unresolved_conflicts=audit.unresolved_conflicts,
        unscoped_pending=audit.unscoped_pending,
        unscoped_approved=audit.unscoped_approved,
        unscoped_rejected=audit.unscoped_rejected,
        unscoped_pending_blocking=audit.unscoped_pending_blocking,
        unmatched=unmatched,
        unmatched_source_identities=unmatched,
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
    future_row_leakage_checks_executed: int = 0,
    future_row_leakage_evidence_hash: str = "",
    mutable_current_leakage_failures: int = 0,
    mutable_current_leakage_checks_executed: int = 0,
    mutable_current_leakage_evidence_hash: str = "",
    mutable_current_rows_examined: int = 0,
    mutable_current_applicable_rows: int = 0,
    mutable_current_synthetic_guard_checks: int = 0,
    mutable_current_leakage_status: str = "not_applicable",
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
        future_row_leakage_checks_executed=future_row_leakage_checks_executed,
        future_row_leakage_evidence_hash=future_row_leakage_evidence_hash,
        mutable_current_leakage_failures=mutable_current_leakage_failures,
        mutable_current_leakage_checks_executed=mutable_current_leakage_checks_executed,
        mutable_current_leakage_evidence_hash=mutable_current_leakage_evidence_hash,
        mutable_current_rows_examined=mutable_current_rows_examined,
        mutable_current_applicable_rows=mutable_current_applicable_rows,
        mutable_current_synthetic_guard_checks=mutable_current_synthetic_guard_checks,
        mutable_current_leakage_status=mutable_current_leakage_status,  # type: ignore[arg-type]
        conflicting_outcomes=conflicting_outcomes,
        missing_required_details=missing_required_details,
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


def _frozen_evidence_meta(path: Path) -> tuple[str, str | None]:
    payload = load_json_object(path)
    digest = sha256_canonical(payload) if payload else None
    observed = None
    if path == UFCSTATS_FROZEN_AUDIT_PATH:
        live = payload.get("live_probe") if isinstance(payload.get("live_probe"), dict) else {}
        observed = live.get("observed_at") if isinstance(live, dict) else None
    probes = payload.get("probes") if isinstance(payload.get("probes"), dict) else {}
    if isinstance(probes, dict):
        times = [
            str(row.get("observed_at") or "") for row in probes.values() if isinstance(row, dict)
        ]
        times = [item for item in times if item]
        observed = min(times) if times else observed
    return digest or "", str(observed) if observed else None


def _status_for_source(
    source: str,
    *,
    mapped: int,
    conflict: int,
    failures: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]],
    ufcstats_audit: Mapping[str, Any],
    regional_probes: Mapping[str, Any],
) -> tuple[SourceStatus, str | None, str, str | None, str | None]:
    """Return access status, reason, evidence_origin, evidence_hash, evidence_observed_at.

    Mapped coverage is computed separately from DB observations.
    """
    if source in VALIDATION_ONLY_SOURCES:
        return "validation_only", "licensed_validation_only_not_live_coverage", "none", None, None
    src_failures = [row for row in failures if row.get("source") == source]
    if src_failures:
        row = src_failures[0]
        reason = str(row.get("reason") or "")
        origin = "persisted"
        observed = row.get("observed_at")
        digest = sha256_canonical({"source": source, "reason": reason, "scope": row.get("scope")})
        if reason in SCHEMA_DRIFT_REASONS:
            return (
                "schema_drift",
                reason,
                origin,
                digest,
                observed if isinstance(observed, str) else None,
            )
        if reason in KILL_REASONS:
            return (
                "source_killed",
                reason,
                origin,
                digest,
                observed if isinstance(observed, str) else None,
            )
        if reason:
            return (
                "source_failed",
                reason,
                origin,
                digest,
                observed if isinstance(observed, str) else None,
            )
    src_checkpoints = [row for row in checkpoints if row.get("source") == source]
    if source == SourceId.UFCSTATS_PUBLIC.value:
        blocked = bool(ufcstats_audit.get("blocked"))
        reason = ufcstats_audit.get("block_reason")
        digest, observed = _frozen_evidence_meta(UFCSTATS_FROZEN_AUDIT_PATH)
        if blocked:
            return "source_killed", str(reason or "blocked"), "frozen_fallback", digest, observed
        if ufcstats_audit:
            bouts = ufcstats_audit.get("bouts") or {}
            if int(bouts.get("present") or 0) == 0:
                return (
                    "unmeasured",
                    "cloudflare_blocked_unmapped",
                    "frozen_fallback",
                    digest,
                    observed,
                )
        else:
            return "unmeasured", "ufcstats_live_unmeasured", "none", None, None
    probes = (
        regional_probes.get("probes")
        if isinstance(regional_probes.get("probes"), dict)
        else regional_probes
    )
    probe = (probes or {}).get(source) if isinstance(probes, dict) else None
    if isinstance(probe, dict):
        digest, observed = _frozen_evidence_meta(REGIONAL_LIVE_PROBE_PATH)
        block_reason = probe.get("block_reason")
        result = str(probe.get("result") or "")
        if result == "BLOCKED" or block_reason:
            reason = str(block_reason or "blocked")
            if reason in SCHEMA_DRIFT_REASONS:
                return "schema_drift", reason, "frozen_fallback", digest, observed
            return "source_killed", reason, "frozen_fallback", digest, observed
        if source == SourceId.SHERDOG_PUBLIC.value and result == "OK":
            return (
                "accessibility_only",
                "listing_only_not_fighter_history",
                "frozen_fallback",
                digest,
                observed,
            )
    if src_checkpoints:
        origin = "checkpoint"
        digest = sha256_canonical({"source": source, "checkpoints": len(src_checkpoints)})
        if mapped > 0 or conflict > 0:
            return "present", None, origin, digest, None
    if mapped > 0 or conflict > 0:
        return "present", None, "none", None, None
    if source == SourceId.DWCS_MANIFEST.value:
        return "unmeasured", "manifest_not_ingested", "none", None, None
    if source == SourceId.EXPLICIT_MISSING.value:
        return "unmeasured", "explicit_missing_unused", "none", None, None
    return "unmeasured", "no_observations", "none", None, None


def source_coverage_rows(
    *,
    source_bout_tiers: Mapping[str, Mapping[str, str]],
    failures: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]] | None = None,
) -> list[SourceCoverageRow]:
    ufcstats_audit = load_json_object(UFCSTATS_FROZEN_AUDIT_PATH)
    regional_probes = load_json_object(REGIONAL_LIVE_PROBE_PATH)
    checkpoint_rows = list(checkpoints or [])
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
        status, reason, origin, digest, observed = _status_for_source(
            source,
            mapped=mapped,
            conflict=conflict,
            failures=failures,
            checkpoints=checkpoint_rows,
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
                never_live_coverage=validation_only
                or status
                in {
                    "unmeasured",
                    "accessibility_only",
                    "source_killed",
                    "schema_drift",
                },
                evidence_origin=origin,  # type: ignore[arg-type]
                evidence_hash=digest,
                evidence_observed_at=observed,
            )
        )
    return rows


def regional_live_payload(session: Session, *, as_of: datetime | None = None) -> dict[str, Any]:
    """Live gates use the adjudicated sample universe, never DB row-count 1/1."""
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
    sample = load_adjudicated_sample()
    failures = source_failures(session)
    found_live_ids = {
        row.external_bout_id
        for row in session.scalars(select(HistorySourceBout)).all()
        if row.observation_origin == "live_public"
        and (
            as_of is None
            or (
                parse_iso_datetime(row.effective_at) is not None
                and parse_iso_datetime(row.effective_at) < as_of
            )
        )
    }
    subject_failures = {
        (str(row.get("source") or ""), str(row.get("subject") or ""))
        for row in failures
        if str(row.get("subject") or "")
    }
    visible_history = [
        row
        for row in session.scalars(select(HistorySourceBout)).all()
        if as_of is None
        or (
            parse_iso_datetime(row.effective_at) is not None
            and parse_iso_datetime(row.effective_at) < as_of
        )
    ]
    origins = {
        getattr(row, "observation_origin", "unknown") or "unknown" for row in visible_history
    }
    if not visible_history:
        evidence_class = "fixture_validation"
    elif "live_public" in origins and "synthetic_fixture" in origins:
        evidence_class = "mixed"
    elif "live_public" in origins:
        evidence_class = "live_source_coverage"
    else:
        evidence_class = "fixture_validation"
    fixture_ids = {
        row.external_bout_id
        for row in session.scalars(select(HistorySourceBout)).all()
        if row.observation_origin == "synthetic_fixture"
    }

    def _score(sample_rows: list[Any]) -> tuple[int, int, int, int]:
        n = len(sample_rows)
        found_n = 0
        failed_n = 0
        for row in sample_rows:
            bout_id = str(row.get("bout_id") or "")
            source = str(row.get("source") or "")
            if bout_id in found_live_ids:
                found_n += 1
            elif bool(row.get("source_failed")) or (source, bout_id) in subject_failures:
                failed_n += 1
        missing_n = n - found_n - failed_n
        return n, found_n, failed_n, missing_n

    pro_sample = list(sample.get("professional_bouts") or [])
    am_sample = list(sample.get("amateur_regulated_us_bouts") or [])
    if evidence_class in {"live_source_coverage", "mixed"}:
        pro_n, pro_found, pro_failed, pro_missing = _score(pro_sample)
        am_n, am_found, am_failed, am_missing = _score(am_sample)
    else:
        pro_n = pro_found = pro_failed = pro_missing = 0
        am_n = am_found = am_failed = am_missing = 0
    fixture_pro_n = len(pro_sample)
    fixture_am_n = len(am_sample)
    fixture_pro_found = sum(1 for row in pro_sample if str(row.get("bout_id")) in fixture_ids)
    fixture_am_found = sum(1 for row in am_sample if str(row.get("bout_id")) in fixture_ids)
    return {
        "evidence_class": evidence_class,
        "probe_evidence_source": "frozen_fallback",
        "professional_n": pro_n,
        "professional_found": pro_found,
        "professional_source_failed": pro_failed,
        "professional_missing": pro_missing,
        "amateur_n": am_n,
        "amateur_found": am_found,
        "amateur_source_failed": am_failed,
        "amateur_missing": am_missing,
        "pre_fight_agreement_n": 0,
        "pre_fight_agreement_d": 0,
        "pre_fight_agreement_rate": None,
        "live_source_coverage": live_map,
        "fixture_professional_n": fixture_pro_n,
        "fixture_professional_found": fixture_pro_found,
        "fixture_amateur_n": fixture_am_n,
        "fixture_amateur_found": fixture_am_found,
        "never_live_coverage_fixture": True,
        "adjudicated_sample_professional_n": len(pro_sample),
        "adjudicated_sample_amateur_n": len(am_sample),
        "left_truncated": int(
            session.scalar(
                select(func.count())
                .select_from(HistorySourceBout)
                .where(HistorySourceBout.left_truncated == 1)
            )
            or 0
        ),
        "unresolved_identities": 0,
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
            source_published_at=parse_iso_datetime(row.get("source_published_at")),  # type: ignore[arg-type]
            source_updated_at=parse_iso_datetime(row.get("source_updated_at")),  # type: ignore[arg-type]
            source=str(row.get("source") or "") or None,
        )
        if not visible:
            continue
        grouped[source][bout_id].append(dict(row))
    return grouped


def load_checkpoints(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(SourceCheckpoint).order_by(
            SourceCheckpoint.source.asc(),
            SourceCheckpoint.stream.asc(),
            SourceCheckpoint.scope.asc(),
        )
    ).all()
    return [
        {
            "source": row.source,
            "stream": row.stream,
            "scope": row.scope,
            "version": row.version,
            "last_ingest_run_id": row.last_ingest_run_id,
        }
        for row in rows
    ]


def influencing_db_fingerprint(
    session: Session,
    *,
    cutoff: datetime | None = None,
    exclude_event_id: str | None = None,
    event_id_by_bout: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Canonical sorted semantic contents of every table that influences coverage."""

    def _iso(value: Any) -> str:
        parsed = parse_iso_datetime(value) if value is not None else None
        return parsed.isoformat() if parsed is not None else ""

    event_map = dict(event_id_by_bout or {})
    raw_observations = load_raw_observations(session)
    observations = [
        row
        for row in raw_observations
        if observation_visible(
            effective_at=parse_iso_datetime(row.get("effective_at")),  # type: ignore[arg-type]
            observed_at=parse_iso_datetime(row.get("observed_at")),  # type: ignore[arg-type]
            proxy_published_at=parse_iso_datetime(row.get("proxy_published_at")),  # type: ignore[arg-type]
            timestamp_quality=str(row.get("timestamp_quality") or ""),
            version_kind=str(row.get("version_kind") or "") or None,
            is_mutable_current=False,
            cutoff=cutoff,
            event_id=event_map.get(str(row.get("subject_id") or "")),
            exclude_event_id=exclude_event_id,
            source_published_at=parse_iso_datetime(row.get("source_published_at")),  # type: ignore[arg-type]
            source_updated_at=parse_iso_datetime(row.get("source_updated_at")),  # type: ignore[arg-type]
            source=str(row.get("source") or "") or None,
        )
    ]
    clocked_results = attach_result_version_clocks(load_result_versions(session), raw_observations)
    results = [
        row
        for row in clocked_results
        if result_version_visible(
            effective_at=row.get("effective_at"),  # type: ignore[arg-type]
            cutoff=cutoff,
            event_id=event_map.get(str(row.get("bout_id") or "")),
            exclude_event_id=exclude_event_id,
            timestamp_quality=row.get("timestamp_quality"),  # type: ignore[arg-type]
            source_published_at=row.get("source_published_at"),  # type: ignore[arg-type]
            source_updated_at=row.get("source_updated_at"),  # type: ignore[arg-type]
            proxy_published_at=row.get("proxy_published_at"),  # type: ignore[arg-type]
            observed_at=row.get("observed_at"),  # type: ignore[arg-type]
        )
    ]
    failures = [
        row
        for row in source_failures(session)
        if cutoff is None
        or parse_iso_datetime(row.get("observed_at")) is None
        or parse_iso_datetime(row.get("observed_at")) <= cutoff  # type: ignore[operator]
    ]
    checkpoints = load_checkpoints(session)
    runs = [
        row
        for row in session.scalars(select(IngestRun)).all()
        if cutoff is None
        or parse_iso_datetime(row.started_at) is None
        or parse_iso_datetime(row.started_at) < cutoff  # type: ignore[operator]
    ]
    reviews = [
        row
        for row in session.scalars(select(IdentityReviewQueue)).all()
        if cutoff is None
        or parse_iso_datetime(row.created_at) is None
        or parse_iso_datetime(row.created_at) < cutoff  # type: ignore[operator]
    ]
    source_ids = list(session.scalars(select(FighterSourceId)).all())
    profiles = [
        row
        for row in session.scalars(select(FighterProfileObservation)).all()
        if fingerprint_in_scope(
            cutoff=cutoff,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
    ]
    history = [
        row
        for row in session.scalars(select(HistorySourceBout)).all()
        if fingerprint_in_scope(
            cutoff=cutoff,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
    ]
    return {
        "observations": sorted(
            (
                str(row.get("source") or ""),
                str(row.get("external_id") or ""),
                str(row.get("payload_hash") or ""),
                str(row.get("entity_kind") or ""),
                str(row.get("version_kind") or ""),
                str(row.get("quality_tier") or ""),
                str(row.get("timestamp_quality") or ""),
                str(row.get("result_type") or ""),
                str(row.get("winner_fighter_id") or ""),
                str(row.get("raw_ref") or ""),
                _iso(row.get("effective_at")),
                _iso(row.get("proxy_published_at")),
                _iso(row.get("source_published_at")),
            )
            for row in observations
        ),
        "result_versions": sorted(
            (
                str(row.get("bout_id") or ""),
                str(row.get("version_kind") or ""),
                int(row.get("revision") or 0),
                str(row.get("result_type") or ""),
                str(row.get("winner_fighter_id") or ""),
                _iso(row.get("effective_at")),
            )
            for row in results
        ),
        "failures": sorted(
            (
                str(row.get("source") or ""),
                str(row.get("reason") or ""),
                str(row.get("scope") or ""),
                str(row.get("subject") or ""),
                str(row.get("observed_at") or ""),
            )
            for row in failures
        ),
        "checkpoints": sorted(
            (
                str(row.get("source") or ""),
                str(row.get("stream") or ""),
                str(row.get("scope") or ""),
                str(row.get("version") or ""),
            )
            for row in checkpoints
        ),
        "runs": sorted(
            (str(row.source), str(row.stream), str(row.scope), str(row.status)) for row in runs
        ),
        "identity_reviews": sorted(
            (str(row.source), str(row.external_id), str(row.status), str(row.bout_id or ""))
            for row in reviews
        ),
        "fighter_source_ids": sorted(
            (str(row.source), str(row.external_id), str(row.fighter_id)) for row in source_ids
        ),
        "profiles": sorted(
            (
                str(row.fighter_id),
                str(row.attribute),
                str(row.source),
                str(row.value_num if row.value_num is not None else row.value_text or ""),
                _iso(row.effective_at),
            )
            for row in profiles
        ),
        "history_bouts": sorted(
            (
                str(row.source),
                str(row.external_bout_id),
                str(row.version_kind),
                int(row.revision),
                str(row.observation_origin),
                str(row.classification),
                int(row.is_current_record or 0),
            )
            for row in history
        ),
    }
