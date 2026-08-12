"""Map parsed Tapology dicts to SourceObservationRecord rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from mma_model.history.map_records import map_fighter_history_to_observations
from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.pit_proxy import PitProxyRule
from mma_model.sources.tapology_public.parser import SOURCE_TAPOLOGY_PUBLIC


def map_fighter_to_observations(
    *,
    parsed: Mapping[str, Any],
    observed_at: datetime,
    payload_hash: str,
    proxy: PitProxyRule | None = None,
    identity_status: str = "unresolved",
    fighter_canonical_id: str | None = None,
    opponent_canonical_by_external_id: Mapping[str, str] | None = None,
) -> list[SourceObservationRecord]:
    return map_fighter_history_to_observations(
        source=SOURCE_TAPOLOGY_PUBLIC,
        parsed=parsed,
        observed_at=observed_at,
        payload_hash=payload_hash,
        proxy=proxy,
        identity_status=identity_status,
        fighter_canonical_id=fighter_canonical_id,
        opponent_canonical_by_external_id=opponent_canonical_by_external_id,
    )
