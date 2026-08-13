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

from mma_model.db.tables.core import CanonicalBout
from mma_model.db.tables.odds import OddsBoutMatchReview
from mma_model.odds.lifecycle import OddsBoutLifecycleState, apply_bout_lifecycle
from mma_model.odds.matching import (
    MATCH_RULE_PROVIDER_ID,
    as_utc_sqlite,
    dump_evidence,
    require_aware_utc,
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
    """Idempotently enqueue a pending odds-bout match review; returns review id."""
    stamp = require_aware_utc(observed_at or _utc_now(), field="observed_at")
    commence = require_aware_utc(commence_time, field="commence_time")
    existing = session.scalar(
        select(OddsBoutMatchReview).where(
            OddsBoutMatchReview.provider == provider,
            OddsBoutMatchReview.external_event_id == external_event_id,
            OddsBoutMatchReview.status == "pending",
        )
    )
    if existing is not None:
        existing.candidate_bout_ids_json = json.dumps(list(candidate_bout_ids))
        existing.reason = reason
        existing.home_team = home_team
        existing.away_team = away_team
        existing.commence_time = commence
        existing.updated_at = stamp
        session.flush()
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
        candidate_bout_ids_json=json.dumps(list(candidate_bout_ids)),
        reason=reason,
        rule_id=RULE_ODDS_BOUT_AMBIGUOUS,
        evidence_json=dump_evidence(
            {
                "kind": "odds_bout_match_review",
                "reason": reason,
                "candidate_bout_ids": list(candidate_bout_ids),
                "actor": actor,
            }
        ),
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row.id


def _committed_review_snapshot(
    session: Session, review_id: str
) -> tuple[str, int, str | None] | None:
    bind = session.get_bind()
    with bind.connect() as conn:
        row = conn.execute(
            select(
                OddsBoutMatchReview.status,
                OddsBoutMatchReview.version,
                OddsBoutMatchReview.decision_bout_id,
            ).where(OddsBoutMatchReview.id == review_id)
        ).one_or_none()
    if row is None:
        return None
    return (str(row[0]), int(row[1]), row[2])


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
    status, version, _decision = committed
    if status != "pending":
        raise OddsBoutMatchReviewError(f"review not pending (status={status})")
    if version != expected_version:
        raise OddsBoutMatchReviewError(
            f"stale review version: expected {expected_version}, got {version}"
        )
    candidates = _parse_candidates(review.candidate_bout_ids_json)
    if candidates and bout_id not in candidates:
        raise OddsBoutMatchReviewError(
            f"bout_id {bout_id!r} not in candidate set {candidates!r}"
        )
    bout = session.get(CanonicalBout, bout_id)
    if bout is None:
        raise OddsBoutMatchReviewError(f"canonical bout missing: {bout_id}")
    if bout.status in {"cancelled", "canceled", "replaced"}:
        raise OddsBoutMatchReviewError(
            f"cannot approve inactive bout status={bout.status}"
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
            decided_by=actor_text,
            decided_at=stamp,
            updated_at=stamp,
        )
    )
    if result.rowcount != 1:
        raise OddsBoutMatchReviewError("concurrent review update rejected")

    activate_provider_alias(
        session,
        provider=review.provider,
        external_event_id=review.external_event_id,
        bout_id=bout_id,
        match_rule=MATCH_RULE_PROVIDER_ID,
        observed_at=stamp,
        evidence={
            "review_id": review_id,
            "approved_by": actor_text,
            "reason": review.reason,
        },
        write_immutable_source_id=True,
    )
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
    status, version, _decision = committed
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
    """Reverse an approved/rejected review back to pending; supersede active alias if any."""
    from mma_model.odds.reconcile import supersede_provider_aliases

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
    status, version, decision_bout_id = committed
    if status not in {"approved", "rejected"}:
        raise OddsBoutMatchReviewError(
            f"only approved/rejected reviews reverse (status={status})"
        )
    if version != expected_version:
        raise OddsBoutMatchReviewError(
            f"stale review version: expected {expected_version}, got {version}"
        )
    if status == "approved":
        supersede_provider_aliases(
            session,
            provider=review.provider,
            external_event_id=review.external_event_id,
            observed_at=stamp,
        )
        if decision_bout_id:
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
        "decided_by": review.decided_by,
    }
