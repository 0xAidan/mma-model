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
            session.commit()

        assert_future_row_invariance(feature_fn, CUTOFF, mutate)
        current = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert current.universe_bouts == 440
        session.close()
    finally:
        env["engine"].dispose()


def test_later_correction_visible_only_after_adjudication_clock(tmp_path, populated) -> None:
    policy = load_source_policy()
    bouts = load_dwcs_bout_manifest()
    target = next(b for b in bouts if b.calendar_year <= 2019)
    bout_id = canonical_bout_id(target.espn_competition_id)
    cutoff = datetime(2020, 6, 1, tzinfo=UTC)
    with populated["Session"]() as session:
        before = compute_coverage_report(
            series="dwcs", session=session, policy=policy, as_of=cutoff
        )
        row = next(item for item in before.bouts if item.bout_id == bout_id)
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
    assert row.bout_id == bout_id


def test_same_card_exclusion_does_not_change_other_cards(populated) -> None:
    events = load_dwcs_event_manifest()
    event = events[0]
    event_id = canonical_event_id(event.espn_event_id)
    cutoff = datetime(2017, 12, 31, tzinfo=UTC)
    with populated["Session"]() as session:
        included = snapshot_for_cutoff(session, cutoff=cutoff)
        excluded = snapshot_for_cutoff(
            session, cutoff=cutoff, exclude_event_id=event_id
        )
    assert included["as_of"] == excluded["as_of"]
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
