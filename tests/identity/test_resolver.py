"""Deterministic identity resolver tests (DWCS-104). Disposable temp DB only."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterProfileObservation,
    FighterSourceId,
)
from mma_model.db.tables.identity import (
    IdentityMatchEvidence,
    IdentityReviewQueue,
    IdentityScoringBlock,
)
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.models import ReviewCandidate
from mma_model.identity.normalize import normalize_person_name
from mma_model.identity.resolver import (
    RESOLVER_VERSION,
    RULE_EXACT_SOURCE_EXTERNAL_ID,
    RULE_EXACT_WIKIDATA,
    RULE_NAME_CONTEXT_UNIQUE,
    RULE_NAME_DOB_UNIQUE,
    RULE_QUEUE_SAME_NAME,
    IdentityResolver,
    resolve_fighter,
)
from mma_model.identity.review import (
    apply_review_decision,
    enqueue_review,
    list_reviews,
    reverse_review_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "identity" / "adjudicated_cases_v1.json"
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 17, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path):
    db_path = tmp_path / "identity.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"db_path": db_path, "engine": engine, "Session": Session}
    engine.dispose()


def _seed_espn_fighter(session, espn_id: str, name: str, dob: date | None = None) -> str:
    fid = canonical_fighter_id(espn_id)
    session.add(CanonicalFighter(id=fid, display_name=name))
    session.add(FighterSourceId(fighter_id=fid, source="espn", external_id=espn_id))
    if dob is not None:
        session.add(
            FighterProfileObservation(
                fighter_id=fid,
                attribute="dob",
                value_date=dob,
                source="espn",
                effective_at=FIXED_NOW,
                observed_at=FIXED_NOW,
            )
        )
    session.flush()
    return fid


def test_same_name_without_external_id_queues(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = resolve_fighter(
            session,
            source="tapology_public",
            external_id="1",
            display_name="John Smith",
            wikidata_id=None,
            dob=None,
            actor="system",
            now=FIXED_NOW,
        )
        b = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="9",
            display_name="John Smith",
            wikidata_id=None,
            dob=None,
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert a.kind in {"created", "linked"}
    assert b.kind == "queued"
    assert b.rule_id == RULE_QUEUE_SAME_NAME


def test_exact_espn_source_id_links_without_rename(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed_espn_fighter(session, "1001", "Alex Exact")
        session.commit()
        result = resolve_fighter(
            session,
            source="espn",
            external_id="1001",
            display_name="ALEX EXACT",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        fighter = session.get(CanonicalFighter, fid)
        assert result.kind == "linked"
        assert result.canonical_id == fid
        assert result.rule_id == RULE_EXACT_SOURCE_EXTERNAL_ID
        assert fighter is not None
        assert fighter.display_name == "Alex Exact"


def test_exact_wikidata_links(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed_espn_fighter(session, "2001", "Wiki Fighter")
        session.add(
            FighterSourceId(fighter_id=fid, source="wikidata", external_id="Q123")
        )
        session.commit()
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-77",
            display_name="Wiki Fighter",
            wikidata_id="Q123",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert result.kind == "linked"
    assert result.canonical_id == fid
    assert result.rule_id == RULE_EXACT_WIKIDATA


def test_wikidata_conflict_queues(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = _seed_espn_fighter(session, "2101", "Person A", dob=date(1990, 1, 1))
        _seed_espn_fighter(session, "2102", "Person B", dob=date(1991, 2, 2))
        session.add(FighterSourceId(fighter_id=a, source="wikidata", external_id="Q999"))
        session.commit()
        # Wikidata points at A, but name+DOB uniquely match B → conflict queue.
        result = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="sd-1",
            display_name="Person B",
            wikidata_id="Q999",
            dob=date(1991, 2, 2),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert result.kind == "queued"


def test_name_dob_unique_links_conflict_and_missing_queue(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed_espn_fighter(session, "3001", "Unique Dob", dob=date(1990, 1, 2))
        session.commit()
        linked = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-dob-1",
            display_name="Unique Dob",
            dob=date(1990, 1, 2),
            actor="system",
            now=FIXED_NOW,
        )
        conflict = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-dob-2",
            display_name="Unique Dob",
            dob=date(1991, 5, 5),
            actor="system",
            now=FIXED_NOW,
        )
        missing = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-dob-3",
            display_name="Unique Dob",
            dob=None,
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert linked.kind == "linked"
    assert linked.canonical_id == fid
    assert linked.rule_id == RULE_NAME_DOB_UNIQUE
    assert conflict.kind == "queued"
    assert missing.kind == "queued"


def test_name_context_unique_links_same_opponent_different_dates_queue(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = _seed_espn_fighter(session, "4001", "Context Alpha")
        opp = _seed_espn_fighter(session, "4002", "Shared Opponent")
        event1 = CanonicalEvent(
            id="evt-1",
            name="Card 1",
            series="dwcs",
            status="completed",
            event_date=date(2020, 1, 1),
        )
        event2 = CanonicalEvent(
            id="evt-2",
            name="Card 2",
            series="dwcs",
            status="completed",
            event_date=date(2021, 1, 1),
        )
        session.add_all([event1, event2])
        session.flush()
        bout1 = CanonicalBout(
            id="bout-1",
            event_id="evt-1",
            fighter_a_id=a,
            fighter_b_id=opp,
            status="completed",
        )
        session.add(bout1)
        session.flush()
        session.add(BoutParticipant(bout_id="bout-1", fighter_id=a, corner="a"))
        session.add(BoutParticipant(bout_id="bout-1", fighter_id=opp, corner="b"))
        session.commit()

        linked = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-ctx-1",
            display_name="Context Alpha",
            opponent_normalized_name=normalize_person_name("Shared Opponent"),
            event_id="evt-1",
            event_date=date(2020, 1, 1),
            actor="system",
            now=FIXED_NOW,
        )
        ambiguous = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="sd-ctx-2",
            display_name="Context Alpha",
            opponent_normalized_name=normalize_person_name("Shared Opponent"),
            event_date=None,
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert linked.kind == "linked"
    assert linked.rule_id == RULE_NAME_CONTEXT_UNIQUE
    assert ambiguous.kind == "queued"


def test_nickname_reordered_transliteration_never_auto_merge(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed_espn_fighter(session, "5001", "Jon Jones")
        session.commit()
        nick = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-nick",
            display_name="Bones",
            actor="system",
            now=FIXED_NOW,
            candidate_hints=("nickname",),
        )
        reordered = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-reord",
            display_name="Jones Jon",
            actor="system",
            now=FIXED_NOW,
            candidate_hints=("reordered",),
        )
        translit = resolve_fighter(
            session,
            source="tapology_public",
            external_id="tp-tr",
            display_name="Jose Mauro",
            actor="system",
            now=FIXED_NOW,
            candidate_hints=("transliterated",),
        )
        fuzzy = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="sd-fuzzy",
            display_name="Jonny Bones",
            actor="system",
            now=FIXED_NOW,
            candidate_hints=("fuzzy",),
        )
        session.commit()
    assert nick.kind == "queued"
    assert reordered.kind == "queued"
    assert translit.kind == "queued"
    assert fuzzy.kind == "queued"
    assert nick.canonical_id is None
    assert reordered.canonical_id is None
    assert translit.canonical_id is None
    assert fuzzy.canonical_id is None


def test_duplicate_external_id_conflict_queues_and_evidence(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = _seed_espn_fighter(session, "6001", "Dup A")
        session.add(
            FighterSourceId(fighter_id=a, source="tapology_public", external_id="dup-1")
        )
        session.commit()
        # Attempting to attach same (source, external_id) to resolve for a different person
        # via creating second fighter then conflicting mapping should queue as conflict.
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="dup-1",
            display_name="Dup B Different",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        evidence = session.scalars(select(IdentityMatchEvidence)).all()
    assert result.kind in {"queued", "blocked"}
    assert result.canonical_id is None
    assert result.rule_id == "identity_conflict_queue"
    assert evidence
    assert any(
        "duplicate_external_id" in e.evidence_json or "conflict" in e.evidence_json
        for e in evidence
    )
    assert all(e.resolver_version == RESOLVER_VERSION for e in evidence)
    _ = a


def test_exact_wikidata_beats_transliteration_hint(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = _seed_espn_fighter(session, "2201", "Wiki First")
        session.add(
            FighterSourceId(fighter_id=fid, source="wikidata", external_id="Q2201")
        )
        session.commit()
        result = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="sd-wiki-hint",
            display_name="Wiki First ASCII",
            wikidata_id="Q2201",
            candidate_hints=("transliterated", "nickname"),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert result.kind == "linked"
    assert result.canonical_id == fid
    assert result.rule_id == RULE_EXACT_WIKIDATA


def test_rule_priority_exact_source_beats_name(env) -> None:
    Session = env["Session"]
    with Session() as session:
        exact = _seed_espn_fighter(session, "7001", "Shared Name", dob=date(1988, 1, 1))
        other = _seed_espn_fighter(session, "7002", "Shared Name", dob=date(1988, 1, 1))
        session.add(
            FighterSourceId(fighter_id=exact, source="tapology_public", external_id="prio-1")
        )
        session.commit()
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="prio-1",
            display_name="Shared Name",
            dob=date(1988, 1, 1),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert result.kind == "linked"
    assert result.canonical_id == exact
    assert result.canonical_id != other
    assert result.rule_id == RULE_EXACT_SOURCE_EXTERNAL_ID


def test_deterministic_candidate_order(env) -> None:
    Session = env["Session"]
    with Session() as session:
        ids = []
        for i, espn in enumerate(("8001", "8002", "8003")):
            ids.append(_seed_espn_fighter(session, espn, "Same Name"))
        session.commit()
        result = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="cand-1",
            display_name="Same Name",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        review = session.get(IdentityReviewQueue, result.review_id)
        candidates = json.loads(review.candidate_canonical_ids_json)
    assert result.kind == "queued"
    assert candidates == sorted(ids)


def test_idempotent_duplicate_enqueue(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed_espn_fighter(session, "8101", "Queue Name")
        session.commit()
        first = resolve_fighter(
            session,
            source="tapology_public",
            external_id="enq-1",
            display_name="Queue Name",
            actor="system",
            now=FIXED_NOW,
        )
        second = resolve_fighter(
            session,
            source="tapology_public",
            external_id="enq-1",
            display_name="Queue Name",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert first.kind == "queued"
    assert second.kind == "queued"
    assert first.review_id == second.review_id


def test_upcoming_block_isolates_unrelated(env) -> None:
    Session = env["Session"]
    with Session() as session:
        known = _seed_espn_fighter(session, "9001", "Known Fighter")
        opp = _seed_espn_fighter(session, "9002", "Opp Fighter")
        session.add(
            CanonicalEvent(
                id="up-evt",
                name="Upcoming",
                series="dwcs",
                status="scheduled",
                event_date=date(2026, 9, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="up-bout-blocked",
                event_id="up-evt",
                fighter_a_id=known,
                fighter_b_id=opp,
                status="scheduled",
            )
        )
        session.add(
            CanonicalBout(
                id="up-bout-clear",
                event_id="up-evt",
                fighter_a_id=known,
                fighter_b_id=opp,
                status="scheduled",
            )
        )
        session.flush()
        session.commit()
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="block-1",
            display_name="Known Fighter",
            bout_id="up-bout-blocked",
            bout_status="upcoming",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        blocks = session.scalars(select(IdentityScoringBlock)).all()
        resolver = IdentityResolver(session, actor="system", now=FIXED_NOW)
    assert result.kind == "blocked"
    assert any(b.bout_id == "up-bout-blocked" and b.active for b in blocks)
    assert resolver.is_bout_scoring_blocked("up-bout-blocked") is True
    assert resolver.is_bout_scoring_blocked("up-bout-clear") is False


def test_completed_bout_unresolved_does_not_block_scoring(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed_espn_fighter(session, "9011", "Completed Name")
        session.commit()
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="done-1",
            display_name="Completed Name",
            bout_id="completed-bout",
            bout_status="completed",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        blocks = session.scalars(select(IdentityScoringBlock)).all()
        resolver = IdentityResolver(session, actor="system", now=FIXED_NOW)
    assert result.kind == "queued"
    assert not any(b.active for b in blocks)
    assert resolver.is_bout_scoring_blocked("completed-bout") is False


def test_evaluated_unresolved_blocks_only_that_bout(env) -> None:
    Session = env["Session"]
    with Session() as session:
        _seed_espn_fighter(session, "9021", "Eval Name")
        session.commit()
        result = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="eval-1",
            display_name="Eval Name",
            bout_id="eval-bout",
            bout_status="evaluated",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        resolver = IdentityResolver(session, actor="system", now=FIXED_NOW)
    assert result.kind == "blocked"
    assert resolver.is_bout_scoring_blocked("eval-bout") is True
    assert resolver.is_bout_scoring_blocked("other-eval-bout") is False


def test_malformed_evidence_and_unknown_source_fail_closed(env) -> None:
    Session = env["Session"]
    with Session() as session:
        with pytest.raises(ValueError, match="source"):
            resolve_fighter(
                session,
                source="not_a_real_source",
                external_id="x",
                display_name="X",
                actor="system",
                now=FIXED_NOW,
            )
        with pytest.raises(ValueError, match="external_id|display_name|actor"):
            resolve_fighter(
                session,
                source="espn",
                external_id="",
                display_name="X",
                actor="system",
                now=FIXED_NOW,
            )
        with pytest.raises(ValueError, match="malformed evidence"):
            enqueue_review(
                session,
                ReviewCandidate(
                    source="tapology_public",
                    external_id="bad-ev",
                    display_name="Bad Evidence",
                    normalized_name="bad evidence",
                    rule_id="manual_enqueue",
                    evidence={"nan": float("nan")},
                ),
                actor="system",
                now=FIXED_NOW,
            )


def test_unique_constraint_race_on_fighter_source_ids(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = _seed_espn_fighter(session, "9101", "Race A")
        b = _seed_espn_fighter(session, "9102", "Race B")
        session.add(
            FighterSourceId(fighter_id=a, source="tapology_public", external_id="race-1")
        )
        session.commit()
        session.add(
            FighterSourceId(fighter_id=b, source="tapology_public", external_id="race-1")
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def _load_adjudicated_fixture() -> dict:
    from mma_model.identity.adjudicated import load_adjudicated_cases

    return load_adjudicated_cases(FIXTURE_PATH)


def test_adjudicated_fixture_precision_recall_and_zero_same_name_conflation(env) -> None:
    from mma_model.identity.adjudicated import score_adjudicated_results
    from mma_model.identity.audit import build_identity_audit

    cases = _load_adjudicated_fixture()
    Session = env["Session"]

    with Session() as session:
        for seed in cases["seeds"]:
            fid = _seed_espn_fighter(
                session,
                seed["espn_id"],
                seed["display_name"],
                dob=date.fromisoformat(seed["dob"]) if seed.get("dob") else None,
            )
            if seed.get("wikidata_id"):
                session.add(
                    FighterSourceId(
                        fighter_id=fid,
                        source="wikidata",
                        external_id=seed["wikidata_id"],
                    )
                )
            for src in seed.get("extra_source_ids", []):
                session.add(
                    FighterSourceId(
                        fighter_id=fid,
                        source=src["source"],
                        external_id=src["external_id"],
                    )
                )
        for event in cases.get("events", []):
            session.add(
                CanonicalEvent(
                    id=event["id"],
                    name=event["name"],
                    series=event.get("series", "dwcs"),
                    status=event.get("status", "completed"),
                    event_date=date.fromisoformat(event["event_date"]),
                )
            )
        session.flush()
        for bout in cases.get("bouts", []):
            session.add(
                CanonicalBout(
                    id=bout["id"],
                    event_id=bout["event_id"],
                    fighter_a_id=canonical_fighter_id(bout["fighter_a_espn"]),
                    fighter_b_id=canonical_fighter_id(bout["fighter_b_espn"]),
                    status=bout.get("status", "completed"),
                )
            )
        session.flush()
        for bout in cases.get("bouts", []):
            session.add(
                BoutParticipant(
                    bout_id=bout["id"],
                    fighter_id=canonical_fighter_id(bout["fighter_a_espn"]),
                    corner="a",
                )
            )
            session.add(
                BoutParticipant(
                    bout_id=bout["id"],
                    fighter_id=canonical_fighter_id(bout["fighter_b_espn"]),
                    corner="b",
                )
            )
        session.commit()

        results_by_id: dict[str, object] = {}
        for case in cases["cases"]:
            result = resolve_fighter(
                session,
                source=case["source"],
                external_id=case["external_id"],
                display_name=case["display_name"],
                wikidata_id=case.get("wikidata_id"),
                dob=date.fromisoformat(case["dob"]) if case.get("dob") else None,
                opponent_normalized_name=case.get("opponent_normalized_name"),
                event_id=case.get("event_id"),
                event_date=(
                    date.fromisoformat(case["event_date"]) if case.get("event_date") else None
                ),
                bout_id=case.get("bout_id"),
                bout_status=case.get("bout_status"),
                candidate_hints=tuple(case.get("candidate_hints") or ()),
                actor="system",
                now=FIXED_NOW,
            )
            results_by_id[case["id"]] = result
        session.commit()

        metrics = score_adjudicated_results(cases, results_by_id)
        assert metrics["denominator_all"] == len(cases["cases"])
        assert metrics["denominator_auto_eligible"] >= 40
        assert metrics["auto_true_pos"] + metrics["auto_false_pos"] + metrics["auto_false_neg"] == (
            metrics["denominator_auto_eligible"]
        )
        assert metrics["queued"] + metrics["blocked"] == metrics["queue_or_blocked"]
        assert metrics["queue_rate"] == metrics["queued"] / metrics["denominator_all"]
        assert metrics["blocked_rate"] == metrics["blocked"] / metrics["denominator_all"]
        assert metrics["same_name_conflations"] == 0
        assert metrics["precision"] >= 0.995
        assert metrics["recall"] >= 0.98
        assert metrics["queued"] >= 5
        assert metrics["blocked"] >= 1
        assert 0.05 <= metrics["queue_rate"] <= 0.6
        assert metrics["coverage"] == 1.0

        report = build_identity_audit(session, series="dwcs")
        payload = report.to_dict()
        for key in (
            "denominator_all",
            "denominator_auto_eligible",
            "auto_true_pos",
            "auto_false_pos",
            "auto_false_neg",
            "precision",
            "recall",
            "queued",
            "queue_rate",
            "blocked",
            "blocked_rate",
            "coverage",
            "same_name_conflations",
        ):
            assert key in payload
        assert payload["queued"] == metrics["queued"]
        assert payload["blocked"] == metrics["blocked"]
        assert payload["precision"] == metrics["precision"]

        for review_case in cases.get("review_cases", []):
            pending = [
                r
                for r in list_reviews(session, status="pending")
                if r.source == review_case["source"]
                and r.external_id == review_case["external_id"]
            ]
            assert pending
            rid = pending[0].id
            if review_case["action"] == "reject":
                apply_review_decision(
                    session,
                    review_id=rid,
                    decision="reject",
                    canonical_id=None,
                    actor="tester",
                    now=FIXED_NOW,
                )
            elif review_case["action"] == "approve":
                apply_review_decision(
                    session,
                    review_id=rid,
                    decision="approve",
                    canonical_id=canonical_fighter_id(review_case["canonical_espn_id"]),
                    actor="tester",
                    now=FIXED_NOW,
                )
            if review_case.get("reverse"):
                reverse_review_decision(
                    session, review_id=rid, actor="tester", now=FIXED_NOW
                )
        session.commit()



def test_swapped_participants_and_late_replacement_do_not_silent_merge(env) -> None:
    Session = env["Session"]
    with Session() as session:
        a = _seed_espn_fighter(session, "9201", "Starter A")
        b = _seed_espn_fighter(session, "9202", "Starter B")
        repl = _seed_espn_fighter(session, "9203", "Late Replacement")
        session.add(
            CanonicalEvent(
                id="swap-evt",
                name="Swap Card",
                series="dwcs",
                status="completed",
                event_date=date(2022, 2, 2),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="swap-bout",
                event_id="swap-evt",
                fighter_a_id=a,
                fighter_b_id=b,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="swap-bout", fighter_id=a, corner="a"))
        session.add(BoutParticipant(bout_id="swap-bout", fighter_id=b, corner="b"))
        session.commit()
        swapped = resolve_fighter(
            session,
            source="tapology_public",
            external_id="swap-1",
            display_name="Starter B",
            opponent_normalized_name=normalize_person_name("Starter A"),
            event_id="swap-evt",
            event_date=date(2022, 2, 2),
            actor="system",
            now=FIXED_NOW,
        )
        late = resolve_fighter(
            session,
            source="tapology_public",
            external_id="late-1",
            display_name="Late Replacement",
            opponent_normalized_name=normalize_person_name("Starter A"),
            event_id="swap-evt",
            event_date=date(2022, 2, 2),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    # Context may uniquely identify Starter B; late replacement must not steal Starter B id.
    if swapped.kind == "linked":
        assert swapped.canonical_id == b
    assert late.canonical_id != b or late.kind == "queued"
    if late.kind == "linked":
        assert late.canonical_id == repl


def test_resolver_imports_enqueue_review_at_module_top() -> None:
    import inspect

    from mma_model.identity import resolver as resolver_mod

    source = inspect.getsource(resolver_mod)
    import_line = "from mma_model.identity.review import enqueue_review"
    assert import_line in source
    assert source.index(import_line) < source.index("class IdentityResolver")
    queue_src = inspect.getsource(resolver_mod.IdentityResolver._queue)
    assert "from mma_model.identity.review import" not in queue_src


def test_policy_same_name_auto_merge_fails_closed(env) -> None:
    from mma_model.sources.policy import load_source_policy

    Session = env["Session"]
    policy = load_source_policy()
    drifted = policy.model_copy(
        update={
            "identity_rules": policy.identity_rules.model_copy(
                update={"same_name_auto_merge": True}
            )
        }
    )
    with Session() as session:
        with pytest.raises(ValueError, match="same_name_auto_merge"):
            resolve_fighter(
                session,
                source="tapology_public",
                external_id="drift-1",
                display_name="Drift",
                actor="system",
                now=FIXED_NOW,
                policy=drifted,
            )


def test_policy_disabling_exact_source_ids_changes_behavior(env) -> None:
    from mma_model.sources.policy import load_source_policy

    Session = env["Session"]
    policy = load_source_policy()
    drifted = policy.model_copy(
        update={
            "identity_rules": policy.identity_rules.model_copy(
                update={"exact_source_ids_first": False}
            )
        }
    )
    with Session() as session:
        fid = _seed_espn_fighter(session, "9301", "Policy Exact")
        session.add(
            FighterSourceId(
                fighter_id=fid, source="tapology_public", external_id="policy-exact-1"
            )
        )
        session.commit()
        result = resolve_fighter(
            session,
            source="tapology_public",
            external_id="policy-exact-1",
            display_name="Policy Exact Other",
            actor="system",
            now=FIXED_NOW,
            policy=drifted,
        )
        session.commit()
    assert result.kind != "linked" or result.rule_id != RULE_EXACT_SOURCE_EXTERNAL_ID

