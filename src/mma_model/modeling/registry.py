"""Champion/challenger model registry and fixed-spec retrain (DWCS-402).

Retrain expands data under the frozen champion specification only. New
spec_id / config_hash values stay shadow. Failed retrains leave the
incumbent pointer and digest unchanged.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Final

import yaml
from sqlalchemy.orm import Session

from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract
from mma_model.modeling.artifacts import (
    PINNED_RIDGE_SPEC_HASH,
    RIDGE_SPEC_ID,
    ArtifactError,
    UntrustedArtifactError,
    load_artifact,
    load_ridge_spec,
)
from mma_model.modeling.baselines import TrainReport, run_protocol_train
from mma_model.modeling.promotion import (
    DecisionAction,
    HealthGateFn,
    Lane,
    PromotionError,
    PromotionEvaluateRequiredError,
    PromotionGateError,
    append_decision,
    evaluate_activation_gates,
    resolve_artifact_path,
)
from mma_model.modeling.splits import HoldoutLockedError
from mma_model.quality.models import CoverageReport, GateResult
from mma_model.quality.schema import sha256_canonical

REGISTRY_FILENAME: Final = "model_registry.yaml"
REGISTRY_ID: Final = "dwcs_model_registry"
EXPECTED_SCHEMA_VERSION: Final = 1
EXPECTED_REGISTRY_VERSION: Final = "1.0.0"
# Identity digest over schema/registry id/version/ticket/champion
# spec+config+lane (mutable digests excluded). Update with registry_version.
PINNED_REGISTRY_HASH: Final = (
    "89fda8803a1fa9da1e24860f598b67d1d73450cd52113e7cb39809b7000dcd8e"
)


class RegistryError(ValueError):
    """Model registry load/save/identity failure."""


class RegistryHashMismatch(RegistryError):
    """Registry content hash did not match the pinned digest."""


@dataclass(frozen=True)
class RegistryEntry:
    spec_id: str
    artifact_digest: str | None
    config_hash: str
    lane: Lane
    artifact_relpath: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_digest": self.artifact_digest,
            "artifact_relpath": self.artifact_relpath,
            "config_hash": self.config_hash,
            "lane": self.lane.value,
            "spec_id": self.spec_id,
        }

    def copy_as_prior(self) -> RegistryEntry:
        return replace(self, lane=Lane.NONE)

    def promote(
        self,
        *,
        spec_id: str,
        artifact_digest: str,
        config_hash: str,
        artifact_relpath: str | None,
    ) -> RegistryEntry:
        return RegistryEntry(
            spec_id=spec_id,
            artifact_digest=artifact_digest,
            config_hash=config_hash,
            lane=Lane.CHAMPION,
            artifact_relpath=artifact_relpath,
        )


@dataclass
class ModelRegistryState:
    schema_version: int
    registry_id: str
    registry_version: str
    ticket: str
    description: str
    champion: RegistryEntry
    prior_champion: RegistryEntry | None = None
    challengers: tuple[RegistryEntry, ...] = ()
    content_hash: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "champion": self.champion.to_dict(),
            "challengers": [row.to_dict() for row in self.challengers],
            "description": self.description,
            "prior_champion": (
                None if self.prior_champion is None else self.prior_champion.to_dict()
            ),
            "registry_id": self.registry_id,
            "registry_version": self.registry_version,
            "schema_version": self.schema_version,
            "ticket": self.ticket,
        }
        payload.update(self.extra)
        return payload


def compute_registry_hash(payload: Mapping[str, Any]) -> str:
    # Hash identity fields only (exclude mutable champion digests/paths).
    identity = {
        "description": payload.get("description"),
        "registry_id": payload.get("registry_id"),
        "registry_version": payload.get("registry_version"),
        "schema_version": payload.get("schema_version"),
        "ticket": payload.get("ticket"),
        "champion_spec_id": (payload.get("champion") or {}).get("spec_id")
        if isinstance(payload.get("champion"), Mapping)
        else None,
        "champion_config_hash": (payload.get("champion") or {}).get("config_hash")
        if isinstance(payload.get("champion"), Mapping)
        else None,
        "champion_lane": (payload.get("champion") or {}).get("lane")
        if isinstance(payload.get("champion"), Mapping)
        else None,
    }
    return sha256_canonical(identity)


def package_registry_path() -> Path:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(REGISTRY_FILENAME)
    with resources.as_file(resource) as path:
        return Path(path)


def visible_registry_path(*, root: Path | None = None) -> Path:
    if root is None:
        root = Path(__file__).resolve().parents[3]
    return root / "config" / REGISTRY_FILENAME


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"unable to read model registry at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RegistryError("model registry root must be a mapping")
    return loaded


def _read_package_registry_payload() -> dict[str, Any]:
    root = resources.files("mma_model.modeling")
    resource = root.joinpath(REGISTRY_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise RegistryError(f"unable to read packaged registry {REGISTRY_FILENAME}") from exc
    loaded = yaml.safe_load(raw)
    if not isinstance(loaded, dict):
        raise RegistryError("packaged model registry root must be a mapping")
    return loaded


def _parse_entry(raw: Mapping[str, Any] | None, *, default_lane: Lane) -> RegistryEntry | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RegistryError("registry entry must be a mapping")
    lane_raw = str(raw.get("lane") or default_lane.value)
    try:
        lane = Lane(lane_raw)
    except ValueError as exc:
        raise RegistryError(f"unknown registry lane {lane_raw!r}") from exc
    digest = raw.get("artifact_digest")
    digest_text = None if digest in (None, "") else str(digest).strip().lower()
    config_hash = str(raw.get("config_hash") or "").strip().lower()
    if len(config_hash) != 64:
        raise RegistryError("config_hash must be a 64-char sha256 hex digest")
    spec_id = str(raw.get("spec_id") or "").strip()
    if not spec_id:
        raise RegistryError("spec_id must be non-empty")
    rel = raw.get("artifact_relpath")
    return RegistryEntry(
        spec_id=spec_id,
        artifact_digest=digest_text,
        config_hash=config_hash,
        lane=lane,
        artifact_relpath=None if rel in (None, "") else str(rel),
    )


def parse_model_registry(
    payload: Mapping[str, Any],
    *,
    enforce_pinned_digest: bool = True,
) -> ModelRegistryState:
    if payload.get("registry_id") != REGISTRY_ID:
        raise RegistryError(
            f"registry_id mismatch: got {payload.get('registry_id')!r}, expected {REGISTRY_ID!r}"
        )
    if payload.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise RegistryError(f"schema_version mismatch: got {payload.get('schema_version')!r}")
    if payload.get("registry_version") != EXPECTED_REGISTRY_VERSION:
        raise RegistryError(
            f"registry_version mismatch: got {payload.get('registry_version')!r}"
        )
    champion_raw = payload.get("champion")
    if not isinstance(champion_raw, Mapping):
        raise RegistryError("champion must be a mapping")
    champion = _parse_entry(champion_raw, default_lane=Lane.CHAMPION)
    if champion is None:
        raise RegistryError("champion entry is required")
    if champion.lane is not Lane.CHAMPION:
        raise RegistryError("champion.lane must be champion")
    prior_raw = payload.get("prior_champion")
    prior = _parse_entry(
        prior_raw if isinstance(prior_raw, Mapping) else None,
        default_lane=Lane.NONE,
    )
    challengers_raw = payload.get("challengers") or []
    if not isinstance(challengers_raw, list):
        raise RegistryError("challengers must be a list")
    challengers: list[RegistryEntry] = []
    for item in challengers_raw:
        if not isinstance(item, Mapping):
            raise RegistryError("challenger entries must be mappings")
        entry = _parse_entry(item, default_lane=Lane.SHADOW)
        if entry is None:
            continue
        if entry.lane is not Lane.SHADOW:
            raise RegistryError("challenger.lane must be shadow")
        challengers.append(entry)
    content_hash = compute_registry_hash(payload)
    if enforce_pinned_digest and content_hash != PINNED_REGISTRY_HASH:
        raise RegistryHashMismatch(
            f"registry hash mismatch: got {content_hash}, expected {PINNED_REGISTRY_HASH}"
        )
    known = {
        "champion",
        "challengers",
        "description",
        "prior_champion",
        "registry_id",
        "registry_version",
        "schema_version",
        "ticket",
    }
    extra = {key: value for key, value in payload.items() if key not in known}
    return ModelRegistryState(
        schema_version=int(payload["schema_version"]),
        registry_id=str(payload["registry_id"]),
        registry_version=str(payload["registry_version"]),
        ticket=str(payload.get("ticket") or "DWCS-402"),
        description=str(payload.get("description") or ""),
        champion=champion,
        prior_champion=prior,
        challengers=tuple(challengers),
        content_hash=content_hash,
        extra=extra,
    )


def load_model_registry(
    *,
    path: Path | None = None,
    enforce_pinned_digest: bool = True,
) -> ModelRegistryState:
    payload = (
        _read_yaml_mapping(Path(path)) if path is not None else _read_package_registry_payload()
    )
    return parse_model_registry(payload, enforce_pinned_digest=enforce_pinned_digest)


def save_model_registry(state: ModelRegistryState, *, path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    text = yaml.safe_dump(payload, sort_keys=True, default_flow_style=False)
    target.write_text(text, encoding="utf-8")


def upsert_challenger(
    state: ModelRegistryState,
    *,
    spec_id: str,
    artifact_digest: str,
    config_hash: str,
    artifact_relpath: str | None = None,
) -> ModelRegistryState:
    entry = RegistryEntry(
        spec_id=spec_id,
        artifact_digest=artifact_digest,
        config_hash=config_hash,
        lane=Lane.SHADOW,
        artifact_relpath=artifact_relpath,
    )
    remaining = tuple(
        row for row in state.challengers if row.artifact_digest != artifact_digest
    )
    state.challengers = remaining + (entry,)
    return state


def register_shadow_challenger(
    session: Session,
    *,
    registry_path: Path,
    spec_id: str,
    artifact_digest: str,
    config_hash: str,
    artifact_relpath: str | None = None,
    reason: str,
    actor: str = "system",
) -> ModelRegistryState:
    """Register a different-spec/config artifact as shadow only (never champion)."""
    state = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    if (
        spec_id == state.champion.spec_id
        and config_hash == state.champion.config_hash
    ):
        # Same frozen spec may still be recorded as a refresh candidate.
        pass
    upsert_challenger(
        state,
        spec_id=spec_id,
        artifact_digest=artifact_digest,
        config_hash=config_hash,
        artifact_relpath=artifact_relpath,
    )
    save_model_registry(state, path=registry_path)
    append_decision(
        session,
        action=DecisionAction.RETRAIN,
        reason=reason,
        lane=Lane.SHADOW,
        artifact_digest=artifact_digest,
        config_hash=config_hash,
        prior_champion_digest=state.champion.artifact_digest,
        evaluator_hash=PINNED_CONTRACT_HASH,
        health_ok=None,
        gates={"lane": "shadow", "auto_promote": False},
        actor=actor,
    )
    session.flush()
    return state


def store_artifact_by_digest(
    *,
    artifacts_dir: Path,
    payload_path: Path,
) -> tuple[str, Path]:
    """Copy a validated JSON artifact into the digest-addressed store."""
    loaded = load_artifact(Path(payload_path))
    digest = loaded.manifest.payload_sha256
    store = Path(artifacts_dir)
    store.mkdir(parents=True, exist_ok=True)
    dest = store / f"{digest}.json"
    side_src = loaded.manifest_path
    side_dest = store / f"{digest}.manifest.json"
    if Path(payload_path).resolve() != dest.resolve():
        shutil.copy2(payload_path, dest)
    if side_src.is_file() and side_src.resolve() != side_dest.resolve():
        shutil.copy2(side_src, side_dest)
    # Re-verify stored bytes.
    load_artifact(dest)
    return digest, dest


TrainRunner = Callable[..., TrainReport]


@dataclass(frozen=True)
class RetrainResult:
    status: str
    champion_unchanged: bool
    artifact_digest: str | None
    prior_champion_digest: str | None
    spec_id: str
    config_hash: str
    activated: bool
    lane: str
    reason: str
    decision_id: str | None = None
    gates: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activated": self.activated,
            "artifact_digest": self.artifact_digest,
            "champion_unchanged": self.champion_unchanged,
            "config_hash": self.config_hash,
            "decision_id": self.decision_id,
            "gates": dict(self.gates),
            "lane": self.lane,
            "prior_champion_digest": self.prior_champion_digest,
            "reason": self.reason,
            "spec_id": self.spec_id,
            "status": self.status,
        }


def _default_train_runner(
    *,
    output_path: Path,
    include_holdout: bool = False,
) -> TrainReport:
    if include_holdout:
        raise HoldoutLockedError("locked 2025 holdout cannot be used for ordinary retrain")
    spec = load_ridge_spec()
    contract = load_evaluation_contract()
    return run_protocol_train(
        spec=spec,
        output_path=output_path,
        include_holdout=False,
        contract=contract,
    )


def retrain_fixed_spec(
    session: Session,
    *,
    registry_path: Path,
    artifacts_dir: Path,
    actor: str = "system",
    train_runner: TrainRunner | None = None,
    coverage_report: CoverageReport | None = None,
    health_result: GateResult | None = None,
    health_gate: HealthGateFn | None = None,
    backtest_ok: bool | None = None,
    calibration_ok: bool | None = None,
    include_holdout: bool = False,
    at: datetime | None = None,
) -> RetrainResult:
    """Refit the frozen champion spec; activate only if same-spec and gates pass.

    Different spec_id/config_hash results are registered shadow-only and never
    auto-promoted. Failures leave the incumbent champion digest unchanged.
    """
    when = at if at is not None else datetime.now(UTC)
    state = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    incumbent = state.champion.artifact_digest
    incumbent_spec = state.champion.spec_id
    incumbent_config = state.champion.config_hash

    if include_holdout:
        row = append_decision(
            session,
            action=DecisionAction.REJECT,
            reason="locked 2025 holdout cannot be used for ordinary retrain",
            lane=Lane.CHAMPION,
            artifact_digest=incumbent,
            config_hash=incumbent_config,
            prior_champion_digest=incumbent,
            evaluator_hash=PINNED_CONTRACT_HASH,
            health_ok=False,
            gates={"holdout": "blocked"},
            actor=actor,
            at=when,
        )
        session.flush()
        return RetrainResult(
            status="failed",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.CHAMPION.value,
            reason="locked 2025 holdout cannot be used for ordinary retrain",
            decision_id=row.id,
            gates={"holdout": "blocked"},
        )

    artifacts_dir = Path(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    scratch = artifacts_dir / f"retrain-{when.strftime('%Y%m%dT%H%M%S')}.json"
    runner = train_runner if train_runner is not None else _default_train_runner

    try:
        report = runner(output_path=scratch, include_holdout=False)
    except Exception as exc:  # noqa: BLE001 — any train failure must leave champion
        row = append_decision(
            session,
            action=DecisionAction.REJECT,
            reason=f"retrain failed: {exc}",
            lane=Lane.CHAMPION,
            artifact_digest=incumbent,
            config_hash=incumbent_config,
            prior_champion_digest=incumbent,
            evaluator_hash=PINNED_CONTRACT_HASH,
            health_ok=False,
            gates={"error": str(exc)},
            actor=actor,
            at=when,
        )
        session.flush()
        return RetrainResult(
            status="failed",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.CHAMPION.value,
            reason=f"retrain failed: {exc}",
            decision_id=row.id,
            gates={"error": str(exc)},
        )

    payload_path = (
        report.artifact.payload_path
        if isinstance(report, TrainReport)
        else scratch
    )
    try:
        digest, stored = store_artifact_by_digest(
            artifacts_dir=artifacts_dir,
            payload_path=Path(payload_path),
        )
        loaded = load_artifact(stored)
    except (ArtifactError, UntrustedArtifactError, OSError) as store_exc:
        row = append_decision(
            session,
            action=DecisionAction.REJECT,
            reason=f"artifact validation failed: {store_exc}",
            lane=Lane.CHAMPION,
            artifact_digest=incumbent,
            config_hash=incumbent_config,
            prior_champion_digest=incumbent,
            evaluator_hash=PINNED_CONTRACT_HASH,
            health_ok=False,
            gates={"error": str(store_exc)},
            actor=actor,
            at=when,
        )
        session.flush()
        return RetrainResult(
            status="failed",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.CHAMPION.value,
            reason=f"artifact validation failed: {store_exc}",
            decision_id=row.id,
            gates={"error": str(store_exc)},
        )

    new_spec = loaded.manifest.spec_id
    new_config = loaded.manifest.config_hash
    same_spec = new_spec == incumbent_spec and new_config == incumbent_config

    if not same_spec:
        register_shadow_challenger(
            session,
            registry_path=registry_path,
            spec_id=new_spec,
            artifact_digest=digest,
            config_hash=new_config,
            artifact_relpath=str(stored),
            reason="different spec/config registered shadow-only; no auto-promote",
            actor=actor,
        )
        return RetrainResult(
            status="shadow",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.SHADOW.value,
            reason="different spec/config registered shadow-only; no auto-promote",
            gates={"candidate_digest": digest, "candidate_spec_id": new_spec},
        )

    try:
        verdict = evaluate_activation_gates(
            stored,
            coverage_report=coverage_report,
            health_result=health_result,
            health_gate=health_gate,
            backtest_ok=backtest_ok,
            calibration_ok=calibration_ok,
        )
    except PromotionGateError as exc:
        append_decision(
            session,
            action=DecisionAction.REJECT,
            reason=str(exc),
            lane=Lane.CHAMPION,
            artifact_digest=digest,
            config_hash=new_config,
            prior_champion_digest=incumbent,
            evaluator_hash=PINNED_CONTRACT_HASH,
            health_ok=False,
            gates={"error": str(exc)},
            actor=actor,
            at=when,
        )
        session.flush()
        return RetrainResult(
            status="failed",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.CHAMPION.value,
            reason=str(exc),
            gates={"error": str(exc)},
        )

    if not verdict.ok:
        row = append_decision(
            session,
            action=DecisionAction.REJECT,
            reason="same-spec retrain failed activation gates; champion unchanged",
            lane=Lane.CHAMPION,
            artifact_digest=digest,
            config_hash=new_config,
            prior_champion_digest=incumbent,
            evaluator_hash=verdict.evaluator_hash,
            health_ok=verdict.health_ok,
            gates=verdict.to_dict(),
            actor=actor,
            at=when,
        )
        session.flush()
        return RetrainResult(
            status="failed",
            champion_unchanged=True,
            artifact_digest=incumbent,
            prior_champion_digest=incumbent,
            spec_id=incumbent_spec,
            config_hash=incumbent_config,
            activated=False,
            lane=Lane.CHAMPION.value,
            reason="same-spec retrain failed activation gates; champion unchanged",
            decision_id=row.id,
            gates=verdict.to_dict(),
        )

    # Same frozen spec + gates pass → activate as champion refresh.
    state = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    state.prior_champion = state.champion.copy_as_prior()
    state.champion = state.champion.promote(
        spec_id=new_spec,
        artifact_digest=digest,
        config_hash=new_config,
        artifact_relpath=str(stored),
    )
    save_model_registry(state, path=registry_path)
    row = append_decision(
        session,
        action=DecisionAction.RETRAIN,
        reason="same-spec data-expansion retrain activated after gates",
        lane=Lane.CHAMPION,
        artifact_digest=digest,
        config_hash=new_config,
        prior_champion_digest=incumbent,
        evaluator_hash=verdict.evaluator_hash,
        health_ok=verdict.health_ok,
        gates=verdict.to_dict(),
        actor=actor,
        at=when,
    )
    session.flush()
    return RetrainResult(
        status="success",
        champion_unchanged=False,
        artifact_digest=digest,
        prior_champion_digest=incumbent,
        spec_id=new_spec,
        config_hash=new_config,
        activated=True,
        lane=Lane.CHAMPION.value,
        reason="same-spec data-expansion retrain activated after gates",
        decision_id=row.id,
        gates=verdict.to_dict(),
    )


def promote_candidate(
    session: Session,
    *,
    registry_path: Path,
    candidate_digest: str,
    evaluate: bool,
    artifacts_dir: Path,
    reason: str,
    actor: str = "cli",
    artifact_path: Path | None = None,
    coverage_report: CoverageReport | None = None,
    health_result: GateResult | None = None,
    health_gate: HealthGateFn | None = None,
    backtest_ok: bool | None = None,
    calibration_ok: bool | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Promote a registered candidate only when all gates pass.

    ``evaluate`` must be True. There is no force/bypass flag.
    """
    if evaluate is not True:
        raise PromotionEvaluateRequiredError(
            "promotion requires --evaluate; cannot bypass holdout/health gates"
        )
    digest = str(candidate_digest or "").strip().lower()
    if len(digest) != 64:
        raise PromotionError("candidate must be a 64-char sha256 hex digest")
    state = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    prior = state.champion.artifact_digest
    path = resolve_artifact_path(
        artifacts_dir=artifacts_dir,
        digest=digest,
        explicit_path=artifact_path,
    )
    try:
        verdict = evaluate_activation_gates(
            path,
            coverage_report=coverage_report,
            health_result=health_result,
            health_gate=health_gate,
            backtest_ok=backtest_ok,
            calibration_ok=calibration_ok,
        )
    except PromotionGateError as exc:
        append_decision(
            session,
            action=DecisionAction.REJECT,
            reason=str(exc),
            lane=Lane.SHADOW,
            artifact_digest=digest,
            prior_champion_digest=prior,
            evaluator_hash=PINNED_CONTRACT_HASH,
            health_ok=False,
            gates={"error": str(exc)},
            actor=actor,
            at=at,
        )
        session.flush()
        raise

    if not verdict.ok:
        append_decision(
            session,
            action=DecisionAction.REJECT,
            reason="activation gates failed",
            lane=Lane.SHADOW,
            artifact_digest=digest,
            config_hash=str(verdict.details.get("artifact", {}).get("config_hash") or "")
            or None,
            prior_champion_digest=prior,
            evaluator_hash=verdict.evaluator_hash,
            health_ok=verdict.health_ok,
            gates=verdict.to_dict(),
            actor=actor,
            at=at,
        )
        session.flush()
        raise PromotionGateError("activation gates failed; champion unchanged")

    loaded = load_artifact(path)
    config_hash = loaded.manifest.config_hash
    spec_id = loaded.manifest.spec_id
    upsert_challenger(
        state,
        spec_id=spec_id,
        artifact_digest=digest,
        config_hash=config_hash,
        artifact_relpath=str(path),
    )
    state.prior_champion = state.champion.copy_as_prior()
    state.champion = state.champion.promote(
        spec_id=spec_id,
        artifact_digest=digest,
        config_hash=config_hash,
        artifact_relpath=str(path),
    )
    state.challengers = tuple(
        row for row in state.challengers if row.artifact_digest != digest
    )
    save_model_registry(state, path=registry_path)
    row = append_decision(
        session,
        action=DecisionAction.PROMOTE,
        reason=reason,
        lane=Lane.CHAMPION,
        artifact_digest=digest,
        config_hash=config_hash,
        prior_champion_digest=prior,
        evaluator_hash=verdict.evaluator_hash,
        health_ok=verdict.health_ok,
        gates=verdict.to_dict(),
        actor=actor,
        at=at,
    )
    session.flush()
    return {
        "action": DecisionAction.PROMOTE.value,
        "artifact_digest": digest,
        "at": row.at.isoformat(),
        "champion_unchanged": False,
        "config_hash": config_hash,
        "decision_id": row.id,
        "prior_champion_digest": prior,
        "reason": reason,
        "spec_id": spec_id,
    }


