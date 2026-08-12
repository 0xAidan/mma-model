"""Source adapter contracts and policy loader (provider HTTP clients in later tickets)."""

from mma_model.sources.contracts import DETAIL_LEVEL_RANK, DetailLevel, SourceObservationRecord
from mma_model.sources.policy import (
    CANONICAL_SOURCE_IDS,
    SourcePolicy,
    SourcePolicyError,
    UnknownSourcePolicyError,
    load_source_policy,
)

__all__ = [
    "CANONICAL_SOURCE_IDS",
    "DETAIL_LEVEL_RANK",
    "DetailLevel",
    "SourceObservationRecord",
    "SourcePolicy",
    "SourcePolicyError",
    "UnknownSourcePolicyError",
    "load_source_policy",
]
