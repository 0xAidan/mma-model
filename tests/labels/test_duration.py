"""Half-round duration bins built on ingest elapsed-second clocks."""

from __future__ import annotations

from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.features.duration import HALF_ROUND_SECONDS, half_round_duration
from mma_model.markets.settlement import DEFAULT_ROUND_SECONDS


def test_early_finish_round_1_maps_to_interval_0() -> None:
    got = half_round_duration(ending_round=1, time_str="1:10", scheduled_rounds=3)
    assert got.status is DurationStatus.VALID
    assert got.elapsed_seconds == 70
    assert got.interval_index == 0
    assert got.interval_count == 6
    ingest = derive_elapsed_seconds(
        ending_round=1, time_str="1:10", scheduled_rounds=3
    )
    assert ingest.elapsed_seconds == got.elapsed_seconds


def test_round_1_at_3_minutes_maps_to_interval_1() -> None:
    got = half_round_duration(ending_round=1, time_str="3:00", scheduled_rounds=3)
    assert got.status is DurationStatus.VALID
    assert got.elapsed_seconds == 180
    assert got.interval_index == 1


def test_full_decision_three_round_maps_to_last_interval() -> None:
    got = half_round_duration(ending_round=3, time_str="5:00", scheduled_rounds=3)
    assert got.status is DurationStatus.VALID
    assert got.elapsed_seconds == 900
    assert got.interval_index == 5
    assert got.interval_count == 6
    assert HALF_ROUND_SECONDS == DEFAULT_ROUND_SECONDS // 2 == 150


def test_five_round_half_round_indexes() -> None:
    early = half_round_duration(ending_round=1, time_str="1:10", scheduled_rounds=5)
    assert early.interval_index == 0
    assert early.interval_count == 10
    mid = half_round_duration(ending_round=3, time_str="3:00", scheduled_rounds=5)
    assert mid.elapsed_seconds == 780
    assert mid.interval_index == 5
    full = half_round_duration(ending_round=5, time_str="5:00", scheduled_rounds=5)
    assert full.elapsed_seconds == 1500
    assert full.interval_index == 9
    assert full.interval_count == 10


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
