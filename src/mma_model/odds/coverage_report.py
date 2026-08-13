"""Odds coverage/cost reports by card/book/market/time (DWCS-205)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from mma_model.odds.normalize import ensure_utc
from mma_model.odds.schedule import OddsScheduleContract, load_default_schedule_contract

ALLOWED_COVERAGE_STATUSES = frozenset(
    {"absent", "failed", "deferred_quota", "unmatched", "observed"}
)


@dataclass(frozen=True)
class CoverageCell:
    card_id: str
    bookmaker_key: str
    market: str
    time_label: str
    status: str
    estimated_cost: int = 0
    actual_cost: int | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_COVERAGE_STATUSES:
            raise ValueError(f"unsupported coverage status: {self.status!r}")
        if self.estimated_cost < 0:
            raise ValueError("estimated_cost must be nonnegative")
        if self.actual_cost is not None and self.actual_cost < 0:
            raise ValueError("actual_cost must be nonnegative")


@dataclass(frozen=True)
class OddsCoverageReport:
    series: str
    as_of: datetime
    cells: tuple[CoverageCell, ...]
    status_counts: Mapping[str, int]
    estimated_cost_total: int
    actual_cost_total: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "as_of": self.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "status_counts": dict(self.status_counts),
            "estimated_cost_total": self.estimated_cost_total,
            "actual_cost_total": self.actual_cost_total,
            "cells": [asdict(cell) for cell in self.cells],
        }


def build_odds_coverage_report(
    *,
    series: str,
    as_of: datetime,
    cells: Sequence[CoverageCell],
    contract: OddsScheduleContract | None = None,
) -> OddsCoverageReport:
    """Aggregate coverage cells; absent and failed stay distinct."""
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    normalized = tuple(cells)
    for cell in normalized:
        if cell.status not in sched.coverage_statuses:
            raise ValueError(
                f"status {cell.status!r} not in schedule coverage_statuses"
            )
    counts: Counter[str] = Counter(cell.status for cell in normalized)
    for status in sched.coverage_statuses:
        counts.setdefault(status, 0)
    estimated = sum(cell.estimated_cost for cell in normalized)
    actual = sum(cell.actual_cost or 0 for cell in normalized)
    return OddsCoverageReport(
        series=series,
        as_of=stamp,
        cells=normalized,
        status_counts=dict(counts),
        estimated_cost_total=estimated,
        actual_cost_total=actual,
    )


__all__ = [
    "ALLOWED_COVERAGE_STATUSES",
    "CoverageCell",
    "OddsCoverageReport",
    "build_odds_coverage_report",
]
