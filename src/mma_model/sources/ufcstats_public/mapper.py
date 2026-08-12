"""Map parsed UFCStats dicts to SourceObservationRecord rows (DWCS-102)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.pit_proxy import PitProxyRule
from mma_model.sources.policy import load_source_policy
from mma_model.sources.ufcstats_public.parser import SOURCE_UFCSTATS_PUBLIC


class ReservedAttributeKeyError(ValueError):
    """Raised when source-specific attributes collide with reserved contract keys."""


def map_fight_to_observations(
    *,
    parsed: dict[str, object],
    observed_at: datetime,
    effective_at: datetime,
    source_published_at: datetime | None,
    source_updated_at: datetime | None,
    proxy: PitProxyRule | None,
    payload_hash: str,
) -> list[SourceObservationRecord]:
    policy = load_source_policy()
    reserved = set(policy.observation_metadata.reserved_attribute_keys)

    fighter_a = _require_fighter(parsed, "fighter_a")
    fighter_b = _require_fighter(parsed, "fighter_b")
    external_fight_id = str(parsed.get("external_fight_id") or "")
    if not external_fight_id:
        raise ValueError("external_fight_id is required")

    quality_tier, timestamp_quality, timestamp_quality_source, proxy_published_at = (
        _resolve_pit_quality(
            source_published_at=source_published_at,
            source_updated_at=source_updated_at,
            effective_at=effective_at,
            proxy=proxy,
        )
    )

    rows: list[SourceObservationRecord] = []
    for side, fighter in (("a", fighter_a), ("b", fighter_b)):
        stats = dict(fighter.get("stats") or {})
        _reject_reserved(stats, reserved)
        attrs: dict[str, Any] = {
            **stats,
            "fighter_id": fighter.get("id"),
            "fighter_name": fighter.get("name"),
            "corner": side,
            "method": parsed.get("method"),
            "ending_round": parsed.get("ending_round"),
            "time_str": parsed.get("time_str"),
            "winner_id": parsed.get("winner_id"),
            "opponent_id": fighter_b.get("id") if side == "a" else fighter_a.get("id"),
        }
        _reject_reserved(attrs, reserved)
        detail = (
            DetailLevel.VERIFIED
            if stats.get("significant_strikes_landed") is not None
            and fighter.get("name")
            and fighter.get("id")
            else DetailLevel.PARTIAL
        )
        rows.append(
            SourceObservationRecord(
                source=SOURCE_UFCSTATS_PUBLIC,
                stream="fight_details",
                external_id=f"{external_fight_id}:{side}",
                entity_kind="bout_stat",
                observed_at=observed_at,
                effective_at=effective_at,
                source_published_at=source_published_at,
                source_updated_at=source_updated_at,
                proxy_published_at=proxy_published_at,
                timestamp_quality=timestamp_quality,
                timestamp_quality_source=timestamp_quality_source,
                quality_tier=quality_tier,
                payload_hash=payload_hash,
                raw_ref=payload_hash,
                detail_level=detail,
                attributes=attrs,
            )
        )
    # Deterministic output order: corner a then b.
    return sorted(rows, key=lambda row: row.external_id)


def _require_fighter(parsed: Mapping[str, object], key: str) -> dict[str, Any]:
    value = parsed.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"parsed[{key!r}] must be a dict")
    return value


def _reject_reserved(attrs: Mapping[str, Any], reserved: set[str]) -> None:
    collisions = sorted(key for key in attrs if key in reserved)
    if collisions:
        raise ReservedAttributeKeyError(
            f"reserved attribute key collision: {collisions[0]}"
        )


def _resolve_pit_quality(
    *,
    source_published_at: datetime | None,
    source_updated_at: datetime | None,
    effective_at: datetime,
    proxy: PitProxyRule | None,
) -> tuple[str, str, str, datetime | None]:
    if source_published_at is not None or source_updated_at is not None:
        return (
            "gold",
            "direct_source_timestamp",
            SOURCE_UFCSTATS_PUBLIC,
            None,
        )
    if proxy is not None:
        proxy.assert_allowed_for("immutable_bout_stat")
        proxy.assert_quality_tier_allowed(proxy.max_quality_tier_when_proxy)
        proxy_published_at = effective_at + timedelta(days=1)
        return (
            "silver",
            "publication_proxy",
            f"{proxy.rule_id}@{proxy.rule_version}",
            proxy_published_at,
        )
    return ("bronze", "unknown", "unknown", None)
