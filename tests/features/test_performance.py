"""Zero vs missing and actual elapsed-time rate tests."""

from __future__ import annotations

from mma_model.features.builder import FeatureBuilder
from mma_model.features.duration import elapsed_seconds_for_rates
from mma_model.features.snapshot import FeatureSnapshot
from tests.features.helpers import (
    add_bout,
    add_event,
    add_result,
    add_strike_stats,
    cutoff_of,
    dt,
    named,
)


def test_zero_landed_with_attempts_is_not_missing() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "c")
    add_result(
        snapshot,
        prior,
        winner_id="c",
        method="U-DEC",
        ending_round=1,
        time_str="1:10",
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="p1",
        landed=0,
        attempted=10,
        at=dt(2018, 1, 1),
        opp_id="c",
        opp_landed=5,
    )
    add_bout(snapshot, "main", "card", "a", "b")
    cutoff = cutoff_of(snapshot, "card")
    row = FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main")
    values = named(row.values, row.names)
    assert values["perf_missing_a"] == 0.0
    assert values["sig_str_acc_missing_a"] == 0.0
    assert values["sig_str_acc_a"] == 0.0
    assert values["sig_str_landed_pm_a"] == 0.0
    assert values["perf_missing_b"] == 1.0
    assert values["sig_str_acc_missing_b"] == 1.0
    assert row.values != FeatureBuilder(snapshot).build("b", "a", cutoff, bout_id="main").values


def test_seventy_second_finish_is_not_a_fifteen_minute_fight() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "fast", dt(2018, 1, 1))
    add_event(snapshot, "slow", dt(2018, 6, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    fast = add_bout(snapshot, "fast-bout", "fast", "a", "c")
    slow = add_bout(snapshot, "slow-bout", "slow", "b", "d")
    add_result(
        snapshot,
        fast,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="1:10",
        effective_at=dt(2018, 1, 1),
    )
    add_result(
        snapshot,
        slow,
        winner_id="b",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 6, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="fast-bout",
        landed=10,
        attempted=12,
        at=dt(2018, 1, 1),
        opp_id="c",
        opp_landed=1,
    )
    add_strike_stats(
        snapshot,
        fighter_id="b",
        bout_id="slow-bout",
        landed=10,
        attempted=12,
        at=dt(2018, 6, 1),
        opp_id="d",
        opp_landed=1,
    )
    add_bout(snapshot, "main", "card", "a", "b")
    cutoff = cutoff_of(snapshot, "card")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").names,
    )
    # 10 strikes in 70s vs 10 in 900s.
    assert values["sig_str_landed_pm_a"] > 8.0
    assert values["sig_str_landed_pm_b"] < 1.0
    assert abs(values["sig_str_landed_pm_a"] - (10.0 / (70.0 / 60.0))) < 1e-9
    assert abs(values["sig_str_landed_pm_b"] - (10.0 / (900.0 / 60.0))) < 1e-9
    assert values["td_landed_per_15_a"] == 0.0
    assert values["td_landed_per_15_missing_a"] == 1.0
    assert values["ctrl_per_min_missing_a"] == 1.0
    assert values["perf_missing_a"] == 0.0
    assert values["perf_missing_b"] == 0.0


def test_missing_elapsed_is_excluded_from_rate_denominator() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "c")
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="KO/TKO",
        ending_round=None,
        time_str=None,
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="p1",
        landed=50,
        attempted=50,
        at=dt(2018, 1, 1),
        opp_id="c",
        opp_landed=0,
    )
    add_bout(snapshot, "main", "card", "a", "b")
    cutoff = cutoff_of(snapshot, "card")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").names,
    )
    assert values["perf_missing_a"] == 1.0
    assert values["sig_str_landed_pm_a"] == 0.0


def test_missing_schedule_does_not_invent_fifteen_minute_rate() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "c", scheduled_rounds=None)
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="p1",
        landed=10,
        attempted=10,
        at=dt(2018, 1, 1),
    )
    add_bout(snapshot, "main", "card", "a", "b")
    cutoff = cutoff_of(snapshot, "card")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff, bout_id="main").names,
    )
    assert values["perf_missing_a"] == 1.0
    assert values["sig_str_landed_pm_missing_a"] == 1.0
    assert values["sig_str_landed_pm_a"] == 0.0
    assert values["prior_minutes_a"] == 0.0


