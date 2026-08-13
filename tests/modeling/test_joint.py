"""Competing-risks joint model and derived markets (DWCS-304)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from mma_model.cli import main
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.evaluation.contract import TerminalAtom
from mma_model.features.spec import FEATURE_NAMES, row_bytes, spec_hash, swap_values
from mma_model.labels.outcomes import ResultClass, WinnerSide, label_from_facts
from mma_model.markets.derive import (
    ATOM_SUM_ATOL,
    METHOD_DRAW_TREATMENT,
    UnsupportedScheduleError,
    aggregate_frozen_atoms,
    derive_markets,
    fine_atom_keys,
    finish_atom_key,
    interval_count_for_schedule,
    swap_fine_atoms,
)
from mma_model.modeling.joint import (
    N_DECISION_PARAMS,
    N_HAZARD_PARAMS,
    N_SYM_T,
    PINNED_JOINT_SPEC_HASH,
    PROBABILITY_CLIP_TOLERANCE,
    SWAP_ATOL,
    EarlyTechnicalOutcomeError,
    HazardClass,
    JointNumericalError,
    JointPredictor,
    JointSpecError,
    MissingJointClassError,
    OofSkipReason,
    _classify_sample,
    _collect_oof,
    expand_person_period,
    fit_joint_predictor,
    identify_model_family,
    joint_protocol_training_universe,
    joint_samples_from_snapshot,
    load_joint_artifact,
    load_joint_spec,
    parse_joint_spec,
    run_protocol_joint_train,
    stable_softmax,
    survival_multiply,
)
from mma_model.modeling.splits import tuning_folds
from mma_model.quality.constants import EXIT_OK

N_FEATURES = len(FEATURE_NAMES)


def _unit_vector(*, diff_scale: float = 0.0) -> tuple[float, ...]:
    values = [0.0] * N_FEATURES
    rating_idx = FEATURE_NAMES.index("rating_diff")
    values[rating_idx] = diff_scale
    return tuple(values)


def _identity_predictor(*, ko_anti: float = 0.0) -> JointPredictor:
    hazard = [0.0] * N_HAZARD_PARAMS
    hazard[N_SYM_T] = ko_anti
    return JointPredictor(
        feature_names=FEATURE_NAMES,
        scaler_mean=tuple(0.0 for _ in FEATURE_NAMES),
        scaler_scale=tuple(1.0 for _ in FEATURE_NAMES),
        hazard_theta=tuple(hazard),
        decision_theta=tuple(0.0 for _ in range(N_DECISION_PARAMS)),
        spec_hash=spec_hash(),
        spec_version="dwcs_pit_v1.1",
        clip_tolerance=PROBABILITY_CLIP_TOLERANCE,
    )


def _assert_simplex(mapping: dict[str, float], *, atol: float = ATOM_SUM_ATOL) -> None:
    total = 0.0
    for key, value in mapping.items():
        assert np.isfinite(value), key
        assert 0.0 <= value <= 1.0, (key, value)
        total += value
    assert abs(total - 1.0) <= atol, total


def _hand_hazard_probs(*, scheduled_rounds: int) -> np.ndarray:
    n_int = interval_count_for_schedule(scheduled_rounds)
    rows = np.zeros((n_int, 7), dtype=np.float64)
    continue_idx = list(HazardClass).index(HazardClass.CONTINUE)
    ako_idx = list(HazardClass).index(HazardClass.A_KO_TKO)
    for interval in range(n_int):
        rows[interval, continue_idx] = 0.5
        rows[interval, ako_idx] = 0.5
    return rows


def test_joint_spec_hash_is_pinned() -> None:
    spec = load_joint_spec()
    assert spec.content_hash == PINNED_JOINT_SPEC_HASH
    assert spec.spec_version == "1.1.0"
    assert spec.ordinary_allow_holdout is False
    assert spec.tied_ab_parameters is True
    assert spec.swap_augment is False
    assert spec.solver == "lbfgs"
    assert spec.atom_sum_tolerance == 1e-10
    packaged = load_joint_spec(path=Path("config/model_specs/joint_v1.yaml"))
    assert packaged.content_hash == PINNED_JOINT_SPEC_HASH
    assert identify_model_family(Path("config/model_specs/joint_v1.yaml")) == "joint"
    assert identify_model_family(Path("config/model_specs/ridge_v1.yaml")) == "ridge"


@pytest.mark.parametrize("scheduled_rounds", [3, 5])
def test_survival_matches_hand_calculated_hazards(scheduled_rounds: int) -> None:
    hazards = _hand_hazard_probs(scheduled_rounds=scheduled_rounds)
    decisions = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    fine = survival_multiply(hazards, decisions, scheduled_rounds=scheduled_rounds)
    _assert_simplex(fine)
    n_int = interval_count_for_schedule(scheduled_rounds)
    survival = 1.0
    for interval in range(n_int):
        key = finish_atom_key(side="a", cause="ko_tko", interval=interval)
        assert fine[key] == pytest.approx(survival * 0.5, abs=1e-12)
        survival *= 0.5
    assert fine["a_decision"] == pytest.approx(survival, abs=1e-12)
    assert fine["b_decision"] == pytest.approx(0.0, abs=1e-12)
    assert fine["draw"] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("scheduled_rounds", [3, 5])
@pytest.mark.parametrize("scale", [0.0, 12.0, -40.0, 1e6, -1e6])
def test_atoms_finite_in_range_and_sum_to_one(scheduled_rounds: int, scale: float) -> None:
    logits = np.full((interval_count_for_schedule(scheduled_rounds), 7), scale, dtype=np.float64)
    logits[:, 0] = 0.0
    logits[:, 1] = scale
    hazards = np.vstack([stable_softmax(row) for row in logits])
    decisions = stable_softmax(np.array([scale, -scale, 0.0], dtype=np.float64))
    fine = survival_multiply(hazards, decisions, scheduled_rounds=scheduled_rounds)
    _assert_simplex(fine)
    frozen = aggregate_frozen_atoms(fine)
    _assert_simplex({atom.value: value for atom, value in frozen.items()})


def test_nonfinite_logits_fail_closed() -> None:
    with pytest.raises(JointNumericalError, match="non-finite"):
        stable_softmax(np.array([np.inf, 0.0, 0.0]))


def test_derived_markets_equal_enumerated_atom_sums() -> None:
    keys = fine_atom_keys(3)
    fine = {key: 0.0 for key in keys}
    fine[finish_atom_key(side="a", cause="ko_tko", interval=0)] = 0.10
    fine[finish_atom_key(side="a", cause="submission", interval=2)] = 0.05
    fine[finish_atom_key(side="b", cause="ko_tko", interval=1)] = 0.08
    fine[finish_atom_key(side="b", cause="other_stoppage", interval=4)] = 0.07
    fine[finish_atom_key(side="a", cause="ko_tko", interval=5)] = 0.04
    fine["a_decision"] = 0.30
    fine["b_decision"] = 0.20
    fine["draw"] = 0.16
    _assert_simplex(fine)
    markets = derive_markets(fine, scheduled_rounds=3)
    assert markets.moneyline[OutcomeKey.FIGHTER_A] == pytest.approx(0.10 + 0.05 + 0.04 + 0.30)
    assert markets.moneyline[OutcomeKey.FIGHTER_B] == pytest.approx(0.08 + 0.07 + 0.20)
    assert markets.draw == pytest.approx(0.16)
    assert markets.moneyline[OutcomeKey.FIGHTER_A] + markets.moneyline[OutcomeKey.FIGHTER_B] < 1.0
    assert markets.goes_distance[OutcomeKey.GOES_DISTANCE] == pytest.approx(0.30 + 0.20 + 0.16)
    assert markets.goes_distance[OutcomeKey.INSIDE_DISTANCE] == pytest.approx(
        0.10 + 0.05 + 0.08 + 0.07 + 0.04
    )
    assert markets.method[OutcomeKey.KO_TKO] == pytest.approx(0.10 + 0.08 + 0.04)
    assert markets.method[OutcomeKey.SUBMISSION] == pytest.approx(0.05)
    assert markets.method[OutcomeKey.OTHER_STOPPAGE] == pytest.approx(0.07)
    assert markets.method[OutcomeKey.DECISION] == pytest.approx(0.30 + 0.20)
    assert METHOD_DRAW_TREATMENT in markets.method_draw_treatment
    assert markets.exact_round[OutcomeKey.ROUND_1] == pytest.approx(0.10 + 0.08)
    assert markets.exact_round[OutcomeKey.ROUND_2] == pytest.approx(0.05)
    assert markets.exact_round[OutcomeKey.ROUND_3] == pytest.approx(0.07 + 0.04)
    assert OutcomeKey.ROUND_4 not in markets.exact_round
    assert markets.totals[1.5][OutcomeKey.UNDER] == pytest.approx(0.10 + 0.05 + 0.08)
    assert markets.totals[1.5][OutcomeKey.OVER] == pytest.approx(1.0 - (0.10 + 0.05 + 0.08))
    assert markets.totals[2.5][OutcomeKey.UNDER] == pytest.approx(0.10 + 0.05 + 0.08 + 0.07)
    assert markets.totals[2.5][OutcomeKey.OVER] == pytest.approx(0.04 + 0.30 + 0.20 + 0.16)
    frozen = aggregate_frozen_atoms(fine)
    assert markets.fighter_by_method[OutcomeKey.A_KO_TKO] == pytest.approx(
        frozen[TerminalAtom.A_KO_TKO]
    )
    family_map = markets.as_family_map()
    assert MarketFamily.MONEYLINE in family_map
    leftover = 1.0 - sum(markets.method.values()) - markets.draw
    assert leftover == pytest.approx(0.0, abs=1e-12)


def test_fighter_swap_maps_atoms_and_preserves_totals() -> None:
    keys = fine_atom_keys(3)
    fine = {key: 0.0 for key in keys}
    fine[finish_atom_key(side="a", cause="ko_tko", interval=0)] = 0.22
    fine[finish_atom_key(side="b", cause="submission", interval=3)] = 0.18
    fine["a_decision"] = 0.25
    fine["b_decision"] = 0.15
    fine["draw"] = 0.20
    _assert_simplex(fine)
    swapped = swap_fine_atoms(fine)
    assert swapped[finish_atom_key(side="b", cause="ko_tko", interval=0)] == 0.22
    assert swapped[finish_atom_key(side="a", cause="submission", interval=3)] == 0.18
    assert swapped["a_decision"] == 0.15
    assert swapped["b_decision"] == 0.25
    assert swapped["draw"] == 0.20
    left = derive_markets(fine, scheduled_rounds=3)
    right = derive_markets(swapped, scheduled_rounds=3)
    assert left.goes_distance == right.goes_distance
    assert left.method == right.method
    assert left.exact_round == right.exact_round
    assert left.totals == right.totals
    assert left.draw == right.draw
    _assert_simplex(swapped)
    frozen_left = aggregate_frozen_atoms(fine)
    frozen_right = aggregate_frozen_atoms(swapped)
    assert frozen_left[TerminalAtom.A_KO_TKO] == frozen_right[TerminalAtom.B_KO_TKO]
    assert frozen_left[TerminalAtom.DRAW] == frozen_right[TerminalAtom.DRAW]


def test_raw_predictor_is_swap_equivariant_without_averaging() -> None:
    predictor = _identity_predictor(ko_anti=1.25)
    values = _unit_vector(diff_scale=0.8)
    swapped = swap_values(values)
    fine = predictor.predict_fine(values, scheduled_rounds=3)
    fine_swapped_input = predictor.predict_fine(swapped, scheduled_rounds=3)
    mapped = swap_fine_atoms(fine)
    for key in fine:
        assert fine_swapped_input[key] == pytest.approx(mapped[key], abs=SWAP_ATOL)
    logits = predictor.raw_hazard_logits(values, 0)
    logits_swap = predictor.raw_hazard_logits(swapped, 0)
    assert logits[list(HazardClass).index(HazardClass.CONTINUE)] == pytest.approx(
        logits_swap[list(HazardClass).index(HazardClass.CONTINUE)]
    )
    assert logits[list(HazardClass).index(HazardClass.A_KO_TKO)] == pytest.approx(
        logits_swap[list(HazardClass).index(HazardClass.B_KO_TKO)]
    )


def test_person_period_reuses_identical_feature_bytes() -> None:
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = joint_samples_from_snapshot(snapshot, cards, spec)
    distance = next(sample for sample in samples if sample.sample_id == "j17-adec")
    rows = expand_person_period(distance)
    assert len(rows) == 6
    expected = row_bytes(distance.values)
    assert all(row.feature_bytes == expected for row in rows)
    assert all(row.values == distance.values for row in rows)
    finish = next(sample for sample in samples if sample.sample_id == "j17-ako")
    finish_rows = expand_person_period(finish)
    assert finish_rows[-1].hazard_class is HazardClass.A_KO_TKO
    assert all(row.hazard_class is HazardClass.CONTINUE for row in finish_rows[:-1])


def test_ordinary_training_does_not_read_holdout_or_same_card_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    seen_years: list[int] = []
    real = joint_samples_from_snapshot.__globals__["training_label"]

    def wrapped(versions, cutoff):
        seen_years.append(cutoff.year)
        return real(versions, cutoff)

    monkeypatch.setattr("mma_model.modeling.joint.training_label", wrapped)
    samples = joint_samples_from_snapshot(snapshot, cards, spec)
    ids = {sample.sample_id for sample in samples}
    assert "j25-hold" not in ids
    assert 2025 not in seen_years
    assert all(sample.cutoff.year < 2025 for sample in samples)


def test_unsupported_schedule_fails() -> None:
    with pytest.raises(UnsupportedScheduleError, match="only 3 or 5"):
        interval_count_for_schedule(4)
    with pytest.raises(UnsupportedScheduleError, match="only 3 or 5"):
        derive_markets({}, scheduled_rounds=2)


def test_early_technical_fails_by_default_and_pooling_is_documented() -> None:
    label = label_from_facts(
        method_raw="T-DEC",
        result_class=ResultClass.DECISIVE,
        winner_side=WinnerSide.A,
    )
    with pytest.raises(EarlyTechnicalOutcomeError, match="early technical"):
        _classify_sample(
            label,
            duration_interval=0,
            scheduled_rounds=3,
            early_mode="fail",
            bout_id="td-early",
        )
    kind, hazard, decision, interval = _classify_sample(
        label,
        duration_interval=0,
        scheduled_rounds=3,
        early_mode="pool_other_stoppage",
        bout_id="td-early",
    )
    assert kind.value == "finish"
    assert hazard is HazardClass.A_OTHER_STOPPAGE
    assert decision is None
    assert interval == 0
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["early_technical"] = "pool_as_distance"
    with pytest.raises(JointSpecError, match="pool_as_distance"):
        parse_joint_spec(payload, enforce_pinned_digest=False)


def test_missing_classes_fail_closed_and_pool_config_is_rejected() -> None:
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = [
        sample
        for sample in joint_samples_from_snapshot(snapshot, cards, spec)
        if sample.hazard_class
        not in {HazardClass.A_OTHER_STOPPAGE, HazardClass.B_OTHER_STOPPAGE}
        and sample.sample_id != "j24-aoth"
    ]
    with pytest.raises(MissingJointClassError, match="a_other_stoppage") as exc:
        fit_joint_predictor(samples, spec)
    assert "a_other_stoppage" in exc.value.missing
    assert "b_other_stoppage" in exc.value.missing
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["missing_classes"] = "pool"
    with pytest.raises(JointSpecError, match="unsupported"):
        parse_joint_spec(payload, enforce_pinned_digest=False)
    payload["missing_classes"] = "fail"
    payload["class_pooling"] = {
        "a_other_stoppage": "a_ko_tko",
        "b_other_stoppage": "b_ko_tko",
    }
    with pytest.raises(JointSpecError, match="class_pooling"):
        parse_joint_spec(payload, enforce_pinned_digest=False)


def test_spec_rejects_dishonest_solver_rounds_tolerance_and_swap_augment() -> None:
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["estimator"] = dict(payload["estimator"])
    payload["estimator"]["solver"] = "saga"
    with pytest.raises(JointSpecError, match="lbfgs"):
        parse_joint_spec(payload, enforce_pinned_digest=False)
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["supported_scheduled_rounds"] = [3]
    with pytest.raises(JointSpecError, match="supported_scheduled_rounds"):
        parse_joint_spec(payload, enforce_pinned_digest=False)
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["atom_sum_tolerance"] = 1.0e-8
    with pytest.raises(JointSpecError, match="atom_sum_tolerance"):
        parse_joint_spec(payload, enforce_pinned_digest=False)
    payload = yaml.safe_load(Path("src/mma_model/modeling/joint_v1.yaml").read_text())
    payload["swap_augment"] = True
    with pytest.raises(JointSpecError, match="swap_augment"):
        parse_joint_spec(payload, enforce_pinned_digest=False)


def test_decisions_only_receive_remaining_survival() -> None:
    hazards = _hand_hazard_probs(scheduled_rounds=3)
    decisions = np.array([0.5, 0.3, 0.2], dtype=np.float64)
    fine = survival_multiply(hazards, decisions, scheduled_rounds=3)
    remaining = 0.5**6
    assert fine["a_decision"] == pytest.approx(remaining * 0.5)
    assert fine["b_decision"] == pytest.approx(remaining * 0.3)
    assert fine["draw"] == pytest.approx(remaining * 0.2)
    markets = derive_markets(fine, scheduled_rounds=3)
    assert markets.goes_distance[OutcomeKey.GOES_DISTANCE] == pytest.approx(remaining)


def test_protocol_train_exposes_oof_and_raw_fitted_swap(tmp_path: Path) -> None:
    report = run_protocol_joint_train(output_path=tmp_path / "joint.json")
    assert report.model_id == "M2"
    assert "j25-hold" not in report.train_sample_ids
    assert report.metrics["n_oof"] >= 1
    assert report.metrics["n_oof_expected"] == (
        report.metrics["n_oof_emitted"] + report.metrics["n_oof_excluded_bouts"]
    )
    assert report.metrics["n_oof_emitted"] == report.metrics["n_oof"]
    oof = report.metrics["oof_predictions"]
    assert oof[0]["hazard_logits"]
    assert oof[0]["fine_probabilities"]
    assert oof[0]["train_event_ids"]
    assert oof[0]["train_event_ids_hash"]
    assert oof[0]["train_max_timestamp"]
    exclusions = report.metrics["oof_exclusions"]
    assert any(item["reason_code"] == "empty_train" for item in exclusions)
    loaded = load_joint_artifact(tmp_path / "joint.json")
    assert loaded.payload["payload_kind"] == "tied_competing_risks_v1"
    assert loaded.payload["oof_exclusions"] == exclusions
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = joint_samples_from_snapshot(snapshot, cards, spec)
    sample = samples[0]
    fine = loaded.predictor.predict_fine(sample.values, scheduled_rounds=sample.scheduled_rounds)
    mapped = swap_fine_atoms(fine)
    swapped_input = loaded.predictor.predict_fine(
        swap_values(sample.values), scheduled_rounds=sample.scheduled_rounds
    )
    for key in fine:
        assert swapped_input[key] == pytest.approx(mapped[key], abs=1e-8)
    _assert_simplex(fine)


def test_sparse_fold_emits_explicit_exclusion_with_exact_missing_classes() -> None:
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = [
        sample
        for sample in joint_samples_from_snapshot(snapshot, cards, spec)
        if sample.hazard_class
        not in {HazardClass.A_OTHER_STOPPAGE, HazardClass.B_OTHER_STOPPAGE}
        and sample.sample_id != "j24-aoth"
    ]
    inner = tuning_folds(cards, require_target_cards=False)
    collected = _collect_oof(inner, samples, spec)
    missing = [
        item
        for item in collected.exclusions
        if item.reason_code is OofSkipReason.MISSING_CLASSES
    ]
    assert missing
    first = missing[0]
    assert first.missing_classes == ("a_other_stoppage", "b_other_stoppage")
    assert first.test_event_ids
    assert first.n_test >= 1
    emitted_events = {row["event_id"] for row in collected.predictions}
    emitted_bouts = {row["bout_id"] for row in collected.predictions}
    for event_id in first.test_event_ids:
        assert event_id not in emitted_events
    for bout_id in first.test_bout_ids:
        assert bout_id not in emitted_bouts
    collected.reconcile()
    assert collected.n_expected == collected.n_emitted + collected.excluded_bouts()


def test_tied_multinomial_raises_when_optimizer_does_not_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scipy.optimize import OptimizeResult

    def fake_minimize(*args: object, **kwargs: object) -> OptimizeResult:
        x0 = args[1]
        return OptimizeResult(
            x=np.zeros_like(x0),
            success=False,
            status=1,
            message="forced failure",
            nit=3,
        )

    monkeypatch.setattr("mma_model.modeling.joint.minimize", fake_minimize)
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = joint_samples_from_snapshot(snapshot, cards, spec)
    with pytest.raises(JointNumericalError, match="L-BFGS-B failed") as exc:
        fit_joint_predictor(samples, spec)
    assert "forced failure" in str(exc.value)
    assert "nit=3" in str(exc.value)
    assert "status=1" in str(exc.value)
    assert exc.value.status == 1
    assert exc.value.nit == 3


def test_joint_model_train_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "joint.json"
    code = main(
        [
            "model",
            "train",
            "--spec",
            "config/model_specs/joint_v1.yaml",
            "--fixture",
            "protocol",
            "--output",
            str(out),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "M2"
    assert Path(payload["artifact_path"]).is_file()
    assert "j25-hold" not in payload["train_sample_ids"]
    metrics = payload["metrics"]
    assert metrics["n_oof_expected"] == metrics["n_oof_emitted"] + metrics["n_oof_excluded_bouts"]
    assert "oof_exclusions" in metrics
    bout_id = payload["train_sample_ids"][0]
    predict_code = main(
        [
            "model",
            "predict",
            "--artifact",
            str(out),
            "--fixture",
            "protocol",
            "--bout-id",
            bout_id,
        ]
    )
    assert predict_code == EXIT_OK
    scored = json.loads(capsys.readouterr().out)
    assert 0.0 < scored["p_fighter_a"] < 1.0
    assert scored["scheduled_rounds"] in (3, 5)
    assert "predict_fine" in scored["prediction_api"]
    assert scored["frozen_probabilities"]

