"""Prior-time OOF calibration and event-block bootstrap (DWCS-305)."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mma_model.cli import main
from mma_model.domain.markets import OutcomeKey
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract
from mma_model.markets.derive import ATOM_SUM_ATOL, derive_markets
from mma_model.modeling.artifacts import (
    CALIBRATED_ARTIFACT_SCHEMA_VERSION,
    CALIBRATION_EVALUATION_SCOPE,
    JOINT_EV_OMISSION_REASON,
    PRODUCTION_BOOTSTRAP_REPLICATES,
    UntrustedArtifactError,
    load_artifact,
    manifest_path_for,
)
from mma_model.modeling.baselines import run_protocol_train
from mma_model.modeling.calibration import (
    CalibrationLeakageError,
    LeakageKind,
    SigmoidCalibrator,
    _joint_nll_at_temperature,
    apply_joint_temperature,
    fit_sigmoid_calibrator,
    fit_temperature_calibrator,
    load_oof_bundle,
    reconstruct_protocol_ridge,
    validate_oof_row,
)
from mma_model.modeling.joint import (
    CONTINUE_INDEX,
    HAZARD_INDEX,
    HazardClass,
    finish_atom_key_for_hazard,
    load_joint_artifact,
    run_protocol_joint_train,
)
from mma_model.modeling.metrics import (
    MIN_RELIABLE_BIN_COUNT,
    MetricsError,
    ReliabilityStatus,
    binary_calibration_report,
    expected_calibration_error,
    joint_calibration_report,
)
from mma_model.modeling.uncertainty import (
    DEFAULT_BOOTSTRAP_REPLICATES,
    DEFAULT_BOOTSTRAP_SEED,
    BootstrapError,
    BootstrapRedrawError,
    EventBlock,
    event_block_refit_bootstrap,
    m1_event_block_bootstrap,
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
    assert report.calibration["evaluation_scope"] == CALIBRATION_EVALUATION_SCOPE
    assert report.calibration["independent_post_calibration_evaluation"] is False
    assert report.calibration["contract_hash"] == PINNED_CONTRACT_HASH
    assert report.bootstrap["calibrator_refit_per_replicate"] is False
    assert report.bootstrap["calibrator_source"] == "prior_time_oof_fixed"
    assert report.bootstrap["prediction_scope"] == "fixed_target_refit_distribution"
    assert report.bootstrap["oob"] is False
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
    assert report.calibration["evaluation_scope"] == CALIBRATION_EVALUATION_SCOPE
    assert report.calibration["independent_post_calibration_evaluation"] is False
    assert report.bootstrap["ev_semantics"] == "joint_void_mass"
    assert report.bootstrap["ev_omission_reason"] == JOINT_EV_OMISSION_REASON
    for summary in report.bootstrap["targets"].values():
        assert "ev50" not in summary


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
    for bag in bags:
        counts = Counter(bag)
        e1 = [item for item in bag if item.startswith("e1-")]
        if e1:
            m = counts["e1-a"]
            assert counts["e1-b"] == m
            assert len(e1) == m * 2
            assert len(e1) != m * 2 * 2
        e3 = [item for item in bag if item.startswith("e3-")]
        if e3:
            m3 = counts["e3-a"]
            assert counts["e3-b"] == m3
            assert counts["e3-c"] == m3
            assert len(e3) == m3 * 3
            assert len(e3) != m3 * 3 * 3


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
    assert report.n_reliable + report.n_weak == report.n_total
    assert report.n_ece_used == report.n_total
    assert report.n_suppressed_display == report.n_weak
    assert report.ece.n_ece_used == report.ece.n_total
    assert report.slope.status.value == "fitted"
    assert report.ece.n_events == len(set(events))
    small = expected_calibration_error(
        [1, 0, 1, 1, 0],
        [0.9, 0.2, 0.8, 0.7, 0.3],
        event_ids=["a", "a", "b", "c", "c"],
        min_reliable_bin_count=MIN_RELIABLE_BIN_COUNT,
    )
    assert small.n_total == 5
    assert small.n_reliable == 0
    assert small.n_weak == 5
    assert small.n_ece_used == 5
    assert small.n_suppressed_display == 5
    assert small.ece is not None
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


def _hashes() -> dict[str, str]:
    return {"estimator_hash": "a" * 64, "config_hash": "b" * 64, "data_hash": "c" * 64}


def _rewrite_calibrated(payload_path: Path, mutate: Any) -> None:
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    mutate(payload)
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    payload_path.write_bytes(blob)
    side = manifest_path_for(payload_path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    manifest["payload_sha256"] = hashlib.sha256(blob).hexdigest()
    if "calibration" in payload:
        manifest["calibration"] = payload["calibration"]
    if "bootstrap" in payload:
        manifest["bootstrap"] = payload["bootstrap"]
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_hand_computed_ece_includes_weak_bins() -> None:
    y = [0, 0, 1, 1]
    p = [0.25, 0.25, 0.75, 0.75]
    report = expected_calibration_error(y, p, n_bins=2, min_reliable_bin_count=20)
    assert report.n_total == 4
    assert report.n_reliable == 0
    assert report.n_weak == 4
    assert report.n_ece_used == 4
    assert report.ece == pytest.approx(0.25)
    for row in report.rows:
        assert row.status is ReliabilityStatus.WEAK
        assert row.observed_frequency is None
        assert row.mean_predicted is not None


def test_joint_decisive_moneyline_uses_conditional_probability() -> None:
    dist = {"a_ko_tko_r1_i0": 0.30, "b_ko_tko_r1_i0": 0.20, "draw": 0.50}
    atoms = ["a_ko_tko_r1_i0", "b_ko_tko_r1_i0", "draw"] * 4
    dists = [dist] * 12
    events = [f"e{idx}" for idx in range(12)]
    report = joint_calibration_report(dists, atoms, event_ids=events, min_reliable_bin_count=20)
    assert report.n_draw == 4
    assert report.n_decisive == 8
    assert report.terminal_nll == pytest.approx(
        -(4 * math.log(0.30) + 4 * math.log(0.20) + 4 * math.log(0.50)) / 12
    )
    assert report.moneyline is not None
    conditional = [0.6] * 8
    y = [1, 0] * 4
    expected = binary_calibration_report(y, conditional, min_reliable_bin_count=20)
    assert report.moneyline.ece.ece == pytest.approx(expected.ece.ece)
    wrong = binary_calibration_report(y, [0.30] * 8, min_reliable_bin_count=20)
    assert report.moneyline.ece.ece != pytest.approx(wrong.ece.ece)
    with pytest.raises(MetricsError, match="pA\\+pB"):
        joint_calibration_report(
            [{"a_ko": 0.0, "b_ko": 0.0, "draw": 1.0}],
            ["a_ko"],
        )


def test_m1_calibrator_is_fit_once_and_applied_to_refit_logits(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    loaded = load_artifact(train.artifact.payload_path)
    bundle = load_oof_bundle(
        loaded.oof_predictions,
        loaded.oof_exclusions,
        family="ridge",
        model_id="M1",
        n_expected=int(train.metrics["n_oof_expected"]),
        n_emitted=int(train.metrics["n_oof_emitted"]),
        final_estimator_hash=loaded.predictor.identity_hash(),
    )
    _cards, samples = reconstruct_protocol_ridge()
    by_id = {sample.sample_id: sample for sample in samples}
    target_values = {
        row.bout_id: by_id[row.bout_id].values
        for row in bundle.rows
        if row.bout_id in by_id
    }
    n_fit = {"n": 0}

    def fit_cal(rows: object) -> SigmoidCalibrator:
        n_fit["n"] += 1
        return SigmoidCalibrator(a=0.0, b=0.0)

    common = {
        "target_values": target_values,
        "n_replicates": 3,
        "seed": 9,
        "estimator_hash": loaded.predictor.identity_hash(),
        "config_hash": loaded.manifest.config_hash,
        "data_hash": loaded.manifest.data_hash,
        "contract_hash": PINNED_CONTRACT_HASH,
    }
    summary = m1_event_block_bootstrap(
        samples, bundle, fit_calibrator=fit_cal, **common
    )
    assert n_fit["n"] == 1
    for target in summary.targets:
        assert target.p05 == pytest.approx(0.5)
        assert target.p50 == pytest.approx(0.5)
        assert target.p95 == pytest.approx(0.5)
    identity = m1_event_block_bootstrap(
        samples,
        bundle,
        calibrator=SigmoidCalibrator(a=1.0, b=0.0),
        **common,
    )
    assert identity.targets[0].p50 != pytest.approx(0.5)


def test_m1_ev_formula_and_invalid_price(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    loaded = load_artifact(train.artifact.payload_path)
    bout_id = str(loaded.oof_predictions[0]["bout_id"])
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated.json",
        fixture="protocol",
        n_replicates=3,
        seed=4,
        observed_prices={bout_id: 2.5},
        contract=load_evaluation_contract(path=Path("config/evaluation/dwcs_v1.json")),
    )
    payload = report.bootstrap["targets"][bout_id]
    assert payload["observed_price"] == 2.5
    assert payload["ev50"] == pytest.approx(2.5 * payload["p50"] - 1.0)
    groups = (EventBlock("e1", ("a", "b")), EventBlock("e2", ("c",)))

    def refit(samples: tuple[str, ...]) -> int:
        return len(samples)

    def predict(fitted: int) -> dict[str, float]:
        return {"t": 0.4}

    with pytest.raises(BootstrapError, match="> 1.0"):
        event_block_refit_bootstrap(
            groups,
            refit=refit,
            predict=predict,
            n_replicates=2,
            seed=1,
            observed_prices={"t": 1.0},
            **_hashes(),
        )


def test_m2_omits_ev_even_with_observed_price(tmp_path: Path) -> None:
    train = run_protocol_joint_train(output_path=tmp_path / "joint.json")
    loaded = load_joint_artifact(train.artifact.payload_path)
    bout_id = str(loaded.oof_predictions[0]["bout_id"])
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "joint.calibrated.json",
        fixture="protocol",
        n_replicates=2,
        seed=8,
        observed_prices={bout_id: 2.2},
    )
    payload = report.bootstrap["targets"][bout_id]
    assert "ev50" not in payload
    assert payload.get("ev_omission_reason") == JOINT_EV_OMISSION_REASON
    assert report.bootstrap["ev_semantics"] == "joint_void_mass"


def test_temperature_objective_uses_fine_atom_nll() -> None:
    fine_atom = finish_atom_key_for_hazard(HazardClass.A_KO_TKO, 0)
    hazard = []
    for interval in range(6):
        row = [0.0] * 7
        if interval == 0:
            row[HAZARD_INDEX[HazardClass.A_KO_TKO]] = 8.0
        else:
            row[CONTINUE_INDEX] = 8.0
        hazard.append(row)
    decision = [0.0, 0.0, 6.0]
    train_ids = ["e0"]
    payload = {
        "bout_id": "2023-a",
        "decision_logits": decision,
        "estimator_hash": "a" * 64,
        "estimator_kind": "joint_competing_risks",
        "event_id": "dev-2023",
        "fold_id": "inner:dev-2023",
        "fold_kind": "inner",
        "hazard_logits": hazard,
        "model_id": "M2",
        "observed_fine_atom": fine_atom,
        "observed_frozen_atom": "draw",
        "observed_label": "draw",
        "scheduled_rounds": 3,
        "test_cutoff": "2023-08-22T01:00:00+00:00",
        "train_event_ids": train_ids,
        "train_event_ids_hash": sha256_canonical({"train_event_ids": train_ids}),
        "train_max_timestamp": "2018-08-11T00:00:00+00:00",
    }
    bundle = load_oof_bundle(
        [payload],
        [],
        family="joint",
        model_id="M2",
        n_expected=1,
        n_emitted=1,
        final_estimator_hash="b" * 64,
    )
    fine = apply_joint_temperature(
        hazard, decision, temperature=1.0, scheduled_rounds=3
    )
    nll_fine = _joint_nll_at_temperature(bundle, 1.0)
    assert nll_fine == pytest.approx(-math.log(fine[fine_atom]))
    nll_if_frozen = -math.log(max(fine["draw"], 1e-15))
    assert nll_fine != pytest.approx(nll_if_frozen)
    nll_hot = _joint_nll_at_temperature(bundle, 2.0)
    assert nll_hot != pytest.approx(nll_fine)


def test_rejected_draws_do_not_enter_percentiles() -> None:
    groups = (EventBlock("e1", ("a",)), EventBlock("e2", ("b",)))
    state = {"n": 0}

    def refit(samples: tuple[str, ...]) -> int:
        state["n"] += 1
        if state["n"] % 2 == 1:
            raise BootstrapRedrawError("odd_draw")
        return state["n"]

    def predict(fitted: int) -> dict[str, float]:
        return {"t": fitted / 100.0}

    summary = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=4,
        seed=1,
        **_hashes(),
    )
    assert summary.n_successful == 4
    assert summary.n_rejected == summary.n_attempts - 4
    assert summary.n_rejected >= 1
    values = [0.02, 0.04, 0.06, 0.08]
    assert summary.targets[0].p50 == pytest.approx(0.05)
    assert min(values) <= summary.targets[0].p05 <= max(values)


def test_percentile_interpolation_hand_check() -> None:
    groups = (EventBlock("e1", ("a",)), EventBlock("e2", ("b",)))
    state = {"n": 0}

    def refit(samples: tuple[str, ...]) -> str:
        return "ok"

    def predict(_fitted: str) -> dict[str, float]:
        state["n"] += 1
        return {"t": state["n"] / 10.0}

    summary = event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=5,
        seed=0,
        **_hashes(),
    )
    assert summary.targets[0].p05 == pytest.approx(0.12)
    assert summary.targets[0].p25 == pytest.approx(0.20)
    assert summary.targets[0].p50 == pytest.approx(0.30)
    assert summary.targets[0].p75 == pytest.approx(0.40)
    assert summary.targets[0].p95 == pytest.approx(0.48)


def test_stale_inner_hashes_fail_closed_with_sidecar_rewrite(tmp_path: Path) -> None:
    train = run_protocol_train(output_path=tmp_path / "ridge.json")
    report = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated.json",
        fixture="protocol",
        n_replicates=3,
        seed=5,
    )

    def mutate_events(payload: dict[str, object]) -> None:
        calibration = payload["calibration"]
        assert isinstance(calibration, dict)
        ids = list(calibration["fitting_event_ids"])
        ids[0] = "tampered-event"
        calibration["fitting_event_ids"] = ids

    _rewrite_calibrated(report.artifact.payload_path, mutate_events)
    with pytest.raises(UntrustedArtifactError, match="fitting_event_ids_hash"):
        load_artifact(report.artifact.payload_path)

    report2 = run_model_calibrate(
        artifact_path=train.artifact.payload_path,
        output_path=tmp_path / "ridge.calibrated2.json",
        fixture="protocol",
        n_replicates=3,
        seed=5,
    )

    def mutate_bootstrap(payload: dict[str, object]) -> None:
        bootstrap = payload["bootstrap"]
        assert isinstance(bootstrap, dict)
        ids = list(bootstrap["event_ids"])
        ids[0] = "tampered-card"
        bootstrap["event_ids"] = ids

    _rewrite_calibrated(report2.artifact.payload_path, mutate_bootstrap)
    with pytest.raises(UntrustedArtifactError, match="event_ids_hash"):
        load_artifact(report2.artifact.payload_path)
