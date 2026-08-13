"""Odds coverage/cost reports by card/book/market/time (DWCS-205).

Collection coverage statuses are distinct from value readiness. ``observed``
means quotes were collected under a PIT-effective alias to the card; it is not
value-eligible by itself. Match/eligibility metadata is emitted separately.
Batch provider costs are recorded once at batch level; card cells stay
cost-neutral.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import CanonicalBout
from mma_model.db.tables.odds import OddsQuote
from mma_model.odds.lifecycle import (
    alias_effective_at,
    resolve_quote_value_eligibility,
)
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
    matched: bool = False
    quote_count: int = 0
    quote_eligible_count: int = 0
    match_reason: str = ""
    quote_ids: tuple[int, ...] = field(default_factory=tuple)

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
        # Card cells are cost-neutral; provider request costs live in BatchCostRecord.
        if self.estimated_cost != 0 or self.actual_cost is not None or self.actual_cost_known:
            raise ValueError(
                "card coverage cells must be cost-neutral; use BatchCostRecord"
            )


@dataclass(frozen=True)
class BatchCostRecord:
    batch_key: str
    estimated_cost: int
    actual_cost: int | None
    actual_cost_known: bool
    actual_cost_source: str | None
    remaining_source: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.estimated_cost < 0:
            raise ValueError("estimated_cost must be nonnegative")
        if self.actual_cost is not None and self.actual_cost < 0:
            raise ValueError("actual_cost must be nonnegative")
        if self.actual_cost_known and self.actual_cost is None:
            raise ValueError("actual_cost_known requires actual_cost")
        if not self.actual_cost_known and self.actual_cost is not None:
            raise ValueError("actual_cost without actual_cost_known is invalid")


@dataclass(frozen=True)
class PlannedWorkItem:
    """Dry-run / not-yet-requested work — never conflated with provider absence."""

    card_id: str
    time_label: str
    market: str
    estimated_cost: int
    batch_key: str
    detail: str = "dry_run_not_requested"


@dataclass(frozen=True)
class OddsCoverageReport:
    series: str
    as_of: datetime
    cells: tuple[CoverageCell, ...]
    status_counts: Mapping[str, int]
    estimated_cost_total: int
    known_actual_cost_total: int
    unknown_actual_cost_batches: int
    batch_costs: tuple[BatchCostRecord, ...]
    planned: tuple[PlannedWorkItem, ...]
    collection_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "as_of": self.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "collection_only": self.collection_only,
            "value_ready": False,
            "status_counts": dict(self.status_counts),
            "estimated_cost_total": self.estimated_cost_total,
            "known_actual_cost_total": self.known_actual_cost_total,
            "unknown_actual_cost_batches": self.unknown_actual_cost_batches,
            "batch_costs": [asdict(item) for item in self.batch_costs],
            "planned": [asdict(item) for item in self.planned],
            "cells": [asdict(cell) for cell in self.cells],
        }


def build_odds_coverage_report(
    *,
    series: str,
    as_of: datetime,
    cells: Sequence[CoverageCell],
    batch_costs: Sequence[BatchCostRecord] = (),
    planned: Sequence[PlannedWorkItem] = (),
    contract: OddsScheduleContract | None = None,
) -> OddsCoverageReport:
    """Aggregate coverage cells; absent/failed/deferred stay distinct from planned."""
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
    batches = tuple(batch_costs)
    estimated = sum(item.estimated_cost for item in batches)
    known_actual = sum(
        item.actual_cost
        for item in batches
        if item.actual_cost_known and item.actual_cost is not None
    )
    unknown_batches = sum(1 for item in batches if not item.actual_cost_known)
    return OddsCoverageReport(
        series=series,
        as_of=stamp,
        cells=normalized,
        status_counts=dict(counts),
        estimated_cost_total=estimated,
        known_actual_cost_total=known_actual,
        unknown_actual_cost_batches=unknown_batches,
        batch_costs=batches,
        planned=tuple(planned),
        collection_only=True,
    )


def _card_bout_ids(session: Session, *, card_id: str) -> set[str]:
    return set(
        session.scalars(
            select(CanonicalBout.id).where(CanonicalBout.event_id == card_id)
        ).all()
    )


def _quote_ids_from_ledger_detail(detail: str | None) -> tuple[int, ...]:
    if not detail:
        return ()
    marker = "quote_ids="
    if marker not in detail:
        return ()
    raw = detail.split(marker, 1)[1].split(";", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(int(x) for x in parsed)


def encode_quote_ids_for_ledger(quote_ids: Sequence[int]) -> str:
    return "quote_ids=" + json.dumps([int(x) for x in quote_ids])


def cells_from_snapshot_quotes(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    as_of: datetime,
    quote_ids: Sequence[int],
    snapshot_at: datetime | None = None,
    stale_after_minutes: int = 60,
) -> list[CoverageCell]:
    """Build per-book cells from exact snapshot quote IDs scoped to the card.

    Uses DWCS-203 ``alias_effective_at(as_of)`` and requires the alias bout to
    belong to ``card_id``. Later global quotes cannot leak into earlier cells.
    """
    stamp = ensure_utc(as_of, field="as_of")
    ids = tuple(int(x) for x in quote_ids)
    if not ids:
        return [
            CoverageCell(
                card_id=card_id,
                bookmaker_key="*",
                market=market,
                time_label=time_label,
                status="absent",
                detail="no_snapshot_quotes",
                match_reason="no_quotes_in_snapshot",
            )
        ]

    quotes = list(
        session.scalars(select(OddsQuote).where(OddsQuote.id.in_(ids))).all()
    )
    bout_ids = _card_bout_ids(session, card_id=card_id)
    by_book: dict[str, list[OddsQuote]] = {}
    for quote in quotes:
        if quote.provider != provider or quote.region != region:
            continue
        if quote.provider_market_key != market:
            continue
        if snapshot_at is not None and quote.snapshot_at is not None:
            q_snap = quote.snapshot_at
            if q_snap.tzinfo is None:
                q_snap = q_snap.replace(tzinfo=UTC)
            if ensure_utc(q_snap, field="snapshot_at") > ensure_utc(
                snapshot_at, field="snapshot_at"
            ):
                continue
        alias = alias_effective_at(
            session,
            provider=provider,
            external_event_id=quote.external_event_id,
            as_of=stamp,
        )
        if alias is None or alias.bout_id not in bout_ids:
            # Quote not mapped to this card at as_of — ignore for this card.
            continue
        by_book.setdefault(quote.bookmaker_key, []).append(quote)

    if not by_book:
        return [
            CoverageCell(
                card_id=card_id,
                bookmaker_key="*",
                market=market,
                time_label=time_label,
                status="absent",
                detail="no_card_scoped_quotes",
                match_reason="no_alias_to_card_bouts",
                quote_ids=ids,
            )
        ]

    cells: list[CoverageCell] = []
    for bookmaker_key in sorted(by_book):
        book_quotes = by_book[bookmaker_key]
        matched = True
        eligible = 0
        for quote in book_quotes:
            alias = alias_effective_at(
                session,
                provider=provider,
                external_event_id=quote.external_event_id,
                as_of=stamp,
            )
            if alias is None or alias.bout_id not in bout_ids:
                matched = False
                continue
            decision = resolve_quote_value_eligibility(
                session,
                quote=quote,
                bout_id=alias.bout_id,
                match_status="matched",
                as_of=stamp,
                stale_after_minutes=stale_after_minutes,
            )
            if decision.eligible:
                eligible += 1
        status = "observed" if matched else "unmatched"
        cells.append(
            CoverageCell(
                card_id=card_id,
                bookmaker_key=bookmaker_key,
                market=market,
                time_label=time_label,
                status=status,
                detail=(
                    "collection_observed_not_value_ready"
                    if status == "observed"
                    else "quotes_without_card_alias"
                ),
                matched=matched,
                quote_count=len(book_quotes),
                quote_eligible_count=eligible,
                match_reason=(
                    "alias_effective_maps_to_card_bout"
                    if matched
                    else "alias_missing_or_wrong_card"
                ),
                quote_ids=tuple(int(q.id) for q in book_quotes),
            )
        )
    return cells


def cells_from_ledger_snapshot(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    as_of: datetime,
    detail: str | None,
    snapshot_at: datetime | None = None,
) -> list[CoverageCell]:
    """Idempotent replay: reconstruct cells from ledger-linked quote IDs."""
    return cells_from_snapshot_quotes(
        session,
        card_id=card_id,
        time_label=time_label,
        market=market,
        provider=provider,
        region=region,
        as_of=as_of,
        quote_ids=_quote_ids_from_ledger_detail(detail),
        snapshot_at=snapshot_at,
    )


# Back-compat name used by older call sites; now requires quote_ids.
def cells_from_persisted_quotes(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    as_of: datetime,
    quote_ids: Sequence[int] = (),
    snapshot_at: datetime | None = None,
    estimated_cost: int = 0,
    actual_cost: int | None = None,
    actual_cost_known: bool = False,
) -> list[CoverageCell]:
    """Deprecated wrapper: ignores cost args (batch-level only) and scopes by IDs."""
    _ = (estimated_cost, actual_cost, actual_cost_known, region)
    return cells_from_snapshot_quotes(
        session,
        card_id=card_id,
        time_label=time_label,
        market=market,
        provider=provider,
        region=region,
        as_of=as_of,
        quote_ids=quote_ids,
        snapshot_at=snapshot_at,
    )


__all__ = [
    "ALLOWED_COVERAGE_STATUSES",
    "BatchCostRecord",
    "CoverageCell",
    "OddsCoverageReport",
    "PlannedWorkItem",
    "build_odds_coverage_report",
    "cells_from_ledger_snapshot",
    "cells_from_persisted_quotes",
    "cells_from_snapshot_quotes",
    "encode_quote_ids_for_ledger",
]
