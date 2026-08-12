"""Apply source-neutral history observations into DWCS-105 tables."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mma_model.db.tables.history import (
    HistoryConflict,
    HistoryExplicitRecord,
    HistorySourceBout,
    HistorySourceFailure,
)
from mma_model.history.constants import (
    ENTITY_CURRENT_RECORD,
    ENTITY_EXPLICIT_PRE_FIGHT,
    ENTITY_HISTORY_CONFLICT,
    ENTITY_REGIONAL_BOUT,
    ENTITY_SOURCE_FAILURE,
)
from mma_model.sources.contracts import SourceObservationRecord

HISTORY_ENTITY_KINDS = frozenset(
    {
        ENTITY_REGIONAL_BOUT,
        ENTITY_HISTORY_CONFLICT,
        ENTITY_SOURCE_FAILURE,
        ENTITY_CURRENT_RECORD,
        ENTITY_EXPLICIT_PRE_FIGHT,
    }
)
REVISION_RETRY_LIMIT = 16


def _as_optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid integer field")
    return int(value)


def _as_optional_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def apply_history_observation(session: Session, obs: SourceObservationRecord) -> str | None:
    """Persist a DWCS-105 observation. Returns skip reason or None."""
    if obs.entity_kind == ENTITY_REGIONAL_BOUT:
        return _apply_regional_bout(session, obs)
    if obs.entity_kind == ENTITY_HISTORY_CONFLICT:
        return _apply_conflict(session, obs)
    if obs.entity_kind == ENTITY_SOURCE_FAILURE:
        return _apply_source_failure(session, obs)
    if obs.entity_kind == ENTITY_CURRENT_RECORD:
        return _apply_current_record(session, obs)
    if obs.entity_kind == ENTITY_EXPLICIT_PRE_FIGHT:
        return _apply_explicit_record(session, obs)
    return None


def _max_revision(
    session: Session,
    *,
    source: str,
    external_bout_id: str,
    version_kind: str,
) -> int:
    current = session.scalar(
        select(func.max(HistorySourceBout.revision)).where(
            HistorySourceBout.source == source,
            HistorySourceBout.external_bout_id == external_bout_id,
            HistorySourceBout.version_kind == version_kind,
        )
    )
    return int(current or 0)


def _flush_new_row(session: Session, row: object) -> bool:
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        return True
    except IntegrityError:
        session.expire_all()
        return False


def _correction_conflict_key(
    *,
    source: str,
    external_bout_id: str,
    version_kind: str,
    payload_hash: str,
    revision: int,
) -> str:
    return f"correction:{source}:{external_bout_id}:{version_kind}:{payload_hash}:{revision}"


def _apply_regional_bout(session: Session, obs: SourceObservationRecord) -> str | None:
    attrs = dict(obs.attributes)
    version_kind = obs.version_kind or "event_night"
    external_bout_id = str(attrs.get("external_bout_id") or obs.external_id.split("#", 1)[0])
    existing_payload = session.scalars(
        select(HistorySourceBout).where(
            HistorySourceBout.source == obs.source,
            HistorySourceBout.external_bout_id == external_bout_id,
            HistorySourceBout.version_kind == version_kind,
            HistorySourceBout.payload_hash == obs.payload_hash,
        )
    ).first()
    if existing_payload is not None:
        return "skipped_identical"

    classification = str(attrs.get("classification") or "unknown")
    if classification not in {"professional", "amateur", "unknown"}:
        classification = "unknown"
    regulated_us = str(attrs.get("regulated_us") or "unknown")
    if regulated_us not in {"true", "false", "unknown"}:
        regulated_us = "unknown"
    result = str(attrs.get("result") or "unknown")
    if result not in {"win", "loss", "draw", "nc", "unknown", "cancelled"}:
        result = "unknown"
    precision = str(attrs.get("event_time_precision") or "date_only")
    if precision not in {"date_only", "exact", "unknown"}:
        precision = "date_only"
    origin = str(attrs.get("observation_origin") or "unknown")
    if origin not in {"synthetic_fixture", "live_public", "unknown"}:
        origin = "unknown"

    for _attempt in range(REVISION_RETRY_LIMIT):
        prior_max = _max_revision(
            session,
            source=obs.source,
            external_bout_id=external_bout_id,
            version_kind=version_kind,
        )
        revision = prior_max + 1
        row = HistorySourceBout(
            id=str(uuid.uuid4()),
            source=obs.source,
            stream=obs.stream,
            external_bout_id=external_bout_id,
            fighter_source=str(attrs.get("fighter_source") or obs.source),
            fighter_external_id=str(attrs.get("fighter_external_id") or ""),
            fighter_name=str(attrs.get("fighter_name") or ""),
            fighter_canonical_id=_as_optional_str(attrs.get("fighter_canonical_id")),
            opponent_source=_as_optional_str(attrs.get("opponent_source")),
            opponent_external_id=_as_optional_str(attrs.get("opponent_external_id")),
            opponent_name=str(attrs.get("opponent_name") or ""),
            opponent_canonical_id=_as_optional_str(attrs.get("opponent_canonical_id")),
            event_name=_as_optional_str(attrs.get("event_name")),
            event_date=_as_optional_date(attrs.get("event_date")),
            event_external_id=_as_optional_str(attrs.get("event_external_id")),
            classification=classification,
            regulated_us=regulated_us,
            result=result,
            method=_as_optional_str(attrs.get("method")),
            ending_round=_as_optional_int(attrs.get("ending_round")),
            time_str=_as_optional_str(attrs.get("time_str")),
            elapsed_seconds=_as_optional_int(attrs.get("elapsed_seconds")),
            scheduled_rounds=_as_optional_int(attrs.get("scheduled_rounds")),
            promotion=_as_optional_str(attrs.get("promotion")),
            missing_reason=_as_optional_str(attrs.get("missing_reason")),
            left_truncated=1 if attrs.get("left_truncated") else 0,
            parser_version=_as_optional_str(attrs.get("parser_version")),
            source_class=_as_optional_str(attrs.get("source_class")),
            source_url=_as_optional_str(attrs.get("source_url")),
            version_kind=version_kind,
            revision=revision,
            bout_status=str(attrs.get("bout_status") or "completed"),
            quality_tier=obs.quality_tier,
            timestamp_quality=obs.timestamp_quality,
            timestamp_quality_source=obs.timestamp_quality_source,
            observed_at=obs.observed_at,
            effective_at=obs.effective_at,
            source_published_at=obs.source_published_at,
            source_updated_at=obs.source_updated_at,
            proxy_published_at=obs.proxy_published_at,
            payload_hash=obs.payload_hash,
            raw_ref=obs.raw_ref,
            identity_status=str(attrs.get("identity_status") or "unresolved"),
            is_current_record=1 if attrs.get("is_current_record") else 0,
            event_time_precision=precision,
            observation_origin=origin,
            wikidata_id=_as_optional_str(attrs.get("wikidata_id")),
        )
        if not _flush_new_row(session, row):
            continue
        if prior_max >= 1:
            _flush_new_row(
                session,
                HistoryConflict(
                    id=str(uuid.uuid4()),
                    conflict_key=_correction_conflict_key(
                        source=obs.source,
                        external_bout_id=external_bout_id,
                        version_kind=version_kind,
                        payload_hash=obs.payload_hash,
                        revision=revision,
                    ),
                    conflict_type="correction_append",
                    fighter_canonical_id=_as_optional_str(attrs.get("fighter_canonical_id")),
                    left_source=obs.source,
                    left_external_id=external_bout_id,
                    right_source=obs.source,
                    right_external_id=external_bout_id,
                    detail_json=json.dumps(
                        {
                            "prior_revision": prior_max,
                            "next_revision": revision,
                            "next_hash": obs.payload_hash,
                            "next_result": result,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    quality_tier="conflict",
                    observed_at=obs.observed_at,
                ),
            )
            return "correction"
        return None

    same = session.scalars(
        select(HistorySourceBout).where(
            HistorySourceBout.source == obs.source,
            HistorySourceBout.external_bout_id == external_bout_id,
            HistorySourceBout.version_kind == version_kind,
            HistorySourceBout.payload_hash == obs.payload_hash,
        )
    ).first()
    if same is not None:
        return "skipped_identical"
    raise RuntimeError(
        f"could not persist regional bout {obs.source}:{external_bout_id} after revision retries"
    )


def _apply_conflict(session: Session, obs: SourceObservationRecord) -> str | None:
    attrs = dict(obs.attributes)
    conflict_key = str(attrs.get("conflict_key") or obs.external_id)
    existing = session.scalars(
        select(HistoryConflict).where(HistoryConflict.conflict_key == conflict_key)
    ).first()
    if existing is not None:
        return "skipped_identical"
    _flush_new_row(
        session,
        HistoryConflict(
            id=str(uuid.uuid4()),
            conflict_key=conflict_key,
            conflict_type=str(attrs.get("conflict_type") or "result"),
            fighter_canonical_id=_as_optional_str(attrs.get("fighter_canonical_id")),
            left_source=str(attrs.get("left_source") or obs.source),
            left_external_id=str(attrs.get("left_external_id") or obs.external_id),
            right_source=str(attrs.get("right_source") or ""),
            right_external_id=str(attrs.get("right_external_id") or ""),
            detail_json=json.dumps(
                dict(attrs.get("detail") or attrs),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
            quality_tier="conflict",
            observed_at=obs.observed_at,
        ),
    )
    return None


def _apply_source_failure(session: Session, obs: SourceObservationRecord) -> str | None:
    attrs = dict(obs.attributes)
    reason = str(attrs.get("reason") or "source_failed")
    scope = str(attrs.get("scope") or obs.stream)
    subject = str(attrs.get("subject") or "")
    existing = session.scalars(
        select(HistorySourceFailure).where(
            HistorySourceFailure.source == obs.source,
            HistorySourceFailure.reason == reason,
            HistorySourceFailure.scope == scope,
            HistorySourceFailure.subject == subject,
        )
    ).first()
    if existing is not None:
        return "skipped_identical"
    evidence = dict(attrs.get("evidence") or {})
    _flush_new_row(
        session,
        HistorySourceFailure(
            id=str(uuid.uuid4()),
            source=obs.source,
            reason=reason,
            scope=scope,
            subject=subject,
            host=_as_optional_str(attrs.get("host")),
            path_category=_as_optional_str(attrs.get("path_category")),
            http_status=_as_optional_int(attrs.get("http_status")),
            evidence_json=json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), default=str
            ),
            payload_hash=obs.payload_hash,
            checkpoint_token=_as_optional_str(attrs.get("checkpoint_token")),
            observed_at=obs.observed_at,
        ),
    )
    return None


def _apply_current_record(session: Session, obs: SourceObservationRecord) -> str | None:
    """Store mutable current profile in history-owned comparison tables only."""
    return _apply_explicit_record(session, obs, is_current=True)


def _apply_explicit_record(
    session: Session,
    obs: SourceObservationRecord,
    *,
    is_current: bool = False,
) -> str | None:
    attrs = dict(obs.attributes)
    fighter_external_id = str(attrs.get("fighter_external_id") or obs.external_id)
    as_of = obs.effective_at
    existing = session.scalars(
        select(HistoryExplicitRecord).where(
            HistoryExplicitRecord.source == obs.source,
            HistoryExplicitRecord.fighter_external_id == fighter_external_id,
            HistoryExplicitRecord.as_of == as_of,
        )
    ).first()
    if existing is not None:
        return "skipped_identical"
    _flush_new_row(
        session,
        HistoryExplicitRecord(
            id=str(uuid.uuid4()),
            source=obs.source,
            fighter_external_id=fighter_external_id,
            fighter_canonical_id=_as_optional_str(attrs.get("fighter_canonical_id")),
            as_of=as_of,
            wins=_as_optional_int(attrs.get("wins")),
            losses=_as_optional_int(attrs.get("losses")),
            draws=_as_optional_int(attrs.get("draws")),
            no_contests=_as_optional_int(attrs.get("no_contests")),
            classification=str(attrs.get("classification") or "unknown"),
            is_current_mutable=1 if is_current or attrs.get("is_current_mutable") else 0,
            feature_eligible=0,
            payload_hash=obs.payload_hash,
            observed_at=obs.observed_at,
        ),
    )
    return None


def conflict_observation(
    *,
    source: str,
    conflict_type: str,
    conflict_key: str,
    left_source: str,
    left_external_id: str,
    right_source: str,
    right_external_id: str,
    observed_at: datetime,
    effective_at: datetime,
    payload_hash: str,
    detail: Mapping[str, Any],
    fighter_canonical_id: str | None = None,
) -> SourceObservationRecord:
    return SourceObservationRecord(
        source=source,
        stream="conflicts",
        external_id=conflict_key,
        entity_kind=ENTITY_HISTORY_CONFLICT,
        observed_at=observed_at,
        effective_at=effective_at,
        timestamp_quality="unknown",
        timestamp_quality_source="conflict",
        quality_tier="conflict",
        payload_hash=payload_hash,
        raw_ref=None,
        raw_blob_absent=True,
        attributes={
            "conflict_key": conflict_key,
            "conflict_type": conflict_type,
            "left_source": left_source,
            "left_external_id": left_external_id,
            "right_source": right_source,
            "right_external_id": right_external_id,
            "fighter_canonical_id": fighter_canonical_id,
            "detail": dict(detail),
        },
    )


def source_failure_observation(
    *,
    source: str,
    reason: str,
    observed_at: datetime,
    payload_hash: str,
    scope: str = "default",
    subject: str = "",
    host: str | None = None,
    path_category: str | None = None,
    http_status: int | None = None,
    checkpoint_token: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> SourceObservationRecord:
    return SourceObservationRecord(
        source=source,
        stream="source_failure",
        external_id=f"{source}:{reason}:{scope}:{subject or 'source'}",
        entity_kind=ENTITY_SOURCE_FAILURE,
        observed_at=observed_at,
        effective_at=observed_at,
        timestamp_quality="unknown",
        timestamp_quality_source="source_failure",
        quality_tier="missing",
        payload_hash=payload_hash,
        raw_ref=None,
        raw_blob_absent=True,
        attributes={
            "reason": reason,
            "scope": scope,
            "subject": subject,
            "host": host,
            "path_category": path_category,
            "http_status": http_status,
            "checkpoint_token": checkpoint_token,
            "evidence": dict(evidence or {}),
        },
    )
