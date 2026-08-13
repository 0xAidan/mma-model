"""Regularized discrete-time competing-risks joint model (DWCS-304).

One multinomial hazard per half-round interval plus a final decision head.
Tied A/B parameters make raw logits swap-equivariant without serving-time
averaging. Ordinary training never reads locked 2025 holdout cards.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal, Never

import numpy as np
import yaml
from scipy.optimize import minimize
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from mma_model.dwcs.classification import SeriesVariant
from mma_model.dwcs.duration import DurationStatus
from mma_model.evaluation.contract import (
    PINNED_CONTRACT_HASH,
    EvaluationContract,
    TerminalAtom,
    compute_contract_hash,
    mutable_fact_allowed_at_cutoff,
)
from mma_model.features.as_of import AsOfCutoff, cutoff_for_event, implied_event_start
from mma_model.features.builder import FeatureBuilder
from mma_model.features.duration import half_round_duration
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotBout,
    SnapshotEvent,
    SnapshotResultVersion,
    snapshot_from_session,
    to_label_version,
)
from mma_model.features.spec import (
    FEATURE_FIELDS,
    FEATURE_NAMES,
    SPEC_VERSION,
    FeatureRole,
    row_bytes,
    spec_hash,
    swap_values,
)
from mma_model.labels.outcomes import (
    MethodLabel,
    OutcomeLabel,
    WinnerSide,
    training_label,
)
from mma_model.markets.derive import (
    ATOM_SUM_ATOL,
    DerivedMarketProbabilities,
    UnsupportedScheduleError,
    aggregate_frozen_atoms,
    derive_markets,
    fine_atom_keys,
    finish_atom_key,
    interval_count_for_schedule,
)
from mma_model.markets.settlement import SUPPORTED_SCHEDULED_ROUNDS
from mma_model.modeling.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    ArtifactChecksumMismatchError,
    ArtifactConfigMismatchError,
    ArtifactFeatureOrderMismatchError,
    ArtifactManifest,
    ArtifactSpecMismatchError,
    SavedArtifact,
    UntrustedArtifactError,
    compute_code_hash,
    manifest_from_mapping,
    manifest_path_for,
    resolve_code_commit,
    sha256_bytes,
)
from mma_model.modeling.baselines import (
    FORBIDDEN_HOLDOUT_METRIC_FRAGMENTS,
    LABEL_LAG,
    ORDINARY_TRAIN_ROLES,
    TrainError,
    TrainReport,
)
from mma_model.modeling.splits import (
    FoldKind,
    FoldPlan,
    FoldRole,
    HoldoutLockedError,
    SplitCard,
    SplitError,
    cards_from_session,
    group_cards,
    tuning_folds,
    validation_folds,
)

JOINT_SPEC_FILENAME: Final = "joint_v1.yaml"
JOINT_SPEC_ID: Final = "joint_v1"
JOINT_CONTRACT_ID: Final = "dwcs_model_spec"
EXPECTED_JOINT_SCHEMA_VERSION: Final = 1
EXPECTED_JOINT_SPEC_VERSION: Final = "1.0.0"
EXPECTED_JOINT_MODEL_ID: Final = "M2"
EXPECTED_JOINT_FAMILY: Final = "competing_risks"
EXPECTED_CUTOFF_POLICY: Final = "scheduled_minus_60m"
EXPECTED_FINAL_REFIT: Final = "development_and_validation"
PAYLOAD_KIND: Final = "tied_competing_risks_v1"
ESTIMATOR_KIND: Final = "tied_multinomial_hazards_plus_decision"
MAX_INTERVALS: Final = 10
SWAP_ATOL: Final = 1e-8
PROBABILITY_CLIP_TOLERANCE: Final = 1e-12
PINNED_JOINT_SPEC_HASH: Final = (
    "0ba554c635cec82c36c6c887ac0c4186a141d10a6a088b6cb035bd2b48008fac"
)

MissingClassMode = Literal["fail", "pool"]
EarlyTechnicalMode = Literal["fail", "pool_other_stoppage", "pool_as_distance"]
ModelFamilyKind = Literal["ridge", "joint"]


class HazardClass(StrEnum):
    CONTINUE = "continue"
    A_KO_TKO = "a_ko_tko"
    B_KO_TKO = "b_ko_tko"
    A_SUBMISSION = "a_submission"
    B_SUBMISSION = "b_submission"
    A_OTHER_STOPPAGE = "a_other_stoppage"
    B_OTHER_STOPPAGE = "b_other_stoppage"


class DecisionClass(StrEnum):
    A_DECISION = "a_decision"
    B_DECISION = "b_decision"
    DRAW = "draw"


class BoutTerminalKind(StrEnum):
    FINISH = "finish"
    DISTANCE = "distance"


HAZARD_CLASSES: Final[tuple[HazardClass, ...]] = tuple(HazardClass)
DECISION_CLASSES: Final[tuple[DecisionClass, ...]] = tuple(DecisionClass)
FINISH_HAZARD_CLASSES: Final[tuple[HazardClass, ...]] = (
    HazardClass.A_KO_TKO,
    HazardClass.B_KO_TKO,
    HazardClass.A_SUBMISSION,
    HazardClass.B_SUBMISSION,
    HazardClass.A_OTHER_STOPPAGE,
    HazardClass.B_OTHER_STOPPAGE,
)
REQUIRED_HAZARD_CLASSES: Final[frozenset[HazardClass]] = frozenset(HAZARD_CLASSES)
REQUIRED_DECISION_CLASSES: Final[frozenset[DecisionClass]] = frozenset(DECISION_CLASSES)
HAZARD_INDEX: Final[dict[HazardClass, int]] = {
    item: idx for idx, item in enumerate(HAZARD_CLASSES)
}
DECISION_INDEX: Final[dict[DecisionClass, int]] = {
    item: idx for idx, item in enumerate(DECISION_CLASSES)
}
CONTINUE_INDEX: Final = HAZARD_INDEX[HazardClass.CONTINUE]


class JointError(ValueError):
    """Joint competing-risks model cannot proceed."""


class JointSpecError(JointError):
    """Joint model spec failed to load or did not match the pinned digest."""


class MissingJointClassError(JointError):
    """Required hazard or decision classes are absent from training labels."""

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = tuple(missing)
        listed = ", ".join(self.missing) if self.missing else "(none)"
        super().__init__(
            "required joint classes are missing from training labels: "
            f"{listed}. Declare class_pooling in the spec or add labeled rows."
        )


class EarlyTechnicalOutcomeError(JointError):
    """Early technical decision/draw without documented pooling."""


class JointNumericalError(JointError):
    """Logits or probabilities were non-finite or failed normalization."""


def oriented_features(values: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """Split a feature row into swap-invariant and swap-antisymmetric parts."""
    if len(values) != len(FEATURE_NAMES):
        raise JointError("feature vector length does not match FEATURE_NAMES")
    by_name = {name: float(values[idx]) for idx, name in enumerate(FEATURE_NAMES)}
    sym: list[float] = []
    anti: list[float] = []
    seen_pairs: set[tuple[str, str]] = set()
    for field in FEATURE_FIELDS:
        if field.role is FeatureRole.SHARED:
            sym.append(by_name[field.name])
            continue
        if field.role is FeatureRole.DIFF:
            anti.append(by_name[field.name])
            continue
        if field.role is FeatureRole.PAIRED:
            if field.pair is None:
                raise JointError(f"paired field {field.name} has no pair")
            pair_key = tuple(sorted((field.name, field.pair)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            a_name, b_name = _paired_ab_names(field.name, field.pair)
            sym.append(by_name[a_name] + by_name[b_name])
            anti.append(by_name[a_name] - by_name[b_name])
            continue
        never_role: Never = field.role
        raise JointError(f"unhandled feature role: {never_role!r}")
    return np.asarray(sym, dtype=np.float64), np.asarray(anti, dtype=np.float64)


def _paired_ab_names(left: str, right: str) -> tuple[str, str]:
    if left.endswith("_a") and right.endswith("_b"):
        return left, right
    if left.endswith("_b") and right.endswith("_a"):
        return right, left
    raise JointError(f"paired fields {left!r} / {right!r} are not _a/_b names")


_ZERO_ROW = tuple(0.0 for _ in FEATURE_NAMES)
N_SYM: Final = int(oriented_features(_ZERO_ROW)[0].size)
N_ANTI: Final = int(oriented_features(_ZERO_ROW)[1].size)
N_SYM_T: Final = N_SYM + MAX_INTERVALS
N_HAZARD_PARAMS: Final = 3 * (N_SYM_T + N_ANTI)
N_DECISION_PARAMS: Final = 1 + N_SYM + N_ANTI


def stable_softmax(logits: np.ndarray, *, axis: int = -1) -> np.ndarray:
    """Finite softmax via log-sum-exp. Non-finite logits fail closed."""
    arr = np.asarray(logits, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise JointNumericalError("logits contain a non-finite value")
    shifted = arr - np.max(arr, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    denom = np.sum(exp, axis=axis, keepdims=True)
    if np.any(denom <= 0.0) or not np.all(np.isfinite(denom)):
        raise JointNumericalError("softmax denominator is not a positive finite mass")
    probs = exp / denom
    if not np.all(np.isfinite(probs)):
        raise JointNumericalError("softmax produced a non-finite probability")
    return probs


def normalize_probability_map(
    values: Mapping[str, float],
    *,
    clip_tolerance: float = PROBABILITY_CLIP_TOLERANCE,
    sum_tolerance: float = ATOM_SUM_ATOL,
) -> dict[str, float]:
    """Clip tiny numeric noise then renormalize. Larger errors fail."""
    clipped: dict[str, float] = {}
    for key, raw in values.items():
        number = float(raw)
        if not math.isfinite(number):
            raise JointNumericalError(f"probability {key!r} is not finite")
        if number < -clip_tolerance or number > 1.0 + clip_tolerance:
            raise JointNumericalError(
                f"probability {key!r}={number} is outside "
                f"[-{clip_tolerance}, 1+{clip_tolerance}]"
            )
        clipped[key] = min(1.0, max(0.0, number))
    total = sum(clipped.values())
    if not math.isfinite(total) or total <= 0.0:
        raise JointNumericalError("probability map has non-positive mass")
    if abs(total - 1.0) > max(clip_tolerance * max(len(clipped), 1), 1e-8):
        raise JointNumericalError(
            f"probability mass {total} exceeds documented clip/renorm tolerance"
        )
    out = {key: value / total for key, value in clipped.items()}
    checksum = sum(out.values())
    if abs(checksum - 1.0) > sum_tolerance:
        raise JointNumericalError(
            f"renormalized probabilities sum to {checksum}, not 1 ± {sum_tolerance}"
        )
    return out


def hazard_class_from_atom(atom: TerminalAtom) -> HazardClass | None:
    if atom is TerminalAtom.A_KO_TKO:
        return HazardClass.A_KO_TKO
    if atom is TerminalAtom.B_KO_TKO:
        return HazardClass.B_KO_TKO
    if atom is TerminalAtom.A_SUBMISSION:
        return HazardClass.A_SUBMISSION
    if atom is TerminalAtom.B_SUBMISSION:
        return HazardClass.B_SUBMISSION
    if atom is TerminalAtom.A_OTHER_STOPPAGE:
        return HazardClass.A_OTHER_STOPPAGE
    if atom is TerminalAtom.B_OTHER_STOPPAGE:
        return HazardClass.B_OTHER_STOPPAGE
    if atom is TerminalAtom.A_DECISION:
        return None
    if atom is TerminalAtom.B_DECISION:
        return None
    if atom is TerminalAtom.DRAW:
        return None
    never_atom: Never = atom
    raise JointError(f"unhandled terminal atom: {never_atom!r}")


def decision_class_from_atom(atom: TerminalAtom) -> DecisionClass | None:
    if atom is TerminalAtom.A_DECISION:
        return DecisionClass.A_DECISION
    if atom is TerminalAtom.B_DECISION:
        return DecisionClass.B_DECISION
    if atom is TerminalAtom.DRAW:
        return DecisionClass.DRAW
    if atom in {
        TerminalAtom.A_KO_TKO,
        TerminalAtom.B_KO_TKO,
        TerminalAtom.A_SUBMISSION,
        TerminalAtom.B_SUBMISSION,
        TerminalAtom.A_OTHER_STOPPAGE,
        TerminalAtom.B_OTHER_STOPPAGE,
    }:
        return None
    never_atom: Never = atom
    raise JointError(f"unhandled terminal atom: {never_atom!r}")


def swap_hazard_class(value: HazardClass) -> HazardClass:
    if value is HazardClass.CONTINUE:
        return HazardClass.CONTINUE
    if value is HazardClass.A_KO_TKO:
        return HazardClass.B_KO_TKO
    if value is HazardClass.B_KO_TKO:
        return HazardClass.A_KO_TKO
    if value is HazardClass.A_SUBMISSION:
        return HazardClass.B_SUBMISSION
    if value is HazardClass.B_SUBMISSION:
        return HazardClass.A_SUBMISSION
    if value is HazardClass.A_OTHER_STOPPAGE:
        return HazardClass.B_OTHER_STOPPAGE
    if value is HazardClass.B_OTHER_STOPPAGE:
        return HazardClass.A_OTHER_STOPPAGE
    never_cls: Never = value
    raise JointError(f"unhandled hazard class: {never_cls!r}")


def swap_decision_class(value: DecisionClass) -> DecisionClass:
    if value is DecisionClass.DRAW:
        return DecisionClass.DRAW
    if value is DecisionClass.A_DECISION:
        return DecisionClass.B_DECISION
    if value is DecisionClass.B_DECISION:
        return DecisionClass.A_DECISION
    never_cls: Never = value
    raise JointError(f"unhandled decision class: {never_cls!r}")


def finish_atom_key_for_hazard(value: HazardClass, interval: int) -> str:
    if value is HazardClass.CONTINUE:
        raise JointError("continue is not a finish atom")
    if value is HazardClass.A_KO_TKO:
        return finish_atom_key(side="a", cause="ko_tko", interval=interval)
    if value is HazardClass.B_KO_TKO:
        return finish_atom_key(side="b", cause="ko_tko", interval=interval)
    if value is HazardClass.A_SUBMISSION:
        return finish_atom_key(side="a", cause="submission", interval=interval)
    if value is HazardClass.B_SUBMISSION:
        return finish_atom_key(side="b", cause="submission", interval=interval)
    if value is HazardClass.A_OTHER_STOPPAGE:
        return finish_atom_key(side="a", cause="other_stoppage", interval=interval)
    if value is HazardClass.B_OTHER_STOPPAGE:
        return finish_atom_key(side="b", cause="other_stoppage", interval=interval)
    never_cls: Never = value
    raise JointError(f"unhandled hazard class: {never_cls!r}")


def survival_multiply(
    hazard_probs: np.ndarray,
    decision_probs: np.ndarray,
    *,
    scheduled_rounds: int,
    clip_tolerance: float = PROBABILITY_CLIP_TOLERANCE,
    inactive_hazard: frozenset[HazardClass] | None = None,
    inactive_decision: frozenset[DecisionClass] | None = None,
) -> dict[str, float]:
    """P(cause at t) = S_t * h_cause,t; remaining survival goes to the decision head."""
    rounds_n = interval_count_for_schedule(scheduled_rounds)
    hazards = np.asarray(hazard_probs, dtype=np.float64)
    decisions = np.asarray(decision_probs, dtype=np.float64).reshape(-1)
    if hazards.shape != (rounds_n, len(HAZARD_CLASSES)):
        raise JointError(
            f"hazard_probs must have shape ({rounds_n}, 7), got {hazards.shape}"
        )
    if decisions.shape != (len(DECISION_CLASSES),):
        raise JointError(f"decision_probs must have shape (3,), got {decisions.shape}")
    skipped_h = inactive_hazard or frozenset()
    skipped_d = inactive_decision or frozenset()
    fine = {key: 0.0 for key in fine_atom_keys(scheduled_rounds)}
    survival = 1.0
    for interval in range(rounds_n):
        row = np.array(hazards[interval], dtype=np.float64, copy=True)
        for cls in skipped_h:
            row[HAZARD_INDEX[cls]] = 0.0
        row = _renormalize_simplex(row, name=f"hazard[{interval}]")
        for cls in FINISH_HAZARD_CLASSES:
            fine[finish_atom_key_for_hazard(cls, interval)] = float(
                survival * row[HAZARD_INDEX[cls]]
            )
        survival = float(survival * row[CONTINUE_INDEX])
        if not math.isfinite(survival) or survival < 0.0:
            raise JointNumericalError("survival path is not a finite non-negative mass")
    dec = np.array(decisions, dtype=np.float64, copy=True)
    for cls in skipped_d:
        dec[DECISION_INDEX[cls]] = 0.0
    dec = _renormalize_simplex(dec, name="decision")
    fine["a_decision"] = float(survival * dec[DECISION_INDEX[DecisionClass.A_DECISION]])
    fine["b_decision"] = float(survival * dec[DECISION_INDEX[DecisionClass.B_DECISION]])
    fine["draw"] = float(survival * dec[DECISION_INDEX[DecisionClass.DRAW]])
    return normalize_probability_map(fine, clip_tolerance=clip_tolerance)


def _renormalize_simplex(values: np.ndarray, *, name: str) -> np.ndarray:
    if not np.all(np.isfinite(values)):
        raise JointNumericalError(f"{name} contains a non-finite probability")
    clipped = np.clip(values, 0.0, 1.0)
    total = float(clipped.sum())
    if total <= 0.0 or not math.isfinite(total):
        raise JointNumericalError(f"{name} simplex has non-positive mass")
    return clipped / total


@dataclass(frozen=True)
class JointModelSpec:
    spec_id: str
    spec_version: str
    model_id: str
    feature_spec_version: str
    penalty: str
    C: float
    max_iter: int
    solver: str
    standardize: bool
    swap_augment: bool
    tied_ab_parameters: bool
    ordinary_allow_holdout: bool
    final_refit: str
    cutoff_policy: str
    missing_classes: MissingClassMode
    early_technical: EarlyTechnicalMode
    class_pooling: Mapping[str, str]
    probability_clip_tolerance: float
    atom_sum_tolerance: float
    content_hash: str


def compute_joint_spec_hash(payload: Mapping[str, Any]) -> str:
    return compute_contract_hash(dict(payload))


def package_joint_spec_path() -> Path:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(JOINT_SPEC_FILENAME)
    with resources.as_file(resource) as path:
        return Path(path)


def visible_joint_spec_path(*, root: Path | None = None) -> Path:
    if root is None:
        root = Path(__file__).resolve().parents[3]
    return root / "config" / "model_specs" / JOINT_SPEC_FILENAME


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise JointSpecError(f"unable to read joint spec at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise JointSpecError("joint spec root must be a mapping")
    return loaded


def _read_package_joint_payload() -> dict[str, Any]:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(JOINT_SPEC_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise JointSpecError(
            f"unable to read packaged joint spec {JOINT_SPEC_FILENAME}"
        ) from exc
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise JointSpecError("packaged joint spec root must be a mapping")
    return loaded


def identify_model_family(path: Path) -> ModelFamilyKind:
    """Dispatch ridge vs joint from spec_id / model_id / model_family."""
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise JointSpecError(f"unable to read model spec at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise JointSpecError("model spec root must be a mapping")
    spec_id = payload.get("spec_id")
    model_id = payload.get("model_id")
    family = payload.get("model_family")
    if spec_id == JOINT_SPEC_ID or model_id == EXPECTED_JOINT_MODEL_ID:
        return "joint"
    if family == EXPECTED_JOINT_FAMILY:
        return "joint"
    if spec_id == "ridge_v1" or model_id == "M1" or family == "ridge_logistic":
        return "ridge"
    raise JointSpecError(
        f"unrecognized model spec {path}: spec_id={spec_id!r} model_id={model_id!r}"
    )


def _require_false_holdout(value: object) -> bool:
    if value is True:
        raise JointSpecError("ordinary_allow_holdout must be false; 2025 stay locked")
    if value is False:
        return False
    raise JointSpecError(f"ordinary_allow_holdout must be boolean false, got {value!r}")


def _parse_missing_mode(value: object) -> MissingClassMode:
    if value == "fail":
        return "fail"
    if value == "pool":
        return "pool"
    raise JointSpecError(f"missing_classes must be 'fail' or 'pool', got {value!r}")


def _parse_early_mode(value: object) -> EarlyTechnicalMode:
    if value == "fail":
        return "fail"
    if value == "pool_other_stoppage":
        return "pool_other_stoppage"
    if value == "pool_as_distance":
        return "pool_as_distance"
    raise JointSpecError(
        "early_technical must be fail, pool_other_stoppage, or pool_as_distance, "
        f"got {value!r}"
    )


def parse_joint_spec(
    payload: Mapping[str, Any],
    *,
    enforce_pinned_digest: bool = True,
) -> JointModelSpec:
    if payload.get("contract_id") != JOINT_CONTRACT_ID:
        raise JointSpecError(
            f"contract_id mismatch: got {payload.get('contract_id')!r}, "
            f"expected {JOINT_CONTRACT_ID!r}"
        )
    if payload.get("schema_version") != EXPECTED_JOINT_SCHEMA_VERSION:
        raise JointSpecError(f"schema_version mismatch: got {payload.get('schema_version')!r}")
    if payload.get("spec_id") != JOINT_SPEC_ID:
        raise JointSpecError(f"spec_id mismatch: got {payload.get('spec_id')!r}")
    if payload.get("spec_version") != EXPECTED_JOINT_SPEC_VERSION:
        raise JointSpecError(f"spec_version mismatch: got {payload.get('spec_version')!r}")
    if payload.get("model_id") != EXPECTED_JOINT_MODEL_ID:
        raise JointSpecError(f"model_id mismatch: got {payload.get('model_id')!r}")
    if payload.get("model_family") != EXPECTED_JOINT_FAMILY:
        raise JointSpecError(f"model_family must be {EXPECTED_JOINT_FAMILY!r}")
    estimator = payload.get("estimator")
    if not isinstance(estimator, Mapping):
        raise JointSpecError("estimator must be a mapping")
    folds = payload.get("folds")
    if not isinstance(folds, Mapping):
        raise JointSpecError("folds must be a mapping")
    if estimator.get("penalty") != "l2":
        raise JointSpecError("estimator.penalty must be l2")
    if folds.get("final_refit") != EXPECTED_FINAL_REFIT:
        raise JointSpecError("final_refit must be development_and_validation")
    if payload.get("cutoff_policy") != EXPECTED_CUTOFF_POLICY:
        raise JointSpecError("cutoff_policy must be scheduled_minus_60m")
    if payload.get("standardize") is not True:
        raise JointSpecError("standardize must be true")
    if payload.get("swap_augment") is not True:
        raise JointSpecError("swap_augment must be true")
    if payload.get("tied_ab_parameters") is not True:
        raise JointSpecError("tied_ab_parameters must be true")
    pooling = payload.get("class_pooling", {})
    if pooling is None:
        pooling = {}
    if not isinstance(pooling, Mapping):
        raise JointSpecError("class_pooling must be a mapping")
    content_hash = compute_joint_spec_hash(payload)
    if enforce_pinned_digest and content_hash != PINNED_JOINT_SPEC_HASH:
        raise JointSpecError(
            f"joint spec hash mismatch: got {content_hash}, expected {PINNED_JOINT_SPEC_HASH}"
        )
    return JointModelSpec(
        spec_id=str(payload["spec_id"]),
        spec_version=str(payload["spec_version"]),
        model_id=str(payload["model_id"]),
        feature_spec_version=str(payload["feature_spec_version"]),
        penalty=str(estimator["penalty"]),
        C=float(estimator["C"]),
        max_iter=int(estimator["max_iter"]),
        solver=str(estimator["solver"]),
        standardize=True,
        swap_augment=True,
        tied_ab_parameters=True,
        ordinary_allow_holdout=_require_false_holdout(folds.get("ordinary_allow_holdout")),
        final_refit=str(folds["final_refit"]),
        cutoff_policy=str(payload["cutoff_policy"]),
        missing_classes=_parse_missing_mode(payload.get("missing_classes", "fail")),
        early_technical=_parse_early_mode(payload.get("early_technical", "fail")),
        class_pooling={str(key): str(value) for key, value in pooling.items()},
        probability_clip_tolerance=float(
            payload.get("probability_clip_tolerance", PROBABILITY_CLIP_TOLERANCE)
        ),
        atom_sum_tolerance=float(payload.get("atom_sum_tolerance", ATOM_SUM_ATOL)),
        content_hash=content_hash,
    )


def load_joint_spec(
    *,
    path: Path | None = None,
    enforce_pinned_digest: bool = True,
) -> JointModelSpec:
    payload = (
        _read_yaml_mapping(Path(path)) if path is not None else _read_package_joint_payload()
    )
    return parse_joint_spec(payload, enforce_pinned_digest=enforce_pinned_digest)


def fit_tied_scaler(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Standardize then tie paired A/B stats and force diff means to 0."""
    if rows.ndim != 2 or rows.shape[1] != len(FEATURE_NAMES):
        raise JointError("scaler rows must be (n, n_features) in FEATURE_NAMES order")
    scaler = StandardScaler(with_mean=True, with_std=True)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        scaler.fit(rows)
    mean = np.asarray(scaler.mean_, dtype=np.float64).copy()
    scale = np.asarray(scaler.scale_, dtype=np.float64).copy()
    scale = np.where(scale < 1e-12, 1.0, scale)
    return _tie_scaler_stats(mean, scale)


