"""Read-only future-row and mutable-current leakage audits (DWCS-106)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import FighterProfileObservation
from mma_model.db.tables.history import HistorySourceBout
from mma_model.quality.classify import (
    classify_overall_bout,
    classify_source_bout,
    fingerprint_in_scope,
    observation_visible,
    parse_iso_datetime,
    result_version_visible,
)
from mma_model.quality.constants import DEFAULT_LEAKAGE_CUTOFF, PHASE1_BOUT_SOURCES
from mma_model.quality.inventory import group_observations_by_source_bout
from mma_model.quality.schema import sha256_canonical

UTC = timezone.utc


def default_leakage_cutoff() -> datetime:
    return datetime.fromisoformat(DEFAULT_LEAKAGE_CUTOFF)


def _tier_snapshot(
    *,
    skeleton: list[Mapping[str, Any]],
    observations: list[Mapping[str, Any]],
    cutoff: datetime | None,
    exclude_event_id: str | None,
    event_id_by_bout: Mapping[str, str],
) -> list[tuple[str, str]]:
    grouped = group_observations_by_source_bout(
        list(observations),
        bout_ids_by_subject={str(row["bout_id"]): str(row["bout_id"]) for row in skeleton},
        cutoff=cutoff,
        exclude_event_id=exclude_event_id,
        event_id_by_bout=event_id_by_bout,
    )
    out: list[tuple[str, str]] = []
    for item in sorted(skeleton, key=lambda row: str(row["bout_id"])):
        bout_id = str(item["bout_id"])
        per_source: dict[str, str] = {}
        source_obs: dict[str, list[Mapping[str, Any]]] = {}
        for source in PHASE1_BOUT_SOURCES:
            rows = grouped.get(source, {}).get(bout_id, [])
            per_source[source] = classify_source_bout(rows)
            source_obs[source] = rows
        core_obs: list[Mapping[str, Any]] = []
        for source in PHASE1_BOUT_SOURCES:
            core_obs.extend(source_obs.get(source, []))
        overall, _source_class, _notes = classify_overall_bout(
            source_tiers=per_source,
            core_observations=list(core_obs),
            source_observations=source_obs,
        )
        out.append((bout_id, overall))
    return out


def run_leakage_audits(
    session: Session,
    *,
    skeleton: list[Mapping[str, Any]],
    observations: list[Mapping[str, Any]],
    result_versions: list[Mapping[str, Any]],
    event_id_by_bout: Mapping[str, str],
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Pure/read-only audits. Never writes to the coverage database."""
    audit_cutoff = cutoff or default_leakage_cutoff()
    mutable_scope_cutoff = cutoff
    baseline = _tier_snapshot(
        skeleton=list(skeleton),
        observations=list(observations),
        cutoff=audit_cutoff,
        exclude_event_id=None,
        event_id_by_bout=event_id_by_bout,
    )
    visible_n = sum(1 for _bout_id, tier in baseline if tier != "missing")
    target = next((bout_id for bout_id, tier in baseline if tier != "missing"), None)
    future_at = audit_cutoff + timedelta(days=400)
    checks = 0
    failures = 0

    injected: list[dict[str, Any]] = list(observations)
    if target is not None:
        injected.append(
            {
                "id": 10_000_000,
                "source": "ufcstats_public",
                "stream": "history",
                "external_id": f"future-leak-{uuid4()}",
                "entity_kind": "bout_result",
                "subject_id": target,
                "quality_tier": "gold",
                "timestamp_quality": "direct_source_timestamp",
                "payload_hash": "f" * 64,
                "raw_ref": None,
                "raw_blob_absent": True,
                "observed_at": future_at,
                "effective_at": future_at,
                "source_published_at": future_at,
                "source_updated_at": future_at,
                "proxy_published_at": future_at,
                "version_kind": "event_night",
                "result_type": "draw",
                "winner_fighter_id": None,
            }
        )
        checks += 1
        after = _tier_snapshot(
            skeleton=list(skeleton),
            observations=injected,
            cutoff=audit_cutoff,
            exclude_event_id=None,
            event_id_by_bout=event_id_by_bout,
        )
        if after != baseline:
            failures += 1
        leaked = _tier_snapshot(
            skeleton=list(skeleton),
            observations=injected,
            cutoff=None,
            exclude_event_id=None,
            event_id_by_bout=event_id_by_bout,
        )
        checks += 1
        if leaked == baseline and visible_n > 0:
            failures += 1

    future_results = [
        row
        for row in result_versions
        if parse_iso_datetime(row.get("effective_at")) is not None
        and parse_iso_datetime(row.get("effective_at")) >= audit_cutoff  # type: ignore[operator]
    ]
    checks += 1
    for row in future_results:
        bout_id = str(row.get("bout_id") or "")
        if result_version_visible(
            effective_at=row.get("effective_at"),  # type: ignore[arg-type]
            cutoff=audit_cutoff,
            event_id=event_id_by_bout.get(bout_id),
            timestamp_quality=row.get("timestamp_quality"),  # type: ignore[arg-type]
            source_published_at=row.get("source_published_at"),  # type: ignore[arg-type]
            source_updated_at=row.get("source_updated_at"),  # type: ignore[arg-type]
            proxy_published_at=row.get("proxy_published_at"),  # type: ignore[arg-type]
            observed_at=row.get("observed_at"),  # type: ignore[arg-type]
        ):
            failures += 1

    same_card_event = next(
        (
            str(item["event_id"])
            for item in skeleton
            if any(bout_id == item["bout_id"] and tier != "missing" for bout_id, tier in baseline)
        ),
        None,
    )
    checks += 1
    if same_card_event is not None:
        excluded = _tier_snapshot(
            skeleton=list(skeleton),
            observations=list(observations),
            cutoff=audit_cutoff,
            exclude_event_id=same_card_event,
            event_id_by_bout=event_id_by_bout,
        )
        excluded_map = dict(excluded)
        for bout_id, tier in baseline:
            event_id = event_id_by_bout.get(bout_id)
            excluded_tier = excluded_map.get(bout_id)
            if event_id == same_card_event:
                if tier != "missing" and excluded_tier != "missing":
                    failures += 1
            elif excluded_tier != tier:
                failures += 1
        if len(excluded) != len(baseline):
            failures += 1

    mutable_failures = 0
    profiles = list(session.scalars(select(FighterProfileObservation)).all())
    history_rows = list(session.scalars(select(HistorySourceBout)).all())
    rows_examined = 0
    applicable_rows = 0
    for row in profiles:
        in_scope = fingerprint_in_scope(
            cutoff=mutable_scope_cutoff,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
        is_mutable = row.source in {"mutable_current", "current_mutable_profile"}
        leaked = observation_visible(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            proxy_published_at=None,
            timestamp_quality="unknown",
            version_kind=None,
            is_mutable_current=is_mutable,
            cutoff=mutable_scope_cutoff,
            source=row.source,
        )
        if is_mutable and leaked:
            mutable_failures += 1
        if not in_scope:
            continue
        rows_examined += 1
        if is_mutable:
            applicable_rows += 1
    for row in history_rows:
        in_scope = fingerprint_in_scope(
            cutoff=mutable_scope_cutoff,
            effective_at=row.effective_at,
            observed_at=row.observed_at,
        )
        is_mutable = int(row.is_current_record or 0) == 1
        leaked = observation_visible(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            proxy_published_at=row.proxy_published_at,
            timestamp_quality=row.timestamp_quality,
            version_kind=row.version_kind,
            is_mutable_current=is_mutable,
            cutoff=mutable_scope_cutoff,
            source=row.source,
        )
        if is_mutable and leaked:
            mutable_failures += 1
        if not in_scope:
            continue
        rows_examined += 1
        if is_mutable:
            applicable_rows += 1

    guard_cutoff = audit_cutoff or default_leakage_cutoff()
    fabricated_effective = guard_cutoff - timedelta(days=400)
    synthetic_guard_checks = 1
    synthetic_leaked = observation_visible(
        effective_at=fabricated_effective,
        observed_at=fabricated_effective,
        proxy_published_at=None,
        timestamp_quality="unknown",
        version_kind=None,
        is_mutable_current=True,
        cutoff=guard_cutoff,
        source="mutable_current",
    )
    if synthetic_leaked:
        mutable_failures += 1
    if mutable_failures > 0:
        mutable_status = "fail"
    elif applicable_rows == 0:
        mutable_status = "not_applicable"
    else:
        mutable_status = "pass"
    checks_executed = synthetic_guard_checks + applicable_rows

    future_hash = sha256_canonical(
        {
            "baseline": baseline,
            "visible_n": visible_n,
            "cutoff": audit_cutoff.isoformat(),
            "failures": failures,
            "checks": checks,
        }
    )
    mutable_hash = sha256_canonical(
        {
            "rows_examined": rows_examined,
            "applicable_rows": applicable_rows,
            "synthetic_guard_checks": synthetic_guard_checks,
            "violations": mutable_failures,
            "status": mutable_status,
            "cutoff": None if mutable_scope_cutoff is None else mutable_scope_cutoff.isoformat(),
            "future_row_audit_cutoff": audit_cutoff.isoformat(),
        }
    )
    return {
        "future_row_leakage_checks_executed": max(checks, 1),
        "future_row_leakage_failures": failures,
        "future_row_leakage_evidence_hash": future_hash,
        "mutable_current_leakage_checks_executed": checks_executed,
        "mutable_current_leakage_failures": mutable_failures,
        "mutable_current_leakage_evidence_hash": mutable_hash,
        "mutable_current_rows_examined": rows_examined,
        "mutable_current_applicable_rows": applicable_rows,
        "mutable_current_synthetic_guard_checks": synthetic_guard_checks,
        "mutable_current_leakage_status": mutable_status,
        "visible_baseline_bouts": visible_n,
    }
