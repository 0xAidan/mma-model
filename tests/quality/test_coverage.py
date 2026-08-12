"""Coverage inventory, tiers, and denominator tests (DWCS-106)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import pytest

from mma_model.db.tables.history import HistorySourceFailure
from mma_model.db.tables.provenance import IngestRun, RawObservation
from mma_model.dwcs.ids import canonical_bout_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.schema import (
    CoverageSchemaError,
    load_coverage_schema,
    validate_coverage_json,
)
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import make_empty_db


def test_empty_db_has_exactly_440_missing_core_tiers(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert len(report.bouts) == 440
        assert report.core_tier_sum == 440
        assert report.core_tiers["missing"] == 440
        assert report.universe_cards == 89
        assert report.standard.cards == 86
        assert report.standard.bouts == 425
        assert report.brazil.cards == 3
        assert report.brazil.bouts == 15
        ids = [row.bout_id for row in report.bouts]
        assert ids == sorted(ids)
        assert len(set(ids)) == 440
        assert {row.overall_tier for row in report.bouts} == {"missing"}
    finally:
        env["engine"].dispose()


def test_populated_manifest_89_440_and_result_lanes(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
    assert report.counts_events == 89
    assert report.counts_bouts == 440
    assert report.counts_result_versions == 880
    assert report.core_tier_sum == 440
    assert report.core_tiers["missing"] == 0
    assert report.core_tiers["silver"] == 0
    assert report.core_tiers["gold"] == 0
    assert report.core_tiers["bronze"] == 440
    assert report.core_tiers["conflict"] == 0
    assert report.standard.bouts == 425
    assert report.brazil.bouts == 15
    assert report.event_night.decisive == 438
    assert report.event_night.draw == 1
    assert report.event_night.no_contest == 1
    assert report.current.decisive == 431
    assert report.current.draw == 1
    assert report.current.no_contest == 8
    bouts = load_dwcs_bout_manifest()
    fighters = {p.espn_athlete_id for bout in bouts for p in bout.participants}
    assert report.counts_fighters == len(fighters)
    assert {row.overall_tier for row in report.bouts} == {"bronze"}
    source_classes = {row.source_class for row in report.bouts}
    assert source_classes == {"internal_manifest"}


def test_every_bout_exactly_one_overall_tier_and_source_specific_no_leak(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
    counts = Counter(row.overall_tier for row in report.bouts)
    assert sum(counts.values()) == 440
    assert len(report.bouts) == 440
    for source_row in report.source_rows:
        source_total = (
            source_row.gold
            + source_row.silver
            + source_row.bronze
            + source_row.missing_bouts
            + source_row.conflict_bouts
        )
        assert source_total == 440
    ufc = next(row for row in report.source_rows if row.source == "ufcstats_public")
    assert ufc.status == "source_killed"
    assert ufc.reason == "cloudflare_challenge"
    assert ufc.mapped_bouts == 0
    assert ufc.missing_bouts == 440
    tap = next(row for row in report.source_rows if row.source == "tapology_public")
    assert tap.status == "source_killed"
    cr = next(row for row in report.source_rows if row.source == "combat_registry")
    assert cr.status == "source_killed"
    sh = next(row for row in report.source_rows if row.source == "sherdog_public")
    assert sh.status == "accessibility_only"
    bdl = next(row for row in report.source_rows if row.source == "balldontlie")
    assert bdl.validation_only is True
    assert bdl.never_live_coverage is True


def test_populated_strict_core_pass_source_and_regional_fail(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        report, gates = report_with_gates(report, policy)
    assert "manifest_representation" in gates.passed_codes
    assert "core_denominator" in gates.passed_codes
    assert "identity_conflict" in gates.passed_codes
    assert "regional_professional_sample" in gates.blocker_codes
    assert "ufcstats_public_live" in gates.blocker_codes
    assert gates.exit_code == 2
    validate_coverage_json(report.model_dump(mode="json"))


def test_deterministic_hash_despite_insertion_order(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        first = compute_coverage_report(series="dwcs", session=session, policy=policy)
        second = compute_coverage_report(series="dwcs", session=session, policy=policy)
    assert first.report_hash == second.report_hash
    assert first.db_hash == second.db_hash
    assert first.config_hash == second.config_hash


def test_config_hash_changes_with_as_of(populated) -> None:
    policy = load_source_policy()
    cutoff = datetime(2018, 1, 1, tzinfo=timezone.utc)
    with populated["Session"]() as session:
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        past = compute_coverage_report(series="dwcs", session=session, policy=policy, as_of=cutoff)
    assert current.config_hash != past.config_hash
    assert past.core_tiers["missing"] > 0
    assert past.core_tiers["bronze"] > 0
    assert sum(past.core_tiers.values()) == 440
    assert past.core_tiers["silver"] == 0


def test_schema_rejects_unknown_enum_missing_required_additional() -> None:
    schema = load_coverage_schema()
    with pytest.raises(CoverageSchemaError, match="missing required"):
        validate_coverage_json({"schema_version": 1}, schema)
    payload = {"schema_version": 1, "extra_field": True}
    with pytest.raises(CoverageSchemaError):
        validate_coverage_json(payload, schema)
    with pytest.raises(CoverageSchemaError):
        validate_coverage_json(
            {
                "schema_version": 1,
                "contract_id": "dwcs_coverage",
                "contract_version": "1.0.0",
                "ticket": "DWCS-106",
                "series": "ufc",
            },
            schema,
        )


def test_duplicate_malformed_source_rows_conflict(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    bout_id = canonical_bout_id(bouts[0].espn_competition_id)
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    with populated["Session"]() as session:
        run = IngestRun(
            source="ufcstats_public",
            stream="history",
            scope="conflict-test",
            status="completed",
        )
        session.add(run)
        session.flush()
        session.add(
            RawObservation(
                ingest_run_id=run.id,
                source="ufcstats_public",
                stream="history",
                scope="conflict-test",
                checkpoint_version="v1",
                external_id="dup-a",
                entity_kind="bout_result",
                observed_at=now,
                effective_at=now,
                quality_tier="gold",
                timestamp_quality="direct_source_timestamp",
                payload_hash="a" * 64,
                raw_ref=None,
                subject_id=bout_id,
                version_kind="event_night",
                attributes_json='{"result_type":"decisive","winner_fighter_id":"aaa"}',
            )
        )
        session.add(
            RawObservation(
                ingest_run_id=run.id,
                source="ufcstats_public",
                stream="history",
                scope="conflict-test",
                checkpoint_version="v1",
                external_id="dup-b",
                entity_kind="bout_result",
                observed_at=now,
                effective_at=now,
                quality_tier="gold",
                timestamp_quality="direct_source_timestamp",
                payload_hash="b" * 64,
                raw_ref=None,
                subject_id=bout_id,
                version_kind="event_night",
                attributes_json='{"result_type":"draw","winner_fighter_id":null}',
            )
        )
        session.flush()
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        session.rollback()
    row = next(item for item in report.bouts if item.bout_id == bout_id)
    assert row.overall_tier == "conflict"
    ufc = next(item for item in report.source_rows if item.source == "ufcstats_public")
    assert ufc.conflict_bouts >= 1
    assert report.core_tier_sum == 440
    assert report.core_tiers["conflict"] >= 1
    assert report.core_tiers["bronze"] + report.core_tiers["conflict"] == 440


def test_killed_vs_absent_vs_unmeasured_vs_schema_drift(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        with env["Session"]() as session:
            session.add(
                HistorySourceFailure(
                    source="mma_ai_bootstrap",
                    reason="schema_drift",
                    scope="default",
                    subject="",
                    evidence_json="{}",
                    observed_at=now,
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        bootstrap = next(row for row in report.source_rows if row.source == "mma_ai_bootstrap")
        assert bootstrap.status == "schema_drift"
        missing = next(row for row in report.source_rows if row.source == "explicit_missing")
        assert missing.status == "unmeasured"
        tap = next(row for row in report.source_rows if row.source == "tapology_public")
        assert tap.status == "source_killed"
    finally:
        env["engine"].dispose()


def test_no_raw_blob_dangling_on_manifest_ingest(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
    assert report.raw_ref_integrity.ok is True
    assert report.raw_ref_integrity.dangling_raw_refs == 0
    assert report.raw_ref_integrity.blob_absent_explicit == 880
    assert report.checkpoint_run_state.ingest_runs >= 1
    assert report.checkpoint_run_state.succeeded_runs == 1
    assert report.checkpoint_run_state.completed_runs == 0
    assert report.pit.proxy_timestamps == 873
    assert report.pit.unknown_timestamps == 7
    assert report.pit.missing_required_details == 440
    assert report.pit.future_row_leakage_checks_executed > 0
    assert report.pit.mutable_current_leakage_checks_executed > 0


def test_coverage_on_alembic_migrated_empty_db(tmp_path) -> None:
    env = make_empty_db(tmp_path, migrate=True)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert report.universe_cards == 89
        assert report.universe_bouts == 440
        assert report.core_tiers["missing"] == 440
        assert report.core_tier_sum == 440
    finally:
        env["engine"].dispose()
