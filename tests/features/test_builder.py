"""PIT builder acceptance: invariance, same-card, swap, reversal, cache."""

from __future__ import annotations

from datetime import UTC, datetime

from mma_model.composites.rolling import RollingProfile
from mma_model.features.as_of import CutoffKind, cutoff_for_event
from mma_model.features.audit import _append_future_facts, build_audit_fixture, run_future_invariance
from mma_model.features.builder import FeatureBuilder
from mma_model.features.matchup import matchup_features
from mma_model.features.snapshot import FeatureSnapshot, SnapshotEvent
from mma_model.features.spec import spec_hash, swap_values
from mma_model.quality.leakage import assert_future_row_invariance
from tests.features.helpers import (
    add_bout,
    add_event,
    add_history,
    add_profile,
    add_result,
    add_strike_stats,
    cutoff_of,
    dt,
    named,
)


def test_future_invariance_on_audit_fixture() -> None:
    snapshot, cutoff, bout_id, fighter_a_id, fighter_b_id = build_audit_fixture()
    run_future_invariance(
        snapshot,
        cutoff,
        bout_id=bout_id,
        fighter_a_id=fighter_a_id,
        fighter_b_id=fighter_b_id,
    )


def test_future_invariance_cache_and_recompute_match() -> None:
    snapshot, cutoff, bout_id, fighter_a_id, fighter_b_id = build_audit_fixture()
    builder = FeatureBuilder(snapshot)
    first = builder.build(fighter_a_id, fighter_b_id, cutoff, bout_id=bout_id)
    cached_bytes = first.row_bytes

    def mutate() -> None:
        _append_future_facts(snapshot, fighter_a_id=fighter_a_id, fighter_b_id=fighter_b_id)

    def feature_fn(_cutoff: datetime) -> bytes:
        return builder.build(
            fighter_a_id,
            fighter_b_id,
            cutoff,
            bout_id=bout_id,
            use_cache=True,
        ).row_bytes

    assert_future_row_invariance(feature_fn, cutoff.cutoff, mutate)
    recomputed = builder.build(
        fighter_a_id,
        fighter_b_id,
        cutoff,
        bout_id=bout_id,
        use_cache=False,
    )
    assert recomputed.row_bytes == cached_bytes


def test_same_card_bout_does_not_see_other_bout_result() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "z")
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 1, 1),
    )
    b1 = add_bout(snapshot, "t1", "card", "a", "c")
    add_bout(snapshot, "t2", "card", "a", "b")
    leak_at = datetime(2019, 6, 1, 0, 30, tzinfo=UTC)
    add_result(
        snapshot,
        b1,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="0:15",
        effective_at=leak_at,
    )
    cutoff = cutoff_of(snapshot, "card")
    builder = FeatureBuilder(snapshot)
    row1 = builder.build("a", "c", cutoff, bout_id="t1")
    row2 = builder.build("a", "b", cutoff, bout_id="t2")
    v1 = named(row1.values, row1.names)
    v2 = named(row2.values, row2.names)
    assert v1["prior_fights_a"] == v2["prior_fights_a"] == 1.0
    assert v1["rating_a"] == v2["rating_a"]
    assert v1["ko_win_rate_missing_a"] == v2["ko_win_rate_missing_a"]
    assert v1["debut_a"] == 0.0


def test_swap_symmetry_negates_diffs_and_keeps_spec_hash() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "c")
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="1:10",
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="p1",
        landed=10,
        attempted=20,
        at=dt(2018, 1, 1),
        opp_id="c",
        opp_landed=3,
    )
    add_history(
        snapshot,
        fighter_id="b",
        opponent_id="reg-opp",
        at=dt(2017, 1, 1),
        result="win",
        method="SUB",
        external_bout_id="hist-b",
    )
    add_profile(snapshot, "a", "reach", at=dt(2017, 1, 1), value_num=70.0)
    add_profile(snapshot, "b", "reach", at=dt(2017, 1, 1), value_num=72.0)
    add_bout(snapshot, "main", "card", "a", "b")
    cutoff = cutoff_of(snapshot, "card")
    builder = FeatureBuilder(snapshot)
    ab = builder.build("a", "b", cutoff, bout_id="main")
    ba = builder.build("b", "a", cutoff, bout_id="main")
    assert ab.spec_hash == ba.spec_hash == spec_hash()
    assert ba.values == swap_values(ab.values)
    v = named(ab.values, ab.names)
    w = named(ba.values, ba.names)
    assert w["rating_diff"] == -v["rating_diff"]
    assert w["rating_a"] == v["rating_b"]
    assert w["reach_a"] == v["reach_b"]
    assert w["scheduled_rounds"] == v["scheduled_rounds"]
    assert w["scheduled_rounds_missing"] == v["scheduled_rounds_missing"]


