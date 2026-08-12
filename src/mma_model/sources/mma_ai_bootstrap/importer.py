"""Import reconciled mma-ai bootstrap rows as SourceObservationRecord (DWCS-102)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.mma_ai_bootstrap.reconcile import BootstrapReject, ReconcileReport
from mma_model.sources.policy import load_source_policy

SOURCE_MMA_AI_BOOTSTRAP = "mma_ai_bootstrap"


def import_reconciled_observations(
    report: ReconcileReport,
    *,
    observed_at: datetime | None = None,
) -> list[SourceObservationRecord]:
    if report.kill_reason:
        raise BootstrapReject(report.kill_reason)
    policy = load_source_policy()
    reserved = set(policy.observation_metadata.reserved_attribute_keys)
    observed = observed_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware UTC")

    rows: list[SourceObservationRecord] = []
    for item in report.rows:
        fight_id = str(item.get("fight_id") or "")
        payload_hash = str(item.get("payload_hash") or "")
        if len(payload_hash) != 64:
            raise BootstrapReject(
                f"mma_ai_bootstrap: invalid payload_hash for fight_id={fight_id!r}"
            )
        attrs: dict[str, Any] = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "fight_id",
                "payload_hash",
                "observed_at",
                "effective_at",
                "source_published_at",
                "source_updated_at",
                "proxy_published_at",
                "timestamp_quality",
                "timestamp_quality_source",
                "quality_tier",
            }
        }
        collisions = sorted(key for key in attrs if key in reserved)
        if collisions:
            raise BootstrapReject(
                f"mma_ai_bootstrap: reserved attribute collision {collisions[0]}"
            )
        effective_raw = item.get("effective_at")
        if isinstance(effective_raw, str) and effective_raw.strip():
            effective_at = datetime.fromisoformat(effective_raw.replace("Z", "+00:00"))
        elif isinstance(effective_raw, datetime):
            effective_at = effective_raw
        else:
            raise BootstrapReject(
                f"mma_ai_bootstrap: missing effective_at for fight_id={fight_id!r}; "
                "refuse fabricated timestamp"
            )
        if effective_at.tzinfo is None:
            raise BootstrapReject(
                f"mma_ai_bootstrap: effective_at must be timezone-aware for "
                f"fight_id={fight_id!r}"
            )
        rows.append(
            SourceObservationRecord(
                source=SOURCE_MMA_AI_BOOTSTRAP,
                stream="normalized_fights",
                external_id=fight_id,
                entity_kind="bout_result",
                observed_at=observed,
                effective_at=effective_at,
                source_published_at=None,
                source_updated_at=None,
                proxy_published_at=None,
                timestamp_quality="unknown",
                timestamp_quality_source="mma_ai_bootstrap",
                quality_tier="bronze",
                payload_hash=payload_hash,
                raw_ref=None,
                raw_blob_absent=True,
                detail_level=DetailLevel.PARTIAL,
                attributes=attrs,
            )
        )
    return sorted(rows, key=lambda row: row.external_id)
