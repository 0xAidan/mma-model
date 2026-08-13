"""Versioned model artifacts with sidecar manifests (DWCS-303).

Production loading never executes pickle. New M1 payloads are JSON: scaler
parameters and logistic coefficients plus validated metadata. A sidecar
manifest carries schema version, hashes, feature order, train IDs, cutoff
policy, metrics, code commit, and the SHA-256 of the payload bytes.
Checksum, feature-order, schema, or hash mismatch fails with a typed error.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Never

import yaml

from mma_model.backtest.contract import PINNED_FEATURE_SPEC_HASH, PINNED_SPLITS_CONFIG_HASH
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, compute_contract_hash
from mma_model.features.spec import FEATURE_NAMES, SPEC_VERSION, spec_hash, swap_values
from mma_model.quality.schema import sha256_canonical

RIDGE_SPEC_FILENAME: Final = "ridge_v1.yaml"
ARTIFACT_SCHEMA_VERSION: Final = "dwcs_artifact_v1"
CALIBRATED_ARTIFACT_SCHEMA_VERSION: Final = "dwcs_artifact_v1.1"
CALIBRATION_SCHEMA_VERSION: Final = "dwcs_calibration_v1"
BOOTSTRAP_SCHEMA_VERSION: Final = "dwcs_bootstrap_v1"
ACCEPTED_ARTIFACT_SCHEMA_VERSIONS: Final = frozenset(
    {ARTIFACT_SCHEMA_VERSION, CALIBRATED_ARTIFACT_SCHEMA_VERSION}
)
PAYLOAD_KIND: Final = "standardized_ridge_logistic_v1"
ESTIMATOR_KIND: Final = "standardized_ridge_logistic"
PRODUCTION_BOOTSTRAP_REPLICATES: Final = 200
RIDGE_SPEC_ID: Final = "ridge_v1"
RIDGE_CONTRACT_ID: Final = "dwcs_model_spec"
EXPECTED_RIDGE_SCHEMA_VERSION: Final = 1
EXPECTED_RIDGE_SPEC_VERSION: Final = "1.0.0"
EXPECTED_MODEL_ID: Final = "M1"
EXPECTED_CUTOFF_POLICY: Final = "scheduled_minus_60m"
EXPECTED_FINAL_REFIT: Final = "development_and_validation"
UNKNOWN_CODE_COMMIT: Final = "unknown"
# Canonical JSON digest of packaged ridge_v1.yaml. Update only with spec_version.
PINNED_RIDGE_SPEC_HASH: Final = (
    "cf4a679519e5fccda46176e40be253525afe4951cfbaa7d91cbd92b0e724d61a"
)

SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_HEX: Final = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_LOGISTIC_CLASSES: Final = (0, 1)


class ArtifactKind(StrEnum):
    CHECKSUM = "checksum"
    CODE = "code"
    CONFIG = "config"
    CONTRACT = "contract"
    DATA = "data"
    FEATURE_ORDER = "feature_order"
    SPEC = "spec"
    UNTRUSTED = "untrusted"


class ArtifactError(ValueError):
    """Invalid, untrusted, or mismatched model artifact."""


class UntrustedArtifactError(ArtifactError):
    """Missing sidecar, non-JSON payload, or schema/type failure."""


class ArtifactChecksumMismatchError(ArtifactError):
    """Payload bytes did not match the manifest SHA-256."""


class ArtifactFeatureOrderMismatchError(ArtifactError):
    """Stored feature names did not match the live spec order."""


class ArtifactSpecMismatchError(ArtifactError):
    """Feature spec version or hash did not match the live spec."""


class ArtifactConfigMismatchError(ArtifactError):
    """Model-spec digest did not match the pinned ridge spec."""


class RidgeSpecError(ValueError):
    """Ridge model spec failed to load or did not match the pinned digest."""


def _kind_label(kind: ArtifactKind) -> str:
    if kind is ArtifactKind.CHECKSUM:
        return "checksum"
    if kind is ArtifactKind.CODE:
        return "code"
    if kind is ArtifactKind.CONFIG:
        return "model spec"
    if kind is ArtifactKind.CONTRACT:
        return "evaluation contract"
    if kind is ArtifactKind.DATA:
        return "data"
    if kind is ArtifactKind.FEATURE_ORDER:
        return "feature order"
    if kind is ArtifactKind.SPEC:
        return "feature spec"
    if kind is ArtifactKind.UNTRUSTED:
        return "untrusted artifact"
    never_kind: Never = kind
    raise ArtifactError(f"unhandled artifact kind: {never_kind!r}")


def _require_sha256(value: object, *, field: str, kind: ArtifactKind) -> str:
    text = str(value or "")
    if not SHA256_HEX.fullmatch(text):
        raise UntrustedArtifactError(
            f"{_kind_label(kind)} hash {field} must be a 64-char sha256 hex digest"
        )
    return text


def _require_code_commit(value: object) -> str:
    text = str(value or "")
    if text == UNKNOWN_CODE_COMMIT:
        return text
    if GIT_COMMIT_HEX.fullmatch(text):
        return text
    raise UntrustedArtifactError(
        "code_commit must be a 40-char git SHA or the token 'unknown'"
    )


def _require_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UntrustedArtifactError("code_commit_reason must be a non-empty string")
    return value.strip()


def _require_finite_floats(value: object, *, n: int, field: str) -> tuple[float, ...]:
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


def _require_positive_scales(values: Sequence[float], *, field: str) -> tuple[float, ...]:
    for item in values:
        if item <= 0.0:
            raise UntrustedArtifactError(f"{field} entries must be finite and > 0")
    return tuple(values)


def _logistic(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def manifest_path_for(payload_path: Path) -> Path:
    """Sidecar path: ``ridge_v1.json`` → ``ridge_v1.manifest.json``."""
    resolved = Path(payload_path)
    return resolved.with_name(resolved.stem + ".manifest.json")


def package_ridge_spec_path() -> Path:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(RIDGE_SPEC_FILENAME)
    with resources.as_file(resource) as path:
        return Path(path)


def visible_ridge_spec_path(*, root: Path | None = None) -> Path:
    if root is None:
        root = Path(__file__).resolve().parents[3]
    return root / "config" / "model_specs" / RIDGE_SPEC_FILENAME


def compute_ridge_spec_hash(payload: Mapping[str, Any]) -> str:
    return compute_contract_hash(dict(payload))


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RidgeSpecError(f"unable to read ridge spec at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RidgeSpecError("ridge spec root must be a mapping")
    return loaded


def _read_package_ridge_payload() -> dict[str, Any]:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(RIDGE_SPEC_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise RidgeSpecError(
            f"unable to read packaged ridge spec {RIDGE_SPEC_FILENAME}"
        ) from exc
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise RidgeSpecError("packaged ridge spec root must be a mapping")
    return loaded


@dataclass(frozen=True)
class RidgeModelSpec:
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
    ordinary_allow_holdout: bool
    final_refit: str
    cutoff_policy: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "C": self.C,
            "content_hash": self.content_hash,
            "cutoff_policy": self.cutoff_policy,
            "feature_spec_version": self.feature_spec_version,
            "final_refit": self.final_refit,
            "max_iter": self.max_iter,
            "model_id": self.model_id,
            "ordinary_allow_holdout": self.ordinary_allow_holdout,
            "penalty": self.penalty,
            "solver": self.solver,
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "standardize": self.standardize,
            "swap_augment": self.swap_augment,
        }


def _require_false_holdout(value: object) -> bool:
    if value is True:
        raise RidgeSpecError("ordinary_allow_holdout must be false; 2025 stay locked")
    if value is False:
        return False
    raise RidgeSpecError(f"ordinary_allow_holdout must be boolean false, got {value!r}")


def parse_ridge_spec(
    payload: Mapping[str, Any],
    *,
    enforce_pinned_digest: bool = True,
) -> RidgeModelSpec:
    if payload.get("contract_id") != RIDGE_CONTRACT_ID:
        raise RidgeSpecError(
            f"contract_id mismatch: got {payload.get('contract_id')!r}, "
            f"expected {RIDGE_CONTRACT_ID!r}"
        )
    if payload.get("schema_version") != EXPECTED_RIDGE_SCHEMA_VERSION:
        raise RidgeSpecError(
            f"schema_version mismatch: got {payload.get('schema_version')!r}"
        )
    if payload.get("spec_id") != RIDGE_SPEC_ID:
        raise RidgeSpecError(f"spec_id mismatch: got {payload.get('spec_id')!r}")
    if payload.get("spec_version") != EXPECTED_RIDGE_SPEC_VERSION:
        raise RidgeSpecError(
            f"spec_version mismatch: got {payload.get('spec_version')!r}"
        )
    if payload.get("model_id") != EXPECTED_MODEL_ID:
        raise RidgeSpecError(f"model_id mismatch: got {payload.get('model_id')!r}")
    estimator = payload.get("estimator")
    if not isinstance(estimator, Mapping):
        raise RidgeSpecError("estimator must be a mapping")
    folds = payload.get("folds")
    if not isinstance(folds, Mapping):
        raise RidgeSpecError("folds must be a mapping")
    if estimator.get("penalty") != "l2":
        raise RidgeSpecError("estimator.penalty must be l2")
    if folds.get("final_refit") != EXPECTED_FINAL_REFIT:
        raise RidgeSpecError("final_refit must be development_and_validation")
    if payload.get("cutoff_policy") != EXPECTED_CUTOFF_POLICY:
        raise RidgeSpecError("cutoff_policy must be scheduled_minus_60m")
    if payload.get("standardize") is not True:
        raise RidgeSpecError("standardize must be true")
    if payload.get("swap_augment") is not True:
        raise RidgeSpecError("swap_augment must be true")
    content_hash = compute_ridge_spec_hash(payload)
    if enforce_pinned_digest and content_hash != PINNED_RIDGE_SPEC_HASH:
        raise RidgeSpecError(
            f"ridge spec hash mismatch: got {content_hash}, expected {PINNED_RIDGE_SPEC_HASH}"
        )
    return RidgeModelSpec(
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
        ordinary_allow_holdout=_require_false_holdout(folds.get("ordinary_allow_holdout")),
        final_refit=str(folds["final_refit"]),
        cutoff_policy=str(payload["cutoff_policy"]),
        content_hash=content_hash,
    )


def load_ridge_spec(
    *,
    path: Path | None = None,
    enforce_pinned_digest: bool = True,
) -> RidgeModelSpec:
    payload = (
        _read_yaml_mapping(Path(path)) if path is not None else _read_package_ridge_payload()
    )
    return parse_ridge_spec(payload, enforce_pinned_digest=enforce_pinned_digest)


def compute_code_hash(*, extra_paths: Sequence[Path] | None = None) -> str:
    """SHA-256 of canonical JSON over named source-file digests."""
    paths = [Path(__file__), Path(__file__).with_name(RIDGE_SPEC_FILENAME)]
    if extra_paths is not None:
        paths.extend(Path(item) for item in extra_paths)
    rows = [
        {"name": path.name, "sha256": sha256_bytes(path.read_bytes())} for path in paths
    ]
    rows.sort(key=lambda item: str(item["name"]))
    return sha256_canonical({"files": rows})


def resolve_code_commit(*, repo_root: Path | None = None) -> tuple[str, str]:
    """Return ``(commit, reason)`` from git without a shell string.

    Uses argv ``git rev-parse HEAD`` only. Missing git, a timeout, or a
    non-SHA reply becomes ``unknown`` plus an explicit reason.
    """
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[3]
    git_dir = root / ".git"
    if not git_dir.exists():
        return UNKNOWN_CODE_COMMIT, "no_git_directory"
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UNKNOWN_CODE_COMMIT, f"git_unavailable:{type(exc).__name__}"
    if completed.returncode != 0:
        return UNKNOWN_CODE_COMMIT, "git_rev_parse_failed"
    sha = completed.stdout.strip().lower()
    if not GIT_COMMIT_HEX.fullmatch(sha):
        return UNKNOWN_CODE_COMMIT, "git_rev_parse_unrecognized"
    return sha, "git_rev_parse_head"


@dataclass(frozen=True)
class RidgePredictor:
    """In-memory M1 predictor built from validated JSON numeric arrays."""

    feature_names: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coef: tuple[float, ...]
    intercept: float
    classes: tuple[int, ...]
    spec_hash: str
    spec_version: str

    def raw_logit(self, values: Sequence[float]) -> float:
        """Linear predictor logit from scaler + coefficients. No swap averaging."""
        if tuple(self.feature_names) != FEATURE_NAMES:
            raise ArtifactFeatureOrderMismatchError(
                f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch on predictor"
            )
        if len(values) != len(FEATURE_NAMES):
            raise ArtifactFeatureOrderMismatchError(
                "prediction vector length does not match FEATURE_NAMES"
            )
        logit = self.intercept
        for value, mean, scale, weight in zip(
            values,
            self.scaler_mean,
            self.scaler_scale,
            self.coef,
            strict=True,
        ):
            number = float(value)
            if not math.isfinite(number):
                raise UntrustedArtifactError("prediction vector contains a non-finite number")
            logit += weight * ((number - mean) / scale)
        if not math.isfinite(logit):
            raise UntrustedArtifactError("raw logit is not finite")
        return float(logit)

    def raw_win_prob(self, values: Sequence[float]) -> float:
        """P(A wins) from scaler + logistic only. No swap averaging."""
        return _logistic(self.raw_logit(values))

    def swap_safe_win_prob(self, values: Sequence[float]) -> float:
        """Average p(x) and 1-p(swap(x)) as a serving-time guard."""
        p_raw = self.raw_win_prob(values)
        p_swap = self.raw_win_prob(swap_values(values))
        return 0.5 * (p_raw + (1.0 - p_swap))

    def identity_hash(self) -> str:
        return sha256_canonical(
            {
                "coef": list(self.coef),
                "intercept": self.intercept,
                "kind": ESTIMATOR_KIND,
                "scaler_mean": list(self.scaler_mean),
                "scaler_scale": list(self.scaler_scale),
            }
        )


@dataclass(frozen=True)
class ArtifactManifest:
    schema_version: str
    model_id: str
    spec_id: str
    spec_version: str
    feature_spec_hash: str
    contract_hash: str
    config_hash: str
    splits_config_hash: str
    data_hash: str
    code_hash: str
    code_commit: str
    code_commit_reason: str
    feature_names: tuple[str, ...]
    train_sample_ids: tuple[str, ...]
    max_train_timestamp: str | None
    cutoff_policy: str
    metrics: dict[str, Any]
    payload_sha256: str
    calibrated: bool = False
    calibration: dict[str, Any] | None = None
    bootstrap: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code_commit": self.code_commit,
            "code_commit_reason": self.code_commit_reason,
            "code_hash": self.code_hash,
            "config_hash": self.config_hash,
            "contract_hash": self.contract_hash,
            "cutoff_policy": self.cutoff_policy,
            "data_hash": self.data_hash,
            "feature_names": list(self.feature_names),
            "feature_spec_hash": self.feature_spec_hash,
            "max_train_timestamp": self.max_train_timestamp,
            "metrics": self.metrics,
            "model_id": self.model_id,
            "payload_sha256": self.payload_sha256,
            "schema_version": self.schema_version,
            "spec_id": self.spec_id,
            "spec_version": self.spec_version,
            "splits_config_hash": self.splits_config_hash,
            "train_sample_ids": list(self.train_sample_ids),
        }
        if self.calibrated or self.calibration is not None or self.bootstrap is not None:
            payload["bootstrap"] = self.bootstrap
            payload["calibrated"] = self.calibrated
            payload["calibration"] = self.calibration
        return payload


def manifest_from_mapping(payload: Mapping[str, Any]) -> ArtifactManifest:
    names = payload.get("feature_names")
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        raise ArtifactError("manifest feature_names must be a list of strings")
    sample_ids = payload.get("train_sample_ids")
    if not isinstance(sample_ids, list) or not all(isinstance(item, str) for item in sample_ids):
        raise ArtifactError("manifest train_sample_ids must be a list of strings")
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ArtifactError("manifest metrics must be an object")
    max_ts = payload.get("max_train_timestamp")
    if max_ts is not None and not isinstance(max_ts, str):
        raise ArtifactError("manifest max_train_timestamp must be a string or null")
    calibration = payload.get("calibration")
    if calibration is not None and not isinstance(calibration, dict):
        raise UntrustedArtifactError("manifest calibration must be an object or null")
    bootstrap = payload.get("bootstrap")
    if bootstrap is not None and not isinstance(bootstrap, dict):
        raise UntrustedArtifactError("manifest bootstrap must be an object or null")
    calibrated = payload.get("calibrated", False)
    if calibrated is not True and calibrated is not False:
        raise UntrustedArtifactError("manifest calibrated must be a boolean")
    return ArtifactManifest(
        schema_version=str(payload.get("schema_version", "")),
        model_id=str(payload.get("model_id", "")),
        spec_id=str(payload.get("spec_id", "")),
        spec_version=str(payload.get("spec_version", "")),
        feature_spec_hash=str(payload.get("feature_spec_hash", "")),
        contract_hash=str(payload.get("contract_hash", "")),
        config_hash=str(payload.get("config_hash", "")),
        splits_config_hash=str(payload.get("splits_config_hash", "")),
        data_hash=str(payload.get("data_hash", "")),
        code_hash=str(payload.get("code_hash", "")),
        code_commit=_require_code_commit(payload.get("code_commit", "")),
        code_commit_reason=_require_reason(payload.get("code_commit_reason", "")),
        feature_names=tuple(names),
        train_sample_ids=tuple(sample_ids),
        max_train_timestamp=max_ts,
        cutoff_policy=str(payload.get("cutoff_policy", "")),
        metrics=dict(metrics),
        payload_sha256=str(payload.get("payload_sha256", "")),
        calibrated=bool(calibrated),
        calibration=None if calibration is None else dict(calibration),
        bootstrap=None if bootstrap is None else dict(bootstrap),
    )


@dataclass(frozen=True)
class SavedArtifact:
    payload_path: Path
    manifest_path: Path
    manifest: ArtifactManifest
    payload_sha256: str


@dataclass(frozen=True)
class LoadedArtifact:
    payload: dict[str, Any]
    predictor: RidgePredictor
    manifest: ArtifactManifest
    payload_path: Path
    manifest_path: Path
    oof_predictions: tuple[dict[str, Any], ...] = ()
    oof_exclusions: tuple[dict[str, Any], ...] = ()
    calibrated: bool = False


def _write_json(path: Path, payload: Mapping[str, Any]) -> bytes:
    blob = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(blob)
    return blob


def write_json_document(path: Path, payload: Mapping[str, Any]) -> bytes:
    """Public JSON writer for calibrated sidecars. Never pickle."""
    return _write_json(path, payload)


def _require_int(value: object, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UntrustedArtifactError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise UntrustedArtifactError(f"{field} must be >= {minimum}")
    return value


def _require_finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UntrustedArtifactError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise UntrustedArtifactError(f"{field} must be a finite number")
    return number


def _require_string_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UntrustedArtifactError(f"{field} must be a list of strings")
    return [str(item) for item in value]


def _require_schema_version(value: object) -> str:
    text = str(value or "")
    if text not in ACCEPTED_ARTIFACT_SCHEMA_VERSIONS:
        raise UntrustedArtifactError(
            f"unknown artifact schema {text!r}; "
            f"expected one of {sorted(ACCEPTED_ARTIFACT_SCHEMA_VERSIONS)}"
        )
    return text


def verify_calibration_metadata(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Fail closed on malformed calibrator metadata. ``None`` means uncalibrated."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise UntrustedArtifactError("calibration metadata must be an object")
    if str(payload.get("schema_version", "")) != CALIBRATION_SCHEMA_VERSION:
        raise UntrustedArtifactError(
            f"calibration schema_version must be {CALIBRATION_SCHEMA_VERSION}"
        )
    kind = str(payload.get("type", ""))
    if kind == "sigmoid":
        a = _require_finite_number(payload.get("a"), field="calibration.a")
        b = _require_finite_number(payload.get("b"), field="calibration.b")
        if payload.get("temperature") not in (None,):
            raise UntrustedArtifactError("sigmoid calibration must not set temperature")
        del a, b
    elif kind == "temperature":
        temperature = _require_finite_number(
            payload.get("temperature"), field="calibration.temperature"
        )
        if temperature <= 0.0:
            raise UntrustedArtifactError("temperature T must be finite and > 0")
        if payload.get("a") not in (None,) or payload.get("b") not in (None,):
            raise UntrustedArtifactError("temperature calibration must not set sigmoid a/b")
    else:
        raise UntrustedArtifactError(f"unknown calibration type {kind!r}")
    n_fit = _require_int(payload.get("n_fitting_oof"), field="n_fitting_oof", minimum=1)
    event_ids = _require_string_list(
        payload.get("fitting_event_ids"), field="fitting_event_ids"
    )
    sample_ids = _require_string_list(
        payload.get("fitting_sample_ids"), field="fitting_sample_ids"
    )
    if n_fit != len(sample_ids):
        raise UntrustedArtifactError("n_fitting_oof must equal len(fitting_sample_ids)")
    _require_sha256(
        payload.get("fitting_event_ids_hash"),
        field="fitting_event_ids_hash",
        kind=ArtifactKind.DATA,
    )
    _require_sha256(
        payload.get("fitting_sample_ids_hash"),
        field="fitting_sample_ids_hash",
        kind=ArtifactKind.DATA,
    )
    expected = _require_int(payload.get("oof_n_expected"), field="oof_n_expected", minimum=0)
    emitted = _require_int(payload.get("oof_n_emitted"), field="oof_n_emitted", minimum=0)
    excluded = _require_int(payload.get("oof_n_excluded"), field="oof_n_excluded", minimum=0)
    if emitted + excluded != expected:
        raise UntrustedArtifactError(
            "calibration OOF counts do not reconcile: "
            f"expected={expected} emitted={emitted} excluded={excluded}"
        )
    if emitted != n_fit:
        raise UntrustedArtifactError("n_fitting_oof must equal oof_n_emitted")
    if not event_ids:
        raise UntrustedArtifactError("fitting_event_ids must be non-empty")
    metrics_pre = payload.get("metrics_pre")
    metrics_post = payload.get("metrics_post")
    if not isinstance(metrics_pre, dict) or not isinstance(metrics_post, dict):
        raise UntrustedArtifactError("calibration metrics_pre/metrics_post must be objects")
    return dict(payload)


def verify_bootstrap_metadata(
    payload: Mapping[str, Any] | None,
    *,
    require_production: bool = False,
) -> dict[str, Any] | None:
    """Fail closed on malformed event-block bootstrap metadata."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise UntrustedArtifactError("bootstrap metadata must be an object")
    if str(payload.get("schema_version", "")) != BOOTSTRAP_SCHEMA_VERSION:
        raise UntrustedArtifactError(
            f"bootstrap schema_version must be {BOOTSTRAP_SCHEMA_VERSION}"
        )
    if str(payload.get("sampling_unit", "")) != "event":
        raise UntrustedArtifactError("bootstrap sampling_unit must be 'event'")
    n_replicates = _require_int(
        payload.get("n_replicates"), field="n_replicates", minimum=1
    )
    n_successful = _require_int(
        payload.get("n_successful"), field="n_successful", minimum=0
    )
    n_attempts = _require_int(payload.get("n_attempts"), field="n_attempts", minimum=0)
    n_rejected = _require_int(payload.get("n_rejected"), field="n_rejected", minimum=0)
    seed = _require_int(payload.get("seed"), field="seed", minimum=0)
    del seed
    if n_successful != n_replicates:
        raise UntrustedArtifactError("bootstrap n_successful must equal n_replicates")
    if n_attempts < n_successful:
        raise UntrustedArtifactError("bootstrap n_attempts must be >= n_successful")
    if n_rejected != n_attempts - n_successful:
        raise UntrustedArtifactError("bootstrap n_rejected must equal attempts minus successes")
    production = payload.get("production_qualified", False)
    if production is not True and production is not False:
        raise UntrustedArtifactError("bootstrap production_qualified must be a boolean")
    if production is True or require_production:
        if n_successful != PRODUCTION_BOOTSTRAP_REPLICATES:
            raise UntrustedArtifactError(
                "production-qualified bootstrap must have "
                f"{PRODUCTION_BOOTSTRAP_REPLICATES} successful refits"
            )
        if n_replicates != PRODUCTION_BOOTSTRAP_REPLICATES:
            raise UntrustedArtifactError(
                "production-qualified bootstrap n_replicates must be "
                f"{PRODUCTION_BOOTSTRAP_REPLICATES}"
            )
    event_ids = _require_string_list(payload.get("event_ids"), field="event_ids")
    if not event_ids:
        raise UntrustedArtifactError("bootstrap event_ids must be non-empty")
    _require_sha256(payload.get("event_ids_hash"), field="event_ids_hash", kind=ArtifactKind.DATA)
    _require_sha256(
        payload.get("estimator_hash"), field="estimator_hash", kind=ArtifactKind.CONFIG
    )
    _require_sha256(payload.get("config_hash"), field="config_hash", kind=ArtifactKind.CONFIG)
    _require_sha256(payload.get("data_hash"), field="data_hash", kind=ArtifactKind.DATA)
    targets = payload.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise UntrustedArtifactError("bootstrap targets must be a non-empty object")
    for target_id, summary in targets.items():
        if not isinstance(target_id, str) or not target_id:
            raise UntrustedArtifactError("bootstrap target ids must be non-empty strings")
        if not isinstance(summary, dict):
            raise UntrustedArtifactError(f"bootstrap target {target_id!r} must be an object")
        for key in ("p05", "p25", "p50", "p75", "p95"):
            _require_finite_number(summary.get(key), field=f"bootstrap.{target_id}.{key}")
        price = summary.get("observed_price")
        ev_keys = ("ev05", "ev25", "ev50", "ev75", "ev95")
        if price is None:
            for key in ev_keys:
                if key in summary and summary[key] is not None:
                    raise UntrustedArtifactError(
                        f"bootstrap target {target_id!r} must not invent EV without a price"
                    )
            continue
        _require_finite_number(price, field=f"bootstrap.{target_id}.observed_price")
        for key in ev_keys:
            _require_finite_number(summary.get(key), field=f"bootstrap.{target_id}.{key}")
    return dict(payload)