def test_reversal_invisible_before_adjudication_visible_after() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "origin", dt(2018, 6, 1))
    add_event(snapshot, "mid", dt(2019, 7, 1))
    add_event(snapshot, "late", dt(2019, 9, 1))
    origin = add_bout(snapshot, "origin-bout", "origin", "a", "c")
    night = dt(2018, 6, 1, 5)
    add_result(
        snapshot,
        origin,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="1:10",
        effective_at=night,
        version_kind="event_night",
        revision=1,
    )
    adjudicated = datetime(2019, 8, 15, 12, 0, tzinfo=UTC)
    add_result(
        snapshot,
        origin,
        winner_id=None,
        method="NC",
        result_type="no_contest",
        ending_round=1,
        time_str="1:10",
        effective_at=adjudicated,
        observed_at=adjudicated,
        version_kind="current",
        revision=1,
    )
    add_bout(snapshot, "mid-bout", "mid", "a", "b")
    add_bout(snapshot, "late-bout", "late", "a", "b")
    builder = FeatureBuilder(snapshot)
    mid = named(
        builder.build("a", "b", cutoff_of(snapshot, "mid"), bout_id="mid-bout").values,
        builder.build("a", "b", cutoff_of(snapshot, "mid"), bout_id="mid-bout").names,
    )
    late = named(
        builder.build("a", "b", cutoff_of(snapshot, "late"), bout_id="late-bout").values,
        builder.build("a", "b", cutoff_of(snapshot, "late"), bout_id="late-bout").names,
    )
    assert mid["prior_fights_a"] == 1.0
    assert mid["ko_win_rate_missing_a"] == 0.0
    assert mid["rating_missing_a"] == 0.0
    # After NC, the bout is still history but is not a decisive win.
    assert late["ko_win_rate_missing_a"] == 1.0
    assert late["rating_missing_a"] == 1.0
    assert late["prior_fights_a"] == 1.0


def test_proxy_cutoff_sets_feature_flag() -> None:
    snapshot = FeatureSnapshot()
    event = SnapshotEvent("proxy-card", None, dt(2019, 6, 1).date(), series="dwcs", name="proxy")
    snapshot.events.append(event)
    add_bout(snapshot, "main", "proxy-card", "a", "b")
    cutoff = cutoff_for_event(event, allow_proxy=True)
    assert cutoff.cutoff_kind is CutoffKind.PROXY_SCHEDULED_START
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").names,
    )
    assert values["is_proxy_cutoff"] == 1.0


def test_builder_exposes_completeness_flag() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "card", dt(2019, 6, 1))
    add_bout(snapshot, "main", "card", "a", "b")
    row = FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main")
    assert row.quality_flag.value in {"healthy", "partial", "sparse"}
    assert 0.0 <= row.data_completeness <= 1.0
    values = named(row.values, row.names)
    assert values["ko_win_rate_missing_a"] == 1.0
    assert values["ko_win_rate_a"] == 0.5
    assert values["debut_a"] == 1.0
    assert values["scheduled_rounds"] == 3.0
    assert values["scheduled_rounds_missing"] == 0.0


def test_unknown_bout_schedule_is_missing_not_three() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "card", dt(2019, 6, 1))
    add_bout(snapshot, "main", "card", "a", "b", scheduled_rounds=None)
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").names,
    )
    assert values["scheduled_rounds"] == 0.0
    assert values["scheduled_rounds_missing"] == 1.0
    swapped = named(
        FeatureBuilder(snapshot).build("b", "a", cutoff_of(snapshot, "card"), bout_id="main").values,
        FeatureBuilder(snapshot).build("b", "a", cutoff_of(snapshot, "card"), bout_id="main").names,
    )
    assert swapped["scheduled_rounds"] == 0.0
    assert swapped["scheduled_rounds_missing"] == 1.0


def test_history_reversal_uses_training_label() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "mid", dt(2019, 7, 1))
    add_event(snapshot, "late", dt(2019, 9, 1))
    night = dt(2018, 6, 1, 5)
    add_history(
        snapshot,
        fighter_id="a",
        opponent_id="c",
        at=night,
        result="win",
        method="KO/TKO",
        external_bout_id="hist-rev",
        ending_round=1,
        time_str="1:10",
        version_kind="event_night",
        revision=1,
    )
    add_history(
        snapshot,
        fighter_id="a",
        opponent_id="c",
        at=datetime(2019, 8, 15, 12, 0, tzinfo=UTC),
        result="nc",
        method="NC",
        external_bout_id="hist-rev",
        ending_round=1,
        time_str="1:10",
        version_kind="current",
        revision=1,
        event_date=night.date(),
    )
    add_bout(snapshot, "mid-bout", "mid", "a", "b")
    add_bout(snapshot, "late-bout", "late", "a", "b")
    builder = FeatureBuilder(snapshot)
    mid = named(
        builder.build("a", "b", cutoff_of(snapshot, "mid"), bout_id="mid-bout").values,
        builder.build("a", "b", cutoff_of(snapshot, "mid"), bout_id="mid-bout").names,
    )
    late = named(
        builder.build("a", "b", cutoff_of(snapshot, "late"), bout_id="late-bout").values,
        builder.build("a", "b", cutoff_of(snapshot, "late"), bout_id="late-bout").names,
    )
    assert mid["prior_fights_a"] == 1.0
    assert mid["ko_win_rate_missing_a"] == 0.0
    assert mid["rating_missing_a"] == 0.0
    assert late["ko_win_rate_missing_a"] == 1.0
    assert late["rating_missing_a"] == 1.0
    assert late["prior_fights_a"] == 1.0


def test_legacy_matchup_import_still_works() -> None:
    a = RollingProfile("a", 1, 1.0, 0.5, 1.0, 0.2, 0.1)
    b = RollingProfile("b", 1, 0.5, 0.4, 0.5, 0.1, 0.05)
    got = matchup_features(a, b)
    assert got.diff_sig_pm == 0.5
