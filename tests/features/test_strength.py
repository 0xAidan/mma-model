"""Sequential strength: same-card freeze and future-event invariance."""

from __future__ import annotations

from mma_model.features.snapshot import FeatureSnapshot
from mma_model.features.strength import INITIAL_RATING, strengths_before_event
from tests.features.helpers import add_bout, add_event, add_result, cutoff_of, dt


def test_same_card_ratings_are_pre_card_and_do_not_chain() -> None:
    snapshot = FeatureSnapshot()
    prior = dt(2018, 1, 1)
    card = dt(2019, 6, 1)
    add_event(snapshot, "prior", prior)
    add_event(snapshot, "card", card)
    prior_bout = add_bout(snapshot, "p1", "prior", "a", "c")
    add_result(
        snapshot,
        prior_bout,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="1:10",
        effective_at=prior,
    )
    b1 = add_bout(snapshot, "t1", "card", "a", "x")
    b2 = add_bout(snapshot, "t2", "card", "a", "b")
    # Clocks before the 60m cutoff so a clock-only gate would leak.
    add_result(
        snapshot,
        b1,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="0:30",
        effective_at=dt(2019, 6, 1, 0),
    )
    add_result(
        snapshot,
        b2,
        winner_id="b",
        method="U-DEC",
        ending_round=3,
        time_str="5:00",
        effective_at=dt(2019, 6, 1, 0),
    )
    cutoff = cutoff_of(snapshot, "card")
    states_one = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "b", "x"))
    states_two = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "b", "x"))
    assert states_one["a"].rating == states_two["a"].rating
    assert states_one["a"].prior_decisive_bouts == 1
    assert states_one["b"].missing is True
    assert states_one["a"].rating != INITIAL_RATING


def test_appending_later_event_does_not_change_earlier_ratings() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "e1", dt(2018, 1, 1))
    add_event(snapshot, "e2", dt(2019, 6, 1))
    b1 = add_bout(snapshot, "b1", "e1", "a", "c")
    add_result(
        snapshot,
        b1,
        winner_id="a",
        method="KO/TKO",
        ending_round=1,
        time_str="1:10",
        effective_at=dt(2018, 1, 1),
    )
    cutoff = cutoff_of(snapshot, "e2")
    before = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "b"))
    add_event(snapshot, "e3", dt(2021, 1, 1))
    b3 = add_bout(snapshot, "b3", "e3", "a", "b")
    add_result(
        snapshot,
        b3,
        winner_id="b",
        method="SUB",
        ending_round=1,
        time_str="0:20",
        effective_at=dt(2021, 1, 1),
    )
    after = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "b"))
    assert before["a"].rating == after["a"].rating
    assert before["a"].rating_sd == after["a"].rating_sd
    assert before["a"].prior_decisive_bouts == after["a"].prior_decisive_bouts


def test_nc_and_pending_do_not_update_ratings() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "e1", dt(2018, 1, 1))
    add_event(snapshot, "e2", dt(2019, 6, 1))
    bout = add_bout(snapshot, "b1", "e1", "a", "c")
    add_result(
        snapshot,
        bout,
        winner_id=None,
        method="NC",
        result_type="no_contest",
        ending_round=1,
        time_str="1:00",
        effective_at=dt(2018, 1, 1),
    )
    cutoff = cutoff_of(snapshot, "e2")
    states = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "c"))
    assert states["a"].missing is True
    assert states["a"].rating == INITIAL_RATING
    assert states["a"].prior_decisive_bouts == 0


def test_decisive_without_winner_does_not_update_ratings() -> None:
    snapshot = FeatureSnapshot()
    add_event(snapshot, "e1", dt(2018, 1, 1))
    add_event(snapshot, "e2", dt(2019, 6, 1))
    bout = add_bout(snapshot, "b1", "e1", "a", "c")
    add_result(
        snapshot,
        bout,
        winner_id=None,
        method="KO/TKO",
        result_type="decisive",
        ending_round=1,
        time_str="1:00",
        effective_at=dt(2018, 1, 1),
    )
    cutoff = cutoff_of(snapshot, "e2")
    states = strengths_before_event(snapshot, cutoff, fighter_ids=("a", "c"))
    assert states["a"].missing is True
    assert states["a"].rating == INITIAL_RATING
    assert states["a"].prior_decisive_bouts == 0
