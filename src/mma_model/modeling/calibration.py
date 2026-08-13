"""Prior-time OOF calibration for M1 sigmoid and M2 temperature (DWCS-305).

Calibrators are fit only on validated out-of-fold rows. Locked 2025 targets,
in-sample/same-card rows, and future training timestamps raise
``CalibrationLeakageError``. The final estimator's in-sample scores are never
used as fitting data.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Never

import numpy as np
from scipy.optimize import minimize
from sqlalchemy.orm import Session

from mma_model.evaluation.contract import EvaluationContract
from mma_model.features.as_of import ensure_utc
from mma_model.features.snapshot import snapshot_from_session
from mma_model.markets.derive import ATOM_SUM_ATOL, derive_markets
from mma_model.modeling.artifacts import (
    CALIBRATED_ARTIFACT_SCHEMA_VERSION,
    CALIBRATION_EVALUATION_SCOPE,
    CALIBRATION_SCHEMA_VERSION,
    ArtifactManifest,
    LoadedArtifact,
    SavedArtifact,
    compute_code_hash,
    manifest_path_for,
    resolve_code_commit,
    sha256_bytes,
    verify_bootstrap_metadata,
    verify_calibration_metadata,
    write_json_document,
)
from mma_model.modeling.baselines import (
    LabeledSample,
    labeled_samples_from_snapshot,
    protocol_training_universe,
)
from mma_model.modeling.joint import (
    PAYLOAD_KIND as JOINT_PAYLOAD_KIND,
)
from mma_model.modeling.joint import (
    PROBABILITY_CLIP_TOLERANCE,
    JointBoutSample,
    LoadedJointArtifact,
    joint_protocol_training_universe,
    joint_samples_from_snapshot,
    load_joint_spec,
    peek_artifact_payload_kind,
    stable_softmax,
    survival_multiply,
)
from mma_model.modeling.metrics import (
    PROBABILITY_EPS,
    BinaryCalibrationReport,
    JointCalibrationReport,
    binary_calibration_report,
    joint_calibration_report,
    stable_logit,
    stable_sigmoid,
)
from mma_model.modeling.splits import (
    FoldRole,
    SplitCard,
    cards_from_session,
    group_cards,
)
from mma_model.quality.schema import sha256_canonical

ALLOWED_FOLD_KINDS: Final = frozenset({"inner", "outer", "tuning", "validation"})
HOLDOUT_YEAR: Final = 2025
MIN_TEMPERATURE: Final = 1e-6
MAX_TEMPERATURE: Final = 1e6
SIGMOID_L2: Final = 1e-4
SIGMOID_PARAM_ABS_BOUND: Final = 50.0
ModelFamily = Literal["ridge", "joint"]


class LeakageKind(StrEnum):
    IN_SAMPLE = "in_sample"
    SAME_CARD = "same_card"
    FUTURE = "future"
    LOCKED_2025 = "locked_2025"
    FOLD_KIND = "fold_kind"
    DUPLICATE = "duplicate"
    FINAL_ESTIMATOR = "final_estimator_insample"
    MISSING_PROVENANCE = "missing_provenance"
    COUNT_MISMATCH = "count_mismatch"


class CalibrationError(ValueError):
    """Calibration cannot proceed."""


class CalibrationLeakageError(CalibrationError):
    """An OOF row leaked training outcomes, 2025, or future information."""

    def __init__(self, kind: LeakageKind, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedOofRow:
    bout_id: str
    event_id: str
    fold_id: str
    fold_kind: str
    test_cutoff: datetime
    train_event_ids: tuple[str, ...]
    train_event_ids_hash: str
    train_max_timestamp: datetime
    estimator_kind: str
    estimator_hash: str
    model_id: str
    y: int | None
    raw_probability: float | None
    raw_logit: float | None
    hazard_logits: tuple[tuple[float, ...], ...] | None
    decision_logits: tuple[float, ...] | None
    observed_fine_atom: str | None
    observed_frozen_atom: str | None
    scheduled_rounds: int | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class OofBundle:
    rows: tuple[ValidatedOofRow, ...]
    exclusions: tuple[dict[str, Any], ...]
    n_expected: int
    n_emitted: int
    n_excluded: int
    family: ModelFamily
    model_id: str

    def reconcile(self) -> None:
        if self.n_emitted != len(self.rows):
            raise CalibrationLeakageError(
                LeakageKind.COUNT_MISMATCH,
                f"n_oof_emitted {self.n_emitted} != {len(self.rows)} validated rows",
            )
        if self.n_emitted + self.n_excluded != self.n_expected:
            raise CalibrationLeakageError(
                LeakageKind.COUNT_MISMATCH,
                "OOF counts do not reconcile: "
                f"expected={self.n_expected} emitted={self.n_emitted} "
                f"excluded={self.n_excluded}",
            )


@dataclass(frozen=True)
class SigmoidCalibrator:
    a: float
    b: float
    schema_version: str = CALIBRATION_SCHEMA_VERSION

    def apply_logit(self, logit: float) -> float:
        probability = float(stable_sigmoid(self.a * float(logit) + self.b))
        if probability <= 0.0 or probability >= 1.0:
            return min(max(probability, PROBABILITY_EPS), 1.0 - PROBABILITY_EPS)
        return probability

    def apply_probability(self, probability: float) -> float:
        return self.apply_logit(float(stable_logit(probability)))


@dataclass(frozen=True)
class TemperatureCalibrator:
    temperature: float
    schema_version: str = CALIBRATION_SCHEMA_VERSION
    clip_tolerance: float = PROBABILITY_CLIP_TOLERANCE

    def apply_logits(
        self,
        hazard_logits: Sequence[Sequence[float]],
        decision_logits: Sequence[float],
        *,
        scheduled_rounds: int,
    ) -> dict[str, float]:
        return apply_joint_temperature(
            hazard_logits,
            decision_logits,
            temperature=self.temperature,
            scheduled_rounds=scheduled_rounds,
            clip_tolerance=self.clip_tolerance,
        )


def _parse_cutoff(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE, f"{field} must be an ISO-8601 timestamp"
        )
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ensure_utc(parsed)


def _is_2025(event_id: str, bout_id: str, cutoff: datetime) -> bool:
    if cutoff.year == HOLDOUT_YEAR:
        return True
    lowered_event = event_id.lower()
    lowered_bout = bout_id.lower()
    if "2025" in lowered_event or "2025" in lowered_bout:
        return True
    return "holdout" in lowered_event or lowered_event.startswith("hold-")


def _normalize_fold_kind(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CalibrationLeakageError(
            LeakageKind.FOLD_KIND, "fold_kind is required on every OOF row"
        )
    kind = raw.strip().lower()
    if kind in {"holdout", "sealed", "locked"}:
        raise CalibrationLeakageError(
            LeakageKind.FOLD_KIND,
            f"fold kind {kind!r} is not prior-time tuning/validation",
        )
    if kind not in ALLOWED_FOLD_KINDS:
        raise CalibrationLeakageError(
            LeakageKind.FOLD_KIND,
            f"fold kind {kind!r} is not prior-time tuning/validation",
        )
    if kind in {"inner", "tuning"}:
        return "tuning"
    if kind in {"outer", "validation"}:
        return "validation"
    never_kind: Never = kind  # pragma: no cover
    raise CalibrationLeakageError(LeakageKind.FOLD_KIND, f"unhandled fold kind {never_kind!r}")


def _require_hash(value: object, *, field: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE, f"{field} must be a sha256 hex digest"
        )
    return text


def validate_oof_row(
    payload: Mapping[str, Any],
    *,
    family: ModelFamily,
    final_estimator_hash: str | None,
) -> ValidatedOofRow:
    bout_id = str(payload.get("bout_id") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    fold_id = str(payload.get("fold_id") or "").strip()
    if not bout_id or not event_id or not fold_id:
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE,
            "OOF row needs bout_id, event_id, and fold_id",
        )
    fold_kind = _normalize_fold_kind(payload.get("fold_kind"))
    role_raw = payload.get("fold_role")
    if role_raw is not None and str(role_raw).strip():
        role = str(role_raw).strip().lower()
        if role in {"holdout", "locked", "sealed"}:
            raise CalibrationLeakageError(
                LeakageKind.LOCKED_2025,
                f"OOF fold_role {role!r} is locked holdout and cannot tune",
            )
    season_raw = payload.get("season")
    if isinstance(season_raw, int) and season_raw == HOLDOUT_YEAR:
        raise CalibrationLeakageError(
            LeakageKind.LOCKED_2025,
            f"OOF season {season_raw} is locked holdout",
        )
    cutoff = _parse_cutoff(payload.get("test_cutoff"), field="test_cutoff")
    train_max = _parse_cutoff(
        payload.get("train_max_timestamp"), field="train_max_timestamp"
    )
    train_ids_raw = payload.get("train_event_ids")
    if not isinstance(train_ids_raw, list) or not all(
        isinstance(item, str) for item in train_ids_raw
    ):
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE, "train_event_ids must be a list of strings"
        )
    train_event_ids = tuple(str(item) for item in train_ids_raw)
    train_hash = _require_hash(
        payload.get("train_event_ids_hash"), field="train_event_ids_hash"
    )
    expected_hash = sha256_canonical({"train_event_ids": list(train_event_ids)})
    if train_hash != expected_hash:
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE, "train_event_ids_hash does not match train_event_ids"
        )
    estimator_kind = str(payload.get("estimator_kind") or "").strip()
    estimator_hash = _require_hash(payload.get("estimator_hash"), field="estimator_hash")
    model_id = str(payload.get("model_id") or "").strip()
    if not estimator_kind or not model_id:
        raise CalibrationLeakageError(
            LeakageKind.MISSING_PROVENANCE, "estimator_kind and model_id are required"
        )
    if event_id in set(train_event_ids):
        raise CalibrationLeakageError(
            LeakageKind.SAME_CARD,
            f"target event {event_id!r} appears in train_event_ids (same-card leakage)",
        )
    if train_max >= cutoff:
        raise CalibrationLeakageError(
            LeakageKind.FUTURE,
            f"train_max_timestamp {train_max.isoformat()} is not strictly before "
            f"test cutoff {cutoff.isoformat()}",
        )
    if _is_2025(event_id, bout_id, cutoff):
        raise CalibrationLeakageError(
            LeakageKind.LOCKED_2025,
            f"OOF target {bout_id} / {event_id} is locked 2025 holdout",
        )
    for train_id in train_event_ids:
        if _is_2025(train_id, "", train_max):
            raise CalibrationLeakageError(
                LeakageKind.LOCKED_2025,
                f"train event {train_id!r} looks like locked 2025 holdout",
            )
    if final_estimator_hash is not None and estimator_hash == final_estimator_hash:
        raise CalibrationLeakageError(
            LeakageKind.FINAL_ESTIMATOR,
            "refusing to fit a calibrator on the final estimator's in-sample scores",
        )
    y = None
    raw_p = None
    raw_logit = None
    hazard_logits = None
    decision_logits = None
    observed_fine = None
    observed_frozen = None
    scheduled_rounds = None
    if family == "ridge":
        if payload.get("y") not in (0, 1):
            raise CalibrationError("ridge OOF y must be 0 or 1")
        y = int(payload["y"])
        raw_p = float(payload.get("raw_probability"))
        raw_logit = float(payload.get("raw_logit"))
        if not math.isfinite(raw_p) or raw_p <= 0.0 or raw_p >= 1.0:
            raise CalibrationError("ridge OOF raw_probability must be in (0, 1)")
        if not math.isfinite(raw_logit):
            raise CalibrationError("ridge OOF raw_logit must be finite")
    elif family == "joint":
        observed_fine = str(payload.get("observed_fine_atom") or "").strip()
        observed_frozen = str(
            payload.get("observed_frozen_atom") or payload.get("observed_label") or ""
        ).strip()
        if not observed_fine:
            raise CalibrationError("joint OOF needs observed_fine_atom")
        hazard_raw = payload.get("hazard_logits")
        decision_raw = payload.get("decision_logits")
        if not isinstance(hazard_raw, list) or not hazard_raw:
            raise CalibrationError("joint OOF hazard_logits must be a non-empty list")
        if not isinstance(decision_raw, list) or not decision_raw:
            raise CalibrationError("joint OOF decision_logits must be a non-empty list")
        hazard_logits = tuple(tuple(float(x) for x in row) for row in hazard_raw)
        decision_logits = tuple(float(x) for x in decision_raw)
        scheduled_rounds = int(payload.get("scheduled_rounds") or 0)
        if scheduled_rounds not in {3, 5}:
            raise CalibrationError("joint OOF scheduled_rounds must be 3 or 5")
    else:
        never_family: Never = family
        raise CalibrationError(f"unhandled model family: {never_family!r}")
    return ValidatedOofRow(
        bout_id=bout_id,
        event_id=event_id,
        fold_id=fold_id,
        fold_kind=fold_kind,
        test_cutoff=cutoff,
        train_event_ids=train_event_ids,
        train_event_ids_hash=train_hash,
        train_max_timestamp=train_max,
        estimator_kind=estimator_kind,
        estimator_hash=estimator_hash,
        model_id=model_id,
        y=y,
        raw_probability=raw_p,
        raw_logit=raw_logit,
        hazard_logits=hazard_logits,
        decision_logits=decision_logits,
        observed_fine_atom=observed_fine,
        observed_frozen_atom=observed_frozen or None,
        scheduled_rounds=scheduled_rounds,
        payload=dict(payload),
    )


def _exclusion_bout_count(item: Mapping[str, Any]) -> int:
    if "n_test" in item:
        return int(item["n_test"])
    bouts = item.get("test_bout_ids")
    if isinstance(bouts, list):
        return len(bouts)
    return 0


def load_oof_bundle(
    predictions: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
    *,
    family: ModelFamily,
    model_id: str,
    n_expected: int | None = None,
    n_emitted: int | None = None,
    final_estimator_hash: str | None = None,
) -> OofBundle:
    rows = [
        validate_oof_row(item, family=family, final_estimator_hash=final_estimator_hash)
        for item in predictions
    ]
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row.bout_id, row.model_id, row.fold_id)
        if key in seen:
            raise CalibrationLeakageError(
                LeakageKind.DUPLICATE,
                f"duplicate OOF prediction for {row.bout_id} / {row.model_id} / {row.fold_id}",
            )
        seen.add(key)
    excl = tuple(dict(item) for item in exclusions)
    n_excluded = sum(_exclusion_bout_count(item) for item in excl)
    emitted = n_emitted if n_emitted is not None else len(rows)
    expected = n_expected if n_expected is not None else emitted + n_excluded
    bundle = OofBundle(
        rows=tuple(rows),
        exclusions=excl,
        n_expected=expected,
        n_emitted=emitted,
        n_excluded=n_excluded,
        family=family,
        model_id=model_id,
    )
    bundle.reconcile()
    return bundle


def fit_sigmoid_calibrator(bundle: OofBundle) -> SigmoidCalibrator:
    """Fit Platt scaling on prior-time OOF only.

    Objective: mean Bernoulli NLL of ``sigmoid(a * raw_logit + b)`` plus
    ``(λ/2) * (a² + b²)`` with ``λ = 1e-4``. Parameters are bounded to
    ``[-50, 50]``; a bound hit is a failure, not a silent clip.
    """
    if bundle.family != "ridge":
        raise CalibrationError("sigmoid calibration is M1/ridge only")
    if not bundle.rows:
        raise CalibrationError("no prior-time OOF rows to fit a sigmoid calibrator")
    logits = np.asarray(
        [float(row.raw_logit) for row in bundle.rows if row.raw_logit is not None],
        dtype=np.float64,
    )
    y_arr = np.asarray(
        [int(row.y) for row in bundle.rows if row.y is not None],
        dtype=np.int64,
    )
    if logits.size != len(bundle.rows) or y_arr.size != len(bundle.rows):
        raise CalibrationError("ridge OOF rows are missing y or raw_logit")
    bound = SIGMOID_PARAM_ABS_BOUND

    def objective(params: np.ndarray) -> float:
        a = float(params[0])
        b = float(params[1])
        z = a * logits + b
        p = np.asarray(stable_sigmoid(z), dtype=np.float64)
        p = np.clip(p, PROBABILITY_EPS, 1.0 - PROBABILITY_EPS)
        nll = float(-np.mean(y_arr * np.log(p) + (1.0 - y_arr) * np.log(1.0 - p)))
        return nll + 0.5 * SIGMOID_L2 * (a * a + b * b)

    result = minimize(
        objective,
        x0=np.array([1.0, 0.0], dtype=np.float64),
        method="L-BFGS-B",
        bounds=((-bound, bound), (-bound, bound)),
    )
    if not bool(result.success):
        raise CalibrationError(f"sigmoid fit failed: {result.message}")
    a = float(result.x[0])
    b = float(result.x[1])
    if not math.isfinite(a) or not math.isfinite(b):
        raise CalibrationError("sigmoid parameters must be finite")
    margin = 1e-8
    if abs(a) >= bound - margin or abs(b) >= bound - margin:
        raise CalibrationError(
            "sigmoid parameters hit numerical bounds; OOF may be separable"
        )
    return SigmoidCalibrator(a=a, b=b)


def apply_joint_temperature(
    hazard_logits: Sequence[Sequence[float]],
    decision_logits: Sequence[float],
    *,
    temperature: float,
    scheduled_rounds: int,
    clip_tolerance: float = PROBABILITY_CLIP_TOLERANCE,
) -> dict[str, float]:
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise CalibrationError("temperature T must be finite and > 0")
    scaled_hazards = []
    for row in hazard_logits:
        arr = np.asarray(row, dtype=np.float64) / temperature
        scaled_hazards.append(stable_softmax(arr))
    hazards = np.vstack(scaled_hazards)
    decisions = stable_softmax(np.asarray(decision_logits, dtype=np.float64) / temperature)
    return survival_multiply(
        hazards,
        decisions,
        scheduled_rounds=scheduled_rounds,
        clip_tolerance=clip_tolerance,
    )


def _joint_nll_at_temperature(bundle: OofBundle, temperature: float) -> float:
    total = 0.0
    for row in bundle.rows:
        if row.hazard_logits is None or row.decision_logits is None:
            raise CalibrationError("joint OOF missing logits")
        if row.observed_fine_atom is None or row.scheduled_rounds is None:
            raise CalibrationError("joint OOF missing observed atom")
        fine = apply_joint_temperature(
            row.hazard_logits,
            row.decision_logits,
            temperature=temperature,
            scheduled_rounds=row.scheduled_rounds,
        )
        p_obs = float(fine[row.observed_fine_atom])
        total += -math.log(max(p_obs, PROBABILITY_EPS))
    return total / len(bundle.rows)


def fit_temperature_calibrator(bundle: OofBundle) -> TemperatureCalibrator:
    if bundle.family != "joint":
        raise CalibrationError("temperature scaling is M2/joint only")
    if not bundle.rows:
        raise CalibrationError("no prior-time OOF rows to fit temperature")

    def objective(params: np.ndarray) -> float:
        temperature = float(math.exp(params[0]))
        return _joint_nll_at_temperature(bundle, temperature)

    result = minimize(
        objective,
        x0=np.array([0.0], dtype=np.float64),
        method="L-BFGS-B",
        bounds=((math.log(MIN_TEMPERATURE), math.log(MAX_TEMPERATURE)),),
    )
    if not bool(result.success):
        raise CalibrationError(f"temperature fit failed: {result.message}")
    temperature = float(math.exp(float(result.x[0])))
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise CalibrationError("fitted temperature must be finite and > 0")
    return TemperatureCalibrator(temperature=temperature)


def ridge_pre_post_metrics(bundle: OofBundle, calibrator: SigmoidCalibrator) -> tuple[
    BinaryCalibrationReport, BinaryCalibrationReport
]:
    y = [int(row.y) for row in bundle.rows if row.y is not None]
    raw_p = [float(row.raw_probability) for row in bundle.rows if row.raw_probability is not None]
    cal_p = [
        calibrator.apply_logit(float(row.raw_logit))
        for row in bundle.rows
        if row.raw_logit is not None
    ]
    events = [row.event_id for row in bundle.rows]
    pre = binary_calibration_report(y, raw_p, event_ids=events)
    post = binary_calibration_report(y, cal_p, event_ids=events)
    return pre, post


def joint_pre_post_metrics(
    bundle: OofBundle, calibrator: TemperatureCalibrator
) -> tuple[JointCalibrationReport, JointCalibrationReport]:
    raw_dists: list[dict[str, float]] = []
    cal_dists: list[dict[str, float]] = []
    atoms: list[str] = []
    events: list[str] = []
    for row in bundle.rows:
        if (
            row.hazard_logits is None
            or row.decision_logits is None
            or row.observed_fine_atom is None
            or row.scheduled_rounds is None
        ):
            raise CalibrationError("joint OOF row missing logits or observed atom")
        raw = apply_joint_temperature(
            row.hazard_logits,
            row.decision_logits,
            temperature=1.0,
            scheduled_rounds=row.scheduled_rounds,
        )
        cal = calibrator.apply_logits(
            row.hazard_logits,
            row.decision_logits,
            scheduled_rounds=row.scheduled_rounds,
        )
        if abs(sum(cal.values()) - 1.0) > ATOM_SUM_ATOL:
            raise CalibrationError("calibrated joint distribution is not normalized")
        derive_markets(raw, scheduled_rounds=row.scheduled_rounds)
        derive_markets(cal, scheduled_rounds=row.scheduled_rounds)
        raw_dists.append(raw)
        cal_dists.append(cal)
        atoms.append(row.observed_fine_atom)
        events.append(row.event_id)
    pre = joint_calibration_report(raw_dists, atoms, event_ids=events)
    post = joint_calibration_report(cal_dists, atoms, event_ids=events)
    return pre, post


def _cutoff_range(rows: Sequence[ValidatedOofRow]) -> tuple[str, str]:
    cutoffs = [row.test_cutoff for row in rows]
    return min(cutoffs).isoformat(), max(cutoffs).isoformat()


def _calibration_record(
    *,
    kind: str,
    bundle: OofBundle,
    metrics_pre: Mapping[str, Any],
    metrics_post: Mapping[str, Any],
    contract_hash: str,
    a: float | None = None,
    b: float | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    event_ids = sorted({row.event_id for row in bundle.rows})
    sample_ids = [row.bout_id for row in bundle.rows]
    cutoff_min, cutoff_max = _cutoff_range(bundle.rows)
    payload: dict[str, Any] = {
        "contract_hash": contract_hash,
        "evaluation_scope": CALIBRATION_EVALUATION_SCOPE,
        "fitting_cutoff_max": cutoff_max,
        "fitting_cutoff_min": cutoff_min,
        "fitting_event_ids": event_ids,
        "fitting_event_ids_hash": sha256_canonical({"fitting_event_ids": event_ids}),
        "fitting_sample_ids": sample_ids,
        "fitting_sample_ids_hash": sha256_canonical({"fitting_sample_ids": sample_ids}),
        "independent_post_calibration_evaluation": False,
        "metrics_post": dict(metrics_post),
        "metrics_pre": dict(metrics_pre),
        "n_fitting_events": len(event_ids),
        "n_fitting_oof": len(bundle.rows),
        "oof_exclusions": list(bundle.exclusions),
        "oof_n_emitted": bundle.n_emitted,
        "oof_n_excluded": bundle.n_excluded,
        "oof_n_expected": bundle.n_expected,
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "type": kind,
    }
    if kind == "sigmoid":
        payload["a"] = a
        payload["b"] = b
        payload["temperature"] = None
    elif kind == "temperature":
        payload["a"] = None
        payload["b"] = None
        payload["temperature"] = temperature
    else:
        never_kind: Never = kind
        raise CalibrationError(f"unhandled calibrator type: {never_kind!r}")
    verify_calibration_metadata(payload)
    return payload


def detect_artifact_family(path: Path) -> ModelFamily:
    kind = peek_artifact_payload_kind(path)
    if kind == JOINT_PAYLOAD_KIND:
        return "joint"
    if kind == "standardized_ridge_logistic_v1":
        return "ridge"
    raise CalibrationError(f"unable to detect M1/M2 artifact family from {path}")


def _oof_from_ridge_loaded(
    loaded: LoadedArtifact,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    predictions = list(loaded.oof_predictions)
    exclusions = list(loaded.oof_exclusions)
    metrics = loaded.manifest.metrics
    if not predictions:
        predictions = list(metrics.get("oof_predictions") or [])
        exclusions = list(metrics.get("oof_exclusions") or [])
    n_expected = int(metrics.get("n_oof_expected", len(predictions) + sum(
        int(item.get("n_test", 0)) for item in exclusions if isinstance(item, dict)
    )))
    n_emitted = int(metrics.get("n_oof_emitted", len(predictions)))
    return predictions, exclusions, n_expected, n_emitted


def _oof_from_joint_loaded(
    loaded: LoadedJointArtifact,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    predictions = list(loaded.oof_predictions)
    exclusions = list(loaded.oof_exclusions)
    metrics = loaded.manifest.metrics
    n_expected = int(metrics.get("n_oof_expected", len(predictions) + sum(
        int(item.get("n_test", 0)) for item in exclusions if isinstance(item, dict)
    )))
    n_emitted = int(metrics.get("n_oof_emitted", len(predictions)))
    return predictions, exclusions, n_expected, n_emitted


def default_calibrated_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.stem + ".calibrated.json")


def save_calibrated_artifact(
    *,
    source_payload: Mapping[str, Any],
    source_manifest: ArtifactManifest,
    output_path: Path,
    calibration: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
    production_qualified: bool,
) -> SavedArtifact:
    """Write a new calibrated JSON artifact. Never mutates the source file."""
    verify_calibration_metadata(dict(calibration))
    verify_bootstrap_metadata(
        dict(bootstrap), require_production=production_qualified
    )
    extra = [
        Path(__file__),
        Path(__file__).with_name("uncertainty.py"),
        Path(__file__).with_name("metrics.py"),
    ]
    code_hash = compute_code_hash(extra_paths=extra)
    code_commit, code_commit_reason = resolve_code_commit()
    stored = dict(source_payload)
    stored["bootstrap"] = dict(bootstrap)
    stored["calibrated"] = True
    stored["calibration"] = dict(calibration)
    stored["code_commit"] = code_commit
    stored["code_commit_reason"] = code_commit_reason
    stored["code_hash"] = code_hash
    stored["production_qualified"] = production_qualified
    stored["schema_version"] = CALIBRATED_ARTIFACT_SCHEMA_VERSION
    stored["source_payload_sha256"] = source_manifest.payload_sha256
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = write_json_document(target, stored)
    digest = sha256_bytes(blob)
    manifest = ArtifactManifest(
        schema_version=CALIBRATED_ARTIFACT_SCHEMA_VERSION,
        model_id=source_manifest.model_id,
        spec_id=source_manifest.spec_id,
        spec_version=source_manifest.spec_version,
        feature_spec_hash=source_manifest.feature_spec_hash,
        contract_hash=source_manifest.contract_hash,
        config_hash=source_manifest.config_hash,
        splits_config_hash=source_manifest.splits_config_hash,
        data_hash=source_manifest.data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
        feature_names=source_manifest.feature_names,
        train_sample_ids=source_manifest.train_sample_ids,
        max_train_timestamp=source_manifest.max_train_timestamp,
        cutoff_policy=source_manifest.cutoff_policy,
        metrics=dict(source_manifest.metrics),
        payload_sha256=digest,
        calibrated=True,
        calibration=dict(calibration),
        bootstrap=dict(bootstrap),
    )
    write_json_document(manifest_path_for(target), manifest.to_dict())
    return SavedArtifact(
        payload_path=target,
        manifest_path=manifest_path_for(target),
        manifest=manifest,
        payload_sha256=digest,
    )


def exclude_locked_samples_ridge(
    samples: Sequence[LabeledSample],
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> tuple[LabeledSample, ...]:
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    kept: list[LabeledSample] = []
    for sample in samples:
        group = groups.get(sample.event_id)
        if group is None:
            continue
        if group.role is FoldRole.HOLDOUT or group.locked:
            continue
        if group.role is FoldRole.DEVELOPMENT or group.role is FoldRole.VALIDATION:
            kept.append(sample)
            continue
        never_role: Never = group.role
        raise CalibrationError(f"unhandled fold role: {never_role!r}")
    return tuple(kept)


def exclude_locked_samples_joint(
    samples: Sequence[JointBoutSample],
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> tuple[JointBoutSample, ...]:
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    kept: list[JointBoutSample] = []
    for sample in samples:
        group = groups.get(sample.event_id)
        if group is None:
            continue
        if group.role is FoldRole.HOLDOUT or group.locked:
            continue
        if group.role is FoldRole.DEVELOPMENT or group.role is FoldRole.VALIDATION:
            kept.append(sample)
            continue
        never_role: Never = group.role
        raise CalibrationError(f"unhandled fold role: {never_role!r}")
    return tuple(kept)


def reconstruct_protocol_ridge(
    contract: EvaluationContract | None = None,
) -> tuple[tuple[SplitCard, ...], tuple[LabeledSample, ...]]:
    cards, snapshot, odds = protocol_training_universe()
    samples = labeled_samples_from_snapshot(
        snapshot,
        cards,
        odds_by_bout=odds,
        allow_holdout=False,
        contract=contract,
    )
    return cards, exclude_locked_samples_ridge(samples, cards, contract)


def reconstruct_protocol_joint(
    contract: EvaluationContract | None = None,
) -> tuple[tuple[SplitCard, ...], tuple[JointBoutSample, ...]]:
    spec = load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    samples = joint_samples_from_snapshot(
        snapshot, cards, spec, allow_holdout=False, contract=contract
    )
    return cards, exclude_locked_samples_joint(samples, cards, contract)


def reconstruct_session_ridge(
    session: Session, contract: EvaluationContract | None = None
) -> tuple[tuple[SplitCard, ...], tuple[LabeledSample, ...]]:
    cards = cards_from_session(session)
    snapshot = snapshot_from_session(session)
    samples = labeled_samples_from_snapshot(
        snapshot,
        cards,
        allow_holdout=False,
        contract=contract,
    )
    return cards, exclude_locked_samples_ridge(samples, cards, contract)


def reconstruct_session_joint(
    session: Session, contract: EvaluationContract | None = None
) -> tuple[tuple[SplitCard, ...], tuple[JointBoutSample, ...]]:
    spec = load_joint_spec()
    cards = cards_from_session(session)
    snapshot = snapshot_from_session(session)
    samples = joint_samples_from_snapshot(
        snapshot, cards, spec, allow_holdout=False, contract=contract
    )
    return cards, exclude_locked_samples_joint(samples, cards, contract)


@dataclass(frozen=True)
class CalibrationReport:
    family: ModelFamily
    artifact: SavedArtifact
    calibration: dict[str, Any]
    bootstrap: dict[str, Any]
    production_qualified: bool
    exclusions: tuple[dict[str, Any], ...]
    metrics_pre: dict[str, Any]
    metrics_post: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact_path": str(self.artifact.payload_path.resolve()),
            "bootstrap": self.bootstrap,
            "calibration": self.calibration,
            "exclusions": list(self.exclusions),
            "family": self.family,
            "manifest_path": str(self.artifact.manifest_path.resolve()),
            "metrics_post": self.metrics_post,
            "metrics_pre": self.metrics_pre,
            "production_qualified": self.production_qualified,
        }
        if self.family == "ridge":
            payload["a"] = self.calibration.get("a")
            payload["b"] = self.calibration.get("b")
        elif self.family == "joint":
            payload["temperature"] = self.calibration.get("temperature")
        else:
            never_family: Never = self.family
            raise CalibrationError(f"unhandled family: {never_family!r}")
        return payload


def calibrate_ridge_bundle(
    bundle: OofBundle,
    *,
    contract_hash: str,
) -> tuple[SigmoidCalibrator, dict[str, Any], dict[str, Any], dict[str, Any]]:
    calibrator = fit_sigmoid_calibrator(bundle)
    pre, post = ridge_pre_post_metrics(bundle, calibrator)
    record = _calibration_record(
        kind="sigmoid",
        bundle=bundle,
        metrics_pre=pre.to_dict(),
        metrics_post=post.to_dict(),
        contract_hash=contract_hash,
        a=calibrator.a,
        b=calibrator.b,
    )
    return calibrator, record, pre.to_dict(), post.to_dict()


def calibrate_joint_bundle(
    bundle: OofBundle,
    *,
    contract_hash: str,
) -> tuple[TemperatureCalibrator, dict[str, Any], dict[str, Any], dict[str, Any]]:
    calibrator = fit_temperature_calibrator(bundle)
    pre, post = joint_pre_post_metrics(bundle, calibrator)
    record = _calibration_record(
        kind="temperature",
        bundle=bundle,
        metrics_pre=pre.to_dict(),
        metrics_post=post.to_dict(),
        contract_hash=contract_hash,
        temperature=calibrator.temperature,
    )
    return calibrator, record, pre.to_dict(), post.to_dict()
