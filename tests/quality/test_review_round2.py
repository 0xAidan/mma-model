"""Second independent Grok 4.6 review findings for DWCS-106."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalFighter,
    FighterProfileObservation,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.dwcs.ids import canonical_bout_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest
from mma_model.quality.classify import observation_visible, result_version_visible
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import evaluate_strict_gates, report_with_gates
from mma_model.quality.leakage import append_correction
from mma_model.quality.schema import load_coverage_schema, validate_coverage_json
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import add_ingest_run, add_observation, make_empty_db

UTC = timezone.utc
PAST = datetime(2018, 6, 2, tzinfo=UTC)
BACKDATED = datetime(2017, 6, 2, tzinfo=UTC)
CUTOFF_2018 = datetime(2018, 1, 1, tzinfo=UTC)
CUTOFF_2020 = datetime(2020, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 8, 12, tzinfo=UTC)
FUTURE = datetime(2026, 9, 1, tzinfo=UTC)
ADJUDICATED = datetime(2021, 6, 1, tzinfo=UTC)


def _first_bout_id() -> str:
    return canonical_bout_id(load_dwcs_bout_manifest()[0].espn_competition_id)


def _attrs(result_type: str = "decisive", winner: str = "aaa") -> str:
    return json.dumps({"result_type": result_type, "winner_fighter_id": winner})


def test_seven_reversed_bouts_are_bronze_ledger_not_conflict(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    reversed_bouts = [row for row in bouts if row.version_state == "reversed_to_no_contest"]
    nc_both = [
        row
        for row in bouts
        if row.event_night_result.class_ == "no_contest"
        and row.current_result.class_ == "no_contest"
    ]
    assert len(reversed_bouts) == 7
    assert len(nc_both) == 1
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        report, _gates = report_with_gates(report, policy)
    validate_coverage_json(report.model_dump(mode="json"), load_coverage_schema())
    assert report.core_tiers["bronze"] == 440
    assert report.core_tiers["conflict"] == 0
    assert report.pit.conflicting_outcomes == 0
    assert report.result_transitions.reversed_to_no_contest == 7
    assert report.result_transitions.both_lanes_no_contest == 1
    assert report.result_transitions.event_night_equals_current == 433
    for bout in reversed_bouts:
        row = next(
            item
            for item in report.bouts
            if item.bout_id == canonical_bout_id(bout.espn_competition_id)
        )
        assert row.overall_tier == "bronze"
        assert row.event_night_result == bout.event_night_result.class_
        assert row.current_result == "no_contest"
        assert row.event_night_result != row.current_result
    both = next(
        item
        for item in report.bouts
        if item.bout_id == canonical_bout_id(nc_both[0].espn_competition_id)
    )
    assert both.overall_tier == "bronze"
    assert both.event_night_result == "no_contest"
    assert both.current_result == "no_contest"


def test_mismatch_ledger_entity_kind_is_gap_not_outcome_conflict(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session, source="dwcs_manifest")
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="mismatch-ledger",
                subject_id=bout_id,
                entity_kind="conflict",
                effective_at=PAST,
                observed_at=NOW,
                timestamp_quality="unknown",
                quality_tier="conflict",
                attributes_json='{"conflict_kind":"participant_or_result_disagreement"}',
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "missing"
        assert report.core_tiers["conflict"] == 0
    finally:
        env["engine"].dispose()


def test_mutable_audit_reports_guard_and_zero_row_scan_without_padding(
    tmp_path,
) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
            gates = evaluate_strict_gates(report, policy)
        pit = report.pit
        assert pit.mutable_current_rows_examined >= 0
        assert pit.mutable_current_applicable_rows == 0
        assert pit.mutable_current_synthetic_guard_checks >= 1
        assert pit.mutable_current_leakage_failures == 0
        assert pit.mutable_current_leakage_status == "not_applicable"
        assert pit.mutable_current_leakage_evidence_hash
        assert pit.mutable_current_leakage_checks_executed == (
            pit.mutable_current_synthetic_guard_checks + pit.mutable_current_applicable_rows
        )
        mutable = next(row for row in gates.gates if row.code == "mutable_current_leakage")
        assert mutable.status == "not_applicable"
        assert mutable.blocking is False
        assert "mutable_current_leakage" not in gates.blocker_codes
        leaked = observation_visible(
            effective_at=PAST,
            observed_at=NOW,
            proxy_published_at=None,
            timestamp_quality="unknown",
            version_kind=None,
            is_mutable_current=True,
            cutoff=CUTOFF_2020,
            source="mutable_current",
        )
        assert leaked is False
    finally:
        env["engine"].dispose()


def test_adding_mutable_profile_changes_global_hash_and_audit_row_count(
    tmp_path,
) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            fighter_id = str(uuid4())
            session.add(CanonicalFighter(id=fighter_id, display_name="Mutable Probe"))
            session.flush()
            baseline = compute_coverage_report(series="dwcs", session=session, policy=policy)
            baseline_cutoff = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            session.add(
                FighterProfileObservation(
                    fighter_id=fighter_id,
                    attribute="record_wins",
                    value_num=12.0,
                    source="mutable_current",
                    effective_at=BACKDATED,
                    observed_at=NOW,
                )
            )
            session.flush()
            after = compute_coverage_report(series="dwcs", session=session, policy=policy)
            cutoff = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            session.rollback()
        assert after.db_hash != baseline.db_hash
        assert after.report_hash != baseline.report_hash
        assert after.pit.mutable_current_applicable_rows >= 1
        assert after.pit.mutable_current_rows_examined >= 1
        assert after.pit.mutable_current_leakage_status == "pass"
        assert after.pit.mutable_current_leakage_failures == 0
        assert cutoff.db_hash != baseline_cutoff.db_hash
        assert cutoff.pit.mutable_current_applicable_rows >= 1
    finally:
        env["engine"].dispose()


def test_future_mutable_row_is_cutoff_filtered_but_in_global_fingerprint(
    tmp_path,
) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            before_cutoff = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            before_global = compute_coverage_report(series="dwcs", session=session, policy=policy)
            fighter_id = str(uuid4())
            session.add(CanonicalFighter(id=fighter_id, display_name="Future Mutable"))
            session.flush()
            session.add(
                FighterProfileObservation(
                    fighter_id=fighter_id,
                    attribute="record_wins",
                    value_num=99.0,
                    source="mutable_current",
                    effective_at=FUTURE,
                    observed_at=FUTURE,
                )
            )
            session.flush()
            after_cutoff = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            after_global = compute_coverage_report(series="dwcs", session=session, policy=policy)
            session.rollback()
        assert after_cutoff.db_hash == before_cutoff.db_hash
        assert after_global.db_hash != before_global.db_hash
        assert after_global.pit.mutable_current_applicable_rows >= 1
    finally:
        env["engine"].dispose()


def test_future_observed_correction_without_past_clock_does_not_leak(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    target = next(bout for bout in bouts if bout.calendar_year <= 2017)
    bout_id = canonical_bout_id(target.espn_competition_id)
    with populated["Session"]() as session:
        before = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        row = next(item for item in before.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "bronze"
        bout = session.get(CanonicalBout, bout_id)
        assert bout is not None
        append_correction(
            session,
            bout_id=bout_id,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            effective_at=BACKDATED,
            observed_at=FUTURE,
            result_type="no_contest",
        )
        session.flush()
        after = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        session.rollback()
    assert after.report_hash == before.report_hash
    assert after.core_tiers == before.core_tiers
    later = next(item for item in current.bouts if item.bout_id == bout_id)
    assert later.current_result == "no_contest"
    assert (
        result_version_visible(
            effective_at=BACKDATED,
            cutoff=CUTOFF_2018,
            timestamp_quality="unknown",
            observed_at=FUTURE,
            provenance_status="unknown",
        )
        is False
    )


def test_later_adjudication_clock_hides_correction_until_published(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    target = next(bout for bout in bouts if bout.calendar_year <= 2017)
    bout_id = canonical_bout_id(target.espn_competition_id)
    with populated["Session"]() as session:
        before = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        bout = session.get(CanonicalBout, bout_id)
        assert bout is not None
        run = add_ingest_run(session, source="dwcs_manifest")
        later_obs = add_observation(
            session,
            run.id,
            source="dwcs_manifest",
            external_id=f"adjudicated-{bout_id}",
            subject_id=bout_id,
            version_kind="current",
            effective_at=BACKDATED,
            observed_at=FUTURE,
            source_published_at=ADJUDICATED,
            timestamp_quality="revision_snapshot",
            quality_tier="gold",
            attributes_json=_attrs("no_contest", ""),
        )
        session.flush()
        append_correction(
            session,
            bout_id=bout_id,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            effective_at=BACKDATED,
            observed_at=FUTURE,
            result_type="no_contest",
            raw_observation_id=later_obs.id,
        )
        session.flush()
        at_2018 = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        after_adjudication = compute_coverage_report(
            series="dwcs",
            session=session,
            policy=policy,
            as_of=datetime(2021, 7, 1, tzinfo=UTC),
        )
        session.rollback()
    assert at_2018.report_hash == before.report_hash
    later = next(item for item in after_adjudication.bouts if item.bout_id == bout_id)
    assert later.current_result == "no_contest"


def test_matching_raw_proxy_keeps_event_night_visible_at_historical_cutoff(
    populated,
) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
    assert report.core_tiers["bronze"] > 0
    visible = next(item for item in report.bouts if item.overall_tier == "bronze")
    assert visible.event_night_result in {"decisive", "draw", "no_contest"}
    assert (
        result_version_visible(
            effective_at=PAST,
            cutoff=CUTOFF_2020,
            timestamp_quality="publication_proxy",
            proxy_published_at=PAST,
            observed_at=NOW,
            provenance_status="linked",
            raw_observation_id=1,
        )
        is True
    )


def test_ufcstats_and_mma_ai_are_same_family_not_silver(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="u1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            add_observation(
                session,
                run.id,
                source="mma_ai_bootstrap",
                external_id="ai1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "bronze"
        assert row.overall_tier != "silver"
    finally:
        env["engine"].dispose()


def test_manifest_and_sportsdataio_proxy_agree_silver(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="m1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            add_observation(
                session,
                run.id,
                source="sportsdataio",
                external_id="sdio1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "silver"
        licensed = next(item for item in report.source_rows if item.source == "sportsdataio")
        assert licensed.validation_only is True
        assert licensed.status == "validation_only"
    finally:
        env["engine"].dispose()


def test_pit_fighter_count_is_visible_participants_not_zero(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        first = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        second = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        visible_ids = [row.bout_id for row in first.bouts if row.overall_tier != "missing"]
        expected: set[str] = set()
        bouts = session.scalars(
            select(CanonicalBout).where(CanonicalBout.id.in_(visible_ids))
        ).all()
        for bout in bouts:
            expected.add(bout.fighter_a_id)
            expected.add(bout.fighter_b_id)
        parts = session.scalars(
            select(BoutParticipant.fighter_id).where(BoutParticipant.bout_id.in_(visible_ids))
        ).all()
        expected.update(str(item) for item in parts)
    assert first.counts_fighters == len(expected)
    assert first.counts_fighters > 0
    assert first.counts_fighters == second.counts_fighters
    assert first.report_hash == second.report_hash
    assert first.core_tiers["bronze"] > 0


def test_dummy_live_sample_row_is_one_of_nine_not_inflated_by_global_kill(
    tmp_path,
) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            session.add(
                HistorySourceBout(
                    source="tapology_public",
                    stream="fighter_history",
                    external_bout_id="tb-pro-1",
                    fighter_source="tapology_public",
                    fighter_external_id="live-sample-1",
                    fighter_name="Live Sample",
                    opponent_name="Opp",
                    classification="professional",
                    result="win",
                    observed_at=PAST,
                    effective_at=PAST,
                    payload_hash="b" * 64,
                    identity_status="unresolved",
                    observation_origin="live_public",
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        regional = report.regional_live
        assert regional["professional_n"] == 9
        assert regional["professional_found"] == 1
        assert regional["professional_source_failed"] == 0
        assert regional["professional_missing"] == 8
        _, gates = report_with_gates(report, policy)
        pro = next(row for row in gates.gates if row.code == "regional_professional_sample")
        assert pro.status == "fail"
        assert pro.numerator == 1
        assert pro.denominator == 9
    finally:
        env["engine"].dispose()


def test_explicit_subject_failure_counts_only_that_sample_row(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            session.add(
                HistorySourceBout(
                    source="tapology_public",
                    stream="fighter_history",
                    external_bout_id="tb-pro-1",
                    fighter_source="tapology_public",
                    fighter_external_id="live-sample-1",
                    fighter_name="Live Sample",
                    opponent_name="Opp",
                    classification="professional",
                    result="win",
                    observed_at=PAST,
                    effective_at=PAST,
                    payload_hash="b" * 64,
                    identity_status="unresolved",
                    observation_origin="live_public",
                )
            )
            session.add(
                HistorySourceFailure(
                    source="tapology_public",
                    reason="http_403",
                    scope="sample",
                    subject="tb-pro-2",
                    evidence_json="{}",
                    observed_at=PAST,
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        regional = report.regional_live
        assert regional["professional_n"] == 9
        assert regional["professional_found"] == 1
        assert regional["professional_source_failed"] == 1
        assert regional["professional_missing"] == 7
    finally:
        env["engine"].dispose()


def test_no_subject_evidence_is_missing_not_global_kill_failure(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        regional = report.regional_live
        assert regional["professional_found"] == 0
        assert regional["professional_source_failed"] == 0
        assert regional["professional_missing"] == 9
        assert regional["amateur_n"] == 2
        assert regional["amateur_missing"] == 2
        assert regional["evidence_class"] == "fixture_validation"
        assert regional["professional_n"] == 9
    finally:
        env["engine"].dispose()