def test_unknown_schedule_does_not_map_round_four_to_five_rounder() -> None:
    assert (
        elapsed_seconds_for_rates(ending_round=4, time_str="1:00", scheduled_rounds=None)
        is None
    )
    assert (
        elapsed_seconds_for_rates(ending_round=4, time_str="1:00", scheduled_rounds=4)
        is None
    )
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    prior = add_bout(snapshot, "p1", "prior", "a", "c", scheduled_rounds=4)
    add_result(
        snapshot,
        prior,
        winner_id="a",
        method="KO/TKO",
        ending_round=4,
        time_str="1:00",
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="p1",
        landed=10,
        attempted=10,
        at=dt(2018, 1, 1),
    )
    add_bout(snapshot, "main", "card", "a", "b")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").names,
    )
    assert values["perf_missing_a"] == 1.0
    assert values["prior_minutes_a"] == 0.0


def test_missing_td_is_not_observed_zero() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "prior", dt(2018, 1, 1))
    add_event(snapshot, "card", dt(2019, 6, 1))
    zero_bout = add_bout(snapshot, "zero-td", "prior", "a", "c")
    missing_bout = add_bout(snapshot, "no-td", "prior", "b", "d")
    add_result(
        snapshot,
        zero_bout,
        winner_id="a",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 1, 1),
    )
    add_result(
        snapshot,
        missing_bout,
        winner_id="b",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2018, 1, 1),
    )
    add_strike_stats(
        snapshot,
        fighter_id="a",
        bout_id="zero-td",
        landed=5,
        attempted=10,
        at=dt(2018, 1, 1),
        td_landed=0,
        td_attempted=4,
        opp_id="c",
        opp_landed=1,
        opp_td_landed=0,
        opp_td_attempted=2,
    )
    add_strike_stats(
        snapshot,
        fighter_id="b",
        bout_id="no-td",
        landed=5,
        attempted=10,
        at=dt(2018, 1, 1),
    )
    add_bout(snapshot, "main", "card", "a", "b")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").names,
    )
    assert values["td_landed_per_15_missing_a"] == 0.0
    assert values["td_landed_per_15_a"] == 0.0
    assert values["td_acc_missing_a"] == 0.0
    assert values["td_acc_a"] == 0.0
    assert values["td_att_per_15_missing_a"] == 0.0
    assert values["td_landed_per_15_missing_b"] == 1.0
    assert values["td_acc_missing_b"] == 1.0
    assert values["ctrl_per_min_missing_a"] == 1.0
    assert values["td_landed_per_15_diff"] == 0.0
    row_ab = FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main")
    row_ba = FeatureBuilder(snapshot).build("b", "a", cutoff_of(snapshot, "card"), bout_id="main")
    assert named(row_ab.values, row_ab.names)["td_landed_per_15_diff"] == 0.0
    assert named(row_ba.values, row_ba.names)["td_landed_per_15_diff"] == 0.0


def test_performance_diff_does_not_treat_missing_as_zero() -> None:
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
        attempted=10,
        at=dt(2018, 1, 1),
        td_landed=3,
        td_attempted=4,
    )
    add_bout(snapshot, "main", "card", "a", "b")
    values = named(
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").values,
        FeatureBuilder(snapshot).build("a", "b", cutoff_of(snapshot, "card"), bout_id="main").names,
    )
    assert values["sig_str_landed_pm_missing_a"] == 0.0
    assert values["sig_str_landed_pm_missing_b"] == 1.0
    assert values["sig_str_landed_pm_a"] > 0.0
    assert values["sig_str_landed_pm_diff"] == 0.0
    assert values["td_landed_per_15_a"] > 0.0
    assert values["td_landed_per_15_missing_b"] == 1.0
    assert values["td_landed_per_15_diff"] == 0.0
