"""Prior-time OOF calibration and event-block bootstrap (DWCS-305)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mma_model.cli import main
from mma_model.domain.markets import OutcomeKey
from mma_model.markets.derive import ATOM_SUM_ATOL, derive_markets
from mma_model.modeling.artifacts import (
    CALIBRATED_ARTIFACT_SCHEMA_VERSION,
    PRODUCTION_BOOTSTRAP_REPLICATES,
    UntrustedArtifactError,
    load_artifact,
    manifest_path_for,
)
from mma_model.modeling.baselines import run_protocol_train
from mma_model.modeling.calibration import (
    CalibrationLeakageError,
    LeakageKind,
    apply_joint_temperature,
    fit_sigmoid_calibrator,
    fit_temperature_calibrator,
    load_oof_bundle,
    validate_oof_row,
)
from mma_model.modeling.joint import load_joint_artifact, run_protocol_joint_train
from mma_model.modeling.metrics import (
    MIN_RELIABLE_BIN_COUNT,
    ReliabilityStatus,
    binary_calibration_report,
    expected_calibration_error,
)
from mma_model.modeling.uncertainty import (
    DEFAULT_BOOTSTRAP_REPLICATES,
    DEFAULT_BOOTSTRAP_SEED,
    EventBlock,
    event_block_refit_bootstrap,
    run_model_calibrate,
)
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK
from mma_model.quality.schema import sha256_canonical


def _ridge_oof_row(**overrides: object) -> dict[str, object]:
    train_event_ids = ["dev-2017"]
    payload: dict[str, object] = {
        "bout_id": "2023-a",
        "estimator_hash": "a" * 64,
        "estimator_kind": "standardized_ridge_logistic",
        "event_id": "dev-2023",
        "fold_id": "inner:dev-2023",
        "fold_kind": "inner",
        "model_id": "M1",
        "raw_logit": 0.2,
        "raw_probability": 0.55,
        "test_cutoff": "2023-08-22T01:00:00+00:00",
        "train_event_ids": train_event_ids,
        "train_event_ids_hash": sha256_canonical({"train_event_ids": train_event_ids}),
        "train_max_timestamp": "2018-08-11T00:00:00+00:00",
        "y": 1,
    }
    payload.update(overrides)
    if "train_event_ids" in overrides and "train_event_ids_hash" not in overrides:
        ids = list(overrides["train_event_ids"])  # type: ignore[arg-type]
        payload["train_event_ids_hash"] = sha256_canonical({"train_event_ids": ids})
    return payload


def test_oof_rejects_same_card_in_sample() -> None:
    row = _ridge_oof_row(event_id="dev-2017", train_event_ids=["dev-2017", "brazil-2018"])
    with pytest.raises(CalibrationLeakageError) as exc:
        validate_oof_row(row, family="ridge", final_estimator_hash="b" * 64)
    assert exc.value.kind is LeakageKind.SAME_CARD


def test_oof_rejects_future_train_timestamp() -> None:
    row = _ridge_oof_row(train_max_timestamp="2023-08-22T01:00:00+00:00")
    with pytest.raises(CalibrationLeakageError) as exc:
        validate_oof_row(row, family="ridge", final_estimator_hash="b" * 64)
    assert exc.value.kind is LeakageKind.FUTURE


def test_oof_rejects_2025_target() -> None:
    row = _ridge_oof_row(
        bout_id="2025-a",
        event_id="hold-2025",
        test_cutoff="2025-08-12T01:00:00+00:00",
    )
    with pytest.raises(CalibrationLeakageError) as exc:
        validate_oof_row(row, family="ridge", final_estimator_hash="b" * 64)
    assert exc.value.kind is LeakageKind.LOCKED_2025


def test_oof_rejects_holdout_fold_kind() -> None:
    row = _ridge_oof_row(fold_kind="holdout")
    with pytest.raises(CalibrationLeakageError) as exc:
        validate_oof_row(row, family="ridge", final_estimator_hash="b" * 64)
    assert exc.value.kind is LeakageKind.FOLD_KIND


def test_oof_rejects_final_estimator_hash() -> None:
    final_hash = "c" * 64
    row = _ridge_oof_row(estimator_hash=final_hash)
    with pytest.raises(CalibrationLeakageError) as exc:
        validate_oof_row(row, family="ridge", final_estimator_hash=final_hash)
    assert exc.value.kind is LeakageKind.FINAL_ESTIMATOR


def test_oof_rejects_duplicates() -> None:
    row = _ridge_oof_row()
    with pytest.raises(CalibrationLeakageError) as exc:
        load_oof_bundle(
            [row, dict(row)],
            [],
            family="ridge",
            model_id="M1",
            n_expected=2,
            n_emitted=2,
            final_estimator_hash="b" * 64,
        )
    assert exc.value.kind is LeakageKind.DUPLICATE


def test_oof_count_mismatch_fails_closed() -> None:
    row = _ridge_oof_row()
    with pytest.raises(CalibrationLeakageError) as exc:
        load_oof_bundle(
            [row],
            [],
            family="ridge",
            model_id="M1",
            n_expected=4,
            n_emitted=1,
            final_estimator_hash="b" * 64,
        )
    assert exc.value.kind is LeakageKind.COUNT_MISMATCH


def test_m1_sigmoid_uses_protocol_oof_and_is_finite(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    loaded = load_artifact(train.artifact.payload_path)
    assert loaded.oof_predictions
    assert "2025-a" not in {row["bout_id"] for row in loaded.oof_predictions}
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated.json",
        fixture="protocol",
        n_replicates=3,
        seed=DEFAULT_BOOTSTRAP_SEED,
    )
    assert report.family == "ridge"
    assert report.production_qualified is False
    assert math_isfinite(report.calibration["a"])
    assert math_isfinite(report.calibration["b"])
    assert report.metrics_pre["log_loss"] is not None
    assert report.metrics_post["log_loss"] is not None
    calibrated = load_artifact(report.artifact.payload_path)
    assert calibrated.calibrated is True
    assert calibrated.manifest.schema_version == CALIBRATED_ARTIFACT_SCHEMA_VERSION
    assert train.artifact.payload_path.read_bytes() != report.artifact.payload_path.read_bytes()


def math_isfinite(value: object) -> bool:
    return isinstance(value, (int, float)) and value == value and abs(float(value)) != float("inf")


def test_m2_temperature_preserves_normalization_and_coherence(tmp_path: Path) -> None:
    train = run_protocol_joint_train(output_path=tmp_path / "joint.json")
    loaded = load_joint_artifact(train.artifact.payload_path)
    assert loaded.oof_predictions
    first = loaded.oof_predictions[0]
    assert first["observed_fine_atom"]
    assert first["observed_label"]
    bundle = load_oof_bundle(
        loaded.oof_predictions,
        loaded.oof_exclusions,
        family="joint",
        model_id="M2",
        n_expected=int(train.metrics["n_oof_expected"]),
        n_emitted=int(train.metrics["n_oof_emitted"]),
        final_estimator_hash=loaded.predictor.identity_hash(),
    )
    calibrator = fit_temperature_calibrator(bundle)
    assert calibrator.temperature > 0
    for row in bundle.rows:
        assert row.hazard_logits is not None
        assert row.decision_logits is not None
        assert row.scheduled_rounds is not None
        fine = apply_joint_temperature(
            row.hazard_logits,
            row.decision_logits,
            temperature=calibrator.temperature,
            scheduled_rounds=row.scheduled_rounds,
        )
        assert abs(sum(fine.values()) - 1.0) <= 1e-10
        markets = derive_markets(fine, scheduled_rounds=row.scheduled_rounds)
        p_a = sum(value for key, value in fine.items() if str(key).startswith("a_"))
        assert markets.moneyline[OutcomeKey.FIGHTER_A] == pytest.approx(p_a, abs=ATOM_SUM_ATOL)


def test_m2_protocol_calibrate_non_production_replicates(tmp_path: Path) -> None:
    train = run_protocol_joint_train(output_path=tmp_path / "joint.json")
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "joint.calibrated.json",
        fixture="protocol",
        n_replicates=2,
        seed=7,
    )
    assert report.family == "joint"
    assert report.production_qualified is False
    assert report.calibration["temperature"] > 0
    loaded = load_joint_artifact(report.artifact.payload_path)
    assert loaded.calibrated is True


def test_event_block_refits_are_reproducible_and_group_cards() -> None:
    class _Sample:
        def __init__(self, event_id: str, sample_id: str) -> None:
            self.event_id = event_id
            self.sample_id = sample_id

    groups = (
        EventBlock("e1", (_Sample("e1", "e1-a"), _Sample("e1", "e1-b"))),
        EventBlock("e2", (_Sample("e2", "e2-a"),)),
        EventBlock("e3", (_Sample("e3", "e3-a"), _Sample("e3", "e3-b"), _Sample("e3", "e3-c"))),
    )
    bags: list[tuple[str, ...]] = []

    def refit(samples: tuple[_Sample, ...]) -> str:
        bags.append(tuple(item.sample_id for item in samples))
        counts = Counter(item.sample_id for item in samples)
        for event_id in {"e1", "e2", "e3"}:
            event_bouts = [item.sample_id for item in samples if item.event_id == event_id]
            if not event_bouts:
                continue
            event_ids = {item.sample_id for item in samples if item.event_id == event_id}
            event_counts = {bout: counts[bout] for bout in event_ids}
            assert len(set(event_counts.values())) == 1
        return "ok"

    def predict(_fitted: str) -> dict[str, float]:
        return {"t": 0.4}

    hashes = {"estimator_hash": "a" * 64, "config_hash": "b" * 64, "data_hash": "c" * 64}
    first = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=8,
        seed=11,
        **hashes,
    )
    n_first = len(bags)
    second = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=8,
        seed=11,
        **hashes,
    )
    assert first.to_dict() == second.to_dict()
    assert bags[:n_first] == bags[n_first:]
    other = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=8,
        seed=12,
        **hashes,
    )
    assert first.to_dict() != other.to_dict()
    assert first.n_successful == 8
    assert "ev50" not in first.targets[0].to_dict()


def test_default_bootstrap_is_200_and_lightweight_200_refit() -> None:
    assert DEFAULT_BOOTSTRAP_REPLICATES == 200
    assert PRODUCTION_BOOTSTRAP_REPLICATES == 200
    groups = (EventBlock("e1", ("a", "b")), EventBlock("e2", ("c",)))
    n_calls = {"n": 0}

    def refit(samples: tuple[str, ...]) -> int:
        n_calls["n"] += 1
        return len(samples)

    def predict(fitted: int) -> dict[str, float]:
        return {"t": 0.01 * fitted}

    summary = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        seed=3,
        estimator_hash="a" * 64,
        config_hash="b" * 64,
        data_hash="c" * 64,
    )
    assert summary.n_successful == 200
    assert summary.n_replicates == 200
    assert summary.production_qualified is True
    assert n_calls["n"] == 200
    priced = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=5,
        seed=3,
        observed_prices={"t": 2.0},
        estimator_hash="a" * 64,
        config_hash="b" * 64,
        data_hash="c" * 64,
    )
    payload = priced.targets[0].to_dict()
    assert payload["observed_price"] == 2.0
    assert payload["ev50"] == pytest.approx(2.0 * payload["p50"] - 1.0)
    unpriced = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=5,
        seed=3,
        estimator_hash="a" * 64,
        config_hash="b" * 64,
        data_hash="c" * 64,
    )
    assert "ev50" not in unpriced.targets[0].to_dict()
    assert unpriced.targets[0].ev50 is None


def test_ece_and_slope_counts_reconcile_and_weak_bins_are_suppressed() -> None:
    y = [0, 1] * 25
    p = [0.12 if item == 0 else 0.88 for item in y]
    events = [f"e{idx // 2}" for idx in range(len(y))]
    report = binary_calibration_report(y, p, event_ids=events, min_reliable_bin_count=20)
    assert report.n_total == 50
    assert report.n_used + report.n_suppressed == report.n_total
    assert report.ece.n_used + report.ece.n_suppressed == report.ece.n_total
    assert report.slope.status.value == "fitted"
    assert report.ece.n_events == len(set(events))
    small = expected_calibration_error(
        [1, 0, 1, 1, 0],
        [0.9, 0.2, 0.8, 0.7, 0.3],
        event_ids=["a", "a", "b", "c", "c"],
        min_reliable_bin_count=MIN_RELIABLE_BIN_COUNT,
    )
    assert small.n_total == 5
    assert small.n_used == 0
    assert small.n_suppressed == 5
    assert small.ece is None
    assert small.n_weak_bins >= 1
    for row in small.rows:
        if row.status is ReliabilityStatus.WEAK:
            assert row.observed_frequency is None
            assert row.count < MIN_RELIABLE_BIN_COUNT


def test_calibrated_artifact_round_trip_and_tamper_fails_closed(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    uncalibrated = load_artifact(train.artifact.payload_path)
    assert uncalibrated.calibrated is False
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated.json",
        fixture="protocol",
        n_replicates=3,
        seed=5,
    )
    loaded = load_artifact(report.artifact.payload_path)
    assert loaded.calibrated is True
    assert loaded.payload["calibration"]["a"] == report.calibration["a"]
    payload = json.loads(report.artifact.payload_path.read_text(encoding="utf-8"))
    payload["calibration"]["a"] = float("nan")
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    report.artifact.payload_path.write_bytes(blob)
    side = manifest_path_for(report.artifact.payload_path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    manifest["payload_sha256"] = hashlib.sha256(blob).hexdigest()
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(UntrustedArtifactError, match="finite"):
        load_artifact(report.artifact.payload_path)


def test_malformed_bootstrap_fails_closed(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated.json",
        fixture="protocol",
        n_replicates=3,
        seed=5,
    )
    payload = json.loads(report.artifact.payload_path.read_text(encoding="utf-8"))
    payload["bootstrap"]["n_successful"] = 199
    payload["bootstrap"]["production_qualified"] = True
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    report.artifact.payload_path.write_bytes(blob)
    side = manifest_path_for(report.artifact.payload_path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    manifest["payload_sha256"] = hashlib.sha256(blob).hexdigest()
    manifest["bootstrap"] = payload["bootstrap"]
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(UntrustedArtifactError):
        load_artifact(report.artifact.payload_path)


def test_model_calibrate_cli_protocol_and_refuses_live_db(tmp_path: Path, capsys) -> None:
    artifact = tmp_path / "ridge.json"
    code = main(
        [
            "model",
            "train",
            "--spec",
            "config/model_specs/ridge_v1.yaml",
            "--fixture",
            "protocol",
            "--output",
            str(artifact),
        ]
    )
    assert code == EXIT_OK
    live = main(
        [
            "model",
            "calibrate",
            "--artifact",
            str(artifact),
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert live == EXIT_INTERNAL
    err = capsys.readouterr().out
    assert "refusing live data/mma.db" in err
    calibrated = tmp_path / "ridge.calibrated.json"
    code = main(
        [
            "model",
            "calibrate",
            "--artifact",
            str(artifact),
            "--contract",
            "config/evaluation/dwcs_v1.json",
            "--fixture",
            "protocol",
            "--output",
            str(calibrated),
            "--bootstrap-replicates",
            "3",
        ]
    )
    assert code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["production_qualified"] is False
    assert printed["a"] is not None
    assert Path(printed["artifact_path"]).is_file()


def test_m1_protocol_oof_exclusions_reconcile(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    metrics = report.metrics
    assert metrics["n_oof_expected"] == metrics["n_oof_emitted"] + metrics["n_oof_excluded_bouts"]
    loaded = load_artifact(report.artifact.payload_path)
    assert loaded.oof_predictions
    assert loaded.payload["oof_predictions"]
    for row in loaded.oof_predictions:
        cutoff = datetime.fromisoformat(str(row["test_cutoff"]).replace("Z", "+00:00"))
        assert cutoff.tzinfo is not None
        assert cutoff.astimezone(UTC).year != 2025
        assert str(row["event_id"]) not in set(row["train_event_ids"])


def test_sigmoid_fit_on_synthetic_oof_is_two_parameter() -> None:
    rows = []
    for idx in range(12):
        train_ids = ["e0"]
        y = idx % 2
        logit = -1.0 if y == 0 else 1.2
        p = 1.0 / (1.0 + pow(2.718281828, -logit))
        rows.append(
            _ridge_oof_row(
                bout_id=f"b{idx}",
                event_id=f"e{idx + 1}",
                fold_id=f"inner:e{idx + 1}",
                train_event_ids=train_ids,
                raw_logit=logit,
                raw_probability=p,
                y=y,
                estimator_hash="d" * 64,
            )
        )
    bundle = load_oof_bundle(
        rows,
        [{"n_test": 0, "reason_code": "empty_train", "test_bout_ids": []}],
        family="ridge",
        model_id="M1",
        n_expected=12,
        n_emitted=12,
        final_estimator_hash="e" * 64,
    )
    calibrator = fit_sigmoid_calibrator(bundle)
    assert math_isfinite(calibrator.a)
    assert math_isfinite(calibrator.b)
    mid = calibrator.apply_logit(0.0)
    assert 0.0 < mid < 1.0
