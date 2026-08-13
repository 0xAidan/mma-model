"""Half-round duration bins built on ingest elapsed-second clocks."""

from __future__ import annotations

import pytest

from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.features.duration import HALF_ROUND_SECONDS, HalfRoundDuration, half_round_duration
from mma_model.markets.settlement import DEFAULT_ROUND_SECONDS, clock_pairs_for_total


def _interval(
    ending_round: int,
    time_str: str,
    scheduled_rounds: int = 3,
) -> HalfRoundDuration:
    return half_round_duration(
        ending_round=ending_round,
        time_str=time_str,
        scheduled_rounds=scheduled_rounds,
    )


@pytest.mark.parametrize(
    ("ending_round", "time_str", "scheduled_rounds", "elapsed", "index"),
    [
        (1, "1:10", 3, 70, 0),
        (1, "2:30", 3, 150, 0),
        (1, "2:31", 3, 151, 1),
        (1, "3:00", 3, 180, 1),
        (1, "5:00", 3, 300, 1),
        (2, "0:00", 3, 300, 1),
        (2, "5:00", 3, 600, 3),
        (3, "5:00", 3, 900, 5),
        (5, "5:00", 5, 1500, 9),
        (3, "5:00", 5, 900, 5),
    ],
)
def test_right_closed_half_round_fenceposts(
    ending_round: int,
    time_str: str,
    scheduled_rounds: int,
    elapsed: int,
    index: int,
) -> None:
    got = _interval(ending_round, time_str, scheduled_rounds)
    assert got.status is DurationStatus.VALID
    assert got.elapsed_seconds == elapsed
    assert got.interval_index == index
    assert got.interval_count == scheduled_rounds * 2
    ingest = derive_elapsed_seconds(
        ending_round=ending_round,
        time_str=time_str,
        scheduled_rounds=scheduled_rounds,
    )
    assert ingest.elapsed_seconds == elapsed


def test_early_finish_round_1_maps_to_interval_0() -> None:
    got = _interval(1, "1:10")
    assert got.status is DurationStatus.VALID
    assert got.elapsed_seconds == 70
    assert got.interval_index == 0
    assert got.interval_count == 6


def test_round_1_at_3_minutes_maps_to_interval_1() -> None:
    got = _interval(1, "3:00")
    assert got.elapsed_seconds == 180
    assert got.interval_index == 1


def test_full_decision_three_round_maps_to_last_interval() -> None:
    got = _interval(3, "5:00")
    assert got.elapsed_seconds == 900
    assert got.interval_index == 5
    assert got.interval_count == 6
    assert HALF_ROUND_SECONDS == DEFAULT_ROUND_SECONDS // 2 == 150


def test_round_boundary_matches_settlement_clock_pairs() -> None:
    pairs = clock_pairs_for_total(
        300, scheduled_rounds=3, round_seconds=DEFAULT_ROUND_SECONDS
    )
    assert (1, 300) in pairs
    assert (2, 0) in pairs
    end_r1 = _interval(1, "5:00")
    start_r2 = _interval(2, "0:00")
    assert end_r1.elapsed_seconds == start_r2.elapsed_seconds == 300
    assert end_r1.interval_index == start_r2.interval_index == 1


def test_five_round_half_round_indexes() -> None:
    early = _interval(1, "1:10", 5)
    assert early.interval_index == 0
    assert early.interval_count == 10
    mid = _interval(3, "3:00", 5)
    assert mid.elapsed_seconds == 780
    assert mid.interval_index == 5
    full = _interval(5, "5:00", 5)
    assert full.elapsed_seconds == 1500
    assert full.interval_index == 9
    r3_end = _interval(3, "5:00", 5)
    assert r3_end.elapsed_seconds == 900
    assert r3_end.interval_index == 5


def test_missing_vs_invalid_clocks() -> None:
    missing = half_round_duration(
        ending_round=None, time_str=None, scheduled_rounds=3
    )
    assert missing.status is DurationStatus.MISSING
    assert missing.interval_index is None
    assert missing.elapsed_seconds is None

    invalid = half_round_duration(
        ending_round=1, time_str="5:01", scheduled_rounds=3
    )
    assert invalid.status is DurationStatus.INVALID
    assert invalid.interval_index is None

    malformed = half_round_duration(
        ending_round=1, time_str="1:60", scheduled_rounds=3
    )
    assert malformed.status is DurationStatus.INVALID


def test_unsupported_scheduled_rounds_fail_closed() -> None:
    four = half_round_duration(ending_round=1, time_str="1:10", scheduled_rounds=4)
    assert four.status is DurationStatus.INVALID
    assert four.reason == "unsupported_scheduled_rounds"
    ingest = derive_elapsed_seconds(
        ending_round=1, time_str="1:10", scheduled_rounds=4
    )
    assert ingest.status is DurationStatus.VALID
    assert ingest.elapsed_seconds == 70
