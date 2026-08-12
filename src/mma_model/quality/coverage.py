"""Compute deterministic DWCS-106 coverage reports from a disposable database."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest, load_dwcs_event_manifest
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract
from mma_model.quality.classify import (
    build_bout_row,
    classify_overall_bout,
    classify_source_bout,
    normalize_result,
    observation_visible,
    parse_iso_datetime,
)
from mma_model.quality.constants import (
    CORE_OVERALL_SOURCES,
    PHASE1_BOUT_SOURCES,
    QUALITY_TIERS,
    REQUIRED_RESULT_FIELDS,
    SOURCE_CLASSES,
)
from mma_model.quality.inventory import (
    checkpoint_run_state,
    db_inventory,
    group_observations_by_source_bout,
    identity_coverage,
    load_raw_observations,
    load_result_versions,
    pit_coverage,
    raw_ref_integrity,
    regional_live_payload,
    source_coverage_rows,
    source_failures,
)
from mma_model.quality.models import (
    CoverageReport,
    FieldCoverageRow,
    LaneCounts,
    LicensedStatus,
    ResultLaneCounts,
    empty_tier_counts,
    mapping_to_dimension,
)
from mma_model.quality.schema import sha256_canonical
from mma_model.sources.policy import SourcePolicy, load_source_policy

_POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "sources" / "source_policy_v1.json"


def _policy_file_hash() -> str:
    payload = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    return sha256_canonical(payload)


def _config_hash(*, series: str, as_of: str | None, policy_hash: str) -> str:
    contract = load_evaluation_contract()
    payload = {
        "series": series,
        "as_of": as_of,
        "policy_hash": policy_hash,
        "evaluation_contract_hash": PINNED_CONTRACT_HASH,
        "evaluation_contract_version": contract.contract_version,
        "coverage_contract_version": CoverageReport.model_fields["contract_version"].default,
    }
    return sha256_canonical(payload)


def _db_hash(
    *,
    observations: list[dict[str, Any]],
    result_versions: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    inventory: dict[str, int],
) -> str:
    obs_fp = sorted(
        (
            str(row.get("source") or ""),
            str(row.get("external_id") or ""),
            str(row.get("payload_hash") or ""),
            str(row.get("entity_kind") or ""),
            str(row.get("version_kind") or ""),
        )
        for row in observations
    )
    result_fp = sorted(
        (
            str(row.get("bout_id") or ""),
            str(row.get("version_kind") or ""),
            int(row.get("revision") or 0),
            str(row.get("result_type") or ""),
            str(row.get("winner_fighter_id") or ""),
        )
        for row in result_versions
    )
    fail_fp = sorted(
        (
            str(row.get("source") or ""),
            str(row.get("reason") or ""),
            str(row.get("scope") or ""),
            str(row.get("subject") or ""),
        )
        for row in failures
    )
    payload = {
        "inventory": dict(sorted(inventory.items())),
        "observations": obs_fp,
        "result_versions": result_fp,
        "failures": fail_fp,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _latest_result(
    versions: list[dict[str, Any]], *, version_kind: str
) -> dict[str, Any] | None:
    matched = [row for row in versions if row.get("version_kind") == version_kind]
    if not matched:
        return None
    return max(matched, key=lambda row: int(row.get("revision") or 0))


def compute_coverage_report(
    *,
    series: str,
    session: Session,
    policy: SourcePolicy | None = None,
    as_of: datetime | None = None,
    exclude_event_id: str | None = None,
    db_url: str | None = None,
) -> CoverageReport:
    """Build a deterministic coverage report for the frozen DWCS universe.

    ``db_url`` is accepted for the Task 10 interface and provenance only; the
    caller must already open a read-only session against that URL.
    """
    del db_url
    if series != "dwcs":
        raise ValueError(f"unsupported series: {series}")
    source_policy = policy or load_source_policy()
    events = load_dwcs_event_manifest()
    bouts = load_dwcs_bout_manifest()
    events_by_espn = {event.espn_event_id: event for event in events}
    universe_bout_ids: list[str] = []
    event_id_by_bout: dict[str, str] = {}
    skeleton: list[dict[str, Any]] = []
    for bout in bouts:
        event = events_by_espn[bout.espn_event_id]
        bout_id = canonical_bout_id(bout.espn_competition_id)
        event_id = canonical_event_id(bout.espn_event_id)
        occurred = event.occurrence_timestamp
        season = int(str(occurred)[:4]) if occurred else int(event.calendar_year)
        universe_bout_ids.append(bout_id)
        event_id_by_bout[bout_id] = event_id
        skeleton.append(
            {
                "bout_id": bout_id,
                "event_id": event_id,
                "season": season,
                "series_variant": bout.series_variant,
                "event_night_result": bout.event_night_result.class_,
                "current_result": bout.current_result.class_,
            }
        )
    universe_bout_ids.sort()
    frozen_ids = set(universe_bout_ids)
    observations = load_raw_observations(session)
    result_versions = load_result_versions(session)
    failures = source_failures(session)

    def _visible_row(row: dict[str, Any], *, bout_key: str) -> bool:
        if as_of is None and exclude_event_id is None:
            return True
        return observation_visible(
            effective_at=parse_iso_datetime(row.get("effective_at")),  # type: ignore[arg-type]
            observed_at=parse_iso_datetime(row.get("observed_at")),  # type: ignore[arg-type]
            proxy_published_at=parse_iso_datetime(row.get("proxy_published_at")),  # type: ignore[arg-type]
            timestamp_quality=str(row.get("timestamp_quality") or "unknown"),
            version_kind=str(row.get("version_kind") or "") or None,
            is_mutable_current=False,
            cutoff=as_of,
            event_id=event_id_by_bout.get(str(row.get(bout_key) or "")),
            exclude_event_id=exclude_event_id,
        )

    observations_for_hash = [
        row
        for row in observations
        if str(row.get("subject_id") or "") in frozen_ids
        and _visible_row({**row, "bout_id": row.get("subject_id")}, bout_key="bout_id")
    ]
    result_versions_for_hash = [
        row
        for row in result_versions
        if str(row.get("bout_id") or "") in frozen_ids and _visible_row(row, bout_key="bout_id")
    ]
    failures_for_hash = [
        row
        for row in failures
        if as_of is None
        or parse_iso_datetime(row.get("observed_at")) is None
        or parse_iso_datetime(row.get("observed_at")) <= as_of  # type: ignore[operator]
    ]
    grouped = group_observations_by_source_bout(
        observations,
        bout_ids_by_subject={row["bout_id"]: row["bout_id"] for row in skeleton},
        cutoff=as_of,
        exclude_event_id=exclude_event_id,
        event_id_by_bout=event_id_by_bout,
    )
    versions_by_bout: dict[str, list[dict[str, Any]]] = defaultdict(list)
    versions_for_class = result_versions_for_hash
    for row in versions_for_class:
        versions_by_bout[str(row["bout_id"])].append(row)

    source_bout_tiers: dict[str, dict[str, str]] = {
        source: {} for source in PHASE1_BOUT_SOURCES
    }
    bout_rows = []
    core_tier_counts = empty_tier_counts()
    season_counts: dict[str, dict[str, int]] = defaultdict(empty_tier_counts)
    class_counts: dict[str, dict[str, int]] = defaultdict(empty_tier_counts)
    field_present: Counter[str] = Counter()
    field_missing: Counter[str] = Counter()
    field_unknown: Counter[str] = Counter()
    event_night_counts = Counter()
    current_counts = Counter()
    conflicting = 0
    missing_details = 0

    for item in sorted(skeleton, key=lambda row: row["bout_id"]):
        bout_id = item["bout_id"]
        per_source: dict[str, str] = {}
        for source in PHASE1_BOUT_SOURCES:
            tier = classify_source_bout(grouped.get(source, {}).get(bout_id, []))
            per_source[source] = tier
            source_bout_tiers[source][bout_id] = tier
        core_obs: list[dict[str, Any]] = []
        for source in CORE_OVERALL_SOURCES:
            core_obs.extend(grouped.get(source, {}).get(bout_id, []))
        overall, source_class, notes = classify_overall_bout(
            source_tiers=per_source,
            core_observations=core_obs,
        )
        versions = versions_by_bout.get(bout_id, [])
        night = _latest_result(versions, version_kind="event_night")
        current = _latest_result(versions, version_kind="current")
        night_result = normalize_result(
            (night or {}).get("result_type") or item["event_night_result"]
            if overall != "missing"
            else None
        )
        current_result = normalize_result(
            (current or {}).get("result_type") or item["current_result"]
            if overall != "missing"
            else None
        )
        if overall == "missing":
            night_result = "missing"
            current_result = "missing"
        ts_quality = "unknown"
        if core_obs:
            ts_quality = str(core_obs[0].get("timestamp_quality") or "unknown")
        if overall == "conflict":
            conflicting += 1
        detail_row = current or night or {}
        any_missing_detail = False
        for field in REQUIRED_RESULT_FIELDS:
            if overall == "missing":
                field_unknown[field] += 1
                continue
            value = detail_row.get(field)
            if field in {"quality_tier", "timestamp_quality", "payload_hash"}:
                if core_obs:
                    value = core_obs[0].get(field) or (
                        overall if field == "quality_tier" else value
                    )
                    if field == "quality_tier":
                        value = overall
                    if field == "timestamp_quality":
                        value = ts_quality
                    if field == "payload_hash":
                        value = core_obs[0].get("payload_hash")
            if field == "winner_fighter_id" and night_result in {"draw", "no_contest"}:
                field_present[field] += 1
                continue
            if value in (None, ""):
                field_missing[field] += 1
                if field in {"method", "ending_round", "time_str"}:
                    any_missing_detail = True
            else:
                field_present[field] += 1
        if any_missing_detail:
            missing_details += 1
        row = build_bout_row(
            bout_id=bout_id,
            event_id=item["event_id"],
            season=int(item["season"]),
            series_variant=str(item["series_variant"]),
            overall_tier=overall,
            event_night_result=night_result,
            current_result=current_result,
            timestamp_quality=ts_quality,
            source_class=source_class,
            notes=notes,
        )
        bout_rows.append(row)
        core_tier_counts[overall] += 1
        season_counts[str(item["season"])][overall] += 1
        class_counts[source_class][overall] += 1
        event_night_counts[night_result] += 1
        current_counts[current_result] += 1

    if sum(core_tier_counts.values()) != 440:
        raise RuntimeError("core coverage denominator drifted from 440")
    seen_ids = [row.bout_id for row in bout_rows]
    if len(seen_ids) != len(set(seen_ids)):
        raise RuntimeError("duplicate bout ids in core coverage denominator")

    field_rows = []
    for field in REQUIRED_RESULT_FIELDS:
        present = int(field_present.get(field) or 0)
        missing = int(field_missing.get(field) or 0)
        unknown = int(field_unknown.get(field) or 0)
        denom = present + missing + unknown
        status = "measured"
        if denom == 0:
            status = "insufficient_sample"
        elif unknown == 440 and present == 0 and missing == 0:
            status = "unmeasured"
        field_rows.append(
            FieldCoverageRow(
                field=field,
                present=present,
                missing=missing,
                unknown=unknown,
                denominator=denom,
                status=status,  # type: ignore[arg-type]
            )
        )

    standard_bouts = sum(1 for row in bout_rows if row.series_variant == "standard")
    brazil_bouts = sum(1 for row in bout_rows if row.series_variant == "brazil")
    standard_cards = sum(1 for event in events if event.series_variant == "standard")
    brazil_cards = sum(1 for event in events if event.series_variant == "brazil")
    as_of_text = as_of.isoformat() if as_of is not None else None
    policy_hash = _policy_file_hash()
    identity = identity_coverage(session, series=series)
    regional = regional_live_payload(session, as_of=as_of)
    integrity = raw_ref_integrity(observations_for_hash)
    pit = pit_coverage(
        session,
        observations_for_hash,
        missing_required_details=missing_details,
        conflicting_outcomes=conflicting,
        future_row_leakage_failures=int(regional.get("future_invariance_failures") or 0),
    )
    source_rows = source_coverage_rows(
        source_bout_tiers=source_bout_tiers, failures=failures_for_hash
    )
    db_inv = db_inventory(session, series=series)
    if as_of is None:
        inventory = db_inv
    else:
        inventory = {
            "events": len({row.event_id for row in bout_rows if row.overall_tier != "missing"}),
            "bouts": sum(1 for row in bout_rows if row.overall_tier != "missing"),
            "fighters": 0,
            "result_versions": len(result_versions_for_hash),
            "provenance": len(observations_for_hash),
        }
    fixture_validation = {
        "identity": dict(identity.fixture_validation),
        "regional_fixture_professional": {
            "n": regional.get("fixture_professional_n"),
            "found": regional.get("fixture_professional_found"),
            "never_live_coverage": True,
        },
        "regional_fixture_amateur": {
            "n": regional.get("fixture_amateur_n"),
            "found": regional.get("fixture_amateur_found"),
            "never_live_coverage": True,
        },
        "label": "synthetic_explicit",
        "never_live_coverage": True,
    }
    licensed = LicensedStatus(
        decision_primary=None,
        licensed_primary_unselected=source_policy.licensed_audit_status.decision_primary
        is None,
        licensed_adoption_not_selected=True,
        licensed_hard_blocker=bool(
            source_policy.licensed_audit_status.licensed_hard_blocker
        ),
        phase1_global_blocker=False,
    )
    quality_dims = tuple(
        mapping_to_dimension(tier, {tier: core_tier_counts[tier]})
        for tier in QUALITY_TIERS
    )
    report_body = {
        "schema_version": 1,
        "contract_id": "dwcs_coverage",
        "contract_version": "1.0.0",
        "ticket": "DWCS-106",
        "series": series,
        "as_of": as_of_text,
        "config_hash": _config_hash(series=series, as_of=as_of_text, policy_hash=policy_hash),
        "db_hash": _db_hash(
            observations=observations_for_hash,
            result_versions=result_versions_for_hash,
            failures=failures_for_hash,
            inventory=inventory,
        ),
        "policy_hash": policy_hash,
        "evaluation_contract_hash": PINNED_CONTRACT_HASH,
        "policy_mode": source_policy.policy_mode,
        "licensed_status": licensed.model_dump(mode="json"),
        "universe_cards": 89,
        "universe_bouts": 440,
        "standard": {"cards": standard_cards, "bouts": standard_bouts},
        "brazil": {"cards": brazil_cards, "bouts": brazil_bouts},
        "event_night": {
            "decisive": int(event_night_counts.get("decisive") or 0),
            "draw": int(event_night_counts.get("draw") or 0),
            "no_contest": int(event_night_counts.get("no_contest") or 0),
        },
        "current": {
            "decisive": int(current_counts.get("decisive") or 0),
            "draw": int(current_counts.get("draw") or 0),
            "no_contest": int(current_counts.get("no_contest") or 0),
        },
        "counts_events": inventory["events"],
        "counts_bouts": inventory["bouts"],
        "counts_fighters": inventory["fighters"],
        "counts_result_versions": inventory["result_versions"],
        "counts_provenance": inventory["provenance"],
        "core_tiers": core_tier_counts,
        "core_tier_sum": sum(core_tier_counts.values()),
        "bouts": [row.model_dump(mode="json") for row in bout_rows],
        "season_dimensions": [
            mapping_to_dimension(key, season_counts[key]).model_dump(mode="json")
            for key in sorted(season_counts)
        ],
        "source_class_dimensions": [
            mapping_to_dimension(key, class_counts[key]).model_dump(mode="json")
            for key in SOURCE_CLASSES
            if key in class_counts
        ],
        "quality_tier_dimensions": [row.model_dump(mode="json") for row in quality_dims],
        "source_rows": [row.model_dump(mode="json") for row in source_rows],
        "field_rows": [row.model_dump(mode="json") for row in field_rows],
        "identity": identity.model_dump(mode="json"),
        "pit": pit.model_dump(mode="json"),
        "raw_ref_integrity": integrity.model_dump(mode="json"),
        "checkpoint_run_state": checkpoint_run_state(session).model_dump(mode="json"),
        "source_failures": failures_for_hash,
        "fixture_validation": fixture_validation,
        "regional_live": regional,
        "notes": [
            "public accessibility is not accuracy, PIT, or rights proof",
            "fixture metrics are validation only and never live coverage",
            "licensed_primary_unselected is not a Phase 1 global blocker",
        ],
        "gates": [],
    }
    report_hash = sha256_canonical(report_body)
    return CoverageReport(
        series=series,
        as_of=as_of_text,
        report_hash=report_hash,
        config_hash=str(report_body["config_hash"]),
        db_hash=str(report_body["db_hash"]),
        policy_hash=policy_hash,
        evaluation_contract_hash=PINNED_CONTRACT_HASH,
        policy_mode=source_policy.policy_mode,
        licensed_status=licensed,
        universe_cards=89,
        universe_bouts=440,
        standard=LaneCounts(cards=standard_cards, bouts=standard_bouts),
        brazil=LaneCounts(cards=brazil_cards, bouts=brazil_bouts),
        event_night=ResultLaneCounts(
            decisive=int(event_night_counts.get("decisive") or 0),
            draw=int(event_night_counts.get("draw") or 0),
            no_contest=int(event_night_counts.get("no_contest") or 0),
        ),
        current=ResultLaneCounts(
            decisive=int(current_counts.get("decisive") or 0),
            draw=int(current_counts.get("draw") or 0),
            no_contest=int(current_counts.get("no_contest") or 0),
        ),
        counts_events=inventory["events"],
        counts_bouts=inventory["bouts"],
        counts_fighters=inventory["fighters"],
        counts_result_versions=inventory["result_versions"],
        counts_provenance=inventory["provenance"],
        core_tiers=core_tier_counts,
        core_tier_sum=sum(core_tier_counts.values()),
        bouts=tuple(bout_rows),
        season_dimensions=tuple(
            mapping_to_dimension(key, season_counts[key]) for key in sorted(season_counts)
        ),
        source_class_dimensions=tuple(
            mapping_to_dimension(key, class_counts[key])
            for key in SOURCE_CLASSES
            if key in class_counts
        ),
        quality_tier_dimensions=quality_dims,
        source_rows=tuple(source_rows),
        field_rows=tuple(field_rows),
        identity=identity,
        pit=pit,
        raw_ref_integrity=integrity,
        checkpoint_run_state=checkpoint_run_state(session),
        source_failures=tuple(failures_for_hash),
        fixture_validation=fixture_validation,
        regional_live=regional,
        notes=tuple(report_body["notes"]),
        gates=(),
    )
