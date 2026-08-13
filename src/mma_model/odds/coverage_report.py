"""Odds coverage/cost reports by card/book/market/time (DWCS-205)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsProviderEventAlias, OddsQuote
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
    actual_cost_known: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_COVERAGE_STATUSES:
            raise ValueError(f"unsupported coverage status: {self.status!r}")
        if self.estimated_cost < 0:
            raise ValueError("estimated_cost must be nonnegative")
        if self.actual_cost is not None and self.actual_cost < 0:
            raise ValueError("actual_cost must be nonnegative")
        if self.actual_cost_known and self.actual_cost is None:
            raise ValueError("actual_cost_known requires actual_cost")
        if not self.actual_cost_known and self.actual_cost is not None:
            raise ValueError("actual_cost without actual_cost_known is invalid")


@dataclass(frozen=True)
class OddsCoverageReport:
    series: str
    as_of: datetime
    cells: tuple[CoverageCell, ...]
    status_counts: Mapping[str, int]
    estimated_cost_total: int
    known_actual_cost_total: int
    unknown_actual_cost_cells: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "as_of": self.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "status_counts": dict(self.status_counts),
            "estimated_cost_total": self.estimated_cost_total,
            "known_actual_cost_total": self.known_actual_cost_total,
            "unknown_actual_cost_cells": self.unknown_actual_cost_cells,
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
    known_actual = sum(
        cell.actual_cost
        for cell in normalized
        if cell.actual_cost_known and cell.actual_cost is not None
    )
    unknown = sum(1 for cell in normalized if not cell.actual_cost_known)
    return OddsCoverageReport(
        series=series,
        as_of=stamp,
        cells=normalized,
        status_counts=dict(counts),
        estimated_cost_total=estimated,
        known_actual_cost_total=known_actual,
        unknown_actual_cost_cells=unknown,
    )


def _active_alias_exists(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> bool:
    stamp = ensure_utc(as_of, field="as_of")
    rows = session.scalars(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.provider == provider,
            OddsProviderEventAlias.external_event_id == external_event_id,
            OddsProviderEventAlias.status == "active",
        )
    ).all()
    for row in rows:
        created = ensure_utc(row.created_at, field="created_at")
        if created > stamp:
            continue
        if row.superseded_at is not None and ensure_utc(
            row.superseded_at, field="superseded_at"
        ) <= stamp:
            continue
        return True
    return False


def cells_from_persisted_quotes(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    as_of: datetime,
    estimated_cost: int = 0,
    actual_cost: int | None = None,
    actual_cost_known: bool = False,
) -> list[CoverageCell]:
    """Build per-book cells from quotes + DWCS-203 alias presence at ``as_of``.

    Scheduling alone never marks value eligibility; ``observed`` means quotes
    exist under an active alias, ``unmatched`` means quotes without alias.
    """
    stamp = ensure_utc(as_of, field="as_of")
    quotes = session.scalars(
        select(OddsQuote).where(
            OddsQuote.provider == provider,
            OddsQuote.region == region,
            OddsQuote.provider_market_key == market,
        )
    ).all()
    if not quotes:
        return [
            CoverageCell(
                card_id=card_id,
                bookmaker_key="*",
                market=market,
                time_label=time_label,
                status="absent",
                estimated_cost=estimated_cost,
                actual_cost=actual_cost if actual_cost_known else None,
                actual_cost_known=actual_cost_known,
                detail="no_quotes",
            )
        ]

    by_book: dict[str, list[OddsQuote]] = {}
    for quote in quotes:
        by_book.setdefault(quote.bookmaker_key, []).append(quote)

    cells: list[CoverageCell] = []
    first = True
    for bookmaker_key in sorted(by_book):
        book_quotes = by_book[bookmaker_key]
        matched = any(
            _active_alias_exists(
                session,
                provider=provider,
                external_event_id=quote.external_event_id,
                as_of=stamp,
            )
            for quote in book_quotes
        )
        status = "observed" if matched else "unmatched"
        cells.append(
            CoverageCell(
                card_id=card_id,
                bookmaker_key=bookmaker_key,
                market=market,
                time_label=time_label,
                status=status,
                estimated_cost=estimated_cost if first else 0,
                actual_cost=(actual_cost if actual_cost_known and first else None),
                actual_cost_known=bool(actual_cost_known and first),
                detail="alias_matched" if matched else "quotes_without_active_alias",
            )
        )
        first = False
    return cells


__all__ = [
    "ALLOWED_COVERAGE_STATUSES",
    "CoverageCell",
    "OddsCoverageReport",
    "build_odds_coverage_report",
    "cells_from_persisted_quotes",
]
