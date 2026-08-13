"""Immutable JSON/Markdown evidence: hashes, bytes, tamper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mma_model.backtest.engine import PrecomputedScorer, run_walk_forward
from mma_model.backtest.gates import EvidenceOverwriteError, EvidenceTamperError
from mma_model.backtest.report import (
    compute_evidence_hash,
    load_evidence,
    markdown_from_payload,
    verify_evidence_payload,
    write_evidence_files,
)
from tests.backtest.helpers import (
    CONTRACT,
    later_dev_card,
    make_prediction,
    make_score,
    two_bout_dev_card,
)


def _payload(**kwargs):
    return run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=PrecomputedScorer(
            {
                "dev-2017": make_score(
                    "dev-2017",
                    (
                        make_prediction("2017-a", "dev-2017", estimator_hash="e"),
                        make_prediction("2017-b", "dev-2017", estimator_hash="e"),
                    ),
                    estimator_hash="e",
                )
            }
        ),
        require_target_cards=False,
        bootstrap_replicates=8,
        generated_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        **kwargs,
    )


def test_same_inputs_reproduce_bytes(tmp_path: Path) -> None:
    first = _payload()
    second = _payload()
    assert first["content_hash"] == second["content_hash"]
    paths_a = write_evidence_files(tmp_path / "a", first)
    paths_b = write_evidence_files(tmp_path / "b", second)
    json_a = Path(paths_a["json_path"]).read_bytes()
    json_b = Path(paths_b["json_path"]).read_bytes()
    assert json_a == json_b
    md_a = Path(paths_a["markdown_path"]).read_bytes()
    md_b = Path(paths_b["markdown_path"]).read_bytes()
    assert md_a == md_b
    loaded = load_evidence(Path(paths_a["json_path"]))
    assert loaded["content_hash"] == first["content_hash"]
    md_text = markdown_from_payload(first)
    assert "DWCS-306" in md_text
    assert first["content_hash"] in md_text


def test_generated_at_is_hashed_for_reproducibility() -> None:
    a = _payload()
    b = run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=PrecomputedScorer(
            {
                "dev-2017": make_score(
                    "dev-2017",
                    (
                        make_prediction("2017-a", "dev-2017", estimator_hash="e"),
                        make_prediction("2017-b", "dev-2017", estimator_hash="e"),
                    ),
                    estimator_hash="e",
                )
            }
        ),
        require_target_cards=False,
        bootstrap_replicates=8,
        generated_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert a["content_hash"] != b["content_hash"]
    assert a["generated_at"] != b["generated_at"]
    same = _payload()
    assert a["content_hash"] == same["content_hash"]


def test_tamper_fails_hash_check(tmp_path: Path) -> None:
    payload = _payload()
    paths = write_evidence_files(tmp_path, payload)
    path = Path(paths["json_path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["universe"]["cards"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(EvidenceTamperError):
        load_evidence(path)
    with pytest.raises(EvidenceTamperError):
        verify_evidence_payload(data)
    assert compute_evidence_hash(data) != data["content_hash"]


def test_refuses_to_overwrite_different_evidence(tmp_path: Path) -> None:
    first = _payload()
    paths = write_evidence_files(tmp_path, first)
    json_path = Path(paths["json_path"])
    json_path.write_bytes(b'{"tampered": true}\n')
    with pytest.raises(EvidenceOverwriteError):
        write_evidence_files(tmp_path, first)


def test_different_hash_versions_new_run_without_mutating_prior(
    tmp_path: Path,
) -> None:
    first = _payload()
    paths_a = write_evidence_files(tmp_path, first)
    original = Path(paths_a["json_path"]).read_bytes()
    other = dict(first)
    other["ticket"] = "not-306"
    other.pop("content_hash", None)
    paths_b = write_evidence_files(tmp_path, other)
    assert paths_a["directory"] != paths_b["directory"]
    assert Path(paths_a["json_path"]).read_bytes() == original
    assert Path(paths_b["json_path"]).exists()
