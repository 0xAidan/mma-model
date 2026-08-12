"""Reversible identity review queue tests (DWCS-104). Disposable temp DB only."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.db.tables.identity import IdentityMatchEvidence, IdentityReviewQueue
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.models import ReviewCandidate
from mma_model.identity.resolver import resolve_fighter
from mma_model.identity.review import (
    ReviewDecisionError,
    apply_review_decision,
    enqueue_review,
    list_reviews,
    reverse_review_decision,
)

UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 18, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path):
    db_path = tmp_path / "review.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"engine": engine, "Session": Session, "db_path": db_path}
    engine.dispose()


def _seed(session, espn_id: str, name: str) -> str:
    fid = canonical_fighter_id(espn_id)
    session.add(CanonicalFighter(id=fid, display_name=name))
    session.add(FighterSourceId(fighter_id=fid, source="espn", external_id=espn_id))
    session.flush()
    return fid


def test_approve_requires_explicit_ids_and_validates_canonical(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "100", "Canon")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="r1",
            display_name="Canon",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        with pytest.raises(ReviewDecisionError, match="review_id"):
            apply_review_decision(
                session,
                review_id="",
                decision="approve",
                canonical_id=fid,
                actor="alice",
                now=FIXED_NOW,
            )
        with pytest.raises(ReviewDecisionError, match="canonical"):
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="approve",
                canonical_id=None,
                actor="alice",
                now=FIXED_NOW,
            )
        with pytest.raises(ReviewDecisionError, match="canonical|exist"):
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="approve",
                canonical_id="00000000-0000-0000-0000-000000000000",
                actor="alice",
                now=FIXED_NOW,
            )
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="approve",
            canonical_id=fid,
            actor="alice",
            now=FIXED_NOW,
        )
        session.commit()
        mapping = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == "tapology_public",
                FighterSourceId.external_id == "r1",
            )
        )
        review = session.get(IdentityReviewQueue, queued.review_id)
        evidence = session.scalars(select(IdentityMatchEvidence)).all()
    assert mapping is not None
    assert mapping.fighter_id == fid
    assert review is not None
    assert review.status == "approved"
    assert any(e.action == "approved" and e.actor == "alice" for e in evidence)


def test_reject_and_decision_idempotency(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed(session, "101", "Reject Me")
        session.commit()
        queued = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="rj-1",
            display_name="Reject Me",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="reject",
            canonical_id=None,
            actor="bob",
            now=FIXED_NOW,
        )
        session.commit()
        # Idempotent replay of same decision.
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="reject",
            canonical_id=None,
            actor="bob",
            now=FIXED_NOW,
        )
        session.commit()
        review = session.get(IdentityReviewQueue, queued.review_id)
        rejects = [
            e
            for e in session.scalars(select(IdentityMatchEvidence)).all()
            if e.action == "rejected"
        ]
    assert review is not None
    assert review.status == "rejected"
    assert len(rejects) == 1


def test_reversal_creates_new_transition_and_restores_mapping(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "102", "Reverse Me")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="rev-1",
            display_name="Reverse Me",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="approve",
            canonical_id=fid,
            actor="carol",
            now=FIXED_NOW,
        )
        session.commit()
        assert (
            session.scalar(
                select(FighterSourceId).where(
                    FighterSourceId.source == "tapology_public",
                    FighterSourceId.external_id == "rev-1",
                )
            )
            is not None
        )
        reverse_review_decision(
            session, review_id=queued.review_id, actor="carol", now=FIXED_NOW
        )
        session.commit()
        mapping = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == "tapology_public",
                FighterSourceId.external_id == "rev-1",
            )
        )
        review = session.get(IdentityReviewQueue, queued.review_id)
        actions = [e.action for e in session.scalars(select(IdentityMatchEvidence)).all()]
        # History retained — no deletes of prior evidence.
        assert "approved" in actions
        assert "reversed" in actions
    assert mapping is None
    assert review is not None
    assert review.status == "reversed"


def test_stale_review_version_rejected(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "103", "Stale")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="stale-1",
            display_name="Stale",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        with pytest.raises(ReviewDecisionError, match="version|stale"):
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="approve",
                canonical_id=fid,
                actor="dave",
                expected_version=0,
                now=FIXED_NOW,
            )


def test_actor_validation(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "104", "Actor")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="act-1",
            display_name="Actor",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        with pytest.raises(ReviewDecisionError, match="actor"):
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="approve",
                canonical_id=fid,
                actor="",
                now=FIXED_NOW,
            )


def test_concurrent_approvals_one_wins(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "105", "Concurrent")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="conc-1",
            display_name="Concurrent",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        review_id = queued.review_id

    errors: list[BaseException] = []
    successes = 0
    lock = threading.Lock()

    def _approve(actor: str) -> None:
        nonlocal successes
        local = sessionmaker(
            bind=env["engine"], autoflush=False, autocommit=False, future=True
        )
        try:
            with local() as session:
                apply_review_decision(
                    session,
                    review_id=review_id,
                    decision="approve",
                    canonical_id=fid,
                    actor=actor,
                    now=FIXED_NOW,
                )
                session.commit()
            with lock:
                successes += 1
        except Exception as exc:  # noqa: BLE001 - collect race outcomes
            errors.append(exc)

    t1 = threading.Thread(target=_approve, args=("t1",))
    t2 = threading.Thread(target=_approve, args=("t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At most one mutating success; the other may idempotently succeed or error.
    with Session() as session:
        review = session.get(IdentityReviewQueue, review_id)
        approved = [
            e
            for e in session.scalars(select(IdentityMatchEvidence)).all()
            if e.action == "approved"
        ]
        mapping = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.source == "tapology_public",
                FighterSourceId.external_id == "conc-1",
            )
        )
    assert review is not None
    assert review.status == "approved"
    assert mapping is not None
    assert mapping.fighter_id == fid
    assert len(approved) == 1


def test_reversal_after_dependent_bout_mapping_safe(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "106", "Dependent")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="dep-1",
            display_name="Dependent",
            bout_id="bout-dep",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="approve",
            canonical_id=fid,
            actor="erin",
            now=FIXED_NOW,
        )
        session.commit()
        reverse_review_decision(
            session, review_id=queued.review_id, actor="erin", now=FIXED_NOW
        )
        session.commit()
        evidence = session.scalars(select(IdentityMatchEvidence)).all()
        review = session.get(IdentityReviewQueue, queued.review_id)
    assert review is not None
    assert review.status == "reversed"
    assert any(e.before_canonical_id == fid or e.after_canonical_id is None for e in evidence)
    # Prior evidence rows remain.
    assert len(evidence) >= 3


def test_stale_session_approve_does_not_duplicate_evidence(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "108", "Stale Session")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="stale-sess-1",
            display_name="Stale Session",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        review_id = queued.review_id

    with Session() as session_a:
        loaded = session_a.get(IdentityReviewQueue, review_id)
        assert loaded is not None
        assert loaded.status == "pending"
        with Session() as session_b:
            apply_review_decision(
                session_b,
                review_id=review_id,
                decision="approve",
                canonical_id=fid,
                actor="first",
                now=FIXED_NOW,
            )
            session_b.commit()
        apply_review_decision(
            session_a,
            review_id=review_id,
            decision="approve",
            canonical_id=fid,
            actor="second",
            now=FIXED_NOW,
        )
        session_a.commit()

    with Session() as session:
        review = session.get(IdentityReviewQueue, review_id)
        approved = [
            e
            for e in session.scalars(select(IdentityMatchEvidence)).all()
            if e.action == "approved"
        ]
    assert review is not None
    assert review.status == "approved"
    assert len(approved) == 1


def test_malformed_evidence_rejected_on_enqueue(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed(session, "109", "Bad JSON")
        session.commit()
        with pytest.raises(ValueError, match="malformed evidence"):
            enqueue_review(
                session,
                ReviewCandidate(
                    source="tapology_public",
                    external_id="bad-json-1",
                    display_name="Bad JSON",
                    normalized_name="bad json",
                    rule_id="manual_enqueue",
                    evidence={"oops": object()},
                ),
                actor="system",
                now=FIXED_NOW,
            )


def test_enqueue_review_direct_and_list(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed(session, "107", "Listable")
        session.commit()
        rid = enqueue_review(
            session,
            ReviewCandidate(
                source="combat_registry",
                external_id="cr-1",
                display_name="Listable",
                normalized_name="listable",
                candidate_canonical_ids=(canonical_fighter_id("107"),),
                rule_id="manual_enqueue",
                evidence={"reason": "manual"},
            ),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        pending = list_reviews(session, status="pending")
    assert rid
    assert any(r.id == rid for r in pending)
