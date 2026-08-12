"""DWCS-106 coverage, strict health gates, and leakage audits."""

from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import evaluate_strict_gates, report_with_gates
from mma_model.quality.leakage import assert_future_row_invariance
from mma_model.quality.models import CoverageReport, GateResult
from mma_model.quality.schema import CoverageSchemaError, validate_coverage_json

__all__ = [
    "CoverageReport",
    "CoverageSchemaError",
    "GateResult",
    "assert_future_row_invariance",
    "compute_coverage_report",
    "evaluate_strict_gates",
    "report_with_gates",
    "validate_coverage_json",
]
