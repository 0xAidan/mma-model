"""Dedicated odds-bout match review queue (DWCS-203).

Separate from fighter IdentityReviewQueue: approvals activate a provider-event
alias to a selected bout after version/concurrency checks and never write
fighter source mappings.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from mma_model.db.tables.odds import OddsBoutMatchReview, OddsProviderEventAlias
from mma_model.odds.lifecycle import OddsBoutLifecycleState, apply_bout_lifecycle
from mma_model.odds.matching import (
    MATCH_RULE_MANUAL_REVIEW,
    as_utc_sqlite,
    dump_evidence,
    load_matching_contract,
    require_aware_utc,
    validate_linked_bout,
)

ReviewStatus = Literal["pending", "approved", "rejected", "reversed"]

RULE_ODDS_BOUT_AMBIGUOUS: Final[str] = "odds_bout_ambiguous_match_queue"


class OddsBoutMatchReviewError(ValueError):
    """Invalid or stale odds-bout match review decision."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_candidates(raw: str) -> tuple[str, ...]:
    payload = json.loads(raw or "[]")
    if not isinstance(payload, list):
        raise OddsBoutMatchReviewError("candidate_bout_ids must be a JSON list")
    return tuple(str(item) for item in payload)


def enqueue_bout_match_review(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    home_team: str,
    away_team: str,
    commence_time: datetime,
    candidate_bout_ids: tuple[str, ...],
    reason: str,
    observed_at: datetime | None = None,
    actor: str = "odds_reconcile",
) -> str:
    """Idempotently enqueue a pending odds-bout match review; returns review id.

    Pending evidence/candidate changes use optimistic CAS and increment version.
    """
    stamp = require_aware_utc(observed_at or _utc_now(), field="observed_at")
    commence = require_aware_utc(commence_time, field="commence_time")
    candidates_json = json.dumps(list(candidate_bout_ids))
    evidence_json = dump_evidence(
        {
            "kind": "odds_bout_match_review",
            "reason": reason,
            "candidate_bout_ids": list(candidate_bout_ids),
            "actor": actor,
        }
    )
    existing = session.scalar(
        select(OddsBoutMatchReview).where(
            OddsBoutMatchReview.provider == provider,
            OddsBoutMatchReview.external_event_id == external_event_id,
            OddsBoutMatchReview.status == "pending",
        )
    )
    if existing is not None:
        unchanged = (
            existing.candidate_bout_ids_json == candidates_json
            and existing.reason == reason
            and existing.home_team == home_team
            and existing.away_team == away_team
            and as_utc_sqlite(existing.commence_time) == commence
        )
        if unchanged:
            return existing.id
        result = session.execute(
            update(OddsBoutMatchReview)
            .where(
                OddsBoutMatchReview.id == existing.id,
                OddsBoutMatchReview.status == "pending",
                OddsBoutMatchReview.version == existing.version,
            )
            .values(
                candidate_bout_ids_json=candidates_json,
                reason=reason,
                home_team=home_team,
                away_team=away_team,
                commence_time=commence,
                evidence_json=evidence_json,
                version=existing.version + 1,
                updated_at=stamp,
            )
        )
        if result.rowcount != 1:
            raise OddsBoutMatchReviewError(
                "concurrent pending review update rejected (stale version)"
            )
        session.expire(existing)
        session.refresh(existing)
        return existing.id

    row = OddsBoutMatchReview(
        id=str(uuid.uuid4()),
        status="pending",
        version=1,
        provider=provider,
        external_event_id=external_event_id,
        home_team=home_team,
        away_team=away_team,
        commence_time=commence,
        candidate_bout_ids_json=candidates_json,
        reason=reason,
        rule_id=RULE_ODDS_BOUT_AMBIGUOUS,
        evidence_json=evidence_json,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row.id


def _committed_review_snapshot(
    session: Session, review_id: str
) -> tuple[str, int, str | None, str | None, int | None] | None:
    bind = session.get_bind()
    with bind.connect() as conn:
        row = conn.execute(
            select(
                OddsBoutMatchReview.status,
                OddsBoutMatchReview.version,
                OddsBoutMatchReview.decision_bout_id,
                OddsBoutMatchReview.activated_alias_id,
                OddsBoutMatchReview.activated_alias_version,
            ).where(OddsBoutMatchReview.id == review_id)
        ).one_or_none()
    if row is None:
        return None
    return (
        str(row[0]),
        int(row[1]),
        row[2],
        row[3],
        int(row[4]) if row[4] is not None else None,
    )


def approve_bout_match_review(
    session: Session,
    *,
    review_id: str,
    bout_id: str,
    actor: str,
    expected_version: int,
    observed_at: datetime | None = None,
) -> OddsBoutMatchReview:
    """Approve a pending review and activate the selected bout alias."""
    from mma_model.odds.reconcile import activate_provider_alias

    actor_text = (actor or "").strip()
    if not actor_text:
        raise OddsBoutMatchReviewError("actor is required")
    stamp = require_aware_utc(observed_at or _utc_now(), field="observed_at")
    review = session.get(OddsBoutMatchReview, review_id)
    if review is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    committed = _committed_review_snapshot(session, review_id)
    if committed is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    status, version, _decision, _alias_id, _alias_ver = committed
    if status != "pending":
        raise OddsBoutMatchReviewError(f"review not pending (status={status})")
    if version != expected_version:
        raise OddsBoutMatchReviewError(
            f"stale review version: expected {expected_version}, got {version}"
        )
    candidates = _parse_candidates(review.candidate_bout_ids_json)
    if not candidates:
        raise OddsBoutMatchReviewError(
            "empty candidate_bout_ids cannot be approved; revalidate candidates first"
        )
    if bout_id not in candidates:
        raise OddsBoutMatchReviewError(
            f"bout_id {bout_id!r} not in candidate set {candidates!r}"
        )
    contract = load_matching_contract()
    ok, reason = validate_linked_bout(
        session,
        bout_id=bout_id,
        home_team=review.home_team,
        away_team=review.away_team,
        commence_time=as_utc_sqlite(review.commence_time),
        max_delta_minutes=contract.match_window_minutes,
        require_dwcs=True,
    )
    if not ok:
        raise OddsBoutMatchReviewError(f"approval revalidation failed: {reason}")

    alias = activate_provider_alias(
        session,
        provider=review.provider,
        external_event_id=review.external_event_id,
        bout_id=bout_id,
        match_rule=MATCH_RULE_MANUAL_REVIEW,
        observed_at=stamp,
        evidence={
            "review_id": review_id,
            "approved_by": actor_text,
            "reason": review.reason,
        },
        write_immutable_source_id=True,
    )
    result = session.execute(
        update(OddsBoutMatchReview)
        .where(
            OddsBoutMatchReview.id == review_id,
            OddsBoutMatchReview.status == "pending",
            OddsBoutMatchReview.version == expected_version,
        )
        .values(
            status="approved",
            version=expected_version + 1,
            decision_bout_id=bout_id,
            activated_alias_id=alias.id,
            activated_alias_version=alias.alias_version,
            decided_by=actor_text,
            decided_at=stamp,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise OddsBoutMatchReviewError("concurrent review update rejected")

    apply_bout_lifecycle(
        session,
        bout_id=bout_id,
        lifecycle=OddsBoutLifecycleState.ACTIVE,
        evidence_kind="odds_bout_match_review_approved",
        observed_at=stamp,
        provider=review.provider,
        external_event_id=review.external_event_id,
        detail=f"review_id={review_id}",
        allow_terminal_override=False,
    )
    session.refresh(review)
    set_committed_value(review, "status", "approved")
    set_committed_value(review, "version", expected_version + 1)
    set_committed_value(review, "decision_bout_id", bout_id)
    set_committed_value(review, "activated_alias_id", alias.id)
    set_committed_value(review, "activated_alias_version", alias.alias_version)
    return review


def reject_bout_match_review(
    session: Session,
    *,
    review_id: str,
    actor: str,
    expected_version: int,
    observed_at: datetime | None = None,
) -> OddsBoutMatchReview:
    """Reject a pending review; match remains blocked (no alias activation)."""
    actor_text = (actor or "").strip()
    if not actor_text:
        raise OddsBoutMatchReviewError("actor is required")
    stamp = require_aware_utc(observed_at or _utc_now(), field="observed_at")
    review = session.get(OddsBoutMatchReview, review_id)
    if review is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    committed = _committed_review_snapshot(session, review_id)
    if committed is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    status, version, _decision, _alias_id, _alias_ver = committed
    if status != "pending":
        raise OddsBoutMatchReviewError(f"review not pending (status={status})")
    if version != expected_version:
        raise OddsBoutMatchReviewError(
            f"stale review version: expected {expected_version}, got {version}"
        )
    result = session.execute(
        update(OddsBoutMatchReview)
        .where(
            OddsBoutMatchReview.id == review_id,
            OddsBoutMatchReview.status == "pending",
            OddsBoutMatchReview.version == expected_version,
        )
        .values(
            status="rejected",
            version=expected_version + 1,
            decision_bout_id=None,
            decided_by=actor_text,
            decided_at=stamp,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise OddsBoutMatchReviewError("concurrent review update rejected")
    session.refresh(review)
    set_committed_value(review, "status", "rejected")
    set_committed_value(review, "version", expected_version + 1)
    set_committed_value(review, "decision_bout_id", None)
    return review


def reverse_bout_match_review(
    session: Session,
    *,
    review_id: str,
    actor: str,
    expected_version: int,
    observed_at: datetime | None = None,
) -> OddsBoutMatchReview:
    """Reverse an approved/rejected review; supersede only this review's alias."""
    from mma_model.odds.reconcile import supersede_provider_alias_if_active

    actor_text = (actor or "").strip()
    if not actor_text:
        raise OddsBoutMatchReviewError("actor is required")
    stamp = require_aware_utc(observed_at or _utc_now(), field="observed_at")
    review = session.get(OddsBoutMatchReview, review_id)
    if review is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    committed = _committed_review_snapshot(session, review_id)
    if committed is None:
        raise OddsBoutMatchReviewError(f"review not found: {review_id}")
    status, version, decision_bout_id, activated_alias_id, _alias_ver = committed
    if status not in {"approved", "rejected"}:
        raise OddsBoutMatchReviewError(
            f"only approved/rejected reviews reverse (status={status})"
        )
    if version != expected_version:
        raise OddsBoutMatchReviewError(
            f"stale review version: expected {expected_version}, got {version}"
        )
    reversed_owned_alias = False
    preserved_newer_alias = False
    if status == "approved" and activated_alias_id:
        owned = session.get(OddsProviderEventAlias, activated_alias_id)
        if owned is not None and owned.status == "active":
            reversed_owned_alias = supersede_provider_alias_if_active(
                session,
                alias_id=activated_alias_id,
                observed_at=stamp,
            )
            if reversed_owned_alias and decision_bout_id:
                apply_bout_lifecycle(
                    session,
                    bout_id=decision_bout_id,
                    lifecycle=OddsBoutLifecycleState.REVIEW_BLOCKED,
                    evidence_kind="odds_bout_match_review_reversed",
                    observed_at=stamp,
                    provider=review.provider,
                    external_event_id=review.external_event_id,
                    detail=f"review_id={review_id}",
                )
        else:
            preserved_newer_alias = True
    result = session.execute(
        update(OddsBoutMatchReview)
        .where(
            OddsBoutMatchReview.id == review_id,
            OddsBoutMatchReview.status.in_(("approved", "rejected")),
            OddsBoutMatchReview.version == expected_version,
        )
        .values(
            status="reversed",
            version=expected_version + 1,
            decided_by=actor_text,
            decided_at=stamp,
            updated_at=stamp,
            evidence_json=dump_evidence(
                {
                    **json.loads(review.evidence_json or "{}"),
                    "reversal": {
                        "reversed_owned_alias": reversed_owned_alias,
                        "preserved_newer_alias": preserved_newer_alias,
                        "activated_alias_id": activated_alias_id,
                    },
                }
            ),
        )
    )
    if result.rowcount != 1:
        raise OddsBoutMatchReviewError("concurrent review reverse rejected")
    session.refresh(review)
    set_committed_value(review, "status", "reversed")
    set_committed_value(review, "version", expected_version + 1)
    return review


def review_as_dict(review: OddsBoutMatchReview) -> dict[str, Any]:
    return {
        "id": review.id,
        "status": review.status,
        "version": review.version,
        "provider": review.provider,
        "external_event_id": review.external_event_id,
        "home_team": review.home_team,
        "away_team": review.away_team,
        "commence_time": as_utc_sqlite(review.commence_time).isoformat(),
        "candidate_bout_ids": list(_parse_candidates(review.candidate_bout_ids_json)),
        "reason": review.reason,
        "decision_bout_id": review.decision_bout_id,
        "activated_alias_id": review.activated_alias_id,
        "activated_alias_version": review.activated_alias_version,
        "decided_by": review.decided_by,
    }
