"""Future-row, same-card, and correction invariance tests (DWCS-106)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from mma_model.db.tables.core import CanonicalBout, CanonicalFighter
from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest, load_dwcs_event_manifest
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.leakage import (
    FutureRowLeakageError,
    append_correction,
    append_future_bout,
    append_future_history_bout,
    append_future_observation,
    append_mutable_profile,
    append_source_failure,
    assert_future_row_invariance,
    snapshot_for_cutoff,
)
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import make_empty_db

UTC = timezone.utc
CUTOFF = datetime(2020, 1, 1, tzinfo=UTC)
FUTURE = datetime(2026, 9, 1, tzinfo=UTC)


def test_future_row_invariance_on_empty_and_appended_rows(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        session = env["Session"]()
        fighter_a = str(uuid4())
        fighter_b = str(uuid4())
        session.add(CanonicalFighter(id=fighter_a, display_name="Future A"))
        session.add(CanonicalFighter(id=fighter_b, display_name="Future B"))
        session.commit()

        def feature_fn(cutoff: datetime):
            return snapshot_for_cutoff(session, cutoff=cutoff)

        def mutate() -> None:
            append_future_bout(
                session,
                event_id=str(uuid4()),
                bout_id=str(uuid4()),
                fighter_a_id=fighter_a,
                fighter_b_id=fighter_b,
                effective_at=FUTURE,
            )
            append_source_failure(
                session,
                source="tapology_public",
                reason="http_403",
                observed_at=FUTURE,
            )
            append_mutable_profile(session, fighter_id=fighter_a, observed_at=FUTURE)
            append_future_history_bout(session, fighter_id=fighter_a, effective_at=FUTURE)
            session.commit()

        assert_future_row_invariance(feature_fn, CUTOFF, mutate)
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert current.universe_bouts == 440
        session.close()
    finally:
        env["engine"].dispose()


def test_populated_cutoff_has_visible_bronze_baseline(populated) -> None:
    policy = load_source_policy()
    with populated["Session"]() as session:
        report = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF
        )
    assert report.core_tiers["bronze"] > 0
    assert report.core_tiers["missing"] > 0
    assert sum(report.core_tiers.values()) == 440
    assert report.core_tiers["silver"] == 0


def test_malicious_future_rows_leave_past_classifications_unchanged(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    target = next(bout for bout in bouts if bout.calendar_year <= 2019)
    bout_id = canonical_bout_id(target.espn_competition_id)
    with populated["Session"]() as session:
        before = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF
        )
        assert before.core_tiers["bronze"] > 0
        visible = next(item for item in before.bouts if item.overall_tier == "bronze")
        bout = session.get(CanonicalBout, bout_id)
        assert bout is not None
        append_future_observation(
            session,
            bout_id=visible.bout_id,
            source="ufcstats_public",
            effective_at=FUTURE,
        )
        append_correction(
            session,
            bout_id=bout_id,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            effective_at=FUTURE,
            result_type="no_contest",
        )
        append_source_failure(
            session,
            source="mma_ai_bootstrap",
            reason="http_403",
            observed_at=FUTURE,
        )
        append_mutable_profile(session, fighter_id=bout.fighter_a_id, observed_at=FUTURE)
        append_future_history_bout(
            session, fighter_id=bout.fighter_a_id, effective_at=FUTURE
        )
        append_future_bout(
            session,
            event_id=str(uuid4()),
            bout_id=str(uuid4()),
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            effective_at=FUTURE,
        )
        session.flush()
        after = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=CUTOFF
        )
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        session.rollback()
    assert after.report_hash == before.report_hash
    assert after.core_tiers == before.core_tiers
    assert [(row.bout_id, row.overall_tier) for row in after.bouts] == [
        (row.bout_id, row.overall_tier) for row in before.bouts
    ]
    leaked = next(item for item in current.bouts if item.bout_id == visible.bout_id)
    assert leaked.overall_tier != "bronze"


def test_later_correction_visible_only_after_adjudication_clock(populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    target = next(bout for bout in bouts if bout.calendar_year <= 2019)
    bout_id = canonical_bout_id(target.espn_competition_id)
    cutoff = datetime(2020, 6, 1, tzinfo=UTC)
    with populated["Session"]() as session:
        before = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=cutoff
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
            effective_at=FUTURE,
            result_type="no_contest",
        )
        session.flush()
        after_cutoff = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=cutoff
        )
        after_now = compute_coverage_report(series="dwcs", session=session, policy=policy)
        session.rollback()
    assert after_cutoff.report_hash == before.report_hash
    later = next(item for item in after_now.bouts if item.bout_id == bout_id)
    assert later.current_result == "no_contest"
    assert row.current_result != "no_contest"


def test_same_card_exclusion_hides_only_that_card(populated) -> None:
    events = load_dwcs_event_manifest()
    event = next(item for item in events if item.calendar_year <= 2019)
    event_id = canonical_event_id(event.espn_event_id)
    with populated["Session"]() as session:
        included = snapshot_for_cutoff(session, cutoff=CUTOFF)
        excluded = snapshot_for_cutoff(
            session, cutoff=CUTOFF, exclude_event_id=event_id
        )
    assert included["core_tiers"]["bronze"] > 0
    included_map = dict(included["bout_tiers"])
    excluded_map = dict(excluded["bout_tiers"])
    bouts = load_dwcs_bout_manifest()
    same_card = [
        canonical_bout_id(bout.espn_competition_id)
        for bout in bouts
        if bout.espn_event_id == event.espn_event_id
    ]
    for bout_id in same_card:
        if included_map[bout_id] != "missing":
            assert excluded_map[bout_id] == "missing"
    for bout_id, tier in included["bout_tiers"]:
        if bout_id not in same_card:
            assert excluded_map[bout_id] == tier
    assert len(included["bout_tiers"]) == 440
    assert len(excluded["bout_tiers"]) == 440


def test_invariance_raises_when_mutation_changes_past(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        session = env["Session"]()
        state = {"n": 0}

        def feature_fn(_cutoff: datetime):
            return {"n": state["n"]}

        def mutate() -> None:
            state["n"] = 1

        try:
            assert_future_row_invariance(feature_fn, CUTOFF, mutate)
            raised = False
        except FutureRowLeakageError:
            raised = True
        assert raised is True
        session.close()
    finally:
        env["engine"].dispose()
