"""Reverse/re-resolve/approve lifecycle and repeated reversal (DWCS-104)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterSourceId,
)
from mma_model.db.tables.identity import (
    IdentityMatchEvidence,
    IdentityReviewQueue,
    IdentityScoringBlock,
)
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.resolver import IdentityResolver, resolve_fighter
from mma_model.identity.review import apply_review_decision, reverse_review_decision

UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 18, 30, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"engine": engine, "Session": Session}
    engine.dispose()


def _seed(session, espn_id: str, name: str) -> str:
    fid = canonical_fighter_id(espn_id)
    session.add(CanonicalFighter(id=fid, display_name=name))
    session.add(FighterSourceId(fighter_id=fid, source="espn", external_id=espn_id))
    session.flush()
    return fid


def _active_blocks(session, *, source: str, external_id: str) -> list[IdentityScoringBlock]:
    review_ids = list(
        session.scalars(
            select(IdentityReviewQueue.id).where(
                IdentityReviewQueue.source == source,
                IdentityReviewQueue.external_id == external_id,
            )
        ).all()
    )
    if not review_ids:
        return []
    return list(
        session.scalars(
            select(IdentityScoringBlock).where(
                IdentityScoringBlock.review_id.in_(review_ids),
                IdentityScoringBlock.active.is_(True),
            )
        ).all()
    )


def _mappings(session, *, source: str, external_id: str) -> list[FighterSourceId]:
    return list(
        session.scalars(
            select(FighterSourceId).where(
                FighterSourceId.source == source,
                FighterSourceId.external_id == external_id,
            )
        ).all()
    )


def test_reverse_reresolve_approve_clears_blocks_and_keeps_one_mapping(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "30001", "Lifecycle A")
        opp = _seed(session, "30002", "Lifecycle Opp")
        session.add(
            CanonicalEvent(
                id="life-evt",
                name="Upcoming Card",
                series="dwcs",
                status="scheduled",
                event_date=date(2026, 11, 1),
            )
        )
        session.add(
            CanonicalEvent(
                id="done-evt",
                name="Completed Card",
                series="dwcs",
                status="completed",
                event_date=date(2020, 1, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="life-bout",
                event_id="life-evt",
                fighter_a_id=fid,
                fighter_b_id=opp,
                status="scheduled",
            )
        )
        session.add(
            CanonicalBout(
                id="done-bout",
                event_id="done-evt",
                fighter_a_id=fid,
                fighter_b_id=opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="life-bout", fighter_id=fid, corner="a"))
        session.add(BoutParticipant(bout_id="life-bout", fighter_id=opp, corner="b"))
        session.add(BoutParticipant(bout_id="done-bout", fighter_id=fid, corner="a"))
        session.add(BoutParticipant(bout_id="done-bout", fighter_id=opp, corner="b"))
        session.commit()

        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="life-1",
            display_name="Lifecycle A",
            bout_id="life-bout",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        first_review_id = queued.review_id
        assert queued.kind == "blocked"
        assert _active_blocks(session, source="tapology_public", external_id="life-1")
        resolver = IdentityResolver(session, actor="system", now=FIXED_NOW)
        assert resolver.is_bout_scoring_blocked("life-bout") is True
        assert resolver.is_bout_scoring_blocked("done-bout") is False

        apply_review_decision(
            session,
            review_id=first_review_id,
            decision="approve",
            canonical_id=fid,
            actor="alice",
            now=FIXED_NOW,
        )
        session.commit()
        assert len(_mappings(session, source="tapology_public", external_id="life-1")) == 1
        assert not _active_blocks(session, source="tapology_public", external_id="life-1")

        reverse_review_decision(
            session, review_id=first_review_id, actor="alice", now=FIXED_NOW
        )
        session.commit()
        assert _mappings(session, source="tapology_public", external_id="life-1") == []

        again = resolve_fighter(
            session,
            source="tapology_public",
            external_id="life-1",
            display_name="Lifecycle A",
            bout_id="life-bout",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        second_review_id = again.review_id
        assert second_review_id != first_review_id
        assert again.kind in {"queued", "blocked"}
        active = _active_blocks(session, source="tapology_public", external_id="life-1")
        assert len(active) == 1
        assert active[0].review_id == second_review_id
        assert not any(b.review_id == first_review_id and b.active for b in session.scalars(select(IdentityScoringBlock)).all() if b.active)

        apply_review_decision(
            session,
            review_id=second_review_id,
            decision="approve",
            canonical_id=fid,
            actor="alice",
            now=FIXED_NOW,
        )
        session.commit()

        maps = _mappings(session, source="tapology_public", external_id="life-1")
        assert len(maps) == 1
        assert maps[0].fighter_id == fid
        assert _active_blocks(session, source="tapology_public", external_id="life-1") == []
        resolver = IdentityResolver(session, actor="system", now=FIXED_NOW)
        assert resolver.is_bout_scoring_blocked("life-bout") is False
        assert resolver.is_bout_scoring_blocked("done-bout") is False
        actions = [e.action for e in session.scalars(select(IdentityMatchEvidence)).all()]
        assert actions.count("reversed") >= 1
        assert actions.count("approved") >= 2


def test_duplicate_reresolve_does_not_overlap_active_blocks(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "30011", "Dup Life")
        session.commit()
        first = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="dup-life",
            display_name="Dup Life",
            bout_id="up-1",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        second = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="dup-life",
            display_name="Dup Life",
            bout_id="up-1",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        assert first.review_id == second.review_id
        active = _active_blocks(session, source="sherdog_public", external_id="dup-life")
        assert len(active) == 1
        _ = fid


def test_repeated_approve_reverse_cycles_keep_history(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "30021", "Cycle Person")
        session.commit()
        review_ids: list[str] = []
        for i in range(2):
            queued = resolve_fighter(
                session,
                source="tapology_public",
                external_id="cycle-1",
                display_name="Cycle Person",
                bout_id="cycle-bout",
                bout_status="upcoming",
                actor="system",
                now=FIXED_NOW,
            )
            session.commit()
            rid = queued.review_id
            assert rid not in review_ids
            review_ids.append(rid)
            apply_review_decision(
                session,
                review_id=rid,
                decision="approve",
                canonical_id=fid,
                actor="bob",
                now=FIXED_NOW,
            )
            session.commit()
            reverse_review_decision(session, review_id=rid, actor="bob", now=FIXED_NOW)
            session.commit()

        reversed_rows = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "tapology_public",
                    IdentityReviewQueue.external_id == "cycle-1",
                    IdentityReviewQueue.status == "reversed",
                )
            ).all()
        )
        assert len(reversed_rows) == 2
        assert {r.id for r in reversed_rows} == set(review_ids)
        pending = session.scalar(
            select(func.count())
            .select_from(IdentityReviewQueue)
            .where(
                IdentityReviewQueue.source == "tapology_public",
                IdentityReviewQueue.external_id == "cycle-1",
                IdentityReviewQueue.status == "pending",
            )
        )
        assert int(pending or 0) == 0
        evidence = list(session.scalars(select(IdentityMatchEvidence)).all())
        assert sum(1 for e in evidence if e.action == "reversed") == 2
        assert sum(1 for e in evidence if e.action == "approved") == 2
        assert _mappings(session, source="tapology_public", external_id="cycle-1") == []


def test_reject_reresolve_creates_new_pending(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed(session, "30041", "Reject Cycle")
        session.commit()
        first = resolve_fighter(
            session,
            source="tapology_public",
            external_id="rej-cycle",
            display_name="Reject Cycle",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=first.review_id,
            decision="reject",
            canonical_id=None,
            actor="bob",
            now=FIXED_NOW,
        )
        session.commit()
        second = resolve_fighter(
            session,
            source="tapology_public",
            external_id="rej-cycle",
            display_name="Reject Cycle",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        assert second.kind in {"queued", "blocked"}
        assert second.review_id != first.review_id
        pending = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "tapology_public",
                    IdentityReviewQueue.external_id == "rej-cycle",
                    IdentityReviewQueue.status == "pending",
                )
            ).all()
        )
        rejected = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "tapology_public",
                    IdentityReviewQueue.external_id == "rej-cycle",
                    IdentityReviewQueue.status == "rejected",
                )
            ).all()
        )
        assert len(pending) == 1
        assert len(rejected) == 1
        assert pending[0].id == second.review_id


def test_approve_different_name_reresolve_queues_conflict(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "30051", "Approved Name")
        session.commit()
        first = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="appr-diff",
            display_name="Approved Name",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=first.review_id,
            decision="approve",
            canonical_id=fid,
            actor="bob",
            now=FIXED_NOW,
        )
        session.commit()
        assert len(_mappings(session, source="sherdog_public", external_id="appr-diff")) == 1
        second = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="appr-diff",
            display_name="Completely Different Person",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        assert second.kind in {"queued", "blocked"}
        assert second.rule_id == "identity_conflict_queue"
        pending = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "sherdog_public",
                    IdentityReviewQueue.external_id == "appr-diff",
                    IdentityReviewQueue.status == "pending",
                )
            ).all()
        )
        approved = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "sherdog_public",
                    IdentityReviewQueue.external_id == "appr-diff",
                    IdentityReviewQueue.status == "approved",
                )
            ).all()
        )
        assert len(pending) == 1
        assert len(approved) == 1
        assert len(_mappings(session, source="sherdog_public", external_id="appr-diff")) == 1


def test_repeated_reject_reresolve_cycles(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed(session, "30061", "Repeat Reject")
        session.commit()
        ids: list[str] = []
        for _ in range(2):
            queued = resolve_fighter(
                session,
                source="tapology_public",
                external_id="rep-rej",
                display_name="Repeat Reject",
                actor="system",
                now=FIXED_NOW,
            )
            session.commit()
            assert queued.review_id not in ids
            ids.append(queued.review_id)
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="reject",
                canonical_id=None,
                actor="bob",
                now=FIXED_NOW,
            )
            session.commit()
        rejected = list(
            session.scalars(
                select(IdentityReviewQueue).where(
                    IdentityReviewQueue.source == "tapology_public",
                    IdentityReviewQueue.external_id == "rep-rej",
                    IdentityReviewQueue.status == "rejected",
                )
            ).all()
        )
        assert len(rejected) == 2


def test_approved_does_not_block_second_pending_index(env) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = env["engine"]
    Session = env["Session"]
    now = FIXED_NOW
    with Session() as session:
        session.add(
            IdentityReviewQueue(
                id="appr-open",
                status="approved",
                version=1,
                source="tapology_public",
                external_id="idx-mix",
                display_name="Mix",
                normalized_name="mix",
                candidate_canonical_ids_json="[]",
                evidence_json="{}",
                rule_id="manual_enqueue",
                resolver_version="1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    other = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with other() as session:
        session.add(
            IdentityReviewQueue(
                id="pend-open",
                status="pending",
                version=1,
                source="tapology_public",
                external_id="idx-mix",
                display_name="Mix",
                normalized_name="mix",
                candidate_canonical_ids_json="[]",
                evidence_json="{}",
                rule_id="manual_enqueue",
                resolver_version="1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    with Session() as session:
        session.add(
            IdentityReviewQueue(
                id="pend-dup",
                status="pending",
                version=1,
                source="tapology_public",
                external_id="idx-mix",
                display_name="Mix",
                normalized_name="mix",
                candidate_canonical_ids_json="[]",
                evidence_json="{}",
                rule_id="manual_enqueue",
                resolver_version="1",
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_cannot_reapprove_reversed_row(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed(session, "30031", "No Reapprove")
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="no-reapp",
            display_name="No Reapprove",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        apply_review_decision(
            session,
            review_id=queued.review_id,
            decision="approve",
            canonical_id=fid,
            actor="bob",
            now=FIXED_NOW,
        )
        session.commit()
        reverse_review_decision(
            session, review_id=queued.review_id, actor="bob", now=FIXED_NOW
        )
        session.commit()
        from mma_model.identity.review import ReviewDecisionError

        with pytest.raises(ReviewDecisionError, match="pending|reversed"):
            apply_review_decision(
                session,
                review_id=queued.review_id,
                decision="approve",
                canonical_id=fid,
                actor="bob",
                now=FIXED_NOW,
            )


def test_concurrent_pending_reviews_are_rejected(env) -> None:
    from sqlalchemy.exc import IntegrityError

    from mma_model.db.tables.identity import IdentityReviewQueue

    engine = env["engine"]
    Session = env["Session"]
    now = FIXED_NOW
    with Session() as session:
        session.add(
            IdentityReviewQueue(
                id="open-1",
                status="pending",
                version=1,
                source="tapology_public",
                external_id="conc-open",
                display_name="Conc",
                normalized_name="conc",
                candidate_canonical_ids_json="[]",
                evidence_json="{}",
                rule_id="manual_enqueue",
                resolver_version="1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    other = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with other() as session:
        session.add(
            IdentityReviewQueue(
                id="open-2",
                status="pending",
                version=1,
                source="tapology_public",
                external_id="conc-open",
                display_name="Conc",
                normalized_name="conc",
                candidate_canonical_ids_json="[]",
                evidence_json="{}",
                rule_id="manual_enqueue",
                resolver_version="1",
                created_at=now,
                updated_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
