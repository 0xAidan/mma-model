"""Regional history sync, identity, conflicts, and batch tests (DWCS-105)."""

from __future__ import annotations

from sqlalchemy import select

from mma_model.db.tables.history import HistoryConflict, HistorySourceBout, HistorySourceFailure
from mma_model.history.audit import coverage_gates_ok, evaluate_sample_coverage
from mma_model.history.identity import resolve_regional_fighter
from mma_model.history.sync import load_upcoming_dwcs_fighters, sync_regional_history
from tests.history.helpers import FIXED_NOW, TAPOLOGY_FIXTURES, stage_sync_fixtures


def test_sync_fixtures_persists_pro_am_unknown_and_conflicts(history_env, tmp_path) -> None:
    env = history_env
    root = stage_sync_fixtures(tmp_path)
    fighters = load_upcoming_dwcs_fighters()
    report = sync_regional_history(
        repo=env["repo"],
        session_factory=env["Session"],
        fighters=fighters,
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    assert report.inserted_observations > 0
    with env["Session"]() as session:
        bouts = list(session.scalars(select(HistorySourceBout)).all())
        ids = {row.external_bout_id for row in bouts}
        assert "tb-pro-1" in ids
        assert "tb-am-us-1" in ids
        assert "tb-unk-1" in ids
        assert "sh-pro-1" in ids
        assert "cr-pro-1" in ids
        classes = {row.classification for row in bouts}
        assert "professional" in classes
        assert "amateur" in classes
        assert "unknown" in classes
        unknown = next(row for row in bouts if row.external_bout_id == "tb-unk-1")
        assert unknown.classification == "unknown"
        conflicts = list(session.scalars(select(HistoryConflict)).all())
        assert conflicts
        coverage = evaluate_sample_coverage(session)
        ok, blockers = coverage_gates_ok(coverage)
        assert coverage.professional_found == coverage.professional_n
        assert coverage.amateur_found == coverage.amateur_n
        assert coverage.professional_missing_unexplained == 0
        assert coverage.amateur_missing_unexplained == 0
        assert ok, blockers
        first_hash = coverage.report_hash
        second = evaluate_sample_coverage(session)
        assert second.report_hash == first_hash
        assert report.licensed_optional
        assert all(row["status"] == "source_failed" for row in report.licensed_optional)


def test_source_kill_does_not_drop_earlier_batch(history_env, tmp_path) -> None:
    env = history_env
    root = stage_sync_fixtures(tmp_path)
    sherdog_page = root / "sherdog_public" / "fighters" / "sh-100.html"
    sherdog_page.write_text(
        (TAPOLOGY_FIXTURES / "fighter_login.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    fighters = load_upcoming_dwcs_fighters()
    report = sync_regional_history(
        repo=env["repo"],
        session_factory=env["Session"],
        fighters=fighters,
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    assert "sherdog_public" in report.killed_sources
    with env["Session"]() as session:
        tap_bouts = session.scalars(
            select(HistorySourceBout).where(HistorySourceBout.source == "tapology_public")
        ).all()
        assert tap_bouts
        failures = session.scalars(select(HistorySourceFailure)).all()
        assert any(row.source == "sherdog_public" for row in failures)


def test_idempotent_rerun(history_env, tmp_path) -> None:
    env = history_env
    root = stage_sync_fixtures(tmp_path)
    fighters = load_upcoming_dwcs_fighters()
    kwargs = dict(
        repo=env["repo"],
        session_factory=env["Session"],
        fighters=fighters,
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    first = sync_regional_history(**kwargs)
    second = sync_regional_history(**kwargs)
    assert second.skipped_identical >= 1 or second.inserted_observations == 0
    assert first.inserted_observations >= second.inserted_observations


def test_same_name_queues_and_does_not_merge(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        first = resolve_regional_fighter(
            session,
            source="tapology_public",
            external_id="name-a",
            display_name="John Smith",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        second = resolve_regional_fighter(
            session,
            source="sherdog_public",
            external_id="name-b",
            display_name="John Smith",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert first.kind in {"created", "linked"}
    assert second.kind == "queued"
    assert second.canonical_id is None


def test_exact_source_id_links(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        created = resolve_regional_fighter(
            session,
            source="tapology_public",
            external_id="tap-100",
            display_name="Alex Sample",
            wikidata_id="Q900001",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        linked = resolve_regional_fighter(
            session,
            source="tapology_public",
            external_id="tap-100",
            display_name="Alex Sample",
            wikidata_id="Q900001",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
    assert created.kind in {"created", "linked"}
    assert linked.kind == "linked"
    assert linked.canonical_id == created.canonical_id
