"""Failing reproductions for the second Grok 4.6 review of PR #18."""

from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterSourceId,
)
from mma_model.db.tables.history import HistoryConflict, HistorySourceBout, HistorySourceFailure
from mma_model.history.apply import apply_history_observation
from mma_model.history.audit import (
    coverage_gates_ok,
    evaluate_sample_coverage,
    render_regional_coverage_markdown,
)
from mma_model.history.constants import SOURCE_COMBAT_REGISTRY, SOURCE_SHERDOG, SOURCE_TAPOLOGY
from mma_model.history.coverage import left_truncated_history_count
from mma_model.history.models import RegionalCoverageReport
from mma_model.history.reconstruct import reconstruct_pre_fight_record
from mma_model.history.sync import load_upcoming_dwcs_fighters, sync_regional_history
from tests.history.helpers import FIXED_NOW
from tests.history.test_reconstruct import CUTOFF, UTC, _add_fighter, _bout
from tests.history.test_review_fixes import HASH_A, HASH_B, _obs

HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_four_sequential_corrections_append_unique_revisions(history_env) -> None:
    Session = history_env["Session"]
    hashes = (HASH_A, HASH_B, HASH_C, HASH_D)
    results = ("win", "nc", "loss", "draw")
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.flush()
        for payload_hash, result in zip(hashes, results, strict=True):
            skipped = apply_history_observation(
                session,
                _obs(
                    payload_hash=payload_hash,
                    raw_ref=payload_hash,
                    attributes={
                        "result": result,
                        "revision": 1,
                        "external_bout_id": "tb-corr",
                    },
                ),
            )
            assert skipped != "revision_collision"
        replay = apply_history_observation(
            session,
            _obs(
                payload_hash=HASH_D,
                raw_ref=HASH_D,
                attributes={"result": "draw", "revision": 1, "external_bout_id": "tb-corr"},
            ),
        )
        assert replay == "skipped_identical"
        rows = list(
            session.scalars(
                select(HistorySourceBout)
                .where(HistorySourceBout.external_bout_id == "tb-corr")
                .order_by(HistorySourceBout.revision.asc())
            )
        )
        assert [row.revision for row in rows] == [1, 2, 3, 4]
        assert [row.result for row in rows] == ["win", "nc", "loss", "draw"]
        assert [row.payload_hash for row in rows] == list(hashes)
        conflicts = list(session.scalars(select(HistoryConflict)))
        assert len(conflicts) == 3
        keys = {row.conflict_key for row in conflicts}
        assert len(keys) == 3
        joined = " ".join(keys)
        assert HASH_A not in joined
        assert HASH_B in joined and HASH_C in joined and HASH_D in joined
        assert any(key.endswith(":2") or ":2" in key for key in keys)


