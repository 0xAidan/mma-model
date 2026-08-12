"""Derive elapsed fight seconds from ending round + in-round clock."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

_ROUND_SECONDS = 300  # standard MMA round length
_TIME_RE = re.compile(r"^(\d+):([0-5]\d)$")


class DurationStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True)
class DurationResult:
    status: DurationStatus
    elapsed_seconds: int | None
    reason: str | None = None
    detail_level_ceiling: Literal["summary", "partial", "verified"] = "summary"

    @property
    def allows_verified_detail(self) -> bool:
        return self.status == DurationStatus.VALID


def derive_elapsed_seconds(
    *,
    ending_round: int | None,
    time_str: str | None,
    scheduled_rounds: int,
    round_seconds: int = _ROUND_SECONDS,
) -> DurationResult:
    """Convert ending round + MM:SS into elapsed seconds with fail-closed validation.

    Invalid or missing duration is explicit and must never become verified detail.
    """
    if scheduled_rounds < 1:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="scheduled_rounds_must_be_positive",
            detail_level_ceiling="summary",
        )

    if ending_round is None and (time_str is None or not str(time_str).strip()):
        return DurationResult(
            status=DurationStatus.MISSING,
            elapsed_seconds=None,
            reason="ending_round_and_time_missing",
            detail_level_ceiling="summary",
        )

    if ending_round is None or time_str is None or not str(time_str).strip():
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="ending_round_or_time_incomplete",
            detail_level_ceiling="summary",
        )

    if not isinstance(ending_round, int) or ending_round < 1:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="ending_round_out_of_range",
            detail_level_ceiling="summary",
        )

    if ending_round > scheduled_rounds:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="ending_round_exceeds_scheduled_rounds",
            detail_level_ceiling="summary",
        )

    match = _TIME_RE.match(str(time_str).strip())
    if match is None:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="malformed_time_str",
            detail_level_ceiling="summary",
        )

    minutes = int(match.group(1))
    seconds = int(match.group(2))
    in_round = minutes * 60 + seconds
    if in_round > round_seconds:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="time_exceeds_round_length",
            detail_level_ceiling="summary",
        )

    elapsed = (ending_round - 1) * round_seconds + in_round
    max_elapsed = scheduled_rounds * round_seconds
    if elapsed > max_elapsed:
        return DurationResult(
            status=DurationStatus.INVALID,
            elapsed_seconds=None,
            reason="elapsed_exceeds_scheduled_duration",
            detail_level_ceiling="summary",
        )

    return DurationResult(
        status=DurationStatus.VALID,
        elapsed_seconds=elapsed,
        reason=None,
        detail_level_ceiling="verified",
    )
