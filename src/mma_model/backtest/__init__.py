"""Frozen backtest evaluator gates (DWCS-302)."""

from mma_model.backtest.contract import (
    PINNED_FEATURE_SPEC_HASH,
    PINNED_SPLITS_CONFIG_HASH,
    EvaluatorHashMismatchError,
    HashKind,
    compute_data_hash,
    compute_splits_config_hash,
    current_feature_spec_hash,
    splits_config_payload,
    verify_evaluator_hashes,
)

__all__ = [
    "PINNED_FEATURE_SPEC_HASH",
    "PINNED_SPLITS_CONFIG_HASH",
    "EvaluatorHashMismatchError",
    "HashKind",
    "compute_data_hash",
    "compute_splits_config_hash",
    "current_feature_spec_hash",
    "splits_config_payload",
    "verify_evaluator_hashes",
]
