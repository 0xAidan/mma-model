"""Strict DWCS-106 health gates. Licensed-primary-null is never a global blocker."""

from __future__ import annotations

from typing import Any

from mma_model.evaluation.contract import load_evaluation_contract
from mma_model.quality.constants import (
    EXIT_OK,
    EXIT_STRICT_BLOCKERS,
    GATE_CORE_DENOMINATOR,
    GATE_CROSS_SOURCE_RECONCILIATION,
    GATE_FUTURE_ROW_LEAKAGE,
    GATE_IDENTITY_CONFLICT,
    GATE_LICENSED_PRIMARY,
    GATE_MANIFEST_REPRESENTATION,
    GATE_MISSING_REQUIRED_DETAILS,
    GATE_MUTABLE_CURRENT_LEAKAGE,
    GATE_PRE_FIGHT_AGREEMENT,
    GATE_RAW_REF_INTEGRITY,
    GATE_REGIONAL_AMATEUR,
    GATE_REGIONAL_PROFESSIONAL,
    GATE_RESULT_AGREEMENT,
    GATE_UFCSTATS_LIVE,
    LICENSED_NON_BLOCKER_CODES,
)
from mma_model.quality.models import CoverageReport, GateAssessment, GateResult
from mma_model.quality.schema import sha256_canonical
from mma_model.quality.universe import load_universe_contract
from mma_model.sources.policy import SourcePolicy, load_source_policy


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _sample_gate(
    *,
    code: str,
    segment: str,
    numerator: int,
    denominator: int,
    threshold: float,
    extra_fail: bool = False,
    extra_reason: str | None = None,
) -> GateAssessment:
    if denominator <= 0:
        return GateAssessment(
            code=code,
            segment=segment,
            status="insufficient_sample",
            blocking=True,
            numerator=numerator,
            denominator=denominator,
            threshold=threshold,
            reason="insufficient_sample",
        )
    rate = _rate(numerator, denominator)
    ok = (rate is not None and rate >= threshold) and not extra_fail
    return GateAssessment(
        code=code,
        segment=segment,
        status="pass" if ok else "fail",
        blocking=not ok,
        numerator=numerator,
        denominator=denominator,
        threshold=threshold,
        reason=None if ok else (extra_reason or "below_threshold"),
    )


def _pass_fail(
    *,
    code: str,
    segment: str,
    ok: bool,
    blocking: bool,
    numerator: int | None = None,
    denominator: int | None = None,
    threshold: float | None = None,
    reason: str | None = None,
) -> GateAssessment:
    return GateAssessment(
        code=code,
        segment=segment,
        status="pass" if ok else "fail",
        blocking=blocking and not ok,
        numerator=numerator,
        denominator=denominator,
        threshold=threshold,
        reason=None if ok else reason,
    )


