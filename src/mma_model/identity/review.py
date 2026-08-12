"""Reversible identity review queue operations (DWCS-104)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.db.tables.identity import (
    IdentityMatchEvidence,
    IdentityReviewQueue,
    IdentityScoringBlock,
)
from mma_model.identity.constants import RESOLVER_VERSION
from mma_model.identity.models import ReviewCandidate, dump_evidence_json
from mma_model.identity.normalize import normalize_person_name

ReviewDecision = Literal["approve", "reject"]


class ReviewDecisionError(ValueError):
    """Invalid or stale review decision."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_actor(actor: str) -> str:
    text = (actor or "").strip()
    if not text:
        raise ReviewDecisionError("actor is required")
    return text


def _require_review_id(review_id: str) -> str:
    text = (review_id or "").strip()
    if not text:
        raise ReviewDecisionError("review_id is required")
    return text


def _committed_review_snapshot(
    session: Session, review_id: str
) -> tuple[str, int, str | None] | None:
    """Read committed review state on a fresh connection (SQLite snapshot-safe)."""
    bind = session.get_bind()
    with bind.connect() as conn:
        row = conn.execute(
            select(
                IdentityReviewQueue.status,
                IdentityReviewQueue.version,
                IdentityReviewQueue.decision_canonical_id,
            ).where(IdentityReviewQueue.id == review_id)
        ).one_or_none()
    if row is None:
        return None
    return (str(row[0]), int(row[1]), row[2])


def _sync_review_from_committed(
    review: IdentityReviewQueue,
    *,
    status: str,
    version: int,
    decision_canonical_id: str | None,
) -> None:
    set_committed_value(review, "status", status)
    set_committed_value(review, "version", version)
    set_committed_value(review, "decision_canonical_id", decision_canonical_id)


def _write_evidence(
    session: Session,
    *,
    action: str,
    rule_id: str,
    source: str,
    external_id: str,
    display_name: str,
    normalized_name: str,
    actor: str,
    evidence: dict[str, Any],
    wikidata_id: str | None = None,
    dob: Any = None,
    before_canonical_id: str | None = None,
    after_canonical_id: str | None = None,
    review_id: str | None = None,
    bout_id: str | None = None,
    reversible: bool = True,
    now: datetime | None = None,
) -> IdentityMatchEvidence:
    row = IdentityMatchEvidence(
        id=str(uuid.uuid4()),
        created_at=now or _utc_now(),
        resolver_version=RESOLVER_VERSION,
        rule_id=rule_id,
        action=action,
        source=source,
        external_id=external_id,
        display_name=display_name,
        normalized_name=normalized_name,
        wikidata_id=wikidata_id,
        dob=dob,
        actor=actor,
        before_canonical_id=before_canonical_id,
        after_canonical_id=after_canonical_id,
        review_id=review_id,
        bout_id=bout_id,
        evidence_json=dump_evidence_json(evidence),
        reversible=reversible,
        status="active",
    )
    session.add(row)
    session.flush()
    return row