def verify_optional_calibration_payload(payload: Mapping[str, Any]) -> None:
    """Uncalibrated artifacts stay loadable; present calibration must be valid."""
    calibrated = payload.get("calibrated", False)
    if calibrated is not True and calibrated is not False and calibrated is not None:
        raise UntrustedArtifactError("calibrated must be a boolean")
    calibration = payload.get("calibration")
    bootstrap = payload.get("bootstrap")
    if calibrated is True:
        verify_calibration_metadata(calibration)
        verify_bootstrap_metadata(bootstrap)
        return
    if calibration is not None:
        verify_calibration_metadata(calibration)
    if bootstrap is not None:
        verify_bootstrap_metadata(bootstrap, require_production=False)


def _classes_from_json(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise UntrustedArtifactError("logistic.classes must be [0, 1]")
    classes: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise UntrustedArtifactError("logistic.classes must be [0, 1]")
        classes.append(item)
    parsed = tuple(classes)
    if parsed != REQUIRED_LOGISTIC_CLASSES:
        raise UntrustedArtifactError("logistic.classes must be [0, 1]")
    return parsed


def _intercept_from_json(value: object) -> float:
    if isinstance(value, list):
        if len(value) != 1:
            raise UntrustedArtifactError("logistic.intercept must be one finite number")
        value = value[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UntrustedArtifactError("logistic.intercept must be a finite number")
    intercept = float(value)
    if not math.isfinite(intercept):
        raise UntrustedArtifactError("logistic.intercept must be a finite number")
    return intercept


def predictor_from_mapping(payload: Mapping[str, Any]) -> RidgePredictor:
    """Validate JSON payload schema/types/shapes/finiteness; no pickle."""
    if payload.get("schema_version") not in ACCEPTED_ARTIFACT_SCHEMA_VERSIONS:
        raise UntrustedArtifactError(
            f"unknown artifact schema {payload.get('schema_version')!r}"
        )
    if payload.get("payload_kind") != PAYLOAD_KIND:
        raise UntrustedArtifactError(
            f"unknown payload_kind {payload.get('payload_kind')!r}; "
            f"expected {PAYLOAD_KIND}"
        )
    names = payload.get("feature_names")
    if not isinstance(names, list) or tuple(names) != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch inside payload"
        )
    n_features = len(FEATURE_NAMES)
    estimator = payload.get("estimator")
    if not isinstance(estimator, Mapping):
        raise UntrustedArtifactError("estimator must be an object")
    if estimator.get("kind") != ESTIMATOR_KIND:
        raise UntrustedArtifactError(f"estimator.kind must be {ESTIMATOR_KIND}")
    scaler = estimator.get("scaler")
    logistic = estimator.get("logistic")
    if not isinstance(scaler, Mapping) or not isinstance(logistic, Mapping):
        raise UntrustedArtifactError("estimator.scaler and estimator.logistic must be objects")
    mean = _require_finite_floats(scaler.get("mean"), n=n_features, field="scaler.mean")
    scale = _require_positive_scales(
        _require_finite_floats(scaler.get("scale"), n=n_features, field="scaler.scale"),
        field="scaler.scale",
    )
    coef = _require_finite_floats(logistic.get("coef"), n=n_features, field="logistic.coef")
    intercept = _intercept_from_json(logistic.get("intercept"))
    classes = _classes_from_json(logistic.get("classes"))
    spec = str(payload.get("spec_hash", "") or payload.get("feature_spec_hash", ""))
    if spec != spec_hash():
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch inside payload"
        )
    version = str(payload.get("spec_version", ""))
    if version != SPEC_VERSION:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} version mismatch inside payload"
        )
    return RidgePredictor(
        feature_names=FEATURE_NAMES,
        scaler_mean=mean,
        scaler_scale=scale,
        coef=coef,
        intercept=intercept,
        classes=classes,
        spec_hash=spec,
        spec_version=version,
    )


