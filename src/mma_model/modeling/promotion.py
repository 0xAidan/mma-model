"""Champion promotion and rollback gates (DWCS-402).

Promotion cannot bypass frozen evaluator, strict health, holdout, or artifact
checks. There is no ``--force`` path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Never

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.backtest.gates import HOLDOUT_YEAR, assert_holdout_not_in_train
from mma_model.db.tables.model_registry import ModelRegistryDecision
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH
from mma_model.modeling.artifacts import (
    ArtifactError,
    LoadedArtifact,
    UntrustedArtifactError,
    load_artifact,
)
from mma_model.quality.gates import evaluate_strict_gates
from mma_model.quality.models import CoverageReport, GateResult

SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")


class PromotionError(ValueError):
    """Promotion or rollback failed closed."""


class PromotionGateError(PromotionError):
    """A required activation gate failed."""


class PromotionEvaluateRequiredError(PromotionError):
    """Promote was called without ``evaluate=True``."""


class Lane(StrEnum):
    CHAMPION = "champion"
    SHADOW = "shadow"
    NONE = "none"


class DecisionAction(StrEnum):
    RETRAIN = "retrain"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    REJECT = "reject"


@dataclass(frozen=True)
class GateVerdict:
    ok: bool
    health_ok: bool
    evaluator_hash: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "details": dict(self.details),
            "evaluator_hash": self.evaluator_hash,
            "health_ok": self.health_ok,
            "ok": self.ok,
        }


HealthGateFn = Callable[..., GateVerdict]
TrainFn = Callable[..., Any]


def _require_digest(value: object, *, field: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA256_HEX.fullmatch(text):
        raise PromotionError(f"{field} must be a 64-char sha256 hex digest")
    return text


def next_decision_seq(session: Session) -> int:
    current = session.scalar(select(func.coalesce(func.max(ModelRegistryDecision.seq), 0)))
    return int(current or 0) + 1


def append_decision(
    session: Session,
    *,
    action: DecisionAction,
    reason: str,
    lane: Lane = Lane.NONE,
    artifact_digest: str | None = None,
    config_hash: str | None = None,
    prior_champion_digest: str | None = None,
    evaluator_hash: str | None = None,
    health_ok: bool | None = None,
    gates: Mapping[str, Any] | None = None,
    actor: str = "system",
    at: datetime | None = None,
) -> ModelRegistryDecision:
    """Insert one append-only decision row. Never updates prior rows."""
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise PromotionError("decision reason must be non-empty")
    cleaned_actor = str(actor or "").strip() or "system"
    when = at if at is not None else datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    row = ModelRegistryDecision(
        at=when,
        action=action.value,
        lane=lane.value,
        artifact_digest=artifact_digest,
        config_hash=config_hash,
        prior_champion_digest=prior_champion_digest,
        reason=cleaned_reason,
        evaluator_hash=evaluator_hash,
        health_ok=health_ok,
        gates_json=None if gates is None else json.dumps(dict(gates), sort_keys=True),
        actor=cleaned_actor,
        seq=next_decision_seq(session),
    )
    session.add(row)
    session.flush()
    return row


def _holdout_ids_from_manifest(sample_ids: Sequence[str]) -> tuple[str, ...]:
    locked: list[str] = []
    year = str(HOLDOUT_YEAR)
    for sample_id in sample_ids:
        text = str(sample_id)
        if year in text or "holdout" in text.lower():
            locked.append(text)
    return tuple(locked)


def assert_artifact_activation_ready(loaded: LoadedArtifact) -> dict[str, Any]:
    """Checksum/schema/feature-order already verified by ``load_artifact``."""
    if loaded.manifest.contract_hash != PINNED_CONTRACT_HASH:
        raise PromotionGateError(
            "frozen evaluator hash mismatch: "
            f"got {loaded.manifest.contract_hash}, expected {PINNED_CONTRACT_HASH}"
        )
    locked = _holdout_ids_from_manifest(loaded.manifest.train_sample_ids)
    if locked:
        raise PromotionGateError(
            "locked 2025 holdout samples present in ordinary artifact train ids"
        )
    # Season metadata may be absent; still refuse year-tagged event ids.
    event_ids = sorted({sid.split(":")[0] for sid in loaded.manifest.train_sample_ids if sid})
    assert_holdout_not_in_train(
        event_ids,
        holdout_seasons=(HOLDOUT_YEAR,),
        event_seasons={eid: HOLDOUT_YEAR for eid in event_ids if str(HOLDOUT_YEAR) in eid},
    )
    return {
        "artifact_digest": loaded.manifest.payload_sha256,
        "config_hash": loaded.manifest.config_hash,
        "contract_hash": loaded.manifest.contract_hash,
        "feature_spec_hash": loaded.manifest.feature_spec_hash,
        "schema_version": loaded.manifest.schema_version,
        "spec_id": loaded.manifest.spec_id,
        "train_sample_count": len(loaded.manifest.train_sample_ids),
    }


def default_health_gate(
    *,
    coverage_report: CoverageReport | None = None,
    health_result: GateResult | None = None,
) -> GateVerdict:
    """Fail closed unless a real coverage report or GateResult is supplied.

    There is no boolean shortcut and no CLI ``--health-ok`` / ``--force`` path.
    """
    details: dict[str, Any] = {"gate": "strict_health"}
    if health_result is not None:
        details["blocker_codes"] = list(health_result.blocker_codes)
        details["exit_code"] = health_result.exit_code
        details["source"] = "health_result"
        return GateVerdict(
            ok=bool(health_result.ok),
            health_ok=bool(health_result.ok),
            evaluator_hash=PINNED_CONTRACT_HASH,
            details=details,
        )
    if coverage_report is not None:
        result = evaluate_strict_gates(coverage_report)
        details["blocker_codes"] = list(result.blocker_codes)
        details["exit_code"] = result.exit_code
        details["source"] = "coverage_report"
        return GateVerdict(
            ok=bool(result.ok),
            health_ok=bool(result.ok),
            evaluator_hash=PINNED_CONTRACT_HASH,
            details=details,
        )
    details["reason"] = "health_evidence_required"
    return GateVerdict(
        ok=False,
        health_ok=False,
        evaluator_hash=PINNED_CONTRACT_HASH,
        details=details,
    )


def evaluate_activation_gates(
    artifact_path: Path,
    *,
    coverage_report: CoverageReport | None = None,
    health_result: GateResult | None = None,
    health_gate: HealthGateFn | None = None,
    backtest_ok: bool | None = None,
    calibration_ok: bool | None = None,
) -> GateVerdict:
    """Run artifact + frozen evaluator + health + backtest + calibration gates.

    Missing ``backtest_ok`` / ``calibration_ok`` fail closed. Health passes only
    via ``coverage_report``, ``health_result``, or an injected ``health_gate``.
    """
    try:
        loaded = load_artifact(Path(artifact_path))
    except (ArtifactError, UntrustedArtifactError, OSError) as exc:
        raise PromotionGateError(f"artifact gate failed: {exc}") from exc
    artifact_details = assert_artifact_activation_ready(loaded)

    runner = health_gate if health_gate is not None else default_health_gate
    health = runner(
        coverage_report=coverage_report,
        health_result=health_result,
    )
    details: dict[str, Any] = {
        "artifact": artifact_details,
        "backtest": {"ok": backtest_ok},
        "calibration": {
            "ok": calibration_ok,
            "calibrated": loaded.calibrated,
        },
        "health": health.to_dict(),
    }
    if backtest_ok is not True:
        details["backtest"]["reason"] = (
            "backtest_evidence_required"
            if backtest_ok is None
            else "backtest_gate_failed"
        )
        return GateVerdict(
            ok=False,
            health_ok=health.health_ok,
            evaluator_hash=PINNED_CONTRACT_HASH,
            details=details,
        )
    if calibration_ok is not True:
        details["calibration"]["reason"] = (
            "calibration_evidence_required"
            if calibration_ok is None
            else "calibration_gate_failed"
        )
        return GateVerdict(
            ok=False,
            health_ok=health.health_ok,
            evaluator_hash=PINNED_CONTRACT_HASH,
            details=details,
        )
    if not health.ok:
        return GateVerdict(
            ok=False,
            health_ok=health.health_ok,
            evaluator_hash=PINNED_CONTRACT_HASH,
            details=details,
        )
    return GateVerdict(
        ok=True,
        health_ok=health.health_ok,
        evaluator_hash=PINNED_CONTRACT_HASH,
        details=details,
    )


def resolve_artifact_path(
    *,
    artifacts_dir: Path,
    digest: str,
    explicit_path: Path | None = None,
) -> Path:
    if explicit_path is not None:
        return Path(explicit_path)
    digest = _require_digest(digest, field="artifact_digest")
    candidate = Path(artifacts_dir) / f"{digest}.json"
    if candidate.is_file():
        return candidate
    raise PromotionError(f"no artifact for digest {digest} under {artifacts_dir}")


def _unused_lane(lane: Lane) -> Never:
    raise PromotionError(f"unhandled lane: {lane!r}")


def assert_lane(lane: Lane) -> str:
    if lane is Lane.CHAMPION:
        return lane.value
    if lane is Lane.SHADOW:
        return lane.value
    if lane is Lane.NONE:
        return lane.value
    return _unused_lane(lane)


__all__ = [
    "DecisionAction",
    "GateVerdict",
    "HealthGateFn",
    "Lane",
    "PromotionError",
    "PromotionEvaluateRequiredError",
    "PromotionGateError",
    "TrainFn",
    "append_decision",
    "assert_artifact_activation_ready",
    "assert_lane",
    "default_health_gate",
    "evaluate_activation_gates",
    "next_decision_seq",
    "resolve_artifact_path",
]