def enqueue_review(
    session: Session,
    candidate: ReviewCandidate,
    *,
    actor: str = "system",
    now: datetime | None = None,
) -> str:
    """Idempotently enqueue a pending review; returns review id."""
    actor = _require_actor(actor)
    stamp = now or _utc_now()
    existing = session.scalar(
        select(IdentityReviewQueue).where(
            IdentityReviewQueue.source == candidate.source,
            IdentityReviewQueue.external_id == candidate.external_id,
            IdentityReviewQueue.status == "pending",
        )
    )
    if existing is not None:
        _write_evidence(
            session,
            action="queued",
            rule_id=candidate.rule_id,
            source=candidate.source,
            external_id=candidate.external_id,
            display_name=candidate.display_name,
            normalized_name=candidate.normalized_name,
            actor=actor,
            evidence={
                "idempotent_enqueue": True,
                "review_id": existing.id,
                **dict(candidate.evidence),
            },
            wikidata_id=candidate.wikidata_id,
            dob=candidate.dob,
            review_id=existing.id,
            bout_id=candidate.bout_id,
            now=stamp,
        )
        return existing.id

    review_id = str(uuid.uuid4())
    row = IdentityReviewQueue(
        id=review_id,
        status="pending",
        version=1,
        source=candidate.source,
        external_id=candidate.external_id,
        display_name=candidate.display_name,
        normalized_name=candidate.normalized_name
        or normalize_person_name(candidate.display_name),
        wikidata_id=candidate.wikidata_id,
        dob=candidate.dob,
        candidate_canonical_ids_json=json.dumps(
            list(candidate.candidate_canonical_ids), sort_keys=False
        ),
        evidence_json=dump_evidence_json(dict(candidate.evidence)),
        bout_id=candidate.bout_id,
        bout_status=candidate.bout_status,
        prior_mapping_json=candidate.prior_mapping_json,
        rule_id=candidate.rule_id,
        resolver_version=RESOLVER_VERSION,
        reversible=True,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    evidence = _write_evidence(
        session,
        action="queued",
        rule_id=candidate.rule_id,
        source=candidate.source,
        external_id=candidate.external_id,
        display_name=candidate.display_name,
        normalized_name=row.normalized_name,
        actor=actor,
        evidence=dict(candidate.evidence),
        wikidata_id=candidate.wikidata_id,
        dob=candidate.dob,
        review_id=review_id,
        bout_id=candidate.bout_id,
        now=stamp,
    )
    if candidate.bout_id and (candidate.bout_status or "") in {
        "upcoming",
        "evaluated",
        "scheduled",
    }:
        session.add(
            IdentityScoringBlock(
                id=str(uuid.uuid4()),
                bout_id=candidate.bout_id,
                review_id=review_id,
                reason="unresolved_identity",
                active=True,
                evidence_id=evidence.id,
                created_at=stamp,
            )
        )
        session.flush()
    return review_id


def list_reviews(
    session: Session, *, status: str | None = "pending"
) -> list[IdentityReviewQueue]:
    stmt = select(IdentityReviewQueue).order_by(
        IdentityReviewQueue.created_at.asc(), IdentityReviewQueue.id.asc()
    )
    if status is not None:
        stmt = stmt.where(IdentityReviewQueue.status == status)
    return list(session.scalars(stmt).all())


def _clear_blocks_for_review(
    session: Session, review_id: str, *, now: datetime
) -> None:
    blocks = session.scalars(
        select(IdentityScoringBlock).where(
            IdentityScoringBlock.review_id == review_id,
            IdentityScoringBlock.active.is_(True),
        )
    ).all()
    for block in blocks:
        block.active = False
        block.cleared_at = now


def _upsert_source_mapping(
    session: Session, *, fighter_id: str, source: str, external_id: str
) -> FighterSourceId | None:
    existing = session.scalar(
        select(FighterSourceId).where(
            FighterSourceId.source == source,
            FighterSourceId.external_id == external_id,
        )
    )
    if existing is not None:
        return existing
    row = FighterSourceId(fighter_id=fighter_id, source=source, external_id=external_id)
    session.add(row)
    session.flush()
    return row


def apply_review_decision(
    session: Session,
    *,
    review_id: str,
    decision: ReviewDecision,
    canonical_id: str | None,
    actor: str,
    expected_version: int | None = None,
    now: datetime | None = None,
) -> IdentityReviewQueue:
    review_id = _require_review_id(review_id)
    actor = _require_actor(actor)
    stamp = now or _utc_now()
    if decision not in {"approve", "reject"}:
        raise ReviewDecisionError(f"unsupported decision: {decision!r}")

    review = session.get(IdentityReviewQueue, review_id)
    if review is None:
        raise ReviewDecisionError(f"review_id not found: {review_id}")

    committed = _committed_review_snapshot(session, review_id)
    if committed is not None:
        committed_status, committed_version, committed_canonical = committed
        if committed_version > int(review.version):
            _sync_review_from_committed(
                review,
                status=committed_status,
                version=committed_version,
                decision_canonical_id=committed_canonical,
            )

    if expected_version is not None and int(review.version) != int(expected_version):
        raise ReviewDecisionError(
            f"stale review version: expected {expected_version}, got {review.version}"
        )

    # Decision idempotency: identical terminal decision is a no-op.
    if decision == "approve" and review.status == "approved":
        if canonical_id and review.decision_canonical_id == canonical_id:
            return review
        raise ReviewDecisionError("review already approved with different canonical_id")
    if decision == "reject" and review.status == "rejected":
        return review
    if review.status not in {"pending", "reversed"}:
        raise ReviewDecisionError(
            f"review status {review.status!r} cannot accept decision {decision!r}"
        )

    before = review.decision_canonical_id
    if decision == "approve":
        if not canonical_id or not str(canonical_id).strip():
            raise ReviewDecisionError("approve requires explicit canonical_id")
        canonical_id = str(canonical_id).strip()
        fighter = session.get(CanonicalFighter, canonical_id)
        if fighter is None:
            raise ReviewDecisionError(f"canonical_id does not exist: {canonical_id}")
        existing_map = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == review.source,
                FighterSourceId.external_id == review.external_id,
            )
        )
        prior = None
        if existing_map is not None:
            prior = {
                "fighter_id": existing_map.fighter_id,
                "source": existing_map.source,
                "external_id": existing_map.external_id,
            }
            if existing_map.fighter_id != canonical_id:
                raise ReviewDecisionError(
                    "existing source mapping conflicts with approve canonical_id"
                )
        else:
            _upsert_source_mapping(
                session,
                fighter_id=canonical_id,
                source=review.source,
                external_id=review.external_id,
            )
        review.prior_mapping_json = json.dumps(prior, sort_keys=True) if prior else "null"
        review.status = "approved"
        review.decision_canonical_id = canonical_id
        after = canonical_id
        action = "approved"
    else:
        review.status = "rejected"
        review.decision_canonical_id = None
        after = None
        action = "rejected"

    review.decided_by = actor
    review.decided_at = stamp
    review.updated_at = stamp
    review.version = int(review.version) + 1
    evidence = _write_evidence(
        session,
        action=action,
        rule_id=f"manual_{decision}",
        source=review.source,
        external_id=review.external_id,
        display_name=review.display_name,
        normalized_name=review.normalized_name,
        actor=actor,
        evidence={
            "decision": decision,
            "review_version": review.version,
            "prior_mapping_json": review.prior_mapping_json,
        },
        wikidata_id=review.wikidata_id,
        dob=review.dob,
        before_canonical_id=before,
        after_canonical_id=after,
        review_id=review.id,
        bout_id=review.bout_id,
        now=stamp,
    )
    _clear_blocks_for_review(session, review.id, now=stamp)
    session.flush()
    _ = evidence
    return review


