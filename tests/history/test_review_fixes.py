"""Failing reproductions for independent Grok review of PR #18 (DWCS-105)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterProfileObservation,
    FighterSourceId,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.history.apply import apply_history_observation
from mma_model.history.audit import (
    coverage_gates_ok,
    evaluate_sample_coverage,
    left_truncated_history_count,
    render_regional_coverage_markdown,
)
from mma_model.history.constants import SOURCE_COMBAT_REGISTRY, SOURCE_SHERDOG, SOURCE_TAPOLOGY
from mma_model.history.identity import compute_identity_conflations, resolve_regional_fighter
from mma_model.history.models import RegionalCoverageReport
from mma_model.history.reconstruct import reconstruct_pre_fight_record
from mma_model.history.sync import load_upcoming_dwcs_fighters, sync_regional_history
from mma_model.sources.combat_registry.client import CombatRegistryPublicClient
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.sherdog_public.client import SherdogPublicClient
from mma_model.sources.sherdog_public.parser import parse_fighter_page
from mma_model.sources.tapology_public.adapter import TapologyPublicAdapter
from mma_model.sources.tapology_public.client import TapologyPublicClient
from mma_model.sources.tapology_public.parser import parse_fighter_page as parse_tapology
from tests.history.helpers import FIXED_NOW, TAPOLOGY_FIXTURES, stage_sync_fixtures
from tests.history.test_reconstruct import CUTOFF, UTC, _add_fighter, _bout

HASH_A = "a" * 64
HASH_B = "b" * 64


def _obs(**overrides) -> SourceObservationRecord:
    attrs = {
        "fighter_source": "tapology_public",
        "fighter_external_id": "tap-100",
        "fighter_name": "Alex Sample",
        "fighter_canonical_id": "f-alex",
        "external_bout_id": "tb-corr",
        "opponent_name": "Opp",
        "classification": "professional",
        "regulated_us": "false",
        "result": "win",
        "revision": 1,
        "bout_status": "completed",
        "identity_status": "linked",
        "is_current_record": False,
        "event_date": "2023-01-01",
        "event_time_precision": "date_only",
        "observation_origin": "synthetic_fixture",
    }
    attrs.update(overrides.pop("attributes", {}))
    payload = {
        "source": "tapology_public",
        "stream": "fighter_history",
        "external_id": "tb-corr#event_night#1",
        "entity_kind": "regional_bout",
        "observed_at": datetime(2026, 8, 12, tzinfo=UTC),
        "effective_at": datetime(2023, 1, 1, tzinfo=UTC),
        "timestamp_quality": "publication_proxy",
        "timestamp_quality_source": "event_completion_plus_delay@1",
        "quality_tier": "bronze",
        "payload_hash": HASH_A,
        "raw_ref": HASH_A,
        "detail_level": DetailLevel.PARTIAL,
        "version_kind": "event_night",
        "attributes": attrs,
    }
    payload.update(overrides)
    return SourceObservationRecord(**payload)


def test_observed_at_2026_does_not_hide_historically_visible_proxy_fact(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="hist-proxy",
            event_date=date(2023, 6, 1),
            effective_at=datetime(2023, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            proxy_published_at=datetime(2023, 6, 2, tzinfo=UTC),
            result="win",
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins == 1
        assert record.completeness in {"complete", "left_truncated"}


def test_missing_visibility_clock_is_unknown_not_zero_history(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="no-clock",
            event_date=date(2023, 6, 1),
            effective_at=datetime(2023, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            proxy_published_at=None,
            source_published_at=None,
            result="win",
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins is None
        assert record.comparable_tuple() is None
        assert record.known_minutes is None
        assert record.minutes_unknown is True
        assert record.visibility_unknown_excluded == 1
        assert record.completeness == "unknown"
        assert record.history_unknown is True


def test_date_only_same_day_cutoff_is_not_visible(history_env) -> None:
    Session = history_env["Session"]
    cutoff = datetime(2023, 6, 1, tzinfo=UTC)
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="same-day",
            event_date=date(2023, 6, 1),
            effective_at=datetime(2023, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            proxy_published_at=datetime(2023, 6, 2, tzinfo=UTC),
            event_time_precision="date_only",
            result="win",
        )
        session.commit()
        early = reconstruct_pre_fight_record(fighter_id=fid, cutoff=cutoff, session=session)
        later = reconstruct_pre_fight_record(
            fighter_id=fid, cutoff=datetime(2023, 6, 3, tzinfo=UTC), session=session
        )
        assert early.wins == 0
        assert later.wins == 1


def test_agreement_denominator_zero_is_blocker() -> None:
    report = RegionalCoverageReport(
        professional_n=9,
        professional_found=9,
        professional_rate=1.0,
        amateur_n=2,
        amateur_found=2,
        amateur_rate=1.0,
        unknown_class_n=1,
        pre_fight_agreement_n=0,
        pre_fight_agreement_d=0,
        pre_fight_agreement_rate=None,
        evidence_class="fixture_validation",
    )
    ok, blockers = coverage_gates_ok(report)
    assert ok is False
    assert "insufficient_comparable_records" in blockers


def test_fixture_decoder_counts_do_not_satisfy_live_gates(history_env, tmp_path) -> None:
    root = stage_sync_fixtures(tmp_path)
    sync_regional_history(
        repo=history_env["repo"],
        session_factory=history_env["Session"],
        fighters=load_upcoming_dwcs_fighters(),
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    with history_env["Session"]() as session:
        coverage = evaluate_sample_coverage(session, years=range(2023, 2026))
        assert coverage.evidence_class == "fixture_validation"
        ok, blockers = coverage_gates_ok(coverage)
        assert ok is False
        assert "insufficient_comparable_records" in blockers or "live_source_unmeasured" in blockers
        assert coverage.live_source_coverage[SOURCE_TAPOLOGY]["status"] in {
            "source_killed",
            "source_failed",
        }
        assert coverage.live_source_coverage[SOURCE_COMBAT_REGISTRY]["status"] in {
            "source_killed",
            "source_failed",
        }
        assert coverage.live_source_coverage[SOURCE_SHERDOG]["status"] in {
            "accessibility_only",
            "unmeasured",
        }
        markdown = render_regional_coverage_markdown(coverage)
        assert "fixture_validation" in markdown
        assert "live_source_coverage" in markdown
        assert "9/9" in markdown
        assert "must not" in markdown.lower() or "not live coverage" in markdown.lower()


def test_synthetic_parser_is_labeled_contract_validation() -> None:
    html = (TAPOLOGY_FIXTURES / "fighter_public_sample.html").read_text(encoding="utf-8")
    parsed = parse_tapology(html)
    assert parsed["parser_mode"] == "synthetic_contract"
    sherdog = parse_fighter_page(
        (Path(__file__).resolve().parents[1] / "fixtures/sources/sherdog/fighter_public_sample.html").read_text(
            encoding="utf-8"
        )
    )
    assert sherdog["parser_mode"] == "synthetic_contract"


def test_fixture_observations_are_not_verified_or_gold(tmp_path: Path) -> None:
    root = tmp_path / "tapology"
    fighters = root / "fighters"
    fighters.mkdir(parents=True)
    (fighters / "tap-100.html").write_text(
        (TAPOLOGY_FIXTURES / "fighter_public_sample.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (fighters / "tap-100-p2.html").write_text(
        (TAPOLOGY_FIXTURES / "fighter_tap-100-p2.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    rows = list(
        adapter.iter_fighter_observations(
            fighter_external_id="tap-100",
            observed_at=FIXED_NOW,
        )
    )
    bouts = [row for row in rows if row.entity_kind == "regional_bout"]
    assert bouts
    assert all(row.detail_level != DetailLevel.VERIFIED for row in bouts)
    assert all(row.quality_tier != "gold" for row in bouts)
    assert all(row.attributes.get("observation_origin") == "synthetic_fixture" for row in bouts)


def test_correction_with_new_payload_appends_revision(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.flush()
        first = apply_history_observation(session, _obs())
        assert first is None
        second = apply_history_observation(
            session,
            _obs(
                payload_hash=HASH_B,
                raw_ref=HASH_B,
                attributes={"result": "nc", "revision": 1, "external_bout_id": "tb-corr"},
            ),
        )
        assert second != "skipped_identical"
        rows = list(
            session.scalars(
                select(HistorySourceBout).where(HistorySourceBout.external_bout_id == "tb-corr")
            )
        )
        assert len(rows) == 2
        assert {row.revision for row in rows} == {1, 2}
        assert {row.result for row in rows} == {"win", "nc"}


def test_identical_payload_remains_idempotent(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.flush()
        apply_history_observation(session, _obs())
        skipped = apply_history_observation(session, _obs())
        assert skipped == "skipped_identical"
        rows = list(session.scalars(select(HistorySourceBout)))
        assert len(rows) == 1


def test_upcoming_db_seed_merges_exact_source_ids(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        session.add(CanonicalFighter(id="f-alex", display_name="Alex Sample"))
        session.add(CanonicalFighter(id="f-opp", display_name="Opp"))
        session.add(
            CanonicalEvent(
                id="e-dwcs",
                name="DWCS upcoming",
                series="dwcs",
                status="scheduled",
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="b-up",
                event_id="e-dwcs",
                fighter_a_id="f-alex",
                fighter_b_id="f-opp",
                status="scheduled",
            )
        )
        session.flush()
        session.add(BoutParticipant(bout_id="b-up", fighter_id="f-alex", corner="a"))
        session.add(BoutParticipant(bout_id="b-up", fighter_id="f-opp", corner="b"))
        session.add(
            FighterSourceId(
                fighter_id="f-alex", source="tapology_public", external_id="tap-100"
            )
        )
        session.add(
            FighterSourceId(
                fighter_id="f-alex", source="sherdog_public", external_id="sh-100"
            )
        )
        session.commit()
        seeds = load_upcoming_dwcs_fighters(session=session)
    alex = next(row for row in seeds if row.canonical_id == "f-alex")
    assert alex.source_ids.get("tapology_public") == "tap-100"
    assert alex.source_ids.get("sherdog_public") == "sh-100"
    assert alex.source_ids.get("combat_registry") == "cr-100"


def test_missing_source_id_persists_failure_not_silent_zero(history_env) -> None:
    Session = history_env["Session"]
    from mma_model.history.sync import FighterSeed

    report = sync_regional_history(
        repo=history_env["repo"],
        session_factory=Session,
        fighters=[FighterSeed(display_name="No Ids", source_ids={})],
        fixture_roots={},
        observed_at=FIXED_NOW,
    )
    assert report.identity.get("queued", 0) + report.identity.get("unresolved", 0) >= 1
    with Session() as session:
        failures = list(session.scalars(select(HistorySourceFailure)))
        assert failures
        assert any("missing" in row.reason or "unresolved" in row.reason for row in failures)


def test_https_live_urls() -> None:
    assert TapologyPublicClient.live_base_url().startswith("https://")
    assert SherdogPublicClient.live_base_url().startswith("https://")
    assert CombatRegistryPublicClient.live_base_url().startswith("https://")
    assert "/fightcenter/fighters/x" in TapologyPublicClient.fighter_url("x")
    assert TapologyPublicClient.fighter_url("x").startswith("https://")


def test_years_filter_excludes_out_of_range_bouts(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="y2022",
            event_date=date(2022, 1, 1),
            effective_at=datetime(2022, 1, 1, tzinfo=UTC),
            proxy_published_at=datetime(2022, 1, 2, tzinfo=UTC),
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="y2024",
            event_date=date(2024, 1, 1),
            effective_at=datetime(2024, 1, 1, tzinfo=UTC),
            proxy_published_at=datetime(2024, 1, 2, tzinfo=UTC),
        )
        session.commit()
        coverage = evaluate_sample_coverage(
            session,
            sample={
                "professional_bouts": [
                    {
                        "bout_id": "y2022",
                        "source": "tapology_public",
                        "classification": "professional",
                        "event_date": "2022-01-01",
                    },
                    {
                        "bout_id": "y2024",
                        "source": "tapology_public",
                        "classification": "professional",
                        "event_date": "2024-01-01",
                    },
                ],
                "amateur_regulated_us_bouts": [],
                "unknown_classification_bouts": [],
                "explicit_pre_fight_records": [],
            },
            years=range(2023, 2026),
        )
        ids = {row["bout_id"] for row in coverage.eligible_sample_bouts}
        assert "y2024" in ids
        assert "y2022" not in ids
        assert coverage.professional_n == 0
        assert coverage.fixture_professional_n >= 1


def test_conflations_are_measured_not_hardcoded(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        first = resolve_regional_fighter(
            session,
            source="tapology_public",
            external_id="dup-1",
            display_name="Jane Twin",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        second = resolve_regional_fighter(
            session,
            source="sherdog_public",
            external_id="dup-2",
            display_name="Jane Twin",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        assert first.kind in {"created", "linked"}
        assert second.kind == "queued"
        n = compute_identity_conflations(session)
        assert n >= 1


def test_current_record_not_written_to_profile_observations(history_env, tmp_path) -> None:
    root = stage_sync_fixtures(tmp_path)
    sync_regional_history(
        repo=history_env["repo"],
        session_factory=history_env["Session"],
        fighters=load_upcoming_dwcs_fighters(),
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    with history_env["Session"]() as session:
        profiles = list(session.scalars(select(FighterProfileObservation)))
        assert profiles == []


def test_left_truncation_counts_histories_not_bout_rows(history_env, tmp_path) -> None:
    root = stage_sync_fixtures(tmp_path)
    sync_regional_history(
        repo=history_env["repo"],
        session_factory=history_env["Session"],
        fighters=load_upcoming_dwcs_fighters(),
        fixture_roots={
            "tapology_public": root / "tapology_public",
            "sherdog_public": root / "sherdog_public",
            "combat_registry": root / "combat_registry",
        },
        observed_at=FIXED_NOW,
    )
    with history_env["Session"]() as session:
        n = left_truncated_history_count(session)
        assert n <= 2
        coverage = evaluate_sample_coverage(session)
        assert coverage.left_truncated == n


def test_missing_fixture_page_persists_source_failure(history_env, tmp_path) -> None:
    root = stage_sync_fixtures(tmp_path)
    (root / "tapology_public" / "fighters" / "tap-jose.html").unlink()
    from mma_model.history.sync import FighterSeed

    sync_regional_history(
        repo=history_env["repo"],
        session_factory=history_env["Session"],
        fighters=[
            FighterSeed(
                display_name="José Núñez",
                source_ids={"tapology_public": "tap-jose"},
            )
        ],
        fixture_roots={"tapology_public": root / "tapology_public"},
        sources=["tapology_public"],
        observed_at=FIXED_NOW,
    )
    with history_env["Session"]() as session:
        failures = list(session.scalars(select(HistorySourceFailure)))
        assert any(
            row.source == "tapology_public" and "missing" in row.reason for row in failures
        )
        evidence = json.loads(failures[0].evidence_json)
        assert "tap-jose" in json.dumps(evidence)


def test_migration_enforces_history_fks_and_checks(history_env) -> None:
    engine = history_env["engine"]
    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO history_reconstructions "
                    "(id, fighter_canonical_id, cutoff, reconstruction_version, "
                    "payload_json, payload_hash, created_at) "
                    "VALUES ('r1', 'missing-fighter', '2024-01-01T00:00:00+00:00', '1', "
                    "'{}', :h, '2024-01-01T00:00:00+00:00')"
                ),
                {"h": HASH_A},
            )
            conn.commit()
    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO history_source_bouts "
                    "(id, source, stream, external_bout_id, fighter_source, "
                    "fighter_external_id, fighter_name, opponent_name, classification, "
                    "regulated_us, result, left_truncated, version_kind, revision, "
                    "bout_status, quality_tier, timestamp_quality, observed_at, "
                    "effective_at, payload_hash, identity_status, is_current_record, "
                    "created_at) VALUES "
                    "('b1','tapology_public','fighter_history','x','tapology_public',"
                    "'x','N','O','professional','unknown','win',0,'event_night',1,"
                    "'completed','bronze','unknown','2024-01-01T00:00:00+00:00',"
                    "'2024-01-01T00:00:00+00:00', :h, 'bogus', 0, "
                    "'2024-01-01T00:00:00+00:00')"
                ),
                {"h": HASH_A},
            )
            conn.commit()


def test_cli_audit_exits_2_for_blockers(tmp_path: Path, capsys) -> None:
    from mma_model.cli import main
    from tests.history.helpers import make_history_db

    env = make_history_db(tmp_path)
    try:
        root = stage_sync_fixtures(tmp_path)
        assert (
            main(
                [
                    "history",
                    "sync",
                    "--fighters",
                    "upcoming-dwcs",
                    "--database-url",
                    env["db_url"],
                    "--raw-store",
                    str(tmp_path / "raw"),
                    "--fixture-root",
                    str(root),
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()
        code = main(
            [
                "history",
                "audit",
                "--years",
                "2023:2025",
                "--database-url",
                env["db_url"],
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert code == 2
        payload = json.loads(out)
        assert payload["gates_ok"] is False
        assert payload["blockers"]
        assert "insufficient_comparable_records" in payload["blockers"] or any(
            "live" in item for item in payload["blockers"]
        )
    finally:
        env["engine"].dispose()
