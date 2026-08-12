"""Map parsed regional fighter history into SourceObservationRecord rows."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from mma_model.dwcs.duration import derive_elapsed_seconds
from mma_model.history.constants import (
    ENTITY_CURRENT_RECORD,
    ENTITY_EXPLICIT_PRE_FIGHT,
    ENTITY_REGIONAL_BOUT,
    PARSER_VERSIONS,
    SOURCE_CLASS,
)
from mma_model.history.pit import resolve_regional_pit_quality
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.pit_proxy import PitProxyRule, load_pit_proxy_rule
from mma_model.sources.policy import load_source_policy


class ReservedAttributeKeyError(ValueError):
    """Raised when source-specific attributes collide with reserved contract keys."""


def _reject_reserved(attrs: Mapping[str, Any], reserved: set[str]) -> None:
    collisions = sorted(key for key in attrs if key in reserved)
    if collisions:
        raise ReservedAttributeKeyError(f"reserved attribute key collision: {collisions[0]}")


def _event_effective_at(event_date: str | None, observed_at: datetime) -> datetime:
    if not event_date:
        return observed_at
    parsed = date.fromisoformat(event_date)
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def _parse_iso_datetime(value: object, fallback: datetime) -> datetime:
    if value is None or value == "":
        return fallback
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_dt(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration_for_bout(bout: Mapping[str, Any]) -> Any:
    scheduled = bout.get("scheduled_rounds")
    ending_round = bout.get("ending_round")
    if scheduled is None:
        # Unknown scheduled rounds stay unknown; use ending_round only as a
        # validation ceiling so elapsed seconds can still be derived.
        scheduled_for_duration = int(ending_round) if ending_round else 1
    else:
        scheduled_for_duration = int(scheduled)
    return derive_elapsed_seconds(
        ending_round=ending_round,
        time_str=bout.get("time_str"),
        scheduled_rounds=scheduled_for_duration,
    )


def map_fighter_history_to_observations(
    *,
    source: str,
    parsed: Mapping[str, Any],
    observed_at: datetime,
    payload_hash: str,
    proxy: PitProxyRule | None = None,
    identity_status: str = "unresolved",
    fighter_canonical_id: str | None = None,
    opponent_canonical_by_external_id: Mapping[str, str] | None = None,
) -> list[SourceObservationRecord]:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware UTC")
    policy = load_source_policy()
    reserved = set(policy.observation_metadata.reserved_attribute_keys)
    proxy_rule = proxy if proxy is not None else load_pit_proxy_rule()
    opponent_canon = dict(opponent_canonical_by_external_id or {})

    fighter_id = str(parsed.get("fighter_external_id") or "")
    fighter_name = str(parsed.get("fighter_name") or "")
    if not fighter_id or not fighter_name:
        raise ValueError("fighter_external_id and fighter_name are required")
    wikidata_id = parsed.get("wikidata_id")
    origin = str(parsed.get("observation_origin") or "unknown")
    if origin not in {"synthetic_fixture", "live_public", "unknown"}:
        origin = "unknown"
    rows: list[SourceObservationRecord] = []

    current_record = parsed.get("current_record")
    if isinstance(current_record, dict):
        quality_tier, ts_q, ts_src, proxy_at = resolve_regional_pit_quality(
            source=source,
            source_published_at=None,
            source_updated_at=None,
            effective_at=observed_at,
            proxy=None,
            is_current_record=True,
            observation_origin=origin,
        )
        attrs = {
            "fighter_external_id": fighter_id,
            "fighter_name": fighter_name,
            "fighter_canonical_id": fighter_canonical_id,
            "record_text": current_record.get("text"),
            "wins": current_record.get("wins"),
            "losses": current_record.get("losses"),
            "draws": current_record.get("draws"),
            "no_contests": current_record.get("no_contests"),
            "classification": current_record.get("classification") or "unknown",
            "is_current_mutable": True,
            "is_current_record": True,
            "observation_origin": origin,
            "feature_eligible": False,
        }
        _reject_reserved(attrs, reserved)
        rows.append(
            SourceObservationRecord(
                source=source,
                stream="current_record",
                external_id=f"{fighter_id}:current_record",
                entity_kind=ENTITY_CURRENT_RECORD,
                observed_at=observed_at,
                effective_at=observed_at,
                timestamp_quality=ts_q,
                timestamp_quality_source=ts_src,
                quality_tier=quality_tier,
                proxy_published_at=proxy_at,
                payload_hash=payload_hash,
                raw_ref=payload_hash,
                detail_level=DetailLevel.SUMMARY,
                attributes=attrs,
            )
        )

    explicit = parsed.get("explicit_pre_fight_record")
    if isinstance(explicit, dict):
        as_of = _event_effective_at(explicit.get("as_of"), observed_at)
        attrs = {
            "fighter_external_id": fighter_id,
            "fighter_canonical_id": fighter_canonical_id,
            "wins": explicit.get("wins"),
            "losses": explicit.get("losses"),
            "draws": explicit.get("draws"),
            "no_contests": explicit.get("no_contests"),
            "classification": explicit.get("classification") or "unknown",
            "is_current_mutable": False,
            "observation_origin": origin,
            "feature_eligible": False,
        }
        _reject_reserved(attrs, reserved)
        quality_tier, ts_q, ts_src, proxy_at = resolve_regional_pit_quality(
            source=source,
            source_published_at=None,
            source_updated_at=None,
            effective_at=as_of,
            proxy=proxy_rule,
            is_current_record=False,
            observation_origin=origin,
        )
        rows.append(
            SourceObservationRecord(
                source=source,
                stream="explicit_pre_fight",
                external_id=f"{fighter_id}:explicit:{explicit.get('as_of') or 'unknown'}",
                entity_kind=ENTITY_EXPLICIT_PRE_FIGHT,
                observed_at=observed_at,
                effective_at=as_of,
                timestamp_quality=ts_q,
                timestamp_quality_source=ts_src,
                quality_tier=quality_tier,
                proxy_published_at=proxy_at,
                payload_hash=payload_hash,
                raw_ref=payload_hash,
                detail_level=DetailLevel.PARTIAL,
                attributes=attrs,
            )
        )

    for bout in list(parsed.get("bouts") or []):
        if not isinstance(bout, dict):
            raise ValueError("bout rows must be objects")
        bout_id = str(bout.get("external_bout_id") or "")
        if not bout_id:
            raise ValueError("external_bout_id is required")
        event_date = bout.get("event_date")
        event_datetime = bout.get("event_datetime")
        if event_datetime:
            event_effective = _parse_iso_datetime(event_datetime, observed_at)
            time_precision = "exact"
        else:
            event_effective = _event_effective_at(
                str(event_date) if event_date else None, observed_at
            )
            time_precision = "date_only" if event_date else "unknown"
        version_kind = str(bout.get("version_kind") or "event_night")
        revision = int(bout.get("revision") or 1)
        observation_id = f"{bout_id}#{version_kind}#{revision}"
        adjudicated = bout.get("adjudicated_at") or bout.get("effective_at")
        if version_kind == "current" and adjudicated:
            effective_at = _parse_iso_datetime(adjudicated, event_effective)
        else:
            effective_at = event_effective
        duration = _duration_for_bout(bout)
        elapsed = duration.elapsed_seconds
        missing_reason = bout.get("missing_reason")
        if duration.status.value == "invalid" and not missing_reason:
            missing_reason = duration.reason
        published = _parse_optional_dt(bout.get("source_published_at"))
        updated = _parse_optional_dt(bout.get("source_updated_at"))
        quality_tier, ts_q, ts_src, proxy_at = resolve_regional_pit_quality(
            source=source,
            source_published_at=published,
            source_updated_at=updated,
            effective_at=effective_at,
            proxy=proxy_rule if event_date else None,
            is_current_record=False,
            observation_origin=origin,
        )
        opponent_ext = bout.get("opponent_external_id")
        source_url = parsed.get("source_url") or bout.get("source_url")
        attrs = {
            "fighter_source": source,
            "fighter_external_id": fighter_id,
            "fighter_name": fighter_name,
            "fighter_canonical_id": fighter_canonical_id,
            "external_bout_id": bout_id,
            "opponent_source": source,
            "opponent_external_id": opponent_ext,
            "opponent_name": bout.get("opponent_name") or "",
            "opponent_canonical_id": opponent_canon.get(str(opponent_ext or "")),
            "event_name": bout.get("event_name"),
            "event_date": event_date,
            "event_external_id": bout.get("event_external_id"),
            "promotion": bout.get("promotion"),
            "classification": bout.get("classification") or "unknown",
            "regulated_us": bout.get("regulated_us") or "unknown",
            "result": bout.get("result") or "unknown",
            "method": bout.get("method"),
            "ending_round": bout.get("ending_round"),
            "time_str": bout.get("time_str"),
            "elapsed_seconds": elapsed,
            "scheduled_rounds": bout.get("scheduled_rounds"),
            "duration_status": duration.status.value,
            "revision": revision,
            "bout_status": bout.get("bout_status") or "completed",
            "identity_status": identity_status,
            "wikidata_id": wikidata_id,
            "is_current_record": False,
            "missing_reason": missing_reason,
            "left_truncated": bool(parsed.get("left_truncated") or bout.get("left_truncated")),
            "parser_version": PARSER_VERSIONS.get(source),
            "source_class": SOURCE_CLASS.get(source),
            "source_url": source_url,
            "event_time_precision": time_precision,
            "observation_origin": origin,
        }
        _reject_reserved(attrs, reserved)
        live_verified = (
            origin == "live_public"
            and duration.allows_verified_detail
            and bout.get("result") in {"win", "loss", "draw", "nc"}
        )
        detail = DetailLevel.VERIFIED if live_verified else DetailLevel.PARTIAL
        rows.append(
            SourceObservationRecord(
                source=source,
                stream="fighter_history",
                external_id=observation_id,
                entity_kind=ENTITY_REGIONAL_BOUT,
                observed_at=observed_at,
                effective_at=effective_at,
                source_published_at=published,
                source_updated_at=updated,
                timestamp_quality=ts_q,
                timestamp_quality_source=ts_src,
                quality_tier=quality_tier,
                proxy_published_at=proxy_at,
                payload_hash=payload_hash,
                raw_ref=payload_hash,
                detail_level=detail,
                version_kind=version_kind,
                attributes=attrs,
            )
        )

    return sorted(rows, key=lambda row: (row.entity_kind, row.external_id, row.version_kind or ""))