def _tie_scaler_stats(mean: np.ndarray, scale: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    tied_mean = mean.copy()
    tied_scale = scale.copy()
    seen: set[tuple[int, int]] = set()
    for idx, field in enumerate(FEATURE_FIELDS):
        if field.role is FeatureRole.DIFF:
            tied_mean[idx] = 0.0
            continue
        if field.role is FeatureRole.SHARED:
            continue
        if field.role is FeatureRole.PAIRED:
            if field.pair is None:
                raise JointError(f"paired field {field.name} has no pair")
            pair_idx = FEATURE_NAMES.index(field.pair)
            key = (min(idx, pair_idx), max(idx, pair_idx))
            if key in seen:
                continue
            seen.add(key)
            pooled_mean = 0.5 * (tied_mean[idx] + tied_mean[pair_idx])
            pooled_scale = 0.5 * (tied_scale[idx] + tied_scale[pair_idx])
            if pooled_scale < 1e-12:
                pooled_scale = 1.0
            tied_mean[idx] = pooled_mean
            tied_mean[pair_idx] = pooled_mean
            tied_scale[idx] = pooled_scale
            tied_scale[pair_idx] = pooled_scale
            continue
        never_role: Never = field.role
        raise JointError(f"unhandled feature role: {never_role!r}")
    return tied_mean, tied_scale


def scale_row(
    values: Sequence[float],
    mean: Sequence[float],
    scale: Sequence[float],
) -> tuple[float, ...]:
    if len(values) != len(FEATURE_NAMES):
        raise JointError("prediction vector length does not match FEATURE_NAMES")
    out: list[float] = []
    for value, center, denom in zip(values, mean, scale, strict=True):
        number = float(value)
        if not math.isfinite(number):
            raise JointNumericalError("feature vector contains a non-finite number")
        scaled = (number - float(center)) / float(denom)
        if not math.isfinite(scaled):
            raise JointNumericalError("scaled feature is not finite")
        out.append(scaled)
    return tuple(out)


@dataclass(frozen=True)
class PersonPeriodRow:
    bout_id: str
    event_id: str
    interval: int
    values: tuple[float, ...]
    feature_bytes: bytes
    hazard_class: HazardClass


@dataclass(frozen=True)
class JointBoutSample:
    sample_id: str
    event_id: str
    fighter_a_id: str
    fighter_b_id: str
    cutoff: datetime
    values: tuple[float, ...]
    names: tuple[str, ...]
    scheduled_rounds: int
    kind: BoutTerminalKind
    terminal_atom: TerminalAtom
    hazard_class: HazardClass | None
    decision_class: DecisionClass | None
    finish_interval: int | None
    method: MethodLabel | None


def _chosen_result_row(
    snapshot: FeatureSnapshot,
    bout_id: str,
    cutoff: datetime,
) -> SnapshotResultVersion | None:
    eligible: list[SnapshotResultVersion] = []
    for row in snapshot.result_versions:
        if row.bout_id != bout_id:
            continue
        if mutable_fact_allowed_at_cutoff(
            effective_at=row.effective_at,
            observed_at=row.observed_at,
            cutoff=cutoff,
        ):
            eligible.append(row)
    if not eligible:
        return None
    return max(eligible, key=lambda row: (row.effective_at, row.observed_at, row.revision))


def _apply_pooling(name: str, pooling: Mapping[str, str], *, seen: set[str] | None = None) -> str:
    trail = seen if seen is not None else set()
    if name in trail:
        raise JointError(f"class_pooling cycle involving {name!r}")
    target = pooling.get(name)
    if target is None:
        return name
    trail.add(name)
    return _apply_pooling(target, pooling, seen=trail)


def _pool_hazard(value: HazardClass, pooling: Mapping[str, str]) -> HazardClass:
    mapped = _apply_pooling(value.value, pooling)
    try:
        return HazardClass(mapped)
    except ValueError as exc:
        raise JointError(f"class_pooling target {mapped!r} is not a hazard class") from exc


def _pool_decision(value: DecisionClass, pooling: Mapping[str, str]) -> DecisionClass:
    mapped = _apply_pooling(value.value, pooling)
    try:
        return DecisionClass(mapped)
    except ValueError as exc:
        raise JointError(f"class_pooling target {mapped!r} is not a decision class") from exc


def _handle_early_technical(
    label: OutcomeLabel,
    *,
    interval: int,
    last_interval: int,
    mode: EarlyTechnicalMode,
    bout_id: str,
) -> tuple[BoutTerminalKind, HazardClass | None, DecisionClass | None, int | None]:
    if interval >= last_interval:
        decision = decision_class_from_atom(label.terminal_atom) if label.terminal_atom else None
        if decision is None:
            raise EarlyTechnicalOutcomeError(
                f"{bout_id}: technical outcome at last interval is not a decision/draw atom"
            )
        return BoutTerminalKind.DISTANCE, None, decision, None
    if mode == "fail":
        raise EarlyTechnicalOutcomeError(
            f"{bout_id}: early technical outcome at interval {interval} "
            "(set early_technical=pool_other_stoppage or pool_as_distance)"
        )
    if mode == "pool_as_distance":
        decision = decision_class_from_atom(label.terminal_atom) if label.terminal_atom else None
        if decision is None:
            if label.method is MethodLabel.TECHNICAL_DRAW:
                decision = DecisionClass.DRAW
            else:
                raise EarlyTechnicalOutcomeError(
                    f"{bout_id}: pool_as_distance needs a decision/draw atom"
                )
        return BoutTerminalKind.DISTANCE, None, decision, None
    if mode == "pool_other_stoppage":
        if label.method is MethodLabel.TECHNICAL_DRAW:
            raise EarlyTechnicalOutcomeError(
                f"{bout_id}: early technical draw has no winner; "
                "pool_other_stoppage is not defined (use fail or pool_as_distance)"
            )
        if label.winner_side is WinnerSide.A:
            return BoutTerminalKind.FINISH, HazardClass.A_OTHER_STOPPAGE, None, interval
        if label.winner_side is WinnerSide.B:
            return BoutTerminalKind.FINISH, HazardClass.B_OTHER_STOPPAGE, None, interval
        raise EarlyTechnicalOutcomeError(
            f"{bout_id}: early technical decision missing winner"
        )
    never_mode: Never = mode
    raise JointError(f"unhandled early_technical mode: {never_mode!r}")


def _classify_sample(
    label: OutcomeLabel,
    *,
    duration_interval: int | None,
    scheduled_rounds: int,
    early_mode: EarlyTechnicalMode,
    bout_id: str,
) -> tuple[BoutTerminalKind, HazardClass | None, DecisionClass | None, int | None]:
    atom = label.terminal_atom
    if atom is None:
        raise JointError(f"{bout_id}: bout is not terminal-labeled")
    last = interval_count_for_schedule(scheduled_rounds) - 1
    method = label.method
    if method is MethodLabel.TECHNICAL_DECISION or method is MethodLabel.TECHNICAL_DRAW:
        if duration_interval is None:
            raise EarlyTechnicalOutcomeError(
                f"{bout_id}: technical decision/draw missing duration; "
                "refusing to silently treat as full distance"
            )
        return _handle_early_technical(
            label,
            interval=duration_interval,
            last_interval=last,
            mode=early_mode,
            bout_id=bout_id,
        )
    finish = hazard_class_from_atom(atom)
    if finish is not None:
        if duration_interval is None:
            raise JointError(f"{bout_id}: finish bout missing a valid half-round interval")
        return BoutTerminalKind.FINISH, finish, None, duration_interval
    decision = decision_class_from_atom(atom)
    if decision is None:
        raise JointError(f"{bout_id}: terminal atom {atom.value} is not a modeled class")
    if duration_interval is not None and duration_interval < last:
        raise JointError(
            f"{bout_id}: ordinary decision/draw ended at interval {duration_interval}, "
            f"before last interval {last}; refusing to invent full distance"
        )
    return BoutTerminalKind.DISTANCE, None, decision, None


def expand_person_period(sample: JointBoutSample) -> tuple[PersonPeriodRow, ...]:
    """One pre-bout feature vector copied onto every interval of the bout."""
    rounds_n = interval_count_for_schedule(sample.scheduled_rounds)
    blob = row_bytes(sample.values)
    rows: list[PersonPeriodRow] = []
    if sample.kind is BoutTerminalKind.FINISH:
        if sample.hazard_class is None or sample.finish_interval is None:
            raise JointError(f"{sample.sample_id}: finish sample missing cause/interval")
        if sample.finish_interval < 0 or sample.finish_interval >= rounds_n:
            raise JointError(
                f"{sample.sample_id}: finish interval {sample.finish_interval} "
                f"outside 0..{rounds_n - 1}"
            )
        for interval in range(sample.finish_interval):
            rows.append(
                PersonPeriodRow(
                    bout_id=sample.sample_id,
                    event_id=sample.event_id,
                    interval=interval,
                    values=sample.values,
                    feature_bytes=blob,
                    hazard_class=HazardClass.CONTINUE,
                )
            )
        rows.append(
            PersonPeriodRow(
                bout_id=sample.sample_id,
                event_id=sample.event_id,
                interval=sample.finish_interval,
                values=sample.values,
                feature_bytes=blob,
                hazard_class=sample.hazard_class,
            )
        )
        return tuple(rows)
    if sample.kind is BoutTerminalKind.DISTANCE:
        for interval in range(rounds_n):
            rows.append(
                PersonPeriodRow(
                    bout_id=sample.sample_id,
                    event_id=sample.event_id,
                    interval=interval,
                    values=sample.values,
                    feature_bytes=blob,
                    hazard_class=HazardClass.CONTINUE,
                )
            )
        return tuple(rows)
    never_kind: Never = sample.kind
    raise JointError(f"unhandled bout terminal kind: {never_kind!r}")


def joint_samples_from_snapshot(
    snapshot: FeatureSnapshot,
    cards: Sequence[SplitCard],
    spec: JointModelSpec,
    *,
    allowed_roles: frozenset[FoldRole] | None = None,
    allow_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> tuple[JointBoutSample, ...]:
    """Build PIT features after dropping locked holdout cards."""
    roles = allowed_roles if allowed_roles is not None else ORDINARY_TRAIN_ROLES
    if FoldRole.HOLDOUT in roles and not allow_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; ordinary labeling must not read holdout cards"
        )
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    eligible = [
        card
        for card in cards
        if card.event_id in groups and groups[card.event_id].role in roles
    ]
    builder = FeatureBuilder(snapshot)
    samples: list[JointBoutSample] = []
    for card in eligible:
        cutoff = cutoff_for_event(card)
        label_at = implied_event_start(cutoff) + LABEL_LAG
        for bout_id in card.bout_ids:
            bout = snapshot.bout_by_id(bout_id)
            if bout is None:
                continue
            sample = _sample_from_bout(
                snapshot,
                builder,
                card,
                bout,
                cutoff=cutoff,
                label_at=label_at,
                spec=spec,
            )
            if sample is not None:
                samples.append(sample)
    return tuple(samples)


def _sample_from_bout(
    snapshot: FeatureSnapshot,
    builder: FeatureBuilder,
    card: SplitCard,
    bout: SnapshotBout,
    *,
    cutoff: AsOfCutoff,
    label_at: datetime,
    spec: JointModelSpec,
) -> JointBoutSample | None:
    versions = [
        to_label_version(row) for row in snapshot.result_versions if row.bout_id == bout.bout_id
    ]
    label = training_label(versions, label_at)
    if label.terminal_atom is None:
        return None
    scheduled = bout.scheduled_rounds
    if scheduled not in SUPPORTED_SCHEDULED_ROUNDS:
        raise UnsupportedScheduleError(
            f"{bout.bout_id}: unsupported scheduled_rounds {scheduled!r}; only 3 or 5"
        )
    chosen = _chosen_result_row(snapshot, bout.bout_id, label_at)
    duration_interval: int | None = None
    if chosen is not None:
        duration = half_round_duration(
            ending_round=chosen.ending_round,
            time_str=chosen.time_str,
            scheduled_rounds=int(scheduled),
        )
        if duration.status is DurationStatus.VALID:
            duration_interval = duration.interval_index
    kind, hazard, decision, finish_interval = _classify_sample(
        label,
        duration_interval=duration_interval,
        scheduled_rounds=int(scheduled),
        early_mode=spec.early_technical,
        bout_id=bout.bout_id,
    )
    if spec.class_pooling:
        if hazard is not None:
            hazard = _pool_hazard(hazard, spec.class_pooling)
        if decision is not None:
            decision = _pool_decision(decision, spec.class_pooling)
    row = builder.build(
        bout.fighter_a_id,
        bout.fighter_b_id,
        cutoff,
        bout_id=bout.bout_id,
    )
    return JointBoutSample(
        sample_id=bout.bout_id,
        event_id=card.event_id,
        fighter_a_id=bout.fighter_a_id,
        fighter_b_id=bout.fighter_b_id,
        cutoff=cutoff.cutoff,
        values=row.values,
        names=row.names,
        scheduled_rounds=int(scheduled),
        kind=kind,
        terminal_atom=label.terminal_atom,
        hazard_class=hazard,
        decision_class=decision,
        finish_interval=finish_interval,
        method=label.method,
    )


def observed_hazard_classes(rows: Sequence[PersonPeriodRow]) -> set[HazardClass]:
    return {row.hazard_class for row in rows}


def observed_decision_classes(samples: Sequence[JointBoutSample]) -> set[DecisionClass]:
    return {sample.decision_class for sample in samples if sample.decision_class is not None}


def require_joint_classes(
    *,
    hazard: set[HazardClass],
    decision: set[DecisionClass],
    spec: JointModelSpec,
) -> tuple[frozenset[HazardClass], frozenset[DecisionClass]]:
    """Fail on missing required classes unless the spec declares pooling."""
    missing = [item.value for item in HAZARD_CLASSES if item not in hazard]
    missing.extend(item.value for item in DECISION_CLASSES if item not in decision)
    inactive_h = frozenset(item for item in HAZARD_CLASSES if item not in hazard)
    inactive_d = frozenset(item for item in DECISION_CLASSES if item not in decision)
    if not missing:
        return frozenset(), frozenset()
    if spec.missing_classes == "fail":
        raise MissingJointClassError(missing)
    if spec.missing_classes == "pool":
        undeclared = [
            name for name in missing if _apply_pooling(name, spec.class_pooling) == name
        ]
        if undeclared:
            raise MissingJointClassError(undeclared)
        return inactive_h - {HazardClass.CONTINUE}, inactive_d
    never_mode: Never = spec.missing_classes
    raise JointError(f"unhandled missing_classes mode: {never_mode!r}")


def _hazard_logits_from_oriented(
    z_sym: np.ndarray,
    z_anti: np.ndarray,
    interval: int,
    theta: np.ndarray,
) -> np.ndarray:
    if interval < 0 or interval >= MAX_INTERVALS:
        raise JointError(f"interval {interval} outside 0..{MAX_INTERVALS - 1}")
    onehot = np.zeros(MAX_INTERVALS, dtype=np.float64)
    onehot[interval] = 1.0
    z_t = np.concatenate([z_sym, onehot])
    logits = np.zeros(len(HAZARD_CLASSES), dtype=np.float64)
    offset = 0
    for cause_a, cause_b in (
        (HazardClass.A_KO_TKO, HazardClass.B_KO_TKO),
        (HazardClass.A_SUBMISSION, HazardClass.B_SUBMISSION),
        (HazardClass.A_OTHER_STOPPAGE, HazardClass.B_OTHER_STOPPAGE),
    ):
        beta = theta[offset : offset + N_SYM_T]
        gamma = theta[offset + N_SYM_T : offset + N_SYM_T + N_ANTI]
        offset += N_SYM_T + N_ANTI
        logits[HAZARD_INDEX[cause_a]] = float(z_t @ beta + z_anti @ gamma)
        logits[HAZARD_INDEX[cause_b]] = float(z_t @ beta - z_anti @ gamma)
    return logits


def _decision_logits_from_oriented(
    z_sym: np.ndarray,
    z_anti: np.ndarray,
    theta: np.ndarray,
) -> np.ndarray:
    bias = float(theta[0])
    beta = theta[1 : 1 + N_SYM]
    gamma = theta[1 + N_SYM :]
    logits = np.zeros(len(DECISION_CLASSES), dtype=np.float64)
    logits[DECISION_INDEX[DecisionClass.A_DECISION]] = bias + float(z_sym @ beta + z_anti @ gamma)
    logits[DECISION_INDEX[DecisionClass.B_DECISION]] = bias + float(z_sym @ beta - z_anti @ gamma)
    return logits


def _fit_tied_multinomial(
    *,
    logits_fn,
    n_params: int,
    y: np.ndarray,
    C: float,
    max_iter: int,
    args: tuple[Any, ...],
) -> np.ndarray:
    def loss_and_grad(theta: np.ndarray) -> tuple[float, np.ndarray]:
        logits = logits_fn(theta, *args)
        probs = stable_softmax(logits, axis=1)
        nll = 0.0
        for idx, label in enumerate(y):
            p = float(probs[idx, int(label)])
            if p <= 0.0:
                nll += 50.0
            else:
                nll += -math.log(p)
        reg = 0.5 * float(theta @ theta)
        loss = float(C) * nll + reg
        grad_logits = probs.copy()
        grad_logits[np.arange(len(y)), y] -= 1.0
        grad = float(C) * _theta_gradient(logits_fn, theta, grad_logits, args)
        grad = grad + theta
        return loss, grad

    start = np.zeros(n_params, dtype=np.float64)
    result = minimize(
        loss_and_grad,
        start,
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": int(max_iter)},
    )
    if not np.all(np.isfinite(result.x)):
        raise JointNumericalError("tied multinomial fit produced non-finite weights")
    return np.asarray(result.x, dtype=np.float64)


def _theta_gradient(
    logits_fn,
    theta: np.ndarray,
    grad_logits: np.ndarray,
    args: tuple[Any, ...],
) -> np.ndarray:
    """Finite-difference-free gradient via a second forward with analytic chain.

    Hazard/decision logits are linear in theta, so we rebuild the design.
    """
    kind = args[0]
    if kind == "hazard":
        z_sym, z_anti, intervals = args[1], args[2], args[3]
        return _hazard_theta_grad(theta, grad_logits, z_sym, z_anti, intervals)
    if kind == "decision":
        z_sym, z_anti = args[1], args[2]
        return _decision_theta_grad(grad_logits, z_sym, z_anti)
    raise JointError(f"unhandled logits kind {kind!r}")


def _hazard_batch_logits(
    theta: np.ndarray,
    kind: str,
    z_sym: np.ndarray,
    z_anti: np.ndarray,
    intervals: np.ndarray,
) -> np.ndarray:
    del kind
    n = z_sym.shape[0]
    out = np.zeros((n, len(HAZARD_CLASSES)), dtype=np.float64)
    for idx in range(n):
        out[idx] = _hazard_logits_from_oriented(
            z_sym[idx], z_anti[idx], int(intervals[idx]), theta
        )
    return out


def _decision_batch_logits(
    theta: np.ndarray,
    kind: str,
    z_sym: np.ndarray,
    z_anti: np.ndarray,
) -> np.ndarray:
    del kind
    n = z_sym.shape[0]
    out = np.zeros((n, len(DECISION_CLASSES)), dtype=np.float64)
    for idx in range(n):
        out[idx] = _decision_logits_from_oriented(z_sym[idx], z_anti[idx], theta)
    return out


def _hazard_theta_grad(
    theta: np.ndarray,
    grad_logits: np.ndarray,
    z_sym: np.ndarray,
    z_anti: np.ndarray,
    intervals: np.ndarray,
) -> np.ndarray:
    del theta
    n = z_sym.shape[0]
    grad = np.zeros(N_HAZARD_PARAMS, dtype=np.float64)
    offset = 0
    pairs = (
        (HazardClass.A_KO_TKO, HazardClass.B_KO_TKO),
        (HazardClass.A_SUBMISSION, HazardClass.B_SUBMISSION),
        (HazardClass.A_OTHER_STOPPAGE, HazardClass.B_OTHER_STOPPAGE),
    )
    for cause_a, cause_b in pairs:
        g_beta = np.zeros(N_SYM_T, dtype=np.float64)
        g_gamma = np.zeros(N_ANTI, dtype=np.float64)
        ia = HAZARD_INDEX[cause_a]
        ib = HAZARD_INDEX[cause_b]
        for idx in range(n):
            onehot = np.zeros(MAX_INTERVALS, dtype=np.float64)
            onehot[int(intervals[idx])] = 1.0
            z_t = np.concatenate([z_sym[idx], onehot])
            g_a = float(grad_logits[idx, ia])
            g_b = float(grad_logits[idx, ib])
            g_beta += (g_a + g_b) * z_t
            g_gamma += (g_a - g_b) * z_anti[idx]
        grad[offset : offset + N_SYM_T] = g_beta
        grad[offset + N_SYM_T : offset + N_SYM_T + N_ANTI] = g_gamma
        offset += N_SYM_T + N_ANTI
    return grad


def _decision_theta_grad(
    grad_logits: np.ndarray,
    z_sym: np.ndarray,
    z_anti: np.ndarray,
) -> np.ndarray:
    n = z_sym.shape[0]
    g_bias = 0.0
    g_beta = np.zeros(N_SYM, dtype=np.float64)
    g_gamma = np.zeros(N_ANTI, dtype=np.float64)
    ia = DECISION_INDEX[DecisionClass.A_DECISION]
    ib = DECISION_INDEX[DecisionClass.B_DECISION]
    for idx in range(n):
        g_a = float(grad_logits[idx, ia])
        g_b = float(grad_logits[idx, ib])
        g_bias += g_a + g_b
        g_beta += (g_a + g_b) * z_sym[idx]
        g_gamma += (g_a - g_b) * z_anti[idx]
    return np.concatenate([np.asarray([g_bias]), g_beta, g_gamma])


@dataclass(frozen=True)
class JointPredictor:
    """Tied competing-risks predictor. Raw forward pass is swap-equivariant."""

    feature_names: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    hazard_theta: tuple[float, ...]
    decision_theta: tuple[float, ...]
    spec_hash: str
    spec_version: str
    clip_tolerance: float
    inactive_hazard: tuple[str, ...]
    inactive_decision: tuple[str, ...]

    def _scaled(self, values: Sequence[float]) -> tuple[float, ...]:
        if tuple(self.feature_names) != FEATURE_NAMES:
            raise ArtifactFeatureOrderMismatchError("joint predictor feature order mismatch")
        return scale_row(values, self.scaler_mean, self.scaler_scale)

    def raw_hazard_logits(self, values: Sequence[float], interval: int) -> np.ndarray:
        scaled = self._scaled(values)
        z_sym, z_anti = oriented_features(scaled)
        return _hazard_logits_from_oriented(
            z_sym, z_anti, interval, np.asarray(self.hazard_theta, dtype=np.float64)
        )

    def raw_decision_logits(self, values: Sequence[float]) -> np.ndarray:
        scaled = self._scaled(values)
        z_sym, z_anti = oriented_features(scaled)
        return _decision_logits_from_oriented(
            z_sym, z_anti, np.asarray(self.decision_theta, dtype=np.float64)
        )

    def predict_fine(self, values: Sequence[float], *, scheduled_rounds: int) -> dict[str, float]:
        rounds_n = interval_count_for_schedule(scheduled_rounds)
        hazards = np.zeros((rounds_n, len(HAZARD_CLASSES)), dtype=np.float64)
        for interval in range(rounds_n):
            hazards[interval] = stable_softmax(self.raw_hazard_logits(values, interval))
        decisions = stable_softmax(self.raw_decision_logits(values))
        inactive_h = frozenset(HazardClass(item) for item in self.inactive_hazard)
        inactive_d = frozenset(DecisionClass(item) for item in self.inactive_decision)
        return survival_multiply(
            hazards,
            decisions,
            scheduled_rounds=scheduled_rounds,
            clip_tolerance=self.clip_tolerance,
            inactive_hazard=inactive_h,
            inactive_decision=inactive_d,
        )

    def predict_frozen(
        self, values: Sequence[float], *, scheduled_rounds: int
    ) -> dict[TerminalAtom, float]:
        return aggregate_frozen_atoms(self.predict_fine(values, scheduled_rounds=scheduled_rounds))

    def predict_markets(
        self, values: Sequence[float], *, scheduled_rounds: int
    ) -> DerivedMarketProbabilities:
        return derive_markets(
            self.predict_fine(values, scheduled_rounds=scheduled_rounds),
            scheduled_rounds=scheduled_rounds,
        )

    def oof_payload(
        self,
        sample: JointBoutSample,
        *,
        fold_id: str,
        fold_kind: str,
    ) -> dict[str, Any]:
        rounds_n = interval_count_for_schedule(sample.scheduled_rounds)
        hazard_logits = [
            [float(x) for x in self.raw_hazard_logits(sample.values, interval)]
            for interval in range(rounds_n)
        ]
        decision_logits = [float(x) for x in self.raw_decision_logits(sample.values)]
        fine = self.predict_fine(sample.values, scheduled_rounds=sample.scheduled_rounds)
        frozen = {atom.value: value for atom, value in self.predict_frozen(
            sample.values, scheduled_rounds=sample.scheduled_rounds
        ).items()}
        return {
            "bout_id": sample.sample_id,
            "decision_logits": decision_logits,
            "event_id": sample.event_id,
            "fine_probabilities": fine,
            "fold_id": fold_id,
            "fold_kind": fold_kind,
            "frozen_probabilities": frozen,
            "hazard_logits": hazard_logits,
            "scheduled_rounds": sample.scheduled_rounds,
        }


def _oriented_stack(rows: Sequence[Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    syms: list[np.ndarray] = []
    antis: list[np.ndarray] = []
    for row in rows:
        z_sym, z_anti = oriented_features(row)
        syms.append(z_sym)
        antis.append(z_anti)
    return np.vstack(syms), np.vstack(antis)


def _augment_hazard(
    scaled_rows: list[tuple[float, ...]],
    intervals: list[int],
    labels: list[int],
    *,
    swap_augment: bool,
) -> tuple[list[tuple[float, ...]], list[int], list[int]]:
    if not swap_augment:
        return scaled_rows, intervals, labels
    out_rows = list(scaled_rows)
    out_intervals = list(intervals)
    out_labels = list(labels)
    for row, interval, label in zip(scaled_rows, intervals, labels, strict=True):
        out_rows.append(swap_values(row))
        out_intervals.append(interval)
        cls = HAZARD_CLASSES[label]
        out_labels.append(HAZARD_INDEX[swap_hazard_class(cls)])
    return out_rows, out_intervals, out_labels


def _augment_decision(
    scaled_rows: list[tuple[float, ...]],
    labels: list[int],
    *,
    swap_augment: bool,
) -> tuple[list[tuple[float, ...]], list[int]]:
    if not swap_augment:
        return scaled_rows, labels
    out_rows = list(scaled_rows)
    out_labels = list(labels)
    for row, label in zip(scaled_rows, labels, strict=True):
        out_rows.append(swap_values(row))
        cls = DECISION_CLASSES[label]
        out_labels.append(DECISION_INDEX[swap_decision_class(cls)])
    return out_rows, out_labels


def fit_joint_predictor(
    samples: Sequence[JointBoutSample],
    spec: JointModelSpec,
) -> JointPredictor:
    if not samples:
        raise TrainError("joint fit needs at least one labeled bout")
    if spec.ordinary_allow_holdout:
        raise HoldoutLockedError("joint spec must not enable ordinary holdout")
    period_rows: list[PersonPeriodRow] = []
    for sample in samples:
        period_rows.extend(expand_person_period(sample))
    if not period_rows:
        raise TrainError("person-period expansion produced no rows")
    hazard_seen = observed_hazard_classes(period_rows)
    decision_seen = observed_decision_classes(samples)
    inactive_h, inactive_d = require_joint_classes(
        hazard=hazard_seen, decision=decision_seen, spec=spec
    )
    bout_matrix = np.asarray([list(sample.values) for sample in samples], dtype=np.float64)
    mean, scale = fit_tied_scaler(bout_matrix)
    scaled_period = [
        scale_row(row.values, mean, scale) for row in period_rows
    ]
    intervals = [row.interval for row in period_rows]
    hazard_y = [HAZARD_INDEX[row.hazard_class] for row in period_rows]
    scaled_period, intervals, hazard_y = _augment_hazard(
        scaled_period, intervals, hazard_y, swap_augment=spec.swap_augment
    )
    z_sym, z_anti = _oriented_stack(scaled_period)
    hazard_theta = _fit_tied_multinomial(
        logits_fn=_hazard_batch_logits,
        n_params=N_HAZARD_PARAMS,
        y=np.asarray(hazard_y, dtype=np.int64),
        C=spec.C,
        max_iter=spec.max_iter,
        args=("hazard", z_sym, z_anti, np.asarray(intervals, dtype=np.int64)),
    )
    decision_samples = [sample for sample in samples if sample.decision_class is not None]
    if not decision_samples:
        raise MissingJointClassError([item.value for item in DECISION_CLASSES])
    scaled_dec = [scale_row(sample.values, mean, scale) for sample in decision_samples]
    decision_y = [DECISION_INDEX[sample.decision_class] for sample in decision_samples]
    scaled_dec, decision_y = _augment_decision(
        scaled_dec, decision_y, swap_augment=spec.swap_augment
    )
    d_sym, d_anti = _oriented_stack(scaled_dec)
    decision_theta = _fit_tied_multinomial(
        logits_fn=_decision_batch_logits,
        n_params=N_DECISION_PARAMS,
        y=np.asarray(decision_y, dtype=np.int64),
        C=spec.C,
        max_iter=spec.max_iter,
        args=("decision", d_sym, d_anti),
    )
    return JointPredictor(
        feature_names=FEATURE_NAMES,
        scaler_mean=tuple(float(x) for x in mean),
        scaler_scale=tuple(float(x) for x in scale),
        hazard_theta=tuple(float(x) for x in hazard_theta),
        decision_theta=tuple(float(x) for x in decision_theta),
        spec_hash=spec_hash(),
        spec_version=SPEC_VERSION,
        clip_tolerance=spec.probability_clip_tolerance,
        inactive_hazard=tuple(item.value for item in sorted(inactive_h, key=lambda x: x.value)),
        inactive_decision=tuple(item.value for item in sorted(inactive_d, key=lambda x: x.value)),
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> bytes:
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(blob)
    return blob


def _require_finite_tuple(value: object, *, n: int, field: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != n:
        raise UntrustedArtifactError(f"{field} must be a list of {n} finite numbers")
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise UntrustedArtifactError(f"{field} contains a non-numeric entry")
        number = float(item)
        if not math.isfinite(number):
            raise UntrustedArtifactError(f"{field} contains a non-finite number")
        out.append(number)
    return tuple(out)


def predictor_to_payload(
    predictor: JointPredictor,
    *,
    train_sample_ids: Sequence[str],
    max_train_timestamp: datetime | None,
    cutoff_policy: str,
    metrics: Mapping[str, Any],
    oof_predictions: Sequence[Mapping[str, Any]],
    contract_hash: str,
    config_hash: str,
    splits_config_hash: str,
    data_hash: str,
    code_hash: str,
    code_commit: str,
    code_commit_reason: str,
    model_id: str,
    spec_id: str,
    spec_version: str,
) -> dict[str, Any]:
    return {
        "code_commit": code_commit,
        "code_commit_reason": code_commit_reason,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "contract_hash": contract_hash,
        "cutoff_policy": cutoff_policy,
        "data_hash": data_hash,
        "decision_theta": list(predictor.decision_theta),
        "estimator_kind": ESTIMATOR_KIND,
        "feature_names": list(predictor.feature_names),
        "feature_spec_hash": predictor.spec_hash,
        "feature_spec_version": predictor.spec_version,
        "hazard_theta": list(predictor.hazard_theta),
        "inactive_decision": list(predictor.inactive_decision),
        "inactive_hazard": list(predictor.inactive_hazard),
        "max_train_timestamp": (
            max_train_timestamp.isoformat() if max_train_timestamp is not None else None
        ),
        "metrics": dict(metrics),
        "model_id": model_id,
        "oof_predictions": [dict(item) for item in oof_predictions],
        "payload_kind": PAYLOAD_KIND,
        "probability_clip_tolerance": predictor.clip_tolerance,
        "scaler_mean": list(predictor.scaler_mean),
        "scaler_scale": list(predictor.scaler_scale),
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "spec_id": spec_id,
        "spec_version": spec_version,
        "splits_config_hash": splits_config_hash,
        "train_sample_ids": list(train_sample_ids),
    }


def save_joint_artifact(
    predictor: JointPredictor,
    output_path: Path,
    *,
    train_sample_ids: Sequence[str],
    max_train_timestamp: datetime | None,
    cutoff_policy: str,
    metrics: Mapping[str, Any],
    oof_predictions: Sequence[Mapping[str, Any]],
    contract_hash: str,
    config_hash: str,
    splits_config_hash: str,
    data_hash: str,
    code_hash: str,
    code_commit: str,
    code_commit_reason: str,
    model_id: str,
    spec_id: str,
    spec_version: str,
) -> SavedArtifact:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = predictor_to_payload(
        predictor,
        train_sample_ids=train_sample_ids,
        max_train_timestamp=max_train_timestamp,
        cutoff_policy=cutoff_policy,
        metrics=metrics,
        oof_predictions=oof_predictions,
        contract_hash=contract_hash,
        config_hash=config_hash,
        splits_config_hash=splits_config_hash,
        data_hash=data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
        model_id=model_id,
        spec_id=spec_id,
        spec_version=spec_version,
    )
    blob = _write_json(target, payload)
    digest = sha256_bytes(blob)
    manifest = ArtifactManifest(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        model_id=model_id,
        spec_id=spec_id,
        spec_version=spec_version,
        feature_spec_hash=predictor.spec_hash,
        contract_hash=contract_hash,
        config_hash=config_hash,
        splits_config_hash=splits_config_hash,
        data_hash=data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
        feature_names=predictor.feature_names,
        train_sample_ids=tuple(train_sample_ids),
        max_train_timestamp=(
            max_train_timestamp.isoformat() if max_train_timestamp is not None else None
        ),
        cutoff_policy=cutoff_policy,
        metrics=dict(metrics),
        payload_sha256=digest,
    )
    side = manifest_path_for(target)
    _write_json(side, manifest.to_dict())
    return SavedArtifact(
        payload_path=target,
        manifest_path=side,
        manifest=manifest,
        payload_sha256=digest,
    )


@dataclass(frozen=True)
class LoadedJointArtifact:
    payload: dict[str, Any]
    predictor: JointPredictor
    manifest: ArtifactManifest
    payload_path: Path
    manifest_path: Path
    oof_predictions: tuple[dict[str, Any], ...]


def load_joint_artifact(payload_path: Path) -> LoadedJointArtifact:
    """Load a JSON joint artifact. Never joblib/pickle."""
    target = Path(payload_path)
    side = manifest_path_for(target)
    if not side.is_file():
        raise UntrustedArtifactError(
            f"refusing untrusted artifact {target}; missing sidecar {side.name}"
        )
    try:
        raw_manifest = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UntrustedArtifactError(f"unable to read artifact manifest {side}: {exc}") from exc
    if not isinstance(raw_manifest, dict):
        raise UntrustedArtifactError("artifact manifest root must be an object")
    manifest = manifest_from_mapping(raw_manifest)
    try:
        blob = target.read_bytes()
    except OSError as exc:
        raise UntrustedArtifactError(f"unable to read artifact payload {target}: {exc}") from exc
    digest = sha256_bytes(blob)
    if digest != manifest.payload_sha256:
        raise ArtifactChecksumMismatchError("joint payload checksum mismatch")
    try:
        loaded = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UntrustedArtifactError(
            f"artifact payload is not valid JSON (refusing execution): {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise UntrustedArtifactError("artifact payload must be a JSON object")
    if loaded.get("payload_kind") != PAYLOAD_KIND:
        raise UntrustedArtifactError(
            f"payload_kind must be {PAYLOAD_KIND!r}, got {loaded.get('payload_kind')!r}"
        )
    if loaded.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise UntrustedArtifactError("joint artifact schema_version mismatch")
    if tuple(loaded.get("feature_names") or ()) != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError("joint artifact feature order mismatch")
    if str(loaded.get("feature_spec_hash", "")) != spec_hash():
        raise ArtifactSpecMismatchError("joint artifact feature spec hash mismatch")
    if str(loaded.get("config_hash", "")) != PINNED_JOINT_SPEC_HASH:
        raise ArtifactConfigMismatchError("joint artifact config hash mismatch")
    if str(loaded.get("contract_hash", "")) != PINNED_CONTRACT_HASH:
        raise ArtifactConfigMismatchError("joint artifact contract hash mismatch")
    predictor = JointPredictor(
        feature_names=FEATURE_NAMES,
        scaler_mean=_require_finite_tuple(
            loaded.get("scaler_mean"), n=len(FEATURE_NAMES), field="scaler_mean"
        ),
        scaler_scale=_require_finite_tuple(
            loaded.get("scaler_scale"), n=len(FEATURE_NAMES), field="scaler_scale"
        ),
        hazard_theta=_require_finite_tuple(
            loaded.get("hazard_theta"), n=N_HAZARD_PARAMS, field="hazard_theta"
        ),
        decision_theta=_require_finite_tuple(
            loaded.get("decision_theta"), n=N_DECISION_PARAMS, field="decision_theta"
        ),
        spec_hash=str(loaded.get("feature_spec_hash", "")),
        spec_version=str(loaded.get("feature_spec_version", SPEC_VERSION)),
        clip_tolerance=float(loaded.get("probability_clip_tolerance", PROBABILITY_CLIP_TOLERANCE)),
        inactive_hazard=tuple(str(item) for item in loaded.get("inactive_hazard") or ()),
        inactive_decision=tuple(str(item) for item in loaded.get("inactive_decision") or ()),
    )
    oof = loaded.get("oof_predictions", [])
    if not isinstance(oof, list):
        raise UntrustedArtifactError("oof_predictions must be a list")
    return LoadedJointArtifact(
        payload=loaded,
        predictor=predictor,
        manifest=manifest,
        payload_path=target,
        manifest_path=side,
        oof_predictions=tuple(dict(item) for item in oof if isinstance(item, dict)),
    )


def _samples_for_events(
    samples: Sequence[JointBoutSample],
    event_ids: Sequence[str],
) -> tuple[JointBoutSample, ...]:
    wanted = set(event_ids)
    return tuple(sample for sample in samples if sample.event_id in wanted)


def _samples_for_ids(
    samples: Sequence[JointBoutSample],
    bout_ids: Sequence[str],
) -> tuple[JointBoutSample, ...]:
    wanted = set(bout_ids)
    return tuple(sample for sample in samples if sample.sample_id in wanted)


def _final_refit_samples(
    samples: Sequence[JointBoutSample],
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> tuple[JointBoutSample, ...]:
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    eligible: list[JointBoutSample] = []
    for sample in samples:
        group = groups.get(sample.event_id)
        if group is None:
            continue
        if group.role is FoldRole.DEVELOPMENT:
            eligible.append(sample)
            continue
        if group.role is FoldRole.VALIDATION:
            eligible.append(sample)
            continue
        if group.role is FoldRole.HOLDOUT:
            continue
        never_role: Never = group.role
        raise SplitError(f"unhandled fold role: {never_role!r}")
    if not eligible:
        raise TrainError("no development/validation labeled bouts for joint final refit")
    return tuple(eligible)


def _assert_no_holdout_betting_keys(payload: Mapping[str, Any]) -> None:
    stack: list[object] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in FORBIDDEN_HOLDOUT_METRIC_FRAGMENTS):
                    raise TrainError(
                        f"refusing holdout betting-evidence key {key!r} in ordinary train report"
                    )
                stack.append(value)
            continue
        if isinstance(current, list):
            stack.extend(current)


def _collect_oof(
    plan: FoldPlan,
    samples: Sequence[JointBoutSample],
    spec: JointModelSpec,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skipped = 0
    for fold in plan.folds:
        train = _samples_for_events(samples, fold.train_event_ids)
        test = _samples_for_ids(samples, fold.test_bout_ids)
        if not train or not test:
            skipped += 1
            continue
        try:
            model = fit_joint_predictor(train, spec)
        except MissingJointClassError:
            skipped += 1
            continue
        kind = fold.kind.value if isinstance(fold.kind, FoldKind) else str(fold.kind)
        for sample in test:
            rows.append(model.oof_payload(sample, fold_id=fold.fold_id, fold_kind=kind))
    return rows


def train_joint(
    *,
    cards: Sequence[SplitCard],
    samples: Sequence[JointBoutSample],
    spec: JointModelSpec,
    output_path: Path,
    require_target_cards: bool = False,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    """Fit M2 through DWCS-302 folds and refit on development+validation only."""
    if include_holdout or spec.ordinary_allow_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; ordinary train must not enable sealed holdout"
        )
    inner = tuning_folds(
        cards, require_target_cards=require_target_cards, contract=contract
    )
    outer = validation_folds(
        cards, require_target_cards=require_target_cards, contract=contract
    )
    oof = _collect_oof(inner, samples, spec) + _collect_oof(outer, samples, spec)
    metrics: dict[str, Any] = {
        "n_labeled": len(samples),
        "n_oof": len(oof),
        "oof_fold_kinds": sorted({item["fold_kind"] for item in oof}),
        "oof_predictions": oof,
    }
    _assert_no_holdout_betting_keys(metrics)
    final_rows = _final_refit_samples(samples, cards, contract)
    model = fit_joint_predictor(final_rows, spec)
    train_ids = tuple(sample.sample_id for sample in final_rows)
    max_ts = max((sample.cutoff for sample in final_rows), default=None)
    code_hash = compute_code_hash(
        extra_paths=[
            Path(__file__),
            Path(__file__).with_name(JOINT_SPEC_FILENAME),
            Path(__file__).resolve().parents[1] / "markets" / "derive.py",
        ]
    )
    code_commit, code_commit_reason = resolve_code_commit()
    saved = save_joint_artifact(
        model,
        output_path,
        train_sample_ids=train_ids,
        max_train_timestamp=max_ts,
        cutoff_policy=spec.cutoff_policy,
        metrics=metrics,
        oof_predictions=oof,
        contract_hash=outer.contract_hash,
        config_hash=spec.content_hash,
        splits_config_hash=outer.config_hash,
        data_hash=outer.data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
        model_id=spec.model_id,
        spec_id=spec.spec_id,
        spec_version=spec.spec_version,
    )
    report = TrainReport(
        model_id=spec.model_id,
        artifact=saved,
        metrics=metrics,
        train_sample_ids=train_ids,
        max_train_timestamp=max_ts,
        contract_hash=outer.contract_hash,
        feature_spec_hash=spec_hash(),
        config_hash=spec.content_hash,
        data_hash=outer.data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
    )
    return report


def train_joint_from_snapshot(
    snapshot: FeatureSnapshot,
    cards: Sequence[SplitCard],
    *,
    spec: JointModelSpec,
    output_path: Path,
    require_target_cards: bool = False,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    samples = joint_samples_from_snapshot(
        snapshot,
        cards,
        spec,
        allow_holdout=include_holdout,
        contract=contract,
    )
    return train_joint(
        cards=cards,
        samples=samples,
        spec=spec,
        output_path=output_path,
        require_target_cards=require_target_cards,
        include_holdout=include_holdout,
        contract=contract,
    )


def train_joint_from_session(
    session: Session,
    *,
    spec: JointModelSpec,
    output_path: Path,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    cards = cards_from_session(session)
    snapshot = snapshot_from_session(session)
    return train_joint_from_snapshot(
        snapshot,
        cards,
        spec=spec,
        output_path=output_path,
        require_target_cards=True,
        include_holdout=include_holdout,
        contract=contract,
    )


def _add_event(
    snapshot: FeatureSnapshot,
    event_id: str,
    start: datetime,
    *,
    series: str,
) -> None:
    snapshot.events.append(
        SnapshotEvent(
            event_id=event_id,
            scheduled_start_at=start,
            event_date=start.date(),
            series=series,
            name=event_id,
        )
    )


def _add_bout(
    snapshot: FeatureSnapshot,
    bout_id: str,
    event_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
    *,
    scheduled_rounds: int = 3,
) -> SnapshotBout:
    bout = SnapshotBout(
        bout_id=bout_id,
        event_id=event_id,
        fighter_a_id=fighter_a_id,
        fighter_b_id=fighter_b_id,
        scheduled_rounds=scheduled_rounds,
        weight_class="lightweight",
        status="completed",
    )
    snapshot.bouts.append(bout)
    return bout


def _add_result(
    snapshot: FeatureSnapshot,
    bout: SnapshotBout,
    *,
    winner_id: str | None,
    method: str,
    result_type: str,
    at: datetime,
    ending_round: int,
    time_str: str,
) -> None:
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id=bout.bout_id,
            version_kind="event_night",
            revision=1,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            winner_fighter_id=winner_id,
            result_type=result_type,
            method=method,
            ending_round=ending_round,
            time_str=time_str,
            effective_at=at,
            observed_at=at,
        )
    )


def joint_protocol_fixture_cards() -> tuple[SplitCard, ...]:
    """Focused joint chronology with every required class and a locked 2025 card."""
    return (
        SplitCard(
            event_id="joint-2017",
            scheduled_start_at=datetime(2017, 7, 11, 19, 0, tzinfo=UTC),
            event_date=datetime(2017, 7, 11, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=(
                "j17-ako",
                "j17-bko",
                "j17-asub",
                "j17-bsub",
                "j17-aoth",
                "j17-both",
                "j17-adec",
                "j17-bdec",
                "j17-draw",
            ),
        ),
        SplitCard(
            event_id="joint-2018-br",
            scheduled_start_at=datetime(2018, 8, 11, 1, 0, tzinfo=UTC),
            event_date=datetime(2018, 8, 11, tzinfo=UTC).date(),
            series_variant=SeriesVariant.BRAZIL,
            bout_ids=("j18-ako", "j18-asub"),
        ),
        SplitCard(
            event_id="joint-2019",
            scheduled_start_at=datetime(2019, 6, 15, 2, 0, tzinfo=UTC),
            event_date=datetime(2019, 6, 15, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("j19-five-ko", "j19-five-dec"),
        ),
        SplitCard(
            event_id="joint-2021",
            scheduled_start_at=datetime(2021, 5, 4, 2, 0, tzinfo=UTC),
            event_date=datetime(2021, 5, 4, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("j21-bko", "j21-draw"),
        ),
        SplitCard(
            event_id="joint-2023",
            scheduled_start_at=datetime(2023, 8, 22, 2, 0, tzinfo=UTC),
            event_date=datetime(2023, 8, 22, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("j23-adec", "j23-bsub"),
        ),
        SplitCard(
            event_id="joint-2024",
            scheduled_start_at=datetime(2024, 8, 13, 2, 0, tzinfo=UTC),
            event_date=datetime(2024, 8, 13, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("j24-aoth", "j24-bdec"),
        ),
        SplitCard(
            event_id="joint-2025",
            scheduled_start_at=datetime(2025, 8, 12, 2, 0, tzinfo=UTC),
            event_date=datetime(2025, 8, 12, tzinfo=UTC).date(),
            series_variant=SeriesVariant.STANDARD,
            bout_ids=("j25-hold",),
        ),
    )


def joint_protocol_training_universe() -> tuple[tuple[SplitCard, ...], FeatureSnapshot]:
    """Synthetic cards covering all hazard and decision classes without production paths."""
    cards = joint_protocol_fixture_cards()
    snapshot = FeatureSnapshot()
    _add_event(
        snapshot,
        "prior-2016",
        datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
        series="dwcs",
    )
    prior_a = _add_bout(snapshot, "prior-a", "prior-2016", "alpha", "fod1")
    prior_b = _add_bout(snapshot, "prior-b", "prior-2016", "bravo", "fod2")
    _add_result(
        snapshot,
        prior_a,
        winner_id="alpha",
        method="KO/TKO",
        result_type="decisive",
        at=datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
        ending_round=1,
        time_str="1:10",
    )
    _add_result(
        snapshot,
        prior_b,
        winner_id="bravo",
        method="SUB",
        result_type="decisive",
        at=datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
        ending_round=2,
        time_str="2:00",
    )

    specs: dict[str, tuple[str, str, str, str | None, str, str, int, str, int]] = {
        "j17-ako": ("joint-2017", "alpha", "n1", "alpha", "KO/TKO", "decisive", 1, "1:10", 3),
        "j17-bko": ("joint-2017", "bravo", "n2", "n2", "KO/TKO", "decisive", 1, "5:00", 3),
        "j17-asub": ("joint-2017", "alpha", "n3", "alpha", "SUB", "decisive", 2, "2:00", 3),
        "j17-bsub": ("joint-2017", "bravo", "n4", "n4", "SUB", "decisive", 2, "4:00", 3),
        "j17-aoth": ("joint-2017", "alpha", "n5", "alpha", "DQ", "decisive", 3, "1:00", 3),
        "j17-both": ("joint-2017", "bravo", "n6", "n6", "DQ", "decisive", 3, "3:00", 3),
        "j17-adec": ("joint-2017", "alpha", "n7", "alpha", "U-DEC", "decisive", 3, "5:00", 3),
        "j17-bdec": ("joint-2017", "bravo", "n8", "n8", "S-DEC", "decisive", 3, "5:00", 3),
        "j17-draw": ("joint-2017", "alpha", "bravo", None, "DRAW", "draw", 3, "5:00", 3),
        "j18-ako": ("joint-2018-br", "alpha", "n1", "alpha", "KO/TKO", "decisive", 1, "2:30", 3),
        "j18-asub": ("joint-2018-br", "bravo", "n2", "bravo", "SUB", "decisive", 2, "1:00", 3),
        "j19-five-ko": ("joint-2019", "alpha", "n3", "alpha", "KO/TKO", "decisive", 4, "1:20", 5),
        "j19-five-dec": ("joint-2019", "bravo", "n4", "bravo", "U-DEC", "decisive", 5, "5:00", 5),
        "j21-bko": ("joint-2021", "alpha", "n5", "n5", "KO/TKO", "decisive", 2, "0:40", 3),
        "j21-draw": ("joint-2021", "bravo", "n6", None, "DRAW", "draw", 3, "5:00", 3),
        "j23-adec": ("joint-2023", "alpha", "n7", "alpha", "U-DEC", "decisive", 3, "5:00", 3),
        "j23-bsub": ("joint-2023", "bravo", "n8", "n8", "SUB", "decisive", 1, "3:20", 3),
        "j24-aoth": ("joint-2024", "alpha", "n1", "alpha", "DQ", "decisive", 2, "2:10", 3),
        "j24-bdec": ("joint-2024", "bravo", "n2", "n2", "S-DEC", "decisive", 3, "5:00", 3),
        "j25-hold": ("joint-2025", "alpha", "bravo", "bravo", "U-DEC", "decisive", 3, "5:00", 3),
    }
    starts = {
        card.event_id: card.scheduled_start_at
        for card in cards
        if card.scheduled_start_at is not None
    }
    for card in cards:
        series = "dwcs_brazil" if card.series_variant is SeriesVariant.BRAZIL else "dwcs"
        _add_event(snapshot, card.event_id, starts[card.event_id], series=series)
        for bout_id in card.bout_ids:
            event_id, a_id, b_id, winner, method, result, ending, clock, rounds = specs[bout_id]
            bout = _add_bout(
                snapshot,
                bout_id,
                event_id,
                a_id,
                b_id,
                scheduled_rounds=rounds,
            )
            _add_result(
                snapshot,
                bout,
                winner_id=winner,
                method=method,
                result_type=result,
                at=starts[card.event_id],
                ending_round=ending,
                time_str=clock,
            )
    return cards, snapshot


def run_protocol_joint_train(
    *,
    spec: JointModelSpec | None = None,
    output_path: Path,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    resolved = spec if spec is not None else load_joint_spec()
    cards, snapshot = joint_protocol_training_universe()
    return train_joint_from_snapshot(
        snapshot,
        cards,
        spec=resolved,
        output_path=output_path,
        require_target_cards=False,
        include_holdout=include_holdout,
        contract=contract,
    )




