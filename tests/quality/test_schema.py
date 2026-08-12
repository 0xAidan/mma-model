"""JSON schema adversarial tests for the DWCS-106 coverage contract."""

from __future__ import annotations

import pytest

from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.schema import (
    CoverageSchemaError,
    load_coverage_schema,
    validate_coverage_json,
)
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import make_empty_db


def _minimal_invalid() -> dict:
    return {
        "schema_version": 1,
        "contract_id": "dwcs_coverage",
        "contract_version": "1.0.0",
        "ticket": "DWCS-106",
        "series": "dwcs",
    }


def test_schema_file_exists_and_forbids_additional_properties() -> None:
    schema = load_coverage_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["bouts"]["minItems"] == 440
    assert schema["properties"]["bouts"]["maxItems"] == 440
    assert schema["properties"]["source_failures"]["items"]["$ref"] == "#/$defs/source_failure"
    assert schema["$defs"]["source_failure"]["additionalProperties"] is False
    assert schema["$defs"]["regional_live"]["additionalProperties"] is False
    assert schema["$defs"]["coverage_fixture_validation"]["additionalProperties"] is False
    assert schema["$defs"]["timestamp_quality"]["enum"] == [
        "direct_source_timestamp",
        "revision_snapshot",
        "publication_proxy",
        "unknown",
    ]
    assert "not_applicable" in schema["$defs"]["gate_status"]["enum"]


def test_schema_rejects_unknown_quality_tier_enum() -> None:
    schema = load_coverage_schema()
    payload = _minimal_invalid()
    payload["bouts"] = [
        {
            "bout_id": "x",
            "event_id": "y",
            "season": 2017,
            "series_variant": "standard",
            "overall_tier": "platinum",
            "event_night_result": "decisive",
            "current_result": "decisive",
            "timestamp_quality": "unknown",
            "source_class": "internal_manifest",
            "notes": [],
        }
    ]
    with pytest.raises(CoverageSchemaError):
        validate_coverage_json(payload, schema)


def test_schema_rejects_additional_root_field() -> None:
    schema = load_coverage_schema()
    payload = _minimal_invalid()
    payload["unexpected"] = 1
    with pytest.raises(CoverageSchemaError):
        validate_coverage_json(payload, schema)


def test_schema_rejects_additional_field_on_complete_payload(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report, _gates = report_with_gates(
                compute_coverage_report(series="dwcs", session=session, policy=policy),
                policy,
            )
        payload = report.model_dump(mode="json")
        payload["unexpected"] = True
        with pytest.raises(CoverageSchemaError, match="additional field"):
            validate_coverage_json(payload, load_coverage_schema())
    finally:
        env["engine"].dispose()


def test_schema_rejects_missing_required_root_field() -> None:
    schema = load_coverage_schema()
    with pytest.raises(CoverageSchemaError, match="missing required"):
        validate_coverage_json(_minimal_invalid(), schema)
