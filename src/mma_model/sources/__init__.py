"""Source adapter contracts and policy loader (provider HTTP clients in later tickets)."""

from mma_model.sources.contracts import DETAIL_LEVEL_RANK, DetailLevel, SourceObservationRecord
from mma_model.sources.http_politeness import (
    HttpPolitenessConfig,
    HttpPolitenessError,
    load_http_politeness,
)
from mma_model.sources.pit_proxy import PitProxyError, PitProxyRule, load_pit_proxy_rule
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
    "HttpPolitenessConfig",
    "HttpPolitenessError",
    "PitProxyError",
    "PitProxyRule",
    "SourceObservationRecord",
    "SourcePolicy",
    "SourcePolicyError",
    "UnknownSourcePolicyError",
    "load_http_politeness",
    "load_pit_proxy_rule",
    "load_source_policy",
]
