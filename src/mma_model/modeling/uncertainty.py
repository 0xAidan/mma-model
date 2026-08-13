"""Seeded event-block bootstrap refits (DWCS-305).

The sampling unit is a whole event/card. Each replicate resamples event IDs
with replacement and refits; it does not IID-resample fights. Locked 2025
holdout cards are excluded before grouping. Production default is 200
successful refits.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Generic, Literal, Never, TypeVar

import numpy as np

from mma_model.evaluation.contract import (
    PINNED_CONTRACT_HASH,
    EvaluationContract,
    load_evaluation_contract,
)
from mma_model.markets.derive import interval_count_for_schedule
from mma_model.modeling.artifacts import (
    BOOTSTRAP_PREDICTION_SCOPE,
    BOOTSTRAP_SCHEMA_VERSION,
    CALIBRATOR_SOURCE_FIXED_OOF,
    EV_SEMANTICS_BINARY_MONEYLINE,
    EV_SEMANTICS_JOINT_VOID,
    JOINT_EV_OMISSION_REASON,
    PRODUCTION_BOOTSTRAP_REPLICATES,
    load_artifact,
    verify_bootstrap_metadata,
)
from mma_model.modeling.baselines import (
    LabeledSample,
    RidgeModelSpec,
    fit_ridge,
    load_ridge_spec,
)
from mma_model.modeling.calibration import (
    CalibrationReport,
    OofBundle,
    SigmoidCalibrator,
    TemperatureCalibrator,
    _oof_from_joint_loaded,
    _oof_from_ridge_loaded,
    calibrate_joint_bundle,
    calibrate_ridge_bundle,
    default_calibrated_path,
    detect_artifact_family,
    fit_sigmoid_calibrator,
    fit_temperature_calibrator,
    load_oof_bundle,
    reconstruct_protocol_joint,
    reconstruct_protocol_ridge,
    reconstruct_session_joint,
    reconstruct_session_ridge,
    save_calibrated_artifact,
)
from mma_model.modeling.joint import (
    JointBoutSample,
    JointModelSpec,
    MissingJointClassError,
    fit_joint_predictor,
    load_joint_artifact,
    load_joint_spec,
)
from mma_model.quality.schema import sha256_canonical

DEFAULT_BOOTSTRAP_REPLICATES: Final = PRODUCTION_BOOTSTRAP_REPLICATES
DEFAULT_BOOTSTRAP_SEED: Final = 305001
DEFAULT_MAX_ATTEMPT_MULTIPLIER: Final = 50
PERCENTILES: Final = (5.0, 25.0, 50.0, 75.0, 95.0)

TFitted = TypeVar("TFitted")
TSample = TypeVar("TSample")


class BootstrapRedrawError(Exception):
    """Class-incomplete or too-small draw; counted and redrawn, not dropped silently."""


class BootstrapError(ValueError):
    """Event-block bootstrap cannot produce the required refits."""


@dataclass(frozen=True)
class EventBlock(Generic[TSample]):
    event_id: str
    samples: tuple[TSample, ...]


EvSemantics = Literal["exhaustive_binary_moneyline", "joint_void_mass"]


@dataclass(frozen=True)
class TargetPercentiles:
    target_id: str
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    observed_price: float | None
    ev05: float | None
    ev25: float | None
    ev50: float | None
    ev75: float | None
    ev95: float | None
    ev_omission_reason: str | None = None
    prob_ev_positive: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "p05": self.p05,
            "p25": self.p25,
            "p50": self.p50,
            "p75": self.p75,
            "p95": self.p95,
        }
        if self.ev_omission_reason is not None:
            payload["ev_omission_reason"] = self.ev_omission_reason
        if self.prob_ev_positive is not None:
            payload["prob_ev_positive"] = self.prob_ev_positive
        if self.observed_price is None:
            return payload
        payload["observed_price"] = self.observed_price
        if self.ev50 is None:
            return payload
        payload["ev05"] = self.ev05
        payload["ev25"] = self.ev25
        payload["ev50"] = self.ev50
        payload["ev75"] = self.ev75
        payload["ev95"] = self.ev95
        return payload


@dataclass(frozen=True)
class BootstrapSummary:
    n_replicates: int
    n_successful: int
    n_attempts: int
    n_rejected: int
    max_attempts: int
    seed: int
    event_ids: tuple[str, ...]
    estimator_hash: str
    config_hash: str
    data_hash: str
    contract_hash: str
    production_qualified: bool
    targets: tuple[TargetPercentiles, ...]
    rejection_reasons: tuple[str, ...]
    calibrator_fitting_event_ids: tuple[str, ...]
    calibrator_fitting_sample_ids: tuple[str, ...]
    ev_semantics: EvSemantics
    ev_omission_reason: str | None = None
    sampling_unit: str = "event"
    schema_version: str = BOOTSTRAP_SCHEMA_VERSION
    calibrator_refit_per_replicate: bool = False
    calibrator_source: str = CALIBRATOR_SOURCE_FIXED_OOF
    prediction_scope: str = BOOTSTRAP_PREDICTION_SCOPE
    oob: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "calibrator_fitting_event_ids": list(self.calibrator_fitting_event_ids),
            "calibrator_fitting_event_ids_hash": sha256_canonical(
                {"fitting_event_ids": list(self.calibrator_fitting_event_ids)}
            ),
            "calibrator_fitting_sample_ids": list(self.calibrator_fitting_sample_ids),
            "calibrator_fitting_sample_ids_hash": sha256_canonical(
                {"fitting_sample_ids": list(self.calibrator_fitting_sample_ids)}
            ),
            "calibrator_refit_per_replicate": self.calibrator_refit_per_replicate,
            "calibrator_source": self.calibrator_source,
            "config_hash": self.config_hash,
            "contract_hash": self.contract_hash,
            "data_hash": self.data_hash,
            "estimator_hash": self.estimator_hash,
            "ev_omission_reason": self.ev_omission_reason,
            "ev_semantics": self.ev_semantics,
            "event_ids": list(self.event_ids),
            "event_ids_hash": sha256_canonical({"event_ids": list(self.event_ids)}),
            "max_attempts": self.max_attempts,
            "n_attempts": self.n_attempts,
            "n_events": len(self.event_ids),
            "n_rejected": self.n_rejected,
            "n_replicates": self.n_replicates,
            "n_successful": self.n_successful,
            "oob": self.oob,
            "prediction_scope": self.prediction_scope,
            "production_qualified": self.production_qualified,
            "rejection_reasons": list(self.rejection_reasons),
            "sampling_unit": self.sampling_unit,
            "schema_version": self.schema_version,
            "seed": self.seed,
            "targets": {item.target_id: item.to_dict() for item in self.targets},
        }
        verify_bootstrap_metadata(payload, require_production=self.production_qualified)
        return payload


def _percentile_tuple(values: Sequence[float]) -> tuple[float, float, float, float, float]:
    arr = np.asarray(list(values), dtype=np.float64)
    if arr.size == 0:
        raise BootstrapError("cannot summarize an empty bootstrap sample")
    if not np.all(np.isfinite(arr)):
        raise BootstrapError("bootstrap probabilities must be finite")
    got = np.percentile(arr, list(PERCENTILES), method="linear")
    out = tuple(float(item) for item in got)
    if len(out) != 5:
        raise BootstrapError("percentile tuple must have five entries")
    return out[0], out[1], out[2], out[3], out[4]


def _ev_tuple(
    probabilities: tuple[float, float, float, float, float],
    price: float,
) -> tuple[float, float, float, float, float]:
    if not math.isfinite(price) or price <= 1.0:
        raise BootstrapError("observed decimal price must be finite and > 1.0")
    values = tuple(float(price) * float(item) - 1.0 for item in probabilities)
    return (values[0], values[1], values[2], values[3], values[4])


def _prob_ev_positive(
    probabilities: Sequence[float],
    price: float,
    ev_fn: Callable[[float, float], float] | None,
) -> float:
    """Share of replicate predictions with EV > 0 at the observed price.

    Never inferred from p25. ``ev_fn(prob, decimal_odds)`` defaults to
    exhaustive binary ``p * odds - 1``. Void markets must pass a
    settlement-aware callback.
    """
    if not math.isfinite(price) or price <= 1.0:
        raise BootstrapError("observed decimal price must be finite and > 1.0")
    series = [float(item) for item in probabilities]
    if not series:
        raise BootstrapError("cannot compute P(EV>0) over empty replicates")

    def default_ev(prob: float, odds: float) -> float:
        return float(odds) * float(prob) - 1.0

    fn = default_ev if ev_fn is None else ev_fn
    hits = sum(1 for prob in series if fn(prob, float(price)) > 0.0)
    value = hits / len(series)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise BootstrapError("prob_ev_positive must be in [0, 1]")
    return value


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, BootstrapRedrawError):
        return True
    if isinstance(exc, MissingJointClassError):
        return True
    if isinstance(exc, BootstrapError):
        return False
    text = str(exc).lower()
    if "missing" in text and "class" in text:
        return True
    if "classes must be (0, 1)" in text:
        return True
    return "one_class" in text or "one class" in text


def _sample_identity(sample: object) -> str:
    if isinstance(sample, str):
        return sample
    sample_id = getattr(sample, "sample_id", None)
    if sample_id is not None:
        return str(sample_id)
    return str(sample)


def event_block_refit_bootstrap(
    groups: Sequence[EventBlock[TSample]],
    *,
    refit: Callable[[tuple[TSample, ...]], TFitted],
    predict: Callable[[TFitted], Mapping[str, float]],
    n_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    max_attempts: int | None = None,
    observed_prices: Mapping[str, float] | None = None,
    estimator_hash: str,
    config_hash: str,
    data_hash: str,
    contract_hash: str | None = None,
    calibrator_fitting_event_ids: Sequence[str] | None = None,
    calibrator_fitting_sample_ids: Sequence[str] | None = None,
    ev_semantics: EvSemantics = EV_SEMANTICS_BINARY_MONEYLINE,
    ev_positive_fn: Callable[[float, float], float] | None = None,
) -> BootstrapSummary:
    """Generic event-block refit API. Production default is 200 successful fits.

    Probability bands are base-model event-block uncertainty conditional on a
    calibrator fitted once on prior-time OOF. Targets are fixed covariates
    (``prediction_scope=fixed_target_refit_distribution``, ``oob=false``).
    """
    if n_replicates < 1:
        raise BootstrapError("n_replicates must be >= 1")
    if not groups:
        raise BootstrapError("event-block bootstrap needs at least one event group")
    if ev_semantics == EV_SEMANTICS_BINARY_MONEYLINE:
        omission_reason: str | None = None
    elif ev_semantics == EV_SEMANTICS_JOINT_VOID:
        omission_reason = JOINT_EV_OMISSION_REASON
    else:
        never_sem: Never = ev_semantics
        raise BootstrapError(f"unhandled ev_semantics: {never_sem!r}")
    ordered = tuple(sorted(groups, key=lambda item: item.event_id))
    event_ids = tuple(item.event_id for item in ordered)
    by_id = {item.event_id: item for item in ordered}
    if len(by_id) != len(ordered):
        raise BootstrapError("event-block groups must have unique event_ids")
    for block in ordered:
        if not block.samples:
            raise BootstrapError(f"event {block.event_id!r} has no bouts")
    attempts_cap = (
        n_replicates * DEFAULT_MAX_ATTEMPT_MULTIPLIER if max_attempts is None else max_attempts
    )
    if attempts_cap < n_replicates:
        raise BootstrapError("max_attempts must be >= n_replicates")
    resolved_contract_hash = (
        PINNED_CONTRACT_HASH if contract_hash is None else str(contract_hash)
    )
    if calibrator_fitting_event_ids is None:
        cal_events = event_ids
    else:
        cal_events = tuple(calibrator_fitting_event_ids)
    if calibrator_fitting_sample_ids is None:
        cal_samples = tuple(
            _sample_identity(sample) for block in ordered for sample in block.samples
        )
    else:
        cal_samples = tuple(calibrator_fitting_sample_ids)
    rng = np.random.default_rng(seed)
    successes: list[dict[str, float]] = []
    n_attempts = 0
    n_rejected = 0
    reasons: list[str] = []
    while len(successes) < n_replicates:
        if n_attempts >= attempts_cap:
            listed = ", ".join(reasons[-8:]) if reasons else "unknown"
            raise BootstrapError(
                "event-block bootstrap failed to reach "
                f"{n_replicates} successful refits after {n_attempts} attempts "
                f"(rejected={n_rejected}). last reasons: {listed}"
            )
        n_attempts += 1
        drawn_idx = rng.choice(len(event_ids), size=len(event_ids), replace=True)
        bag: list[TSample] = []
        for idx in drawn_idx:
            bag.extend(by_id[event_ids[int(idx)]].samples)
        try:
            fitted = refit(tuple(bag))
            predicted = dict(predict(fitted))
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            n_rejected += 1
            if isinstance(exc, MissingJointClassError):
                reasons.append("missing_classes:" + ",".join(exc.missing))
            else:
                reasons.append(type(exc).__name__ + ":" + str(exc))
            continue
        if not predicted:
            n_rejected += 1
            reasons.append("empty_prediction")
            continue
        if any(not math.isfinite(float(value)) for value in predicted.values()):
            n_rejected += 1
            reasons.append("non_finite_prediction")
            continue
        successes.append({key: float(value) for key, value in predicted.items()})
    target_ids = sorted(successes[0].keys())
    for row in successes:
        if sorted(row.keys()) != target_ids:
            raise BootstrapError("bootstrap target ids must be identical across replicates")
    prices = observed_prices or {}
    emit_ev = ev_semantics == EV_SEMANTICS_BINARY_MONEYLINE
    targets: list[TargetPercentiles] = []
    for target_id in target_ids:
        replicate_probs = [row[target_id] for row in successes]
        series = _percentile_tuple(replicate_probs)
        price = prices.get(target_id)
        prob_ev: float | None = None
        if price is not None and (emit_ev or ev_positive_fn is not None):
            prob_ev = _prob_ev_positive(replicate_probs, float(price), ev_positive_fn)
        if not emit_ev:
            targets.append(
                TargetPercentiles(
                    target_id=target_id,
                    p05=series[0],
                    p25=series[1],
                    p50=series[2],
                    p75=series[3],
                    p95=series[4],
                    observed_price=None if price is None else float(price),
                    ev05=None,
                    ev25=None,
                    ev50=None,
                    ev75=None,
                    ev95=None,
                    ev_omission_reason=omission_reason,
                    prob_ev_positive=prob_ev,
                )
            )
            continue
        if price is None:
            targets.append(
                TargetPercentiles(
                    target_id=target_id,
                    p05=series[0],
                    p25=series[1],
                    p50=series[2],
                    p75=series[3],
                    p95=series[4],
                    observed_price=None,
                    ev05=None,
                    ev25=None,
                    ev50=None,
                    ev75=None,
                    ev95=None,
                    prob_ev_positive=None,
                )
            )
            continue
        ev = _ev_tuple(series, float(price))
        targets.append(
            TargetPercentiles(
                target_id=target_id,
                p05=series[0],
                p25=series[1],
                p50=series[2],
                p75=series[3],
                p95=series[4],
                observed_price=float(price),
                ev05=ev[0],
                ev25=ev[1],
                ev50=ev[2],
                ev75=ev[3],
                ev95=ev[4],
                prob_ev_positive=prob_ev,
            )
        )
    production = n_replicates == DEFAULT_BOOTSTRAP_REPLICATES
    return BootstrapSummary(
        n_replicates=n_replicates,
        n_successful=len(successes),
        n_attempts=n_attempts,
        n_rejected=n_rejected,
        max_attempts=attempts_cap,
        seed=seed,
        event_ids=event_ids,
        estimator_hash=estimator_hash,
        config_hash=config_hash,
        data_hash=data_hash,
        contract_hash=resolved_contract_hash,
        production_qualified=production,
        targets=tuple(targets),
        rejection_reasons=tuple(reasons),
        calibrator_fitting_event_ids=cal_events,
        calibrator_fitting_sample_ids=cal_samples,
        ev_semantics=ev_semantics,
        ev_omission_reason=omission_reason,
    )


def group_ridge_samples(samples: Sequence[LabeledSample]) -> tuple[EventBlock[LabeledSample], ...]:
    grouped: dict[str, list[LabeledSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.event_id, []).append(sample)
    return tuple(
        EventBlock(event_id=event_id, samples=tuple(rows))
        for event_id, rows in sorted(grouped.items())
    )


def group_joint_samples(
    samples: Sequence[JointBoutSample],
) -> tuple[EventBlock[JointBoutSample], ...]:
    grouped: dict[str, list[JointBoutSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.event_id, []).append(sample)
    return tuple(
        EventBlock(event_id=event_id, samples=tuple(rows))
        for event_id, rows in sorted(grouped.items())
    )


def _oof_calibrator_ids(bundle: OofBundle) -> tuple[tuple[str, ...], tuple[str, ...]]:
    event_ids = tuple(sorted({row.event_id for row in bundle.rows}))
    sample_ids = tuple(row.bout_id for row in bundle.rows)
    return event_ids, sample_ids


def m1_event_block_bootstrap(
    samples: Sequence[LabeledSample],
    bundle: OofBundle,
    *,
    target_values: Mapping[str, Sequence[float]],
    spec: RidgeModelSpec | None = None,
    n_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    observed_prices: Mapping[str, float] | None = None,
    estimator_hash: str,
    config_hash: str,
    data_hash: str,
    contract_hash: str | None = None,
    calibrator: SigmoidCalibrator | None = None,
    fit_calibrator: Callable[[OofBundle], SigmoidCalibrator] | None = None,
) -> BootstrapSummary:
    """Refit ridge on event blocks; apply a calibrator fitted once on prior-time OOF."""
    resolved = spec if spec is not None else load_ridge_spec()
    groups = group_ridge_samples(samples)
    fit_fn = fit_calibrator if fit_calibrator is not None else fit_sigmoid_calibrator
    fixed = calibrator if calibrator is not None else fit_fn(bundle)
    cal_events, cal_samples = _oof_calibrator_ids(bundle)

    def refit(bag: tuple[LabeledSample, ...]) -> Any:
        return fit_ridge(bag, resolved)

    def predict(model: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for target_id, values in target_values.items():
            logit = model.predictor.raw_logit(values)
            out[target_id] = fixed.apply_logit(logit)
        return out

    return event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=n_replicates,
        seed=seed,
        observed_prices=observed_prices,
        estimator_hash=estimator_hash,
        config_hash=config_hash,
        data_hash=data_hash,
        contract_hash=contract_hash,
        calibrator_fitting_event_ids=cal_events,
        calibrator_fitting_sample_ids=cal_samples,
        ev_semantics=EV_SEMANTICS_BINARY_MONEYLINE,
    )


def m2_event_block_bootstrap(
    samples: Sequence[JointBoutSample],
    bundle: OofBundle,
    *,
    target_rows: Mapping[str, tuple[Sequence[float], int]],
    spec: JointModelSpec | None = None,
    n_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    observed_prices: Mapping[str, float] | None = None,
    estimator_hash: str,
    config_hash: str,
    data_hash: str,
    contract_hash: str | None = None,
    calibrator: TemperatureCalibrator | None = None,
    fit_calibrator: Callable[[OofBundle], TemperatureCalibrator] | None = None,
) -> BootstrapSummary:
    """Refit joint on event blocks; apply a temperature fitted once on prior-time OOF.

    Joint A-moneyline has draw/void mass, so EV percentiles are omitted even
    when an observed price is supplied.
    """
    resolved = spec if spec is not None else load_joint_spec()
    groups = group_joint_samples(samples)
    fit_fn = fit_calibrator if fit_calibrator is not None else fit_temperature_calibrator
    fixed = calibrator if calibrator is not None else fit_fn(bundle)
    cal_events, cal_samples = _oof_calibrator_ids(bundle)

    def refit(bag: tuple[JointBoutSample, ...]) -> Any:
        return fit_joint_predictor(bag, resolved)

    def predict(model: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for target_id, (values, scheduled_rounds) in target_rows.items():
            hazard = [
                [float(x) for x in model.raw_hazard_logits(values, interval)]
                for interval in range(interval_count_for_schedule(scheduled_rounds))
            ]
            decision = [float(x) for x in model.raw_decision_logits(values)]
            fine = fixed.apply_logits(
                hazard, decision, scheduled_rounds=scheduled_rounds
            )
            out[target_id] = float(sum(v for k, v in fine.items() if str(k).startswith("a_")))
        return out

    return event_block_refit_bootstrap(
        groups,
        refit=refit,
        predict=predict,
        n_replicates=n_replicates,
        seed=seed,
        observed_prices=observed_prices,
        estimator_hash=estimator_hash,
        config_hash=config_hash,
        data_hash=data_hash,
        contract_hash=contract_hash,
        calibrator_fitting_event_ids=cal_events,
        calibrator_fitting_sample_ids=cal_samples,
        ev_semantics=EV_SEMANTICS_JOINT_VOID,
    )


def run_model_calibrate(
    *,
    artifact_path: Path,
    output_path: Path | None = None,
    fixture: str | None = "protocol",
    session: Any | None = None,
    n_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    observed_prices: Mapping[str, float] | None = None,
    contract: EvaluationContract | None = None,
) -> CalibrationReport:
    """Fit M1/M2 calibrators on artifact OOF and run event-block bootstrap refits."""
    resolved_contract = (
        contract if contract is not None else load_evaluation_contract()
    )
    contract_hash = resolved_contract.content_hash
    family = detect_artifact_family(artifact_path)
    target = output_path if output_path is not None else default_calibrated_path(artifact_path)
    if family == "ridge":
        loaded = load_artifact(artifact_path)
        predictions, exclusions, n_expected, n_emitted = _oof_from_ridge_loaded(loaded)
        bundle = load_oof_bundle(
            predictions,
            exclusions,
            family="ridge",
            model_id=str(loaded.manifest.model_id or "M1"),
            n_expected=n_expected,
            n_emitted=n_emitted,
            final_estimator_hash=loaded.predictor.identity_hash(),
        )
        calibrator, record, pre, post = calibrate_ridge_bundle(
            bundle, contract_hash=contract_hash
        )
        if fixture == "protocol":
            _cards, samples = reconstruct_protocol_ridge(resolved_contract)
        elif session is not None:
            _cards, samples = reconstruct_session_ridge(session, resolved_contract)
        else:
            raise BootstrapError("bootstrap refits need --fixture protocol or a disposable DB")
        by_id = {sample.sample_id: sample for sample in samples}
        target_values = {
            row.bout_id: by_id[row.bout_id].values
            for row in bundle.rows
            if row.bout_id in by_id
        }
        if not target_values:
            raise BootstrapError("no reconstructed protocol samples match OOF bout ids")
        summary = m1_event_block_bootstrap(
            samples,
            bundle,
            target_values=target_values,
            n_replicates=n_replicates,
            seed=seed,
            observed_prices=observed_prices,
            estimator_hash=loaded.predictor.identity_hash(),
            config_hash=loaded.manifest.config_hash,
            data_hash=loaded.manifest.data_hash,
            contract_hash=contract_hash,
            calibrator=calibrator,
        )
        saved = save_calibrated_artifact(
            source_payload=loaded.payload,
            source_manifest=loaded.manifest,
            output_path=target,
            calibration=record,
            bootstrap=summary.to_dict(),
            production_qualified=summary.production_qualified,
        )
        return CalibrationReport(
            family="ridge",
            artifact=saved,
            calibration=record,
            bootstrap=summary.to_dict(),
            production_qualified=summary.production_qualified,
            exclusions=bundle.exclusions,
            metrics_pre=pre,
            metrics_post=post,
        )
    if family == "joint":
        loaded_joint = load_joint_artifact(artifact_path)
        predictions, exclusions, n_expected, n_emitted = _oof_from_joint_loaded(
            loaded_joint
        )
        bundle = load_oof_bundle(
            predictions,
            exclusions,
            family="joint",
            model_id=str(loaded_joint.manifest.model_id or "M2"),
            n_expected=n_expected,
            n_emitted=n_emitted,
            final_estimator_hash=loaded_joint.predictor.identity_hash(),
        )
        calibrator_t, record, pre, post = calibrate_joint_bundle(
            bundle, contract_hash=contract_hash
        )
        if fixture == "protocol":
            _cards, samples_j = reconstruct_protocol_joint(resolved_contract)
        elif session is not None:
            _cards, samples_j = reconstruct_session_joint(session, resolved_contract)
        else:
            raise BootstrapError("bootstrap refits need --fixture protocol or a disposable DB")
        by_jid = {sample.sample_id: sample for sample in samples_j}
        target_rows = {
            row.bout_id: (by_jid[row.bout_id].values, by_jid[row.bout_id].scheduled_rounds)
            for row in bundle.rows
            if row.bout_id in by_jid
        }
        if not target_rows:
            raise BootstrapError("no reconstructed protocol samples match joint OOF bout ids")
        summary = m2_event_block_bootstrap(
            samples_j,
            bundle,
            target_rows=target_rows,
            n_replicates=n_replicates,
            seed=seed,
            observed_prices=observed_prices,
            estimator_hash=loaded_joint.predictor.identity_hash(),
            config_hash=loaded_joint.manifest.config_hash,
            data_hash=loaded_joint.manifest.data_hash,
            contract_hash=contract_hash,
            calibrator=calibrator_t,
        )
        saved = save_calibrated_artifact(
            source_payload=loaded_joint.payload,
            source_manifest=loaded_joint.manifest,
            output_path=target,
            calibration=record,
            bootstrap=summary.to_dict(),
            production_qualified=summary.production_qualified,
        )
        return CalibrationReport(
            family="joint",
            artifact=saved,
            calibration=record,
            bootstrap=summary.to_dict(),
            production_qualified=summary.production_qualified,
            exclusions=bundle.exclusions,
            metrics_pre=pre,
            metrics_post=post,
        )
    never_family: Never = family
    raise BootstrapError(f"unhandled artifact family: {never_family!r}")
