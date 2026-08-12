"""PIT quality helpers for regional bout observations (DWCS-105)."""

from __future__ import annotations

from datetime import datetime, timedelta

from mma_model.sources.pit_proxy import PitProxyRule


def resolve_regional_pit_quality(
    *,
    source: str,
    source_published_at: datetime | None,
    source_updated_at: datetime | None,
    effective_at: datetime,
    proxy: PitProxyRule | None,
    is_current_record: bool,
) -> tuple[str, str, str, datetime | None]:
    """Return quality_tier, timestamp_quality, timestamp_quality_source, proxy_at.

    Mutable current profile aggregates cannot use the publication proxy and
    remain bronze/unknown. Historical immutable bout facts may use silver proxy.
    """
    if is_current_record:
        return ("bronze", "unknown", "current_mutable_profile", None)
    if source_published_at is not None or source_updated_at is not None:
        return ("gold", "direct_source_timestamp", source, None)
    if proxy is not None:
        proxy.assert_allowed_for("immutable_bout_result")
        proxy.assert_quality_tier_allowed(proxy.max_quality_tier_when_proxy)
        return (
            "silver",
            "publication_proxy",
            f"{proxy.rule_id}@{proxy.rule_version}",
            effective_at + timedelta(days=1),
        )
    return ("bronze", "unknown", "unknown", None)