def rollback_champion(
    session: Session,
    *,
    registry_path: Path,
    reason: str,
    actor: str = "cli",
    at: datetime | None = None,
) -> dict[str, Any]:
    """Restore prior champion digest without rolling back other DB tables."""
    state = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    prior = state.prior_champion
    if prior is None or not prior.artifact_digest:
        raise PromotionError("no prior champion digest retained for rollback")
    current = state.champion.artifact_digest
    restored = prior.artifact_digest
    state.prior_champion = state.champion.copy_as_prior()
    state.champion = state.champion.promote(
        spec_id=str(prior.spec_id or state.champion.spec_id),
        artifact_digest=str(restored),
        config_hash=str(prior.config_hash or state.champion.config_hash),
        artifact_relpath=prior.artifact_relpath,
    )
    save_model_registry(state, path=registry_path)
    row = append_decision(
        session,
        action=DecisionAction.ROLLBACK,
        reason=reason,
        lane=Lane.CHAMPION,
        artifact_digest=restored,
        config_hash=state.champion.config_hash,
        prior_champion_digest=current,
        evaluator_hash=PINNED_CONTRACT_HASH,
        health_ok=None,
        gates={"restored_digest": restored, "replaced_digest": current},
        actor=actor,
        at=at,
    )
    session.flush()
    return {
        "action": DecisionAction.ROLLBACK.value,
        "artifact_digest": restored,
        "at": row.at.isoformat(),
        "champion_unchanged": False,
        "decision_id": row.id,
        "prior_champion_digest": current,
        "reason": reason,
    }