def load_feature_vector(payload: Mapping[str, Any]) -> tuple[float, ...]:
    """Validate a features-json object against live FEATURE_NAMES order."""
    names = payload.get("names")
    if not isinstance(names, list) or tuple(str(item) for item in names) != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch in features-json"
        )
    return _require_finite_floats(
        payload.get("values"),
        n=len(FEATURE_NAMES),
        field="values",
    )


def save_artifact(
    predictor: RidgePredictor,
    payload_path: Path,
    *,
    train_sample_ids: Sequence[str],
    max_train_timestamp: datetime | None,
    cutoff_policy: str,
    metrics: Mapping[str, Any],
    contract_hash: str,
    config_hash: str,
    splits_config_hash: str,
    data_hash: str,
    code_hash: str,
    code_commit: str,
    code_commit_reason: str,
    model_id: str = EXPECTED_MODEL_ID,
    spec_id: str = RIDGE_SPEC_ID,
    oof_predictions: Sequence[Mapping[str, Any]] = (),
    oof_exclusions: Sequence[Mapping[str, Any]] = (),
) -> SavedArtifact:
    """Serialize a JSON ridge payload and write a verified sidecar manifest."""
    if tuple(predictor.feature_names) != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch: "
            "predictor feature_names must equal the live FEATURE_NAMES order"
        )
    live_spec = spec_hash()
    if live_spec != PINNED_FEATURE_SPEC_HASH:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch: "
            f"got {live_spec}, expected {PINNED_FEATURE_SPEC_HASH}"
        )
    if predictor.spec_hash != live_spec or predictor.spec_version != SPEC_VERSION:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} mismatch on fitted predictor"
        )
    if contract_hash != PINNED_CONTRACT_HASH:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.CONTRACT)} hash mismatch: "
            f"got {contract_hash}, expected {PINNED_CONTRACT_HASH}"
        )
    if config_hash != PINNED_RIDGE_SPEC_HASH:
        raise ArtifactConfigMismatchError(
            f"{_kind_label(ArtifactKind.CONFIG)} hash mismatch: "
            f"got {config_hash}, expected {PINNED_RIDGE_SPEC_HASH}"
        )
    if splits_config_hash != PINNED_SPLITS_CONFIG_HASH:
        raise ArtifactConfigMismatchError(
            f"{_kind_label(ArtifactKind.CONFIG)} splits hash mismatch: "
            f"got {splits_config_hash}, expected {PINNED_SPLITS_CONFIG_HASH}"
        )
    data_digest = _require_sha256(data_hash, field="data_hash", kind=ArtifactKind.DATA)
    code_digest = _require_sha256(code_hash, field="code_hash", kind=ArtifactKind.CODE)
    contract_digest = _require_sha256(
        contract_hash, field="contract_hash", kind=ArtifactKind.CONTRACT
    )
    config_digest = _require_sha256(config_hash, field="config_hash", kind=ArtifactKind.CONFIG)
    splits_digest = _require_sha256(
        splits_config_hash, field="splits_config_hash", kind=ArtifactKind.CONFIG
    )
    commit = _require_code_commit(code_commit)
    reason = _require_reason(code_commit_reason)

    stored: dict[str, Any] = {
        "code_commit": commit,
        "code_commit_reason": reason,
        "code_hash": code_digest,
        "config_hash": config_digest,
        "contract_hash": contract_digest,
        "data_hash": data_digest,
        "estimator": {
            "kind": ESTIMATOR_KIND,
            "logistic": {
                "classes": list(predictor.classes),
                "coef": list(predictor.coef),
                "intercept": predictor.intercept,
            },
            "scaler": {
                "mean": list(predictor.scaler_mean),
                "scale": list(predictor.scaler_scale),
            },
        },
        "feature_names": list(FEATURE_NAMES),
        "feature_spec_hash": live_spec,
        "model_id": model_id,
        "oof_exclusions": [dict(item) for item in oof_exclusions],
        "oof_predictions": [dict(item) for item in oof_predictions],
        "payload_kind": PAYLOAD_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "spec_hash": live_spec,
        "spec_version": SPEC_VERSION,
        "splits_config_hash": splits_digest,
    }
    predictor_from_mapping(stored)

    target = Path(payload_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = _write_json(target, stored)
    checksum = sha256_bytes(blob)
    manifest = ArtifactManifest(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        model_id=model_id,
        spec_id=spec_id,
        spec_version=SPEC_VERSION,
        feature_spec_hash=live_spec,
        contract_hash=contract_digest,
        config_hash=config_digest,
        splits_config_hash=splits_digest,
        data_hash=data_digest,
        code_hash=code_digest,
        code_commit=commit,
        code_commit_reason=reason,
        feature_names=FEATURE_NAMES,
        train_sample_ids=tuple(train_sample_ids),
        max_train_timestamp=(
            max_train_timestamp.isoformat() if max_train_timestamp is not None else None
        ),
        cutoff_policy=cutoff_policy,
        metrics=dict(metrics),
        payload_sha256=checksum,
    )
    side = manifest_path_for(target)
    _write_json(side, manifest.to_dict())
    return SavedArtifact(
        payload_path=target,
        manifest_path=side,
        manifest=manifest,
        payload_sha256=checksum,
    )


def _verify_manifest(manifest: ArtifactManifest, payload_bytes: bytes) -> None:
    _require_schema_version(manifest.schema_version)
    digest = sha256_bytes(payload_bytes)
    payload_digest = _require_sha256(
        manifest.payload_sha256, field="payload_sha256", kind=ArtifactKind.CHECKSUM
    )
    if digest != payload_digest:
        raise ArtifactChecksumMismatchError(
            f"{_kind_label(ArtifactKind.CHECKSUM)} mismatch: "
            f"got {digest}, expected {payload_digest}"
        )
    if tuple(manifest.feature_names) != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch with live FEATURE_NAMES"
        )
    live_spec = spec_hash()
    if live_spec != PINNED_FEATURE_SPEC_HASH:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch: "
            f"got {live_spec}, expected {PINNED_FEATURE_SPEC_HASH}"
        )
    if manifest.spec_version != SPEC_VERSION:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} version mismatch: "
            f"got {manifest.spec_version!r}, expected {SPEC_VERSION!r}"
        )
    feature_digest = _require_sha256(
        manifest.feature_spec_hash, field="feature_spec_hash", kind=ArtifactKind.SPEC
    )
    if feature_digest != live_spec:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch: "
            f"got {feature_digest}, expected {live_spec}"
        )
    contract_digest = _require_sha256(
        manifest.contract_hash, field="contract_hash", kind=ArtifactKind.CONTRACT
    )
    if contract_digest != PINNED_CONTRACT_HASH:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.CONTRACT)} hash mismatch: "
            f"got {contract_digest}, expected {PINNED_CONTRACT_HASH}"
        )
    config_digest = _require_sha256(
        manifest.config_hash, field="config_hash", kind=ArtifactKind.CONFIG
    )
    if config_digest != PINNED_RIDGE_SPEC_HASH:
        raise ArtifactConfigMismatchError(
            f"{_kind_label(ArtifactKind.CONFIG)} hash mismatch: "
            f"got {config_digest}, expected {PINNED_RIDGE_SPEC_HASH}"
        )
    splits_digest = _require_sha256(
        manifest.splits_config_hash, field="splits_config_hash", kind=ArtifactKind.CONFIG
    )
    if splits_digest != PINNED_SPLITS_CONFIG_HASH:
        raise ArtifactConfigMismatchError(
            f"{_kind_label(ArtifactKind.CONFIG)} splits hash mismatch: "
            f"got {splits_digest}, expected {PINNED_SPLITS_CONFIG_HASH}"
        )
    _require_sha256(manifest.data_hash, field="data_hash", kind=ArtifactKind.DATA)
    _require_sha256(manifest.code_hash, field="code_hash", kind=ArtifactKind.CODE)
    _require_code_commit(manifest.code_commit)
    _require_reason(manifest.code_commit_reason)
    verify_optional_calibration_payload(
        {
            "bootstrap": manifest.bootstrap,
            "calibrated": manifest.calibrated,
            "calibration": manifest.calibration,
        }
    )