def test_session_usable_after_correction_conflict(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.flush()
        apply_history_observation(session, _obs())
        apply_history_observation(
            session,
            _obs(
                payload_hash=HASH_B,
                raw_ref=HASH_B,
                attributes={"result": "nc", "revision": 1, "external_bout_id": "tb-corr"},
            ),
        )
        apply_history_observation(
            session,
            _obs(
                payload_hash=HASH_C,
                raw_ref=HASH_C,
                attributes={"result": "loss", "revision": 1, "external_bout_id": "tb-corr"},
            ),
        )
        other = apply_history_observation(
            session,
            _obs(
                payload_hash=HASH_E,
                raw_ref=HASH_E,
                external_id="tb-other#event_night#1",
                attributes={"external_bout_id": "tb-other", "result": "win", "revision": 1},
            ),
        )
        assert other is None
        session.commit()
        assert session.scalar(select(HistorySourceBout).where(
            HistorySourceBout.external_bout_id == "tb-other"
        )) is not None


def test_interleaved_sessions_retry_without_dropping_row(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.flush()
        apply_history_observation(session, _obs())
        session.commit()

    engine = create_engine(
        history_env["db_url"],
        future=True,
        connect_args={"check_same_thread": False, "timeout": 15},
        poolclass=NullPool,
    )
    _attach_sqlite_listeners(engine)
    ThreadSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _worker(payload_hash: str, result: str) -> None:
        with ThreadSession() as session:
            try:
                barrier.wait(timeout=5)
                skipped = apply_history_observation(
                    session,
                    _obs(
                        payload_hash=payload_hash,
                        raw_ref=payload_hash,
                        attributes={
                            "result": result,
                            "revision": 1,
                            "external_bout_id": "tb-corr",
                        },
                    ),
                )
                assert skipped != "revision_collision"
                session.commit()
            except BaseException as exc:  # noqa: BLE001 — collect for the parent thread
                errors.append(exc)
                session.rollback()

    threads = [
        threading.Thread(target=_worker, args=(HASH_B, "nc")),
        threading.Thread(target=_worker, args=(HASH_C, "loss")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    engine.dispose()
    assert errors == []
    with Session() as session:
        rows = list(
            session.scalars(
                select(HistorySourceBout).where(HistorySourceBout.external_bout_id == "tb-corr")
            )
        )
        assert len(rows) == 3
        assert {row.payload_hash for row in rows} == {HASH_A, HASH_B, HASH_C}
        assert {row.revision for row in rows} == {1, 2, 3}


def _card(
    session,
    *,
    event_id: str,
    event_status: str,
    bout_id: str,
    bout_status: str,
    fighter_a: str,
    fighter_b: str,
    name_a: str,
    name_b: str,
    a_ids: dict[str, str] | None = None,
    series: str = "dwcs",
) -> None:
    if session.get(CanonicalFighter, fighter_a) is None:
        session.add(CanonicalFighter(id=fighter_a, display_name=name_a))
    if session.get(CanonicalFighter, fighter_b) is None:
        session.add(CanonicalFighter(id=fighter_b, display_name=name_b))
    if session.get(CanonicalEvent, event_id) is None:
        session.add(
            CanonicalEvent(
                id=event_id,
                name=event_id,
                series=series,
                status=event_status,
            )
        )
    session.flush()
    session.add(
        CanonicalBout(
            id=bout_id,
            event_id=event_id,
            fighter_a_id=fighter_a,
            fighter_b_id=fighter_b,
            status=bout_status,
        )
    )
    session.flush()
    session.add(BoutParticipant(bout_id=bout_id, fighter_id=fighter_a, corner="a"))
    session.add(BoutParticipant(bout_id=bout_id, fighter_id=fighter_b, corner="b"))
    for source, external_id in (a_ids or {}).items():
        session.add(
            FighterSourceId(fighter_id=fighter_a, source=source, external_id=external_id)
        )


def test_upcoming_scope_excludes_unrelated_and_scratched(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        _card(
            session,
            event_id="e-up",
            event_status="upcoming",
            bout_id="b-up",
            bout_status="scheduled",
            fighter_a="f-up",
            fighter_b="f-opp",
            name_a="Upcoming One",
            name_b="Opp One",
            a_ids={"tapology_public": "tap-up"},
        )
        _card(
            session,
            event_id="e-sched",
            event_status="scheduled",
            bout_id="b-rep",
            bout_status="replacement",
            fighter_a="f-rep",
            fighter_b="f-rep-opp",
            name_a="Replacement One",
            name_b="Opp Two",
            a_ids={"sherdog_public": "sh-rep"},
        )
        _card(
            session,
            event_id="e-up",
            event_status="upcoming",
            bout_id="b-scratch",
            bout_status="scratched",
            fighter_a="f-scratch",
            fighter_b="f-scratch-opp",
            name_a="Scratched One",
            name_b="Opp Three",
        )
        _card(
            session,
            event_id="e-done",
            event_status="completed",
            bout_id="b-done",
            bout_status="completed",
            fighter_a="f-done",
            fighter_b="f-done-opp",
            name_a="Completed One",
            name_b="Opp Four",
        )
        session.add(CanonicalFighter(id="f-roster", display_name="Unrelated Roster"))
        session.commit()
        seeds = load_upcoming_dwcs_fighters(session=session)
    names = {row.display_name for row in seeds}
    assert "Upcoming One" in names
    assert "Replacement One" in names
    assert "Opp One" in names
    assert "Scratched One" not in names
    assert "Completed One" not in names
    assert "Unrelated Roster" not in names
    upcoming = next(row for row in seeds if row.display_name == "Upcoming One")
    assert upcoming.source_ids.get("tapology_public") == "tap-up"


def test_upcoming_duplicate_names_merge_by_source_id_not_display_name(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        _card(
            session,
            event_id="e-up",
            event_status="scheduled",
            bout_id="b-alex",
            bout_status="scheduled",
            fighter_a="f-alex-card",
            fighter_b="f-opp",
            name_a="Alex Sample",
            name_b="Opp",
            a_ids={"tapology_public": "tap-100"},
        )
        session.add(CanonicalFighter(id="f-alex-other", display_name="Alex Sample"))
        session.add(
            FighterSourceId(
                fighter_id="f-alex-other",
                source="tapology_public",
                external_id="tap-other-alex",
            )
        )
        session.commit()
        seeds = load_upcoming_dwcs_fighters(session=session)
    alexes = [row for row in seeds if row.display_name == "Alex Sample"]
    assert len(alexes) == 1
    assert alexes[0].canonical_id == "f-alex-card"
    assert alexes[0].source_ids.get("tapology_public") == "tap-100"
    assert alexes[0].source_ids.get("sherdog_public") == "sh-100"
    assert "tap-other-alex" not in alexes[0].source_ids.values()


def test_upcoming_missing_source_id_blocks_only_that_fighter(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        _card(
            session,
            event_id="e-up",
            event_status="scheduled",
            bout_id="b-missing",
            bout_status="scheduled",
            fighter_a="f-missing",
            fighter_b="f-ok",
            name_a="Missing Ids",
            name_b="Has Ids",
            a_ids=None,
        )
        session.add(
            FighterSourceId(fighter_id="f-ok", source="tapology_public", external_id="tap-ok")
        )
        session.add(CanonicalFighter(id="f-roster", display_name="Unrelated Roster"))
        session.commit()
        seeds = load_upcoming_dwcs_fighters(session=session)
        missing = next(row for row in seeds if row.canonical_id == "f-missing")
        ok = next(row for row in seeds if row.canonical_id == "f-ok")
        assert not any(
            missing.source_ids.get(source)
            for source in (SOURCE_TAPOLOGY, SOURCE_SHERDOG, SOURCE_COMBAT_REGISTRY)
        )
        assert ok.source_ids.get("tapology_public") == "tap-ok"
        assert all(row.canonical_id != "f-roster" for row in seeds)
    report = sync_regional_history(
        repo=history_env["repo"],
        session_factory=Session,
        fighters=[missing],
        fixture_roots={},
        sources=["tapology_public"],
        observed_at=FIXED_NOW,
    )
    assert any("Missing Ids" in item for item in report.blockers)
    assert not any("Unrelated Roster" in item for item in report.blockers)
    with Session() as session:
        failures = list(session.scalars(select(HistorySourceFailure)))
        subjects = {row.subject for row in failures}
        assert "f-missing" in subjects or any("Missing" in (row.subject or "") for row in failures)
        assert "f-roster" not in subjects
        assert "Unrelated Roster" not in subjects


def test_unknown_history_is_not_semantic_zero(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        unknown_id = _add_fighter(session, "f-unknown")
        empty_id = _add_fighter(session, "f-empty")
        _bout(
            session,
            fighter_id=unknown_id,
            external_bout_id="no-clock",
            event_date=date(2023, 6, 1),
            effective_at=datetime(2023, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            proxy_published_at=None,
            source_published_at=None,
            result="win",
        )
        session.commit()
        unknown = reconstruct_pre_fight_record(
            fighter_id=unknown_id, cutoff=CUTOFF, session=session
        )
        empty = reconstruct_pre_fight_record(
            fighter_id=empty_id, cutoff=CUTOFF, session=session
        )
    assert unknown.completeness == "unknown"
    assert unknown.wins is None
    assert unknown.losses is None
    assert unknown.draws is None
    assert unknown.no_contests is None
    assert unknown.experience_bouts is None
    assert unknown.known_minutes is None
    assert unknown.minutes_unknown is True
    assert unknown.comparable_tuple() is None
    assert empty.completeness == "complete"
    assert empty.wins == 0
    assert empty.losses == 0
    assert empty.known_minutes == 0.0
    assert empty.minutes_unknown is False
    assert empty.comparable_tuple() == (0, 0, 0, 0)
    assert unknown.comparable_tuple() != empty.comparable_tuple()


def test_unknown_pre_fight_excluded_from_agreement_denominator(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="no-clock",
            event_date=date(2023, 6, 1),
            effective_at=datetime(2023, 6, 1, tzinfo=UTC),
            proxy_published_at=None,
            source_published_at=None,
            result="win",
        )
        session.commit()
        coverage = evaluate_sample_coverage(
            session,
            sample={
                "professional_bouts": [],
                "amateur_regulated_us_bouts": [],
                "unknown_classification_bouts": [],
                "explicit_pre_fight_records": [
                    {
                        "fighter_id": fid,
                        "cutoff": CUTOFF.isoformat(),
                        "wins": 0,
                        "losses": 0,
                        "draws": 0,
                        "no_contests": 0,
                    }
                ],
            },
        )
    assert coverage.pre_fight_agreement_d == 0
    assert coverage.pre_fight_unknown_n >= 1
    assert "unknown" in " ".join(coverage.pre_fight_exclusions).lower() or coverage.pre_fight_unknown_n >= 1


def test_zero_denominator_blocks_every_live_segment() -> None:
    empty = RegionalCoverageReport(
        professional_n=0,
        professional_found=0,
        professional_rate=None,
        amateur_n=0,
        amateur_found=0,
        amateur_rate=None,
        unknown_class_n=0,
        pre_fight_agreement_n=1,
        pre_fight_agreement_d=1,
        pre_fight_agreement_rate=1.0,
        evidence_class="live_source_coverage",
        fixture_professional_n=9,
        fixture_professional_found=9,
        fixture_amateur_n=2,
        fixture_amateur_found=2,
    )
    ok, blockers = coverage_gates_ok(empty)
    assert ok is False
    assert "insufficient_live_professional_sample" in blockers
    assert "insufficient_live_amateur_sample" in blockers
    assert "professional_coverage" not in blockers


def test_fixture_year_filter_does_not_fill_live_numerators(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="y2024",
            event_date=date(2024, 1, 1),
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            proxy_published_at=datetime(2024, 1, 2, tzinfo=UTC),
            observation_origin="synthetic_fixture",
        )
        session.commit()
        coverage = evaluate_sample_coverage(
            session,
            sample={
                "professional_bouts": [
                    {
                        "bout_id": "y2024",
                        "source": "tapology_public",
                        "classification": "professional",
                        "event_date": "2024-01-01",
                    }
                ],
                "amateur_regulated_us_bouts": [],
                "unknown_classification_bouts": [],
                "explicit_pre_fight_records": [],
            },
            years=range(2023, 2026),
        )
    assert coverage.evidence_class == "fixture_validation"
    assert coverage.professional_n == 0
    assert coverage.amateur_n == 0
    assert coverage.fixture_professional_n >= 1
    ids = {row["bout_id"] for row in coverage.eligible_sample_bouts}
    assert "y2024" in ids


def _probe(*, source: str, result: str, reason: str | None, path: str, status: int | None) -> dict:
    return {
        "source": source,
        "result": result,
        "block_reason": reason,
        "http_status": status,
        "path_category": path,
        "host": "example.test",
        "robots": {"policy_decision": "rfc9309_parsed_allow"},
    }


def test_injected_live_probes_feed_coverage_hash_not_frozen(history_env) -> None:
    Session = history_env["Session"]
    killed = {
        SOURCE_TAPOLOGY: _probe(
            source=SOURCE_TAPOLOGY,
            result="BLOCKED",
            reason="http_403",
            path="/rankings/",
            status=403,
        ),
        SOURCE_SHERDOG: _probe(
            source=SOURCE_SHERDOG,
            result="OK",
            reason=None,
            path="/events/",
            status=200,
        ),
        SOURCE_COMBAT_REGISTRY: _probe(
            source=SOURCE_COMBAT_REGISTRY,
            result="BLOCKED",
            reason="login_wall",
            path="/",
            status=200,
        ),
    }
    accessible = {
        **killed,
        SOURCE_TAPOLOGY: _probe(
            source=SOURCE_TAPOLOGY,
            result="OK",
            reason=None,
            path="/fightcenter/fighters/x",
            status=200,
        ),
    }
    drifted = {
        **killed,
        SOURCE_SHERDOG: _probe(
            source=SOURCE_SHERDOG,
            result="BLOCKED",
            reason="schema_drift",
            path="/fighter/x",
            status=200,
        ),
    }
    with Session() as session:
        frozen = evaluate_sample_coverage(session, probe_mode="offline")
        live_killed = evaluate_sample_coverage(
            session,
            live_probes={"probes": killed},
            probe_mode="live",
        )
        injected_ok = evaluate_sample_coverage(
            session,
            live_probes={"probes": accessible},
            probe_mode="injected",
        )
        injected_drift = evaluate_sample_coverage(
            session,
            live_probes={"probes": drifted},
            probe_mode="injected",
        )
        again = evaluate_sample_coverage(
            session,
            live_probes={"probes": accessible},
            probe_mode="injected",
        )
    assert frozen.probe_evidence_source == "frozen"
    assert live_killed.probe_evidence_source == "live"
    assert injected_ok.probe_evidence_source == "injected"
    assert live_killed.live_source_coverage[SOURCE_TAPOLOGY]["status"] == "source_killed"
    assert injected_ok.live_source_coverage[SOURCE_TAPOLOGY]["status"] == "accessible"
    assert injected_drift.live_source_coverage[SOURCE_SHERDOG]["status"] == "source_failed"
    assert injected_ok.report_hash == again.report_hash
    assert injected_ok.report_hash != live_killed.report_hash
    assert injected_ok.report_hash != frozen.report_hash
    with Session() as session:
        live_not_run = evaluate_sample_coverage(
            session,
            live_probes={"probes": {}},
            probe_mode="live",
        )
    assert live_not_run.probe_evidence_source == "live"
    assert live_not_run.report_hash != frozen.report_hash


def test_identity_exact_links_count_unique_source_ids_not_bouts(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        session.add(
            FighterSourceId(fighter_id=fid, source="tapology_public", external_id="tap-100")
        )
        session.add(FighterSourceId(fighter_id=fid, source="wikidata", external_id="Q1"))
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="b1",
            event_date=date(2023, 1, 1),
            effective_at=datetime(2023, 1, 1, tzinfo=UTC),
            identity_status="linked",
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="b2",
            event_date=date(2023, 2, 1),
            effective_at=datetime(2023, 2, 1, tzinfo=UTC),
            identity_status="linked",
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="b3",
            event_date=date(2023, 3, 1),
            effective_at=datetime(2023, 3, 1, tzinfo=UTC),
            identity_status="linked",
        )
        session.commit()
        coverage = evaluate_sample_coverage(session)
    assert coverage.identity_exact_links == 2
    assert coverage.identity_exact_links != 3


def test_gitignore_restores_ds_store_and_keeps_probe_cache() -> None:
    lines = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert ".DS_Store" in lines
    assert ".local-probe-cache/" in lines


def test_left_truncation_uses_audit_years_not_hardcoded_2026(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="trunc-2025",
            event_date=date(2025, 6, 1),
            effective_at=datetime(2025, 6, 1, tzinfo=UTC),
            left_truncated=1,
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="trunc-2027",
            event_date=date(2027, 6, 1),
            effective_at=datetime(2027, 6, 1, tzinfo=UTC),
            left_truncated=1,
        )
        session.commit()
        before = left_truncated_history_count(session, years=range(2020, 2026))
        after = left_truncated_history_count(session, years=range(2026, 2028))
        none = left_truncated_history_count(session, years=None)
        again = left_truncated_history_count(session, years=range(2020, 2026))
        coverage_before = evaluate_sample_coverage(session, years=range(2020, 2026))
        coverage_after = evaluate_sample_coverage(session, years=range(2026, 2028))
    assert before == 1
    assert after == 1
    assert none == 1
    assert again == before
    assert coverage_before.left_truncated == 1
    assert coverage_after.left_truncated == 1
    assert coverage_before.report_hash != coverage_after.report_hash


def test_coverage_markdown_separates_persisted_and_probe_failures(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        coverage = evaluate_sample_coverage(session, probe_mode="offline")
        markdown = render_regional_coverage_markdown(coverage)
    assert "## Persisted source failures" in markdown
    assert "## Probe source statuses" in markdown
    assert "frozen" in markdown.lower() or "sanitized" in markdown.lower()
    assert not (
        "## Source failures" in markdown and "\n- none\n" in markdown.split("## Source failures", 1)[-1][:80]
    )
    assert "source_killed" in markdown
    assert "Source failures: none" not in markdown
