"""mma-ai bootstrap reconciliation tests (DWCS-102 Task 6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.mma_ai_bootstrap.importer import import_reconciled_observations
from mma_model.sources.mma_ai_bootstrap.reconcile import (
    BootstrapReject,
    reconcile_mma_ai_dump,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/mma_ai"
UTC = timezone.utc


def test_reject_opaque_feature_csv() -> None:
    with pytest.raises(BootstrapReject, match="opaque_precomputed_feature"):
        reconcile_mma_ai_dump(
            normalized_path=FIXTURES / "opaque_features.csv",
            ufcstats_sample_hashes={},
            expected_counts={},
        )


def test_reconcile_normalized_sample_passes() -> None:
    report = reconcile_mma_ai_dump(
        normalized_path=FIXTURES / "normalized_fights_sample.jsonl",
        ufcstats_sample_hashes={
            "fight001": "a" * 64,
            "fight002": "b" * 64,
        },
        expected_counts={"fights": 2},
    )
    assert report.row_count == 2
    assert report.hash_agreement >= 0.99
    assert report.rows[0]["fight_id"] <= report.rows[1]["fight_id"]


def test_count_mismatch_rejects(tmp_path: Path) -> None:
    path = tmp_path / "normalized.jsonl"
    path.write_text(
        (FIXTURES / "normalized_fights_sample.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(BootstrapReject, match="count mismatch"):
        reconcile_mma_ai_dump(
            normalized_path=path,
            ufcstats_sample_hashes={},
            expected_counts={"fights": 99},
        )


def test_hash_mismatch_rejects(tmp_path: Path) -> None:
    path = tmp_path / "normalized.jsonl"
    path.write_text(
        (FIXTURES / "normalized_fights_sample.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(BootstrapReject, match="hash"):
        reconcile_mma_ai_dump(
            normalized_path=path,
            ufcstats_sample_hashes={"fight001": "c" * 64, "fight002": "d" * 64},
            expected_counts={"fights": 2},
        )


def test_schema_mismatch_rejects(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps({"fight_id": "x", "event_id": "e"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BootstrapReject, match="schema mismatch"):
        reconcile_mma_ai_dump(
            normalized_path=path,
            ufcstats_sample_hashes={},
            expected_counts={"fights": 1},
        )


def test_import_emits_first_class_pit_fields() -> None:
    report = reconcile_mma_ai_dump(
        normalized_path=FIXTURES / "normalized_fights_sample.jsonl",
        ufcstats_sample_hashes={},
        expected_counts={"fights": 2},
    )
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    rows = import_reconciled_observations(report, observed_at=observed)
    assert [r.external_id for r in rows] == sorted(r.external_id for r in rows)
    assert all(r.source == "mma_ai_bootstrap" for r in rows)
    assert all(r.observed_at == observed for r in rows)
    assert all(r.quality_tier == "bronze" for r in rows)
    assert all(r.timestamp_quality_source == "mma_ai_bootstrap" for r in rows)
    assert all("quality_tier" not in r.attributes for r in rows)
    assert all("significant_strikes_landed" in r.attributes for r in rows)
