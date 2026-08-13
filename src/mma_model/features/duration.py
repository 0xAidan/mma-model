"""Actual elapsed fight time and half-round finishing intervals (DWCS-300).

Reuses ingest ``derive_elapsed_seconds`` (fail-closed missing vs invalid) and
maps validated clocks onto modeling bins. Does not change ingest duration
behavior. Round length matches settlement clocks (300 seconds).
"""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.dwcs.duration import DurationResult, DurationStatus, derive_elapsed_seconds
from mma_model.markets.settlement import DEFAULT_ROUND_SECONDS, SUPPORTED_SCHEDULED_ROUNDS

HALF_ROUND_SECONDS: int = DEFAULT_ROUND_SECONDS // 2


@dataclass(frozen=True)
class HalfRoundDuration:
    """Elapsed seconds plus the 0-based finishing half-round interval."""

    status: DurationStatus
    elapsed_seconds: int | None
    interval_index: int | None
    interval_count: int | None
    reason: str | None = None

    @property
    def allows_verified_detail(self) -> bool:
        return self.status is DurationStatus.VALID


def _invalid(
    *,
    reason: str,
    interval_count: int | None = None,
) -> HalfRoundDuration:
    return HalfRoundDuration(
        status=DurationStatus.INVALID,
        elapsed_seconds=None,
        interval_index=None,
        interval_count=interval_count,
        reason=reason,
    )


def _from_ingest(result: DurationResult, *, interval_count: int | None) -> HalfRoundDuration:
    return HalfRoundDuration(
        status=result.status,
        elapsed_seconds=result.elapsed_seconds,
        interval_index=None,
        interval_count=interval_count,
        reason=result.reason,
    )


def half_round_duration(
    *,
    ending_round: int | None,
    time_str: str | None,
    scheduled_rounds: int,
    round_seconds: int = DEFAULT_ROUND_SECONDS,
) -> HalfRoundDuration:
    """Elapsed seconds and finishing half-round index, or missing/invalid.

    Three-round bouts have 6 intervals; five-round bouts have 10. Interval
    length is ``round_seconds / 2`` (150s for 300s rounds). A full-distance
    3x5:00 decision (900s) maps to the last interval (index 5). Round 1 at
    1:10 (70s) → 0; round 1 at 3:00 (180s) → 1.

    Scheduled rounds outside {3, 5} fail closed as invalid for modeling bins.
    Ingest ``derive_elapsed_seconds`` still accepts other positive schedules;
    this wrapper does not change that function.
    """
    if round_seconds <= 0 or round_seconds % 2 != 0:
        return _invalid(reason="round_seconds_must_be_positive_even")

    if scheduled_rounds not in SUPPORTED_SCHEDULED_ROUNDS:
        return _invalid(reason="unsupported_scheduled_rounds")

    interval_count = scheduled_rounds * 2
    ingest = derive_elapsed_seconds(
        ending_round=ending_round,
        time_str=time_str,
        scheduled_rounds=scheduled_rounds,
        round_seconds=round_seconds,
    )
    if ingest.status is not DurationStatus.VALID:
        return _from_ingest(ingest, interval_count=interval_count)

    elapsed = ingest.elapsed_seconds
    if elapsed is None:
        return _invalid(reason="elapsed_missing_after_valid", interval_count=interval_count)

    interval_seconds = round_seconds // 2
    raw_index = elapsed // interval_seconds
    max_index = interval_count - 1
    max_elapsed = scheduled_rounds * round_seconds
    if elapsed == max_elapsed:
        index = max_index
    elif raw_index > max_index:
        return _invalid(reason="interval_index_out_of_range", interval_count=interval_count)
    else:
        index = raw_index

    return HalfRoundDuration(
        status=DurationStatus.VALID,
        elapsed_seconds=elapsed,
        interval_index=index,
        interval_count=interval_count,
        reason=None,
    )
