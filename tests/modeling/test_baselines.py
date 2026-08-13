"""M0/M1 baselines, versioned JSON artifacts, and deprecation wrappers (DWCS-303)."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import joblib
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.cli import main
from mma_model.db.models import Event, Fight, Fighter, FightFighterStats
from mma_model.db.session import create_all_for_tests
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import FeatureSnapshot
from mma_model.features.spec import FEATURE_NAMES, spec_hash, swap_values
from mma_model.labels.outcomes import training_label as real_training_label
from mma_model.modeling.artifacts import (
    GIT_COMMIT_HEX,
    PINNED_RIDGE_SPEC_HASH,
    UNKNOWN_CODE_COMMIT,
    ArtifactChecksumMismatchError,
    ArtifactFeatureOrderMismatchError,
    ArtifactSpecMismatchError,
    UntrustedArtifactError,
    load_artifact,
    load_ridge_spec,
    manifest_path_for,
)
from mma_model.modeling.baselines import (
    SWAP_ATOL,
    MissingNoVig,
    PreCutoffMoneyline,
    coin_flip_win_prob,
    labeled_samples_from_snapshot,
    no_vig_win_prob,
    predict_loaded_ridge,
    predict_loaded_ridge_raw,
    protocol_training_universe,
    run_protocol_train,
    sequential_rating_win_prob,
)
from mma_model.modeling.splits import HoldoutLockedError
from mma_model.predict.train import DEPRECATED_RANDOM_SPLIT_KEY, train_and_save
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK
from tests.features.helpers import add_bout, add_event, add_result, cutoff_of, dt, named


def _flatten_keys(payload: object) -> list[str]:
    keys: list[str] = []
    stack: list[object] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                keys.append(str(key))
                stack.append(value)
            continue
        if isinstance(current, list):
            stack.extend(current)
    return keys


def _rewrite_payload_with_matching_checksum(path: Path, blob: bytes) -> None:
    path.write_bytes(blob)
    side = manifest_path_for(path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    manifest["payload_sha256"] = hashlib.sha256(blob).hexdigest()
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mutate_json_payload(path: Path, mutator) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutator(payload)
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _rewrite_payload_with_matching_checksum(path, blob)


def _seed_legacy_fights(session) -> None:
    session.add_all([Fighter(id=f"f{idx}", name=f"Fighter {idx}") for idx in range(4)])
    start = date(2020, 1, 1)
    for idx in range(12):
        event_id = f"e{idx}"
        fight_id = f"g{idx}"
        a_id = f"f{idx % 4}"
        b_id = f"f{(idx + 1) % 4}"
        session.add(
            Event(id=event_id, name=f"Event {idx}", event_date=start + timedelta(days=idx * 14))
        )
        winner_id = a_id if idx % 2 == 0 else b_id
        session.add(
            Fight(
                id=fight_id,
                event_id=event_id,
                fighter_a_id=a_id,
                fighter_b_id=b_id,
                winner_id=winner_id,
                method="U-DEC",
                detail_ingested=True,
            )
        )
        session.add(
            FightFighterStats(
                fight_id=fight_id,
                fighter_id=a_id,
                sig_str_landed=20 + idx,
                sig_str_attempted=40,
                td_landed=1,
                td_attempted=3,
                sub_att=1,
                ctrl_seconds=60,
            )
        )
        session.add(
            FightFighterStats(
                fight_id=fight_id,
                fighter_id=b_id,
                sig_str_landed=10 + idx,
                sig_str_attempted=40,
                td_landed=0,
                td_attempted=2,
                sub_att=0,
                ctrl_seconds=20,
            )
        )
    session.commit()


def test_legacy_train_keys_are_deprecated_not_holdout(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-train.db'}", future=True)
    create_all_for_tests(engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        _seed_legacy_fights(session)
        report = train_and_save(session, tmp_path / "legacy.joblib")
    engine.dispose()
    keys = _flatten_keys(report)
    assert DEPRECATED_RANDOM_SPLIT_KEY in report
    assert "accuracy_holdout" not in keys
    assert "log_loss_holdout" not in keys
    assert "brier_holdout" not in keys
    assert all("holdout" not in key.lower() for key in keys)
    nested = report[DEPRECATED_RANDOM_SPLIT_KEY]
    assert isinstance(nested, dict)
    assert "accuracy" in nested
    assert "not betting evidence" in str(nested["note"]).lower()


def test_ridge_trainer_report_has_no_holdout_betting_keys(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    payload = report.to_dict()
    keys = _flatten_keys(payload)
    assert all("holdout" not in key.lower() for key in keys)
    assert DEPRECATED_RANDOM_SPLIT_KEY not in keys
    assert "2025-a" not in report.train_sample_ids
    assert "2017-b" not in report.train_sample_ids
    assert payload["metrics"]["n_labeled"] == 5
    assert payload["metrics"]["baselines"]["coin_flip"]["n"] >= 1
    commit = payload["code_commit"]
    assert commit == UNKNOWN_CODE_COMMIT or GIT_COMMIT_HEX.fullmatch(commit)
    assert payload["code_commit_reason"]


def test_ordinary_train_does_not_read_holdout_labels(tmp_path: Path, monkeypatch) -> None:
    def guarded_label(versions, as_of):
        for version in versions:
            if getattr(version, "bout_id", None) == "2025-a":
                raise AssertionError("holdout training_label")
        return real_training_label(versions, as_of)

    real_build = FeatureBuilder.build

    def guarded_build(self, *args, bout_id=None, **kwargs):
        if bout_id == "2025-a":
            raise AssertionError("holdout FeatureBuilder")
        return real_build(self, *args, bout_id=bout_id, **kwargs)

    monkeypatch.setattr("mma_model.modeling.baselines.training_label", guarded_label)
    monkeypatch.setattr(FeatureBuilder, "build", guarded_build)
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    assert report.metrics["n_labeled"] == 5
    assert "2025-a" not in report.train_sample_ids


def test_payload_bit_flip_is_rejected(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    path = report.artifact.payload_path
    blob = bytearray(path.read_bytes())
    blob[min(32, len(blob) - 1)] ^= 0xFF
    path.write_bytes(bytes(blob))
    with pytest.raises(ArtifactChecksumMismatchError, match="checksum"):
        load_artifact(path)


def test_contract_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    side = manifest_path_for(report.artifact.payload_path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    manifest["contract_hash"] = "0" * 64
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactSpecMismatchError, match="evaluation contract"):
        load_artifact(report.artifact.payload_path)


def test_feature_order_change_is_rejected(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    side = manifest_path_for(report.artifact.payload_path)
    manifest = json.loads(side.read_text(encoding="utf-8"))
    names = list(manifest["feature_names"])
    names[0], names[1] = names[1], names[0]
    manifest["feature_names"] = names
    side.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactFeatureOrderMismatchError, match="feature order"):
        load_artifact(report.artifact.payload_path)


def test_bare_pickle_is_untrusted(tmp_path: Path) -> None:
    path = tmp_path / "bare.joblib"
    joblib.dump({"feature_names": list(FEATURE_NAMES), "pipeline": object()}, path)
    with pytest.raises(UntrustedArtifactError, match="untrusted"):
        load_artifact(path)


def test_load_artifact_never_calls_joblib(tmp_path: Path, monkeypatch) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")

    def boom(*_args, **_kwargs):
        raise AssertionError("joblib.load invoked")

    monkeypatch.setattr(joblib, "load", boom)
    loaded = load_artifact(report.artifact.payload_path)
    assert loaded.predictor.feature_names == FEATURE_NAMES


def test_matching_sidecar_arbitrary_bytes_fail_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")

    def boom(*_args, **_kwargs):
        raise AssertionError("joblib.load invoked")

    monkeypatch.setattr(joblib, "load", boom)
    _rewrite_payload_with_matching_checksum(
        report.artifact.payload_path,
        b"\x80\x04arbitrary-pickle-bytes\xff",
    )
    with pytest.raises(UntrustedArtifactError, match="not valid JSON"):
        load_artifact(report.artifact.payload_path)


def test_malformed_and_nonfinite_payload_is_rejected(tmp_path: Path) -> None:
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    path = report.artifact.payload_path

    def shorten_coef(payload: dict) -> None:
        payload["estimator"]["logistic"]["coef"] = [0.1, 0.2]

    _mutate_json_payload(path, shorten_coef)
    with pytest.raises(UntrustedArtifactError, match="logistic.coef"):
        load_artifact(path)

    report = run_protocol_train(output_path=tmp_path / "ridge-nan.json")
    nan_path = report.artifact.payload_path

    def inject_nan(payload: dict) -> None:
        payload["estimator"]["scaler"]["mean"][0] = float("nan")

    raw = json.loads(nan_path.read_text(encoding="utf-8"))
    inject_nan(raw)
    blob = (json.dumps(raw, indent=2, sort_keys=True, allow_nan=True) + "\n").encode("utf-8")
    _rewrite_payload_with_matching_checksum(nan_path, blob)
    with pytest.raises(UntrustedArtifactError, match="finite"):
        load_artifact(nan_path)


def test_swap_predictions_complementary_within_1e8(tmp_path: Path) -> None:
    cards, snapshot, odds = protocol_training_universe()
    samples = labeled_samples_from_snapshot(snapshot, cards, odds_by_bout=odds)
    sample = next(item for item in samples if item.sample_id == "br-a")
    swapped = swap_values(sample.values)
    assert coin_flip_win_prob(sample.values) == pytest.approx(0.5)
    assert abs(coin_flip_win_prob(sample.values) + coin_flip_win_prob(swapped) - 1.0) <= SWAP_ATOL
    p_rating = sequential_rating_win_prob(sample.values)
    p_rating_swap = sequential_rating_win_prob(swapped)
    assert abs(p_rating + p_rating_swap - 1.0) <= SWAP_ATOL
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    loaded = load_artifact(report.artifact.payload_path)
    p_raw = predict_loaded_ridge_raw(loaded, sample.values)
    p_raw_swap = predict_loaded_ridge_raw(loaded, swapped)
    assert abs(p_raw + p_raw_swap - 1.0) <= SWAP_ATOL
    p_m1 = predict_loaded_ridge(loaded, sample.values)
    p_m1_swap = predict_loaded_ridge(loaded, swapped)
    assert abs(p_m1 + p_m1_swap - 1.0) <= SWAP_ATOL


def test_no_vig_swap_maps_pa_to_one_minus_pa() -> None:
    cards, snapshot, odds = protocol_training_universe()
    samples = {
        item.sample_id: item
        for item in labeled_samples_from_snapshot(snapshot, cards, odds_by_bout=odds)
    }
    original = samples["2024-a"].moneyline
    assert original is not None
    p_a = no_vig_win_prob(original, cutoff=samples["2024-a"].cutoff)
    swapped = PreCutoffMoneyline(
        decimal_odds={
            "fighter_a": float(original.decimal_odds["fighter_b"]),
            "fighter_b": float(original.decimal_odds["fighter_a"]),
        },
        observed_at=original.observed_at,
    )
    p_swapped = no_vig_win_prob(swapped, cutoff=samples["2024-a"].cutoff)
    assert not isinstance(p_a, MissingNoVig)
    assert not isinstance(p_swapped, MissingNoVig)
    assert abs(float(p_a) + float(p_swapped) - 1.0) <= SWAP_ATOL


def test_legacy_cli_train_still_callable(tmp_path: Path, monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}

    def fake_init() -> None:
        captured["init"] = True

    @contextmanager
    def fake_scope():
        yield object()

    def fake_train(session, path, **kwargs):
        Path(path).write_bytes(b"legacy")
        return {
            "n_samples": 8,
            DEPRECATED_RANDOM_SPLIT_KEY: {"accuracy": 0.5, "note": "not betting evidence"},
            "deprecation": "not betting evidence",
            "model_path": str(path),
        }

    monkeypatch.setattr("mma_model.cli.init_db", fake_init)
    monkeypatch.setattr("mma_model.cli.session_scope", fake_scope)
    monkeypatch.setattr("mma_model.cli.train_and_save", fake_train)
    out_path = tmp_path / "legacy.joblib"
    code = main(["train", "--output", str(out_path)])
    assert code == EXIT_OK
    err = capsys.readouterr()
    assert "DEPRECATED" in err.err
    assert "not betting evidence" in err.err.lower()
    payload = json.loads(err.out)
    assert DEPRECATED_RANDOM_SPLIT_KEY in payload
    assert "accuracy_holdout" not in payload


def test_legacy_predict_fight_still_works(tmp_path: Path, monkeypatch, capsys) -> None:
    def fake_init() -> None:
        return None

    @contextmanager
    def fake_scope():
        yield object()

    def fake_predict(session, fight_id, model_path, **kwargs):
        assert fight_id == "g0"
        return 0.61

    monkeypatch.setattr("mma_model.cli.init_db", fake_init)
    monkeypatch.setattr("mma_model.cli.session_scope", fake_scope)
    monkeypatch.setattr("mma_model.cli.predict_fight_a_win_prob", fake_predict)
    code = main(
        ["predict-fight", "--fight-id", "g0", "--model", str(tmp_path / "legacy.joblib")]
    )
    assert code == EXIT_OK
    captured = capsys.readouterr()
    assert "legacy unversioned joblib" in captured.err.lower()
    assert "cannot load dwcs-303 json" in captured.err.lower()
    payload = json.loads(captured.out)
    assert payload["p_fighter_a"] == 0.61
    assert "not betting evidence" in str(payload["deprecation"]).lower()


def test_ordinary_train_refuses_2025_holdout(tmp_path: Path) -> None:
    with pytest.raises(HoldoutLockedError, match="locked"):
        run_protocol_train(output_path=tmp_path / "nope.json", include_holdout=True)
    report = run_protocol_train(output_path=tmp_path / "ridge.json")
    assert "2025-a" not in report.train_sample_ids
    assert report.metrics["n_labeled"] == 5
    keys = _flatten_keys(report.to_dict())
    assert all("holdout" not in key.lower() for key in keys)


def test_coin_flip_always_half() -> None:
    zeros = tuple(0.0 for _ in FEATURE_NAMES)
    ones = tuple(1.0 for _ in FEATURE_NAMES)
    assert coin_flip_win_prob(zeros) == 0.5
    assert coin_flip_win_prob(ones) == 0.5
    assert coin_flip_win_prob(None) == 0.5


def test_sequential_rating_uses_pre_card_same_card_freeze() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "z")
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 1, 1),
    )
    b1 = add_bout(snapshot, "t1", "card", "a", "c")
    add_bout(snapshot, "t2", "card", "a", "b")
    leak_at = datetime(2019, 6, 1, 0, 30, tzinfo=UTC)
    add_result(
        snapshot,
        b1,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="0:15",
        effective_at=leak_at,
    )
    cutoff = cutoff_of(snapshot, "card")
    builder = FeatureBuilder(snapshot)
    row1 = builder.build("a", "c", cutoff, bout_id="t1")
    row2 = builder.build("a", "b", cutoff, bout_id="t2")
    v1 = named(row1.values, row1.names)
    v2 = named(row2.values, row2.names)
    assert v1["rating_a"] == v2["rating_a"]
    assert v1["prior_fights_a"] == v2["prior_fights_a"] == 1.0
    p1 = sequential_rating_win_prob(row1.values)
    p2 = sequential_rating_win_prob(row2.values)
    assert abs(p1 + sequential_rating_win_prob(swap_values(row1.values)) - 1.0) <= SWAP_ATOL
    assert abs(p2 + sequential_rating_win_prob(swap_values(row2.values)) - 1.0) <= SWAP_ATOL


def test_no_vig_missing_does_not_fabricate() -> None:
    cards, snapshot, odds = protocol_training_universe()
    samples = {
        item.sample_id: item
        for item in labeled_samples_from_snapshot(snapshot, cards, odds_by_bout=odds)
    }
    missing = no_vig_win_prob(samples["2017-a"].moneyline, cutoff=samples["2017-a"].cutoff)
    assert isinstance(missing, MissingNoVig)
    incomplete = no_vig_win_prob(samples["2023-a"].moneyline, cutoff=samples["2023-a"].cutoff)
    assert isinstance(incomplete, MissingNoVig)
    complete = no_vig_win_prob(samples["2024-a"].moneyline, cutoff=samples["2024-a"].cutoff)
    assert not isinstance(complete, MissingNoVig)
    assert 0.0 < float(complete) < 1.0
    late = PreCutoffMoneyline(
        decimal_odds={"fighter_a": 1.80, "fighter_b": 2.10},
        observed_at=datetime(2024, 8, 13, 3, 0, tzinfo=UTC),
    )
    after_cutoff = no_vig_win_prob(late, cutoff=samples["2024-a"].cutoff)
    assert isinstance(after_cutoff, MissingNoVig)
    none_odds = no_vig_win_prob(None)
    assert isinstance(none_odds, MissingNoVig)
    assert "2025-a" not in samples


def test_model_train_cli_protocol_fixture(tmp_path: Path, capsys) -> None:
    out_path = tmp_path / "ridge.json"
    code = main(
        [
            "model",
            "train",
            "--spec",
            "config/model_specs/ridge_v1.yaml",
            "--fixture",
            "protocol",
            "--output",
            str(out_path),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "M1"
    assert payload["metrics"]["n_labeled"] == 5
    assert all("holdout" not in key.lower() for key in _flatten_keys(payload))
    assert "2025-a" not in payload["train_sample_ids"]
    loaded = load_artifact(out_path)
    assert loaded.manifest.feature_spec_hash == spec_hash()
    assert loaded.manifest.config_hash == PINNED_RIDGE_SPEC_HASH
    assert loaded.manifest.code_commit == payload["code_commit"]


def test_model_predict_cli_protocol_and_tamper(tmp_path: Path, capsys) -> None:
    out_path = tmp_path / "ridge.json"
    train_code = main(
        [
            "model",
            "train",
            "--spec",
            "config/model_specs/ridge_v1.yaml",
            "--fixture",
            "protocol",
            "--output",
            str(out_path),
        ]
    )
    assert train_code == EXIT_OK
    capsys.readouterr()
    predict_code = main(
        [
            "model",
            "predict",
            "--artifact",
            str(out_path),
            "--fixture",
            "protocol",
            "--bout-id",
            "br-a",
        ]
    )
    assert predict_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["bout_id"] == "br-a"
    assert 0.0 < float(payload["p_fighter_a"]) < 1.0
    blob = bytearray(out_path.read_bytes())
    blob[min(32, len(blob) - 1)] ^= 0xFF
    out_path.write_bytes(bytes(blob))
    tamper_code = main(
        [
            "model",
            "predict",
            "--artifact",
            str(out_path),
            "--fixture",
            "protocol",
            "--bout-id",
            "br-a",
        ]
    )
    assert tamper_code == EXIT_INTERNAL
    assert "checksum" in capsys.readouterr().out.lower()


def test_model_train_cli_refuses_live_db(capsys) -> None:
    code = main(
        [
            "model",
            "train",
            "--spec",
            "config/model_specs/ridge_v1.yaml",
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert code == EXIT_INTERNAL
    assert "refusing" in capsys.readouterr().out.lower()


def test_ridge_spec_hash_is_pinned() -> None:
    spec = load_ridge_spec()
    assert spec.content_hash == PINNED_RIDGE_SPEC_HASH
    assert spec.ordinary_allow_holdout is False
    packaged = load_ridge_spec(path=Path("config/model_specs/ridge_v1.yaml"))
    assert packaged.content_hash == PINNED_RIDGE_SPEC_HASH
