"""Human/JSON serialization and sanitized evidence writers for DWCS-106."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mma_model.quality.models import CoverageReport, GateResult
from mma_model.quality.schema import load_coverage_schema, validate_coverage_json


def report_payload(report: CoverageReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def dumps_report(report: CoverageReport) -> str:
    payload = report_payload(report)
    validate_coverage_json(payload, load_coverage_schema())
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def human_report(report: CoverageReport, gates: GateResult, *, strict: bool) -> str:
    tiers = report.core_tiers
    blockers = ",".join(gates.blocker_codes) if gates.blocker_codes else "none"
    passed = ",".join(gates.passed_codes) if gates.passed_codes else "none"
    source_lines = []
    for row in report.source_rows:
        source_lines.append(
            f"{row.source}={row.status}:{row.reason or 'ok'} "
            f"mapped={row.mapped_bouts} missing={row.missing_bouts}"
        )
    return "\n".join(
        [
            f"DWCS-106 coverage series={report.series} as_of={report.as_of or 'current'}",
            (
                f"universe={report.universe_cards}/{report.universe_bouts} "
                f"standard={report.standard.cards}/{report.standard.bouts} "
                f"brazil={report.brazil.cards}/{report.brazil.bouts}"
            ),
            (
                f"db_counts events={report.counts_events} bouts={report.counts_bouts} "
                f"fighters={report.counts_fighters} "
                f"result_versions={report.counts_result_versions} "
                f"provenance={report.counts_provenance}"
            ),
            (
                f"core_tiers gold={tiers.get('gold', 0)} silver={tiers.get('silver', 0)} "
                f"bronze={tiers.get('bronze', 0)} missing={tiers.get('missing', 0)} "
                f"conflict={tiers.get('conflict', 0)} sum={report.core_tier_sum}"
            ),
            (
                f"event_night decisive={report.event_night.decisive} "
                f"draw={report.event_night.draw} nc={report.event_night.no_contest}"
            ),
            (
                f"current decisive={report.current.decisive} "
                f"draw={report.current.draw} nc={report.current.no_contest}"
            ),
            (
                f"identity scoped_pending={report.identity.scoped_pending} "
                f"unscoped_pending={report.identity.unscoped_pending} "
                f"upcoming_blocks={report.identity.upcoming_blocks} "
                f"unmatched={report.identity.unmatched} "
                f"unmatched_source_identities={report.identity.unmatched_source_identities}"
            ),
            (
                f"pit proxy={report.pit.proxy_timestamps} "
                f"unknown={report.pit.unknown_timestamps} "
                f"left_truncated={report.pit.left_truncated_histories} "
                f"leakage_future_checks={report.pit.future_row_leakage_checks_executed} "
                f"leakage_future_fail={report.pit.future_row_leakage_failures} "
                f"leakage_mutable_checks={report.pit.mutable_current_leakage_checks_executed}"
            ),
            (
                f"raw_ref ok={str(report.raw_ref_integrity.ok).lower()} "
                f"absent={report.raw_ref_integrity.blob_absent_explicit} "
                f"dangling={report.raw_ref_integrity.dangling_raw_refs}"
            ),
            *source_lines,
            f"gates_pass={passed}",
            f"gates_block={blockers}",
            (
                "licensed_status=licensed_primary_unselected "
                "(not a Phase 1 blocker)"
            ),
            f"report_hash={report.report_hash}",
            f"config_hash={report.config_hash}",
            f"db_hash={report.db_hash}",
            f"strict={str(strict).lower()} exit={gates.exit_code if strict else 0}",
        ]
    )


def sanitized_summary(report: CoverageReport, gates: GateResult) -> dict[str, Any]:
    return {
        "ticket": "DWCS-106",
        "series": report.series,
        "universe_cards": report.universe_cards,
        "universe_bouts": report.universe_bouts,
        "standard": report.standard.model_dump(mode="json"),
        "brazil": report.brazil.model_dump(mode="json"),
        "event_night": report.event_night.model_dump(mode="json"),
        "current": report.current.model_dump(mode="json"),
        "core_tiers": dict(report.core_tiers),
        "core_tier_sum": report.core_tier_sum,
        "counts": {
            "events": report.counts_events,
            "bouts": report.counts_bouts,
            "fighters": report.counts_fighters,
            "result_versions": report.counts_result_versions,
            "provenance": report.counts_provenance,
        },
        "identity": {
            "scoped_pending": report.identity.scoped_pending,
            "unscoped_pending": report.identity.unscoped_pending,
            "upcoming_blocks": report.identity.upcoming_blocks,
            "unmatched": report.identity.unmatched,
            "unmatched_source_identities": report.identity.unmatched_source_identities,
            "fixture_never_live_coverage": True,
        },
        "pit": report.pit.model_dump(mode="json"),
        "sources": [
            {
                "source": row.source,
                "source_class": row.source_class,
                "status": row.status,
                "reason": row.reason,
                "mapped_bouts": row.mapped_bouts,
                "missing_bouts": row.missing_bouts,
                "conflict_bouts": row.conflict_bouts,
                "validation_only": row.validation_only,
                "never_live_coverage": row.never_live_coverage,
                "evidence_origin": row.evidence_origin,
                "evidence_hash": row.evidence_hash,
            }
            for row in report.source_rows
        ],
        "fields": [row.model_dump(mode="json") for row in report.field_rows],
        "raw_ref_integrity": report.raw_ref_integrity.model_dump(mode="json"),
        "checkpoint_run_state": {
            "ingest_runs": report.checkpoint_run_state.ingest_runs,
            "succeeded_runs": report.checkpoint_run_state.succeeded_runs,
            "completed_runs": report.checkpoint_run_state.completed_runs,
            "failed_runs": report.checkpoint_run_state.failed_runs,
            "running_runs": report.checkpoint_run_state.running_runs,
            "checkpoints": report.checkpoint_run_state.checkpoints,
        },
        "source_failures": list(report.source_failures),
        "gates_passed": list(gates.passed_codes),
        "gates_blocked": list(gates.blocker_codes),
        "licensed_status": report.licensed_status.model_dump(mode="json"),
        "report_hash": report.report_hash,
        "config_hash": report.config_hash,
        "db_hash": report.db_hash,
        "strict_exit": gates.exit_code,
        "notes": list(report.notes),
    }


def write_coverage_evidence(
    report: CoverageReport,
    gates: GateResult,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    summary = sanitized_summary(report, gates)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# DWCS coverage and data-health gates",
        "",
        "Sanitized DWCS-106 evidence. No database files, raw HTML, or secrets.",
        "",
        f"- Report hash: `{report.report_hash}`",
        f"- Config hash: `{report.config_hash}`",
        f"- DB hash: `{report.db_hash}`",
        f"- Universe: {report.universe_cards} cards / {report.universe_bouts} bouts",
        (
            f"- Standard: {report.standard.cards}/{report.standard.bouts}; "
            f"Brazil: {report.brazil.cards}/{report.brazil.bouts}"
        ),
        (
            f"- Event-night results: decisive={report.event_night.decisive} "
            f"draw={report.event_night.draw} nc={report.event_night.no_contest}"
        ),
        (
            f"- Current results: decisive={report.current.decisive} "
            f"draw={report.current.draw} nc={report.current.no_contest}"
        ),
        (
            f"- Core tiers: gold={report.core_tiers.get('gold', 0)} "
            f"silver={report.core_tiers.get('silver', 0)} "
            f"bronze={report.core_tiers.get('bronze', 0)} "
            f"missing={report.core_tiers.get('missing', 0)} "
            f"conflict={report.core_tiers.get('conflict', 0)}"
        ),
        (
            f"- DB counts: events={report.counts_events} bouts={report.counts_bouts} "
            f"fighters={report.counts_fighters} "
            f"result_versions={report.counts_result_versions} "
            f"provenance={report.counts_provenance}"
        ),
        (
            f"- PIT: proxy={report.pit.proxy_timestamps} "
            f"unknown={report.pit.unknown_timestamps} "
            f"direct={report.pit.direct_timestamps} "
            f"left_truncated={report.pit.left_truncated_histories} "
            f"conflicting_outcomes={report.pit.conflicting_outcomes} "
            f"missing_required_details={report.pit.missing_required_details}"
        ),
        (
            f"- Raw-ref: ok={str(report.raw_ref_integrity.ok).lower()} "
            f"absent_explicit={report.raw_ref_integrity.blob_absent_explicit} "
            f"dangling={report.raw_ref_integrity.dangling_raw_refs} "
            f"present={report.raw_ref_integrity.blob_present}"
        ),
        (
            f"- Checkpoint/run: ingest_runs={report.checkpoint_run_state.ingest_runs} "
            f"succeeded={report.checkpoint_run_state.succeeded_runs} "
            f"completed={report.checkpoint_run_state.completed_runs} "
            f"failed={report.checkpoint_run_state.failed_runs} "
            f"checkpoints={report.checkpoint_run_state.checkpoints}"
        ),
        f"- Passing gates: {', '.join(gates.passed_codes) or 'none'}",
        f"- Blocking gates: {', '.join(gates.blocker_codes) or 'none'}",
        f"- Non-strict CLI exit: 0; strict CLI exit: {gates.exit_code}",
        (
            "- Licensed status is `licensed_primary_unselected` / "
            "`decision.primary=null` and is **not** a Phase 1 blocker."
        ),
        "- Fixture identity/regional metrics are validation only and never live coverage.",
        "- Public accessibility is not accuracy, PIT, or rights proof.",
        "",
        "## Sources",
        "",
    ]
    for row in report.source_rows:
        lines.append(
            f"- `{row.source}` ({row.source_class}): status=`{row.status}` "
            f"reason=`{row.reason}` mapped={row.mapped_bouts} "
            f"missing={row.missing_bouts} validation_only={row.validation_only}"
        )
    lines.extend(["", "## Fields", ""])
    for row in report.field_rows:
        lines.append(
            f"- `{row.field}`: present={row.present} missing={row.missing} "
            f"unknown={row.unknown} denominator={row.denominator} status=`{row.status}`"
        )
    lines.extend(["", "## Identity", ""])
    ident = report.identity
    lines.append(
        f"- scoped pending={ident.scoped_pending}; unscoped pending={ident.unscoped_pending}; "
        f"upcoming blocks={ident.upcoming_blocks}; unmatched={ident.unmatched}; "
        f"unmatched_source_identities={ident.unmatched_source_identities}"
    )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