def reverse_review_decision(
    session: Session,
    *,
    review_id: str,
    actor: str,
    now: datetime | None = None,
) -> IdentityReviewQueue:
    """Create a new audited reversal transition; never delete history."""
    review_id = _require_review_id(review_id)
    actor = _require_actor(actor)
    stamp = now or _utc_now()
    review = session.get(IdentityReviewQueue, review_id)
    if review is None:
        raise ReviewDecisionError(f"review_id not found: {review_id}")
    committed = _committed_review_snapshot(session, review_id)
    if committed is not None and committed[1] > int(review.version):
        _sync_review_from_committed(
            review,
            status=committed[0],
            version=committed[1],
            decision_canonical_id=committed[2],
        )
    if not review.reversible:
        raise ReviewDecisionError("review is not reversible")
    if review.status not in {"approved", "rejected"}:
        raise ReviewDecisionError(
            f"review status {review.status!r} cannot be reversed"
        )

    before = review.decision_canonical_id
    if review.status == "approved":
        mapping = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == review.source,
                FighterSourceId.external_id == review.external_id,
            )
        )
        if mapping is not None and (
            before is None or mapping.fighter_id == before
        ):
            # Only remove the mapping created/owned by this approval path.
            prior = None
            if review.prior_mapping_json and review.prior_mapping_json != "null":
                prior = json.loads(review.prior_mapping_json)
            if prior is None:
                session.delete(mapping)
            elif prior.get("fighter_id") != mapping.fighter_id:
                # Restore prior fighter_id safely.
                mapping.fighter_id = str(prior["fighter_id"])

    review.status = "reversed"
    review.decision_canonical_id = None
    review.decided_by = actor
    review.decided_at = stamp
    review.updated_at = stamp
    review.version = int(review.version) + 1
    _write_evidence(
        session,
        action="reversed",
        rule_id="manual_reverse",
        source=review.source,
        external_id=review.external_id,
        display_name=review.display_name,
        normalized_name=review.normalized_name,
        actor=actor,
        evidence={
            "reversed_from": "approved" if before else "rejected",
            "review_version": review.version,
        },
        wikidata_id=review.wikidata_id,
        dob=review.dob,
        before_canonical_id=before,
        after_canonical_id=None,
        review_id=review.id,
        bout_id=review.bout_id,
        now=stamp,
    )
    # Re-activate or create scoring block if bout still unresolved after reverse.
    if review.bout_id and (review.bout_status or "") in {
        "upcoming",
        "evaluated",
        "scheduled",
    }:
        existing_block = session.scalar(
            select(IdentityScoringBlock).where(
                IdentityScoringBlock.bout_id == review.bout_id,
                IdentityScoringBlock.review_id == review.id,
            )
        )
        if existing_block is not None:
            existing_block.active = True
            existing_block.cleared_at = None
            existing_block.reason = "identity_reversed_unresolved"
        else:
            session.add(
                IdentityScoringBlock(
                    id=str(uuid.uuid4()),
                    bout_id=review.bout_id,
                    review_id=review.id,
                    reason="identity_reversed_unresolved",
                    active=True,
                    created_at=stamp,
                )
            )
    session.flush()
    return review
