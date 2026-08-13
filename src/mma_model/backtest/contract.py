"""Frozen evaluator hash gate for event-grouped fold plans (DWCS-302).

A fold plan is emitted or accepted only when all four hashes match:
evaluation contract, feature spec, split-config, and the canonical
event/bout/cutoff data digest. Mismatch hard-fails; it never proceeds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Never

from mma_model.evaluation.contract import (
    PINNED_CONTRACT_HASH,
    EvaluationContract,
    compute_contract_hash,
)
from mma_model.features.spec import spec_hash

# SHA-256 of the DWCS-301 feature spec payload (spec_hash()). Update only
# together with an intentional SPEC_VERSION bump.
PINNED_FEATURE_SPEC_HASH: Final = (
    "e02a8c4832990003de3075fc1ecd46e06f3572dbfaccb5a18a4b6e7448ba6755"
)

# SHA-256 of splits_config_payload() over the frozen evaluation contract.
PINNED_SPLITS_CONFIG_HASH: Final = (
    "e2d7ac2cf3e456eee7cc15c585d1ac88a34f79a42ac97f903b6c9cb8983d9856"
)


class HashKind(StrEnum):
    CONTRACT = "contract"
    FEATURE_SPEC = "feature_spec"
    DATA = "data"
    CONFIG = "config"


class EvaluatorHashMismatchError(ValueError):
    """Fold plan hash did not match the pinned or recomputed digest."""

    def __init__(self, kind: HashKind, *, got: str, expected: str) -> None:
        self.kind = kind
        self.got = got
        self.expected = expected
        super().__init__(
            f"{_kind_label(kind)} hash mismatch: got {got}, expected {expected}"
        )


def _kind_label(kind: HashKind) -> str:
    if kind is HashKind.CONTRACT:
        return "contract"
    if kind is HashKind.FEATURE_SPEC:
        return "feature spec"
    if kind is HashKind.DATA:
        return "data"
    if kind is HashKind.CONFIG:
        return "splits config"
    never_kind: Never = kind
    raise ValueError(f"unhandled hash kind: {never_kind!r}")


def current_feature_spec_hash() -> str:
    return spec_hash()


def splits_config_payload(contract: EvaluationContract) -> dict[str, Any]:
    """Small frozen splits config derived from the evaluation contract."""
    return {
        "grouping": contract.splits.grouping.value,
        "outer_fold": contract.splits.outer_fold.value,
        "target_cards": contract.splits.target_cards,
        "prediction_cutoff_minutes": int(
            contract.point_in_time.prediction_cutoff_minutes_before_scheduled_start
        ),
        "development_seasons": list(contract.splits.development.seasons),
        "validation_seasons": list(contract.splits.validation.seasons),
        "holdout_seasons": list(contract.splits.holdout.seasons),
        "report_universes": [item.value for item in contract.sensitivity.report_universes],
    }


def compute_splits_config_hash(contract: EvaluationContract) -> str:
    return compute_contract_hash(splits_config_payload(contract))


def compute_data_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """Hash canonical event/bout IDs plus cutoffs (sorted, compact JSON)."""
    events = sorted(
        (
            {
                "bout_ids": sorted(str(bout_id) for bout_id in row["bout_ids"]),
                "cutoff": str(row["cutoff"]),
                "event_id": str(row["event_id"]),
            }
            for row in rows
        ),
        key=lambda item: str(item["event_id"]),
    )
    return compute_contract_hash({"events": events})


def verify_evaluator_hashes(
    *,
    contract_hash: str,
    feature_spec_hash: str,
    data_hash: str,
    config_hash: str,
    expected_data_hash: str,
    expected_config_hash: str,
    expected_contract_hash: str = PINNED_CONTRACT_HASH,
    expected_feature_spec_hash: str = PINNED_FEATURE_SPEC_HASH,
) -> None:
    """Hard-fail unless every stored hash matches the pinned/recomputed digest."""
    live_spec = current_feature_spec_hash()
    if live_spec != expected_feature_spec_hash:
        raise EvaluatorHashMismatchError(
            HashKind.FEATURE_SPEC, got=live_spec, expected=expected_feature_spec_hash
        )
    if feature_spec_hash != expected_feature_spec_hash:
        raise EvaluatorHashMismatchError(
            HashKind.FEATURE_SPEC, got=feature_spec_hash, expected=expected_feature_spec_hash
        )
    if contract_hash != expected_contract_hash:
        raise EvaluatorHashMismatchError(
            HashKind.CONTRACT, got=contract_hash, expected=expected_contract_hash
        )
    if config_hash != expected_config_hash:
        raise EvaluatorHashMismatchError(
            HashKind.CONFIG, got=config_hash, expected=expected_config_hash
        )
    if expected_config_hash != PINNED_SPLITS_CONFIG_HASH:
        raise EvaluatorHashMismatchError(
            HashKind.CONFIG, got=expected_config_hash, expected=PINNED_SPLITS_CONFIG_HASH
        )
    if data_hash != expected_data_hash:
        raise EvaluatorHashMismatchError(
            HashKind.DATA, got=data_hash, expected=expected_data_hash
        )