def load_artifact(payload_path: Path) -> LoadedArtifact:
    """Load a JSON artifact after checksum, spec, schema, and feature-order checks.

    Never calls ``joblib.load`` / pickle. Invalid UTF-8 or JSON fails closed.
    """
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
    _verify_manifest(manifest, blob)
    try:
        loaded = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UntrustedArtifactError(
            f"artifact payload is not valid JSON (refusing execution): {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise UntrustedArtifactError("artifact payload must be a JSON object")
    predictor = predictor_from_mapping(loaded)
    hash_fields = (
        ("code_hash", manifest.code_hash, ArtifactKind.CODE),
        ("config_hash", manifest.config_hash, ArtifactKind.CONFIG),
        ("contract_hash", manifest.contract_hash, ArtifactKind.CONTRACT),
        ("data_hash", manifest.data_hash, ArtifactKind.DATA),
        ("feature_spec_hash", manifest.feature_spec_hash, ArtifactKind.SPEC),
        ("splits_config_hash", manifest.splits_config_hash, ArtifactKind.CONFIG),
    )
    for field, expected, kind in hash_fields:
        got = str(loaded.get(field, ""))
        if got != expected:
            raise ArtifactSpecMismatchError(
                f"{_kind_label(kind)} hash mismatch inside payload field {field}"
            )
    if str(loaded.get("code_commit", "")) != manifest.code_commit:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.CODE)} commit mismatch inside payload"
        )
    verify_optional_calibration_payload(loaded)
    oof = loaded.get("oof_predictions", [])
    if oof is None:
        oof = []
    if not isinstance(oof, list):
        raise UntrustedArtifactError("oof_predictions must be a list")
    exclusions = loaded.get("oof_exclusions", [])
    if exclusions is None:
        exclusions = []
    if not isinstance(exclusions, list):
        raise UntrustedArtifactError("oof_exclusions must be a list")
    calibrated = loaded.get("calibrated", False) is True
    return LoadedArtifact(
        payload=loaded,
        predictor=predictor,
        manifest=manifest,
        payload_path=target,
        manifest_path=side,
        oof_predictions=tuple(dict(item) for item in oof if isinstance(item, dict)),
        oof_exclusions=tuple(
            dict(item) for item in exclusions if isinstance(item, dict)
        ),
        calibrated=calibrated,
    )