def evaluate_strict_gates(
    report: CoverageReport,
    policy: SourcePolicy | None = None,
) -> GateResult:
    source_policy = policy or load_source_policy()
    contract = load_evaluation_contract()
    universe = load_universe_contract()
    universe_bouts = universe.bouts
    gates_retained = source_policy.gates_retained
    go_live = contract.go_live_gates.data
    regional = report.regional_live or {}
    assessments: list[GateAssessment] = []

    represented = (
        int(report.core_tiers.get("gold") or 0)
        + int(report.core_tiers.get("silver") or 0)
        + int(report.core_tiers.get("bronze") or 0)
        + int(report.core_tiers.get("conflict") or 0)
    )
    categorized = report.core_tier_sum == universe_bouts and represented + int(
        report.core_tiers.get("missing") or 0
    ) == universe_bouts
    assessments.append(
        _pass_fail(
            code=GATE_CORE_DENOMINATOR,
            segment="core_overall",
            ok=categorized and report.core_tier_sum == universe_bouts,
            blocking=True,
            numerator=report.core_tier_sum,
            denominator=universe_bouts,
            reason="dropped_or_duplicate_denominator",
        )
    )
    assessments.append(
        _pass_fail(
            code=GATE_MANIFEST_REPRESENTATION,
            segment="internal_manifest",
            ok=represented == universe_bouts and int(report.core_tiers.get("missing") or 0) == 0,
            blocking=True,
            numerator=represented,
            denominator=universe_bouts,
            reason="uncategorized_or_missing_core_bouts",
        )
    )

    comparable = 0
    agreed = 0
    ufcstats = next(
        (row for row in report.source_rows if row.source == "ufcstats_public"), None
    )
    if ufcstats is not None and ufcstats.mapped_bouts > 0:
        comparable = ufcstats.mapped_bouts
        agreed = ufcstats.mapped_bouts - ufcstats.conflict_bouts
    assessments.append(
        _sample_gate(
            code=GATE_CROSS_SOURCE_RECONCILIATION,
            segment="ufcstats_public_vs_manifest",
            numerator=agreed,
            denominator=comparable,
            threshold=float(gates_retained.cross_source_reconciliation_min_where_comparable),
        )
    )
    assessments.append(
        _sample_gate(
            code=GATE_RESULT_AGREEMENT,
            segment="result_agreement_comparable",
            numerator=agreed,
            denominator=comparable,
            threshold=float(go_live.min_result_agreement),
        )
    )

    identity_ok = (
        report.identity.scoped_unresolved_conflicts
        <= gates_retained.unresolved_evaluated_or_upcoming_identity_conflicts_max
        and report.identity.upcoming_blocks
        <= gates_retained.unresolved_evaluated_or_upcoming_identity_conflicts_max
    )
    assessments.append(
        _pass_fail(
            code=GATE_IDENTITY_CONFLICT,
            segment="evaluated_or_upcoming_identity",
            ok=identity_ok,
            blocking=True,
            numerator=report.identity.scoped_unresolved_conflicts,
            denominator=0,
            threshold=0.0,
            reason="identity_conflict",
        )
    )
    assessments.append(
        _pass_fail(
            code=GATE_FUTURE_ROW_LEAKAGE,
            segment="pit_future_row",
            ok=(
                report.pit.future_row_leakage_checks_executed > 0
                and report.pit.future_row_leakage_failures
                <= gates_retained.future_row_leakage_failures_max
            ),
            blocking=True,
            numerator=report.pit.future_row_leakage_failures,
            denominator=report.pit.future_row_leakage_checks_executed,
            reason="future_row_leakage",
        )
    )
    assessments.append(
        _pass_fail(
            code=GATE_MUTABLE_CURRENT_LEAKAGE,
            segment="mutable_current_profile",
            ok=(
                report.pit.mutable_current_leakage_checks_executed > 0
                and report.pit.mutable_current_leakage_failures
                <= gates_retained.mutable_current_as_historical_feature_failures_max
            ),
            blocking=True,
            numerator=report.pit.mutable_current_leakage_failures,
            denominator=report.pit.mutable_current_leakage_checks_executed,
            reason="mutable_current_leakage",
        )
    )

    pro_n = int(regional.get("professional_n") or 0)
    pro_found = int(regional.get("professional_found") or 0)
    pro_failed = int(regional.get("professional_source_failed") or 0)
    assessments.append(
        _sample_gate(
            code=GATE_REGIONAL_PROFESSIONAL,
            segment="regional_live_professional",
            numerator=pro_found + pro_failed,
            denominator=pro_n,
            threshold=0.95,
        )
    )
    am_n = int(regional.get("amateur_n") or 0)
    am_found = int(regional.get("amateur_found") or 0)
    am_failed = int(regional.get("amateur_source_failed") or 0)
    assessments.append(
        _sample_gate(
            code=GATE_REGIONAL_AMATEUR,
            segment="regional_live_amateur",
            numerator=am_found + am_failed,
            denominator=am_n,
            threshold=0.80,
        )
    )
    agree_n = int(regional.get("pre_fight_agreement_n") or 0)
    agree_d = int(regional.get("pre_fight_agreement_d") or 0)
    assessments.append(
        _sample_gate(
            code=GATE_PRE_FIGHT_AGREEMENT,
            segment="pre_fight_record_agreement",
            numerator=agree_n,
            denominator=agree_d,
            threshold=0.98,
        )
    )

    ufc_ok = (
        ufcstats is not None
        and ufcstats.status == "present"
        and ufcstats.mapped_bouts == universe_bouts
    )
    assessments.append(
        _pass_fail(
            code=GATE_UFCSTATS_LIVE,
            segment="ufcstats_public",
            ok=ufc_ok,
            blocking=True,
            numerator=0 if ufcstats is None else ufcstats.mapped_bouts,
            denominator=universe_bouts,
            reason=None if ufc_ok else (ufcstats.reason if ufcstats else "unmeasured"),
        )
    )
    for row in report.source_rows:
        if row.source in {"tapology_public", "sherdog_public", "combat_registry"}:
            live_ok = row.status == "present" and row.mapped_bouts > 0
            assessments.append(
                _pass_fail(
                    code=f"live_{row.status}:{row.source}",
                    segment=f"regional_{row.source}",
                    ok=live_ok,
                    blocking=True,
                    numerator=row.mapped_bouts,
                    denominator=universe_bouts,
                    reason=row.reason or row.status,
                )
            )

    method_row = next((row for row in report.field_rows if row.field == "method"), None)
    details_ok = (
        method_row is not None
        and method_row.missing == 0
        and method_row.present == universe_bouts
    )
    assessments.append(
        _pass_fail(
            code=GATE_MISSING_REQUIRED_DETAILS,
            segment="required_result_fields",
            ok=details_ok,
            blocking=True,
            numerator=0 if method_row is None else method_row.present,
            denominator=universe_bouts,
            reason="missing_method_round_or_time",
        )
    )
    assessments.append(
        _pass_fail(
            code=GATE_RAW_REF_INTEGRITY,
            segment="provenance_raw_ref",
            ok=report.raw_ref_integrity.ok
            and report.raw_ref_integrity.unverifiable == 0
            and report.raw_ref_integrity.missing_blobs == 0
            and report.raw_ref_integrity.corrupt_blobs == 0,
            blocking=True,
            numerator=report.raw_ref_integrity.dangling_raw_refs,
            denominator=report.raw_ref_integrity.blob_absent_explicit
            + report.raw_ref_integrity.blob_present,
            reason="dangling_or_malformed_raw_ref",
        )
    )
    assessments.append(
        GateAssessment(
            code=GATE_LICENSED_PRIMARY,
            segment="licensed_adoption",
            status="informational",
            blocking=False,
            numerator=None,
            denominator=None,
            threshold=None,
            reason="licensed_primary_unselected",
        )
    )

    ordered = tuple(sorted(assessments, key=lambda row: (row.segment, row.code)))
    blockers = tuple(
        row.code
        for row in ordered
        if row.blocking and row.status in {"fail", "insufficient_sample"}
        and row.code not in LICENSED_NON_BLOCKER_CODES
    )
    passed = tuple(row.code for row in ordered if row.status == "pass")
    informational = tuple(row.code for row in ordered if row.status == "informational")
    ok = len(blockers) == 0
    return GateResult(
        ok=ok,
        exit_code=EXIT_OK if ok else EXIT_STRICT_BLOCKERS,
        blocker_codes=blockers,
        passed_codes=passed,
        informational_codes=informational,
        gates=ordered,
    )


def attach_gates(report: CoverageReport, result: GateResult) -> CoverageReport:
    payload: dict[str, Any] = report.model_dump(mode="json")
    payload["gates"] = [row.model_dump(mode="json") for row in result.gates]
    payload.pop("report_hash", None)
    payload["report_hash"] = sha256_canonical(payload)
    return CoverageReport.model_validate(payload)


def report_with_gates(
    report: CoverageReport, policy: SourcePolicy | None = None
) -> tuple[CoverageReport, GateResult]:
    result = evaluate_strict_gates(report, policy)
    return attach_gates(report, result), result