def write_registry_document(
    path: Path,
    *,
    champion_digest: str | None,
    champion_config_hash: str = PINNED_RIDGE_SPEC_HASH,
    champion_spec_id: str = RIDGE_SPEC_ID,
    prior_digest: str | None = None,
    challengers: Sequence[Mapping[str, Any]] = (),
    artifact_relpath: str | None = None,
) -> ModelRegistryState:
    """Helper for tests: write a mutable registry YAML under tmp_path."""
    payload: dict[str, Any] = {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "registry_id": REGISTRY_ID,
        "registry_version": EXPECTED_REGISTRY_VERSION,
        "ticket": "DWCS-402",
        "description": "Test registry for fixed-spec retrain and promotion.",
        "champion": {
            "spec_id": champion_spec_id,
            "artifact_digest": champion_digest,
            "config_hash": champion_config_hash,
            "lane": Lane.CHAMPION.value,
            "artifact_relpath": artifact_relpath,
        },
        "prior_champion": (
            None
            if prior_digest is None
            else {
                "spec_id": champion_spec_id,
                "artifact_digest": prior_digest,
                "config_hash": champion_config_hash,
                "lane": Lane.NONE.value,
                "artifact_relpath": None,
            }
        ),
        "challengers": [dict(item) for item in challengers],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    return load_model_registry(path=path, enforce_pinned_digest=False)


__all__ = [
    "EXPECTED_REGISTRY_VERSION",
    "EXPECTED_SCHEMA_VERSION",
    "PINNED_REGISTRY_HASH",
    "REGISTRY_FILENAME",
    "REGISTRY_ID",
    "ModelRegistryState",
    "RegistryEntry",
    "RegistryError",
    "RegistryHashMismatch",
    "RetrainResult",
    "TrainRunner",
    "compute_registry_hash",
    "load_model_registry",
    "package_registry_path",
    "parse_model_registry",
    "promote_candidate",
    "register_shadow_challenger",
    "retrain_fixed_spec",
    "rollback_champion",
    "save_model_registry",
    "store_artifact_by_digest",
    "upsert_challenger",
    "visible_registry_path",
    "write_registry_document",
]
