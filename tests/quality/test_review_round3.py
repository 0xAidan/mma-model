"""Third independent Grok 4.6 review findings for DWCS-106."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from mma_model.db.tables.core import (
    BoutResultVersion,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
)
from mma_model.db.tables.history import HistorySourceBout, HistorySourceFailure
from mma_model.db.tables.provenance import RawObservation
from mma_model.dwcs.ids import canonical_bout_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest
from mma_model.quality.classify import result_version_visible
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.leakage import append_correction
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import add_ingest_run, add_observation, make_empty_db

UTC = timezone.utc
HOLOBAUGH_ESPN = "237192"
HOLOBAUGH_ID = canonical_bout_id(HOLOBAUGH_ESPN)
CUTOFF_2018 = datetime(2018, 1, 1, tzinfo=UTC)
PAST = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 12, tzinfo=UTC)
FUTURE = datetime(2026, 9, 1, tzinfo=UTC)
ADJUDICATED = datetime(2021, 6, 1, tzinfo=UTC)


def _attrs(result_type: str = "decisive", winner: str = "aaa") -> str:
    return json.dumps({"result_type": result_type, "winner_fighter_id": winner})


def _ensure_universe_bout(session, bout_id: str) -> tuple[str, str]:
    fighter_a_id = str(uuid4())
    fighter_b_id = str(uuid4())
    event_id = str(uuid4())
    session.add(CanonicalFighter(id=fighter_a_id, display_name="Fighter A"))
    session.add(CanonicalFighter(id=fighter_b_id, display_name="Fighter B"))
    session.add(
        CanonicalEvent(id=event_id, name="Test card", series="dwcs", status="completed")
    )
    session.flush()
    session.add(
        CanonicalBout(
            id=bout_id,
            event_id=event_id,
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            status="completed",
        )
    )
    session.flush()
    return fighter_a_id, fighter_b_id


def test_empty_db_uses_fixed_live_sample_denominator(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        regional = report.regional_live
        assert regional["professional_n"] == 9
        assert regional["professional_found"] == 0
        assert regional["professional_source_failed"] == 0
        assert regional["professional_missing"] == 9
        assert regional["amateur_n"] == 2
        assert regional["amateur_found"] == 0
        assert regional["amateur_source_failed"] == 0
        assert regional["amateur_missing"] == 2
        assert regional["fixture_professional_found"] == 0
        _, gates = report_with_gates(report, policy)
        pro = next(row for row in gates.gates if row.code == "regional_professional_sample")
        am = next(row for row in gates.gates if row.code == "regional_amateur_sample")
        assert pro.status == "fail"
        assert pro.numerator == 0
        assert pro.denominator == 9
        assert am.status == "fail"
        assert am.numerator == 0
        assert am.denominator == 2
    finally:
        env["engine"].dispose()


def test_dummy_live_and_subject_failure_do_not_use_global_kill(tmp_path) -> None:
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
        assert regional["fixture_professional_found"] == 0
    finally:
        env["engine"].dispose()


def test_result_version_uses_exact_raw_link_not_competing_source(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = canonical_bout_id(load_dwcs_bout_manifest()[0].espn_competition_id)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            fighter_a_id, fighter_b_id = _ensure_universe_bout(session, bout_id)
            run = add_ingest_run(session, source="dwcs_manifest")
            linked = add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="linked-current",
                subject_id=bout_id,
                version_kind="current",
                effective_at=PAST,
                observed_at=NOW,
                timestamp_quality="unknown",
                attributes_json=_attrs("decisive", "aaa"),
            )
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="competitor",
                subject_id=bout_id,
                version_kind="current",
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs("decisive", "aaa"),
            )
            session.flush()
            session.add(
                BoutResultVersion(
                    bout_id=bout_id,
                    version_kind="current",
                    revision=1,
                    fighter_a_id=fighter_a_id,
                    fighter_b_id=fighter_b_id,
                    result_type="decisive",
                    winner_fighter_id=None,
                    effective_at=PAST,
                    observed_at=NOW,
                    raw_observation_id=linked.id,
                    provenance_status="linked",
                )
            )
            session.flush()
            linked_id = linked.id
            cutoff = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            session.rollback()
        row = next(item for item in cutoff.bouts if item.bout_id == bout_id)
        assert row.current_result == "missing"
        assert result_version_visible(
            effective_at=PAST,
            cutoff=CUTOFF_2018,
            timestamp_quality="unknown",
            observed_at=NOW,
            provenance_status="linked",
            raw_observation_id=linked_id,
        ) is False
        assert result_version_visible(
            effective_at=PAST,
            cutoff=CUTOFF_2018,
            timestamp_quality="publication_proxy",
            proxy_published_at=PAST,
            observed_at=NOW,
            provenance_status="unknown",
        ) is False
    finally:
        env["engine"].dispose()


def test_later_revision_does_not_inherit_original_proxy(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        original = session.scalars(
            select(BoutResultVersion).where(
                BoutResultVersion.bout_id == HOLOBAUGH_ID,
                BoutResultVersion.version_kind == "current",
            )
        ).one()
        assert original.raw_observation_id is not None
        linked = session.get(RawObservation, original.raw_observation_id)
        assert linked is not None
        assert linked.timestamp_quality == "unknown"
        assert linked.proxy_published_at is None
        bout = session.get(CanonicalBout, HOLOBAUGH_ID)
        assert bout is not None
        run = add_ingest_run(session, source="dwcs_manifest")
        later_obs = add_observation(
            session,
            run.id,
            source="dwcs_manifest",
            external_id="later-current",
            subject_id=HOLOBAUGH_ID,
            version_kind="current",
            effective_at=PAST,
            observed_at=FUTURE,
            source_published_at=ADJUDICATED,
            timestamp_quality="revision_snapshot",
            quality_tier="gold",
            attributes_json=_attrs("no_contest", ""),
        )
        session.flush()
        append_correction(
            session,
            bout_id=HOLOBAUGH_ID,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            effective_at=PAST,
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
    holobaugh_2018 = next(item for item in at_2018.bouts if item.bout_id == HOLOBAUGH_ID)
    assert holobaugh_2018.current_result == "missing"
    later = next(item for item in after_adjudication.bouts if item.bout_id == HOLOBAUGH_ID)
    assert later.current_result == "no_contest"


def test_holobaugh_reversal_hidden_at_2018_cutoff(populated) -> None:
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
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        pit = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
        )
        versions = list(
            session.scalars(
                select(BoutResultVersion).where(BoutResultVersion.bout_id == HOLOBAUGH_ID)
            ).all()
        )
    assert current.core_tiers["bronze"] == 440
    assert current.core_tiers["conflict"] == 0
    assert current.result_transitions.reversed_to_no_contest == 7
    assert current.result_transitions.both_lanes_no_contest == 1
    holobaugh_now = next(item for item in current.bouts if item.bout_id == HOLOBAUGH_ID)
    assert holobaugh_now.event_night_result == "decisive"
    assert holobaugh_now.current_result == "no_contest"
    holobaugh_pit = next(item for item in pit.bouts if item.bout_id == HOLOBAUGH_ID)
    assert holobaugh_pit.overall_tier == "bronze"
    assert holobaugh_pit.event_night_result == "decisive"
    assert holobaugh_pit.current_result == "missing"
    assert pit.result_transitions.reversed_to_no_contest == 0
    current_row = next(row for row in versions if row.version_kind == "current")
    night_row = next(row for row in versions if row.version_kind == "event_night")
    assert current_row.raw_observation_id is not None
    assert night_row.raw_observation_id is not None
    assert current_row.raw_observation_id != night_row.raw_observation_id
    with populated["Session"]() as session:
        current_obs = session.get(RawObservation, current_row.raw_observation_id)
        night_obs = session.get(RawObservation, night_row.raw_observation_id)
    assert night_obs is not None
    assert night_obs.timestamp_quality == "publication_proxy"
    assert night_obs.proxy_published_at is not None
    assert current_obs is not None
    assert current_obs.timestamp_quality == "unknown"
    assert current_obs.proxy_published_at is None


def test_both_nc_lane_may_share_event_proxy(populated) -> None:
    policy = load_source_policy()
    both = next(
        row
        for row in load_dwcs_bout_manifest()
        if row.event_night_result.class_ == "no_contest"
        and row.current_result.class_ == "no_contest"
    )
    bout_id = canonical_bout_id(both.espn_competition_id)
    with populated["Session"]() as session:
        report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        versions = list(
            session.scalars(
                select(BoutResultVersion).where(BoutResultVersion.bout_id == bout_id)
            ).all()
        )
        obs_ids = [row.raw_observation_id for row in versions]
        observations = list(
            session.scalars(select(RawObservation).where(RawObservation.id.in_(obs_ids))).all()
        )
    row = next(item for item in report.bouts if item.bout_id == bout_id)
    assert row.event_night_result == "no_contest"
    assert row.current_result == "no_contest"
    assert {item.timestamp_quality for item in observations} == {"publication_proxy"}
    assert all(item.proxy_published_at is not None for item in observations)


def test_unlinked_result_version_is_hidden_at_cutoff(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = canonical_bout_id(load_dwcs_bout_manifest()[0].espn_competition_id)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            fighter_a_id, fighter_b_id = _ensure_universe_bout(session, bout_id)
            run = add_ingest_run(session, source="dwcs_manifest")
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="night",
                subject_id=bout_id,
                version_kind="event_night",
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            append_correction(
                session,
                bout_id=bout_id,
                fighter_a_id=fighter_a_id,
                fighter_b_id=fighter_b_id,
                effective_at=PAST,
                observed_at=NOW,
                result_type="no_contest",
            )
            session.flush()
            pit = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=CUTOFF_2018
            )
            current = compute_coverage_report(series="dwcs", session=session, policy=policy)
            session.rollback()
        pit_row = next(item for item in pit.bouts if item.bout_id == bout_id)
        now_row = next(item for item in current.bouts if item.bout_id == bout_id)
        assert pit_row.current_result == "missing"
        assert now_row.current_result == "no_contest"
    finally:
        env["engine"].dispose()
