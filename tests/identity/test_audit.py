"""Deterministic identity audit report tests (DWCS-104)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterSourceId,
)
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.audit import build_identity_audit
from mma_model.identity.resolver import resolve_fighter

UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 21, 0, 0, tzinfo=UTC)
METRIC_KEYS = (
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
)


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def test_audit_deterministic_fixed_db_config_hash(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = canonical_fighter_id("88001")
        opp = canonical_fighter_id("88002")
        session.add(CanonicalFighter(id=fid, display_name="Audit Fighter"))
        session.add(CanonicalFighter(id=opp, display_name="Audit Opponent"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="88001"))
        session.add(FighterSourceId(fighter_id=opp, source="espn", external_id="88002"))
        session.add(
            CanonicalEvent(
                id="aud-evt",
                name="Audit Card",
                series="dwcs",
                status="completed",
                event_date=date(2020, 1, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="aud-bout",
                event_id="aud-evt",
                fighter_a_id=fid,
                fighter_b_id=opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="aud-bout", fighter_id=fid, corner="a"))
        session.add(BoutParticipant(bout_id="aud-bout", fighter_id=opp, corner="b"))
        session.commit()
        resolve_fighter(
            session,
            source="tapology_public",
            external_id="aud-1",
            display_name="Audit Fighter",
            bout_id="aud-bout",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        a = build_identity_audit(session, series="dwcs")
        b = build_identity_audit(session, series="dwcs")
    assert a.report_hash == b.report_hash
    assert a.to_dict() == b.to_dict()
    assert a.canonical_fighter_count == 2
    assert a.exact_espn_mappings == 2
    assert a.unresolved_conflicts >= 1
    payload = a.to_dict()
    assert "fixture_validation" in payload
    fixture_metrics = payload["fixture_validation"]
    for key in METRIC_KEYS:
        assert key in fixture_metrics
    assert fixture_metrics["statistical_confidence_claim"] is False
    assert fixture_metrics["fixture_status"] == "synthetic_explicit"
    assert "unscoped_pending" in payload
    assert "unscoped_rejected" in payload
    assert "unscoped_approved" in payload
    human = a.human_summary()
    assert "unscoped_pending=" in human
    assert "fixture_n=" in human
    assert "precision=" in human
    assert "no statistical confidence" in human.lower() or "synthetic" in human.lower()


def test_audit_series_filters_unrelated_rows_and_fails_closed(env) -> None:
    Session = env["Session"]
    with Session() as session:
        dwcs_fid = canonical_fighter_id("88001")
        dwcs_opp = canonical_fighter_id("88002")
        ufc_fid = canonical_fighter_id("99001")
        ufc_opp = canonical_fighter_id("99003")
        session.add(CanonicalFighter(id=dwcs_fid, display_name="DWCS Only"))
        session.add(CanonicalFighter(id=dwcs_opp, display_name="DWCS Opp"))
        session.add(CanonicalFighter(id=ufc_fid, display_name="UFC Only"))
        session.add(CanonicalFighter(id=ufc_opp, display_name="UFC Opp"))
        session.add(FighterSourceId(fighter_id=dwcs_fid, source="espn", external_id="88001"))
        session.add(FighterSourceId(fighter_id=dwcs_opp, source="espn", external_id="88002"))
        session.add(FighterSourceId(fighter_id=ufc_fid, source="espn", external_id="99001"))
        session.add(FighterSourceId(fighter_id=ufc_opp, source="espn", external_id="99003"))
        session.add(
            CanonicalEvent(
                id="dwcs-evt",
                name="DWCS Card",
                series="dwcs_standard",
                status="completed",
                event_date=date(2020, 1, 1),
            )
        )
        session.add(
            CanonicalEvent(
                id="ufc-evt",
                name="UFC Card",
                series="ufc",
                status="completed",
                event_date=date(2021, 1, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="dwcs-bout",
                event_id="dwcs-evt",
                fighter_a_id=dwcs_fid,
                fighter_b_id=dwcs_opp,
                status="completed",
            )
        )
        session.add(
            CanonicalBout(
                id="ufc-bout",
                event_id="ufc-evt",
                fighter_a_id=ufc_fid,
                fighter_b_id=ufc_opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="dwcs-bout", fighter_id=dwcs_fid, corner="a"))
        session.add(BoutParticipant(bout_id="dwcs-bout", fighter_id=dwcs_opp, corner="b"))
        session.add(BoutParticipant(bout_id="ufc-bout", fighter_id=ufc_fid, corner="a"))
        session.add(BoutParticipant(bout_id="ufc-bout", fighter_id=ufc_opp, corner="b"))
        session.commit()
        resolve_fighter(
            session,
            source="tapology_public",
            external_id="aud-dwcs",
            display_name="DWCS Only",
            bout_id="dwcs-bout",
            actor="system",
            now=FIXED_NOW,
        )
        resolve_fighter(
            session,
            source="sherdog_public",
            external_id="aud-ufc",
            display_name="UFC Only",
            bout_id="ufc-bout",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        dwcs_a = build_identity_audit(session, series="dwcs")
        extra_ufc = canonical_fighter_id("99002")
        session.add(CanonicalFighter(id=extra_ufc, display_name="UFC Extra"))
        session.add(FighterSourceId(fighter_id=extra_ufc, source="espn", external_id="99002"))
        session.add(
            CanonicalEvent(
                id="ufc-evt-2",
                name="UFC Card 2",
                series="ufc",
                status="completed",
                event_date=date(2021, 2, 2),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="ufc-bout-2",
                event_id="ufc-evt-2",
                fighter_a_id=extra_ufc,
                fighter_b_id=ufc_opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="ufc-bout-2", fighter_id=extra_ufc, corner="a"))
        session.add(BoutParticipant(bout_id="ufc-bout-2", fighter_id=ufc_opp, corner="b"))
        session.commit()
        dwcs_b = build_identity_audit(session, series="dwcs")
        ufc = build_identity_audit(session, series="ufc")
        with pytest.raises(ValueError, match="unsupported series"):
            build_identity_audit(session, series="not_a_series")
    assert dwcs_a.report_hash == dwcs_b.report_hash
    assert dwcs_a.to_dict() == dwcs_b.to_dict()
    assert dwcs_a.canonical_fighter_count == 2
    assert dwcs_a.exact_espn_mappings == 2
    assert ufc.canonical_fighter_count >= 3
    assert ufc.report_hash != dwcs_a.report_hash


def test_audit_includes_mapping_linked_and_unscoped_reviews(env) -> None:
    Session = env["Session"]
    with Session() as session:
        dwcs_fid = canonical_fighter_id("88101")
        dwcs_opp = canonical_fighter_id("88102")
        session.add(CanonicalFighter(id=dwcs_fid, display_name="Mapped DWCS"))
        session.add(CanonicalFighter(id=dwcs_opp, display_name="Mapped Opp"))
        session.add(FighterSourceId(fighter_id=dwcs_fid, source="espn", external_id="88101"))
        session.add(FighterSourceId(fighter_id=dwcs_opp, source="espn", external_id="88102"))
        session.add(
            CanonicalEvent(
                id="map-evt",
                name="Mapped Card",
                series="dwcs_standard",
                status="completed",
                event_date=date(2020, 3, 3),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="map-bout",
                event_id="map-evt",
                fighter_a_id=dwcs_fid,
                fighter_b_id=dwcs_opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="map-bout", fighter_id=dwcs_fid, corner="a"))
        session.add(BoutParticipant(bout_id="map-bout", fighter_id=dwcs_opp, corner="b"))
        session.commit()
        mapped = resolve_fighter(
            session,
            source="tapology_public",
            external_id="map-linked",
            display_name="Mapped DWCS",
            actor="system",
            now=FIXED_NOW,
        )
        unscoped = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="no-bout-person",
            display_name="Unscoped Stranger",
            candidate_hints=("nickname",),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        assert mapped.kind in {"queued", "blocked"}
        assert unscoped.kind in {"queued", "blocked"}
        report = build_identity_audit(session, series="dwcs")
        payload = report.to_dict()
        assert payload["unscoped_pending"] >= 1
        assert payload["pending_reviews"] >= 1
        assert mapped.review_id is not None
        assert payload["unscoped_pending_blocking"] is True
        human = report.human_summary()
        assert "unscoped_pending=" in human
        assert "precision" not in payload
        assert "queued" not in payload


def test_audit_hash_changes_with_material_inputs_not_insert_order(env) -> None:
    Session = env["Session"]

    def _seed_pair(session, *, fighter_espn: str, opp_espn: str, review_ext: str) -> None:
        fid = canonical_fighter_id(fighter_espn)
        opp = canonical_fighter_id(opp_espn)
        session.add(CanonicalFighter(id=fid, display_name=f"Hash {fighter_espn}"))
        session.add(CanonicalFighter(id=opp, display_name=f"Hash Opp {opp_espn}"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id=fighter_espn))
        session.add(FighterSourceId(fighter_id=opp, source="espn", external_id=opp_espn))
        session.add(
            CanonicalEvent(
                id=f"hash-evt-{fighter_espn}",
                name="Hash Card",
                series="dwcs",
                status="completed",
                event_date=date(2020, 1, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id=f"hash-bout-{fighter_espn}",
                event_id=f"hash-evt-{fighter_espn}",
                fighter_a_id=fid,
                fighter_b_id=opp,
                status="completed",
            )
        )
        session.flush()
        session.add(
            BoutParticipant(bout_id=f"hash-bout-{fighter_espn}", fighter_id=fid, corner="a")
        )
        session.add(
            BoutParticipant(bout_id=f"hash-bout-{fighter_espn}", fighter_id=opp, corner="b")
        )
        session.commit()
        resolve_fighter(
            session,
            source="tapology_public",
            external_id=review_ext,
            display_name=f"Hash {fighter_espn}",
            bout_id=f"hash-bout-{fighter_espn}",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()

    with Session() as session:
        _seed_pair(session, fighter_espn="88201", opp_espn="88202", review_ext="hash-a")
        _seed_pair(session, fighter_espn="88203", opp_espn="88204", review_ext="hash-b")
        first = build_identity_audit(session, series="dwcs")
        unscoped = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="hash-unscoped",
            display_name="Hash Unscoped",
            candidate_hints=("nickname",),
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        with_unscoped = build_identity_audit(session, series="dwcs")
        assert unscoped.kind in {"queued", "blocked"}
    assert first.report_hash != with_unscoped.report_hash
    payload = first.to_dict()
    assert payload["case_file_hash"]
    assert payload["fixture_validation"]["case_file_hash"] == payload["case_file_hash"]
    assert payload["allowed_resolve_sources"]
    assert "espn" in payload["allowed_resolve_sources"]
    assert "queued" not in payload
    assert "precision" not in payload

    engine = env["engine"]
    SessionB = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session() as session:
        ordered_a = build_identity_audit(session, series="dwcs")
    with SessionB() as session:
        ordered_b = build_identity_audit(session, series="dwcs")
    assert ordered_a.report_hash == ordered_b.report_hash


def test_audit_hash_changes_with_resolver_version_and_allowed_sources(env, monkeypatch) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = canonical_fighter_id("88301")
        opp = canonical_fighter_id("88302")
        session.add(CanonicalFighter(id=fid, display_name="Hash Policy"))
        session.add(CanonicalFighter(id=opp, display_name="Hash Policy Opp"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="88301"))
        session.add(FighterSourceId(fighter_id=opp, source="espn", external_id="88302"))
        session.add(
            CanonicalEvent(
                id="hash-pol-evt",
                name="Hash Policy Card",
                series="dwcs",
                status="completed",
                event_date=date(2020, 1, 1),
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="hash-pol-bout",
                event_id="hash-pol-evt",
                fighter_a_id=fid,
                fighter_b_id=opp,
                status="completed",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="hash-pol-bout", fighter_id=fid, corner="a"))
        session.add(BoutParticipant(bout_id="hash-pol-bout", fighter_id=opp, corner="b"))
        session.commit()
        baseline = build_identity_audit(session, series="dwcs")
        monkeypatch.setattr("mma_model.identity.audit.RESOLVER_VERSION", "hash-test-version")
        versioned = build_identity_audit(session, series="dwcs")
        monkeypatch.setattr(
            "mma_model.identity.audit.ALLOWED_RESOLVE_SOURCES",
            frozenset({"espn", "tapology_public"}),
        )
        sourced = build_identity_audit(session, series="dwcs")
    assert baseline.report_hash != versioned.report_hash
    assert versioned.report_hash != sourced.report_hash
    assert sourced.config_hash != baseline.config_hash
