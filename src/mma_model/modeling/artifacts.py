"""Versioned model artifacts with sidecar manifests (DWCS-303).

Production loading never trusts a bare pickle. A sidecar JSON manifest must
carry schema version, hashes, feature order, train IDs, cutoff policy, metrics,
and the SHA-256 of the payload bytes. Checksum, feature-order, or spec mismatch
fails with a typed error.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Never

import joblib
import yaml

from mma_model.backtest.contract import PINNED_FEATURE_SPEC_HASH, PINNED_SPLITS_CONFIG_HASH
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, compute_contract_hash
from mma_model.features.spec import FEATURE_NAMES, SPEC_VERSION, spec_hash
from mma_model.quality.schema import sha256_canonical

RIDGE_SPEC_FILENAME: Final = "ridge_v1.yaml"
ARTIFACT_SCHEMA_VERSION: Final = "dwcs_artifact_v1"
RIDGE_SPEC_ID: Final = "ridge_v1"
RIDGE_CONTRACT_ID: Final = "dwcs_model_spec"
EXPECTED_RIDGE_SCHEMA_VERSION: Final = 1
EXPECTED_RIDGE_SPEC_VERSION: Final = "1.0.0"
EXPECTED_MODEL_ID: Final = "M1"
EXPECTED_CUTOFF_POLICY: Final = "scheduled_minus_60m"
EXPECTED_FINAL_REFIT: Final = "development_and_validation"
# Canonical JSON digest of packaged ridge_v1.yaml. Update only with spec_version.
PINNED_RIDGE_SPEC_HASH: Final = (
    "cf4a679519e5fccda46176e40be253525afe4951cfbaa7d91cbd92b0e724d61a"
)


SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")


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
    """Bare pickle / joblib without a verified sidecar manifest."""


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


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def manifest_path_for(payload_path: Path) -> Path:
    """Sidecar path: ``ridge_v1.joblib`` → ``ridge_v1.manifest.json``."""
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
    feature_names: tuple[str, ...]
    train_sample_ids: tuple[str, ...]
    max_train_timestamp: str | None
    cutoff_policy: str
    metrics: dict[str, Any]
    payload_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
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
        feature_names=tuple(names),
        train_sample_ids=tuple(sample_ids),
        max_train_timestamp=max_ts,
        cutoff_policy=str(payload.get("cutoff_policy", "")),
        metrics=dict(metrics),
        payload_sha256=str(payload.get("payload_sha256", "")),
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
    manifest: ArtifactManifest
    payload_path: Path
    manifest_path: Path


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def save_artifact(
    payload: Mapping[str, Any],
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
    model_id: str = EXPECTED_MODEL_ID,
    spec_id: str = RIDGE_SPEC_ID,
) -> SavedArtifact:
    """Serialize ``payload`` with joblib and write a verified sidecar manifest."""
    feature_names = tuple(str(name) for name in payload.get("feature_names", ()))
    if feature_names != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch: "
            "payload feature_names must equal the live FEATURE_NAMES order"
        )
    live_spec = spec_hash()
    if live_spec != PINNED_FEATURE_SPEC_HASH:
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch: "
            f"got {live_spec}, expected {PINNED_FEATURE_SPEC_HASH}"
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

    stored = dict(payload)
    stored["code_hash"] = code_digest
    stored["config_hash"] = config_digest
    stored["contract_hash"] = contract_digest
    stored["data_hash"] = data_digest
    stored["feature_names"] = list(FEATURE_NAMES)
    stored["feature_spec_hash"] = live_spec
    stored["spec_hash"] = live_spec
    stored["splits_config_hash"] = splits_digest

    target = Path(payload_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    joblib.dump(stored, buffer)
    blob = buffer.getvalue()
    target.write_bytes(blob)
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
    if manifest.schema_version != ARTIFACT_SCHEMA_VERSION:
        raise UntrustedArtifactError(
            f"unknown artifact schema {manifest.schema_version!r}; "
            f"expected {ARTIFACT_SCHEMA_VERSION}"
        )
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


def load_artifact(payload_path: Path) -> LoadedArtifact:
    """Load a versioned artifact after checksum, spec, and feature-order checks."""
    target = Path(payload_path)
    side = manifest_path_for(target)
    if not side.is_file():
        raise UntrustedArtifactError(
            f"refusing bare untrusted pickle {target}; missing sidecar {side.name}"
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
    loaded = joblib.load(io.BytesIO(blob))
    if not isinstance(loaded, dict):
        raise UntrustedArtifactError("artifact payload must be a mapping")
    payload_names = tuple(str(name) for name in loaded.get("feature_names", ()))
    if payload_names != FEATURE_NAMES:
        raise ArtifactFeatureOrderMismatchError(
            f"{_kind_label(ArtifactKind.FEATURE_ORDER)} mismatch inside payload"
        )
    payload_spec = str(loaded.get("spec_hash", "") or loaded.get("feature_spec_hash", ""))
    if payload_spec != spec_hash():
        raise ArtifactSpecMismatchError(
            f"{_kind_label(ArtifactKind.SPEC)} hash mismatch inside payload"
        )
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
    return LoadedArtifact(
        payload=loaded,
        manifest=manifest,
        payload_path=target,
        manifest_path=side,
    )
