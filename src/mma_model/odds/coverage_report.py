"""Odds coverage/cost reports by card/book/market/time (DWCS-205).

Collection coverage statuses are distinct from value readiness. ``observed``
means quotes were collected under a card-linked alias at the declared match
clock; it is not value-eligible by itself. Match/eligibility metadata is
emitted separately. Batch provider costs are recorded once at batch level;
card cells stay cost-neutral.

Clocks are explicit and separate:
- ``quote_snapshot_at`` / ``requested_cutoff`` — historical collection timing
- ``match_reconciliation_as_of`` — current retrospective alias mapping
- ``pit_match_as_of`` — only when claiming point-in-time match at checkpoint

Retrospective reconciliation must never be labeled PIT-at-checkpoint.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import CanonicalBout
from mma_model.db.tables.odds import OddsAvailabilityObservation, OddsQuote
from mma_model.odds.lifecycle import (
    alias_effective_at,
    resolve_quote_value_eligibility,
)
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.schedule import OddsScheduleContract, load_default_schedule_contract

ALLOWED_COVERAGE_STATUSES = frozenset(
    {"absent", "failed", "deferred_quota", "unmatched", "observed"}
)

MatchClockKind = Literal["retrospective_reconciliation", "pit_at_checkpoint"]
CardCoverageState = Literal[
    "attributed",
    "incomplete_attribution",
    "absent_proven",
    "no_snapshot_quotes",
]
UNASSIGNED_CARD_ID = "__unassigned__"


def _response_evidence_key(
    *,
    external_event_id: str,
    bookmaker_key: str | None,
    region: str,
    market: str,
) -> tuple[str, str, str, str]:
    """Identity for quote/availability evidence within one provider response."""
    return (
        str(external_event_id),
        str(bookmaker_key or ""),
        str(region),
        str(market),
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value, field="timestamp").astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
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
    availability_observation_ids: tuple[int, ...] = field(default_factory=tuple)
    quote_snapshot_at: str | None = None
    requested_cutoff: str | None = None
    match_reconciliation_as_of: str | None = None
    pit_match_as_of: str | None = None
    match_clock_kind: MatchClockKind | None = None
    card_coverage_state: CardCoverageState | None = None

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
        if self.match_clock_kind == "pit_at_checkpoint" and not self.pit_match_as_of:
            raise ValueError("pit_at_checkpoint requires pit_match_as_of")
        if (
            self.match_clock_kind == "retrospective_reconciliation"
            and self.pit_match_as_of
        ):
            raise ValueError(
                "retrospective_reconciliation must not set pit_match_as_of"
            )
        if (
            self.card_coverage_state == "incomplete_attribution"
            and self.status in {"observed", "absent"}
        ):
            raise ValueError(
                "incomplete_attribution must not claim observed/absent completeness"
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
    match_reconciliation_as_of: str | None = None
    match_clock_kind: MatchClockKind | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "as_of": self.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "match_reconciliation_as_of": self.match_reconciliation_as_of,
            "match_clock_kind": self.match_clock_kind,
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
    match_reconciliation_as_of: datetime | None = None,
    match_clock_kind: MatchClockKind | None = None,
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
    recon = (
        _iso(match_reconciliation_as_of)
        if match_reconciliation_as_of is not None
        else None
    )
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
        match_reconciliation_as_of=recon,
        match_clock_kind=match_clock_kind,
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


def encode_availability_ids_for_ledger(availability_ids: Sequence[int]) -> str:
    return "availability_ids=" + json.dumps([int(x) for x in availability_ids])


def _resolve_match_clocks(
    *,
    match_reconciliation_as_of: datetime,
    pit_match_as_of: datetime | None,
) -> tuple[datetime, MatchClockKind, datetime | None]:
    recon = ensure_utc(match_reconciliation_as_of, field="match_reconciliation_as_of")
    if pit_match_as_of is not None:
        pit = ensure_utc(pit_match_as_of, field="pit_match_as_of")
        return pit, "pit_at_checkpoint", pit
    return recon, "retrospective_reconciliation", None


def _clock_fields(
    *,
    quote_snapshot_at: datetime | None,
    requested_cutoff: datetime | None,
    match_reconciliation_as_of: datetime,
    pit_match_as_of: datetime | None,
    match_clock_kind: MatchClockKind,
) -> dict[str, Any]:
    return {
        "quote_snapshot_at": _iso(quote_snapshot_at),
        "requested_cutoff": _iso(requested_cutoff),
        "match_reconciliation_as_of": _iso(match_reconciliation_as_of),
        "pit_match_as_of": _iso(pit_match_as_of),
        "match_clock_kind": match_clock_kind,
    }


def cells_from_snapshot_quotes(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    quote_ids: Sequence[int],
    match_reconciliation_as_of: datetime | None = None,
    quote_snapshot_at: datetime | None = None,
    requested_cutoff: datetime | None = None,
    pit_match_as_of: datetime | None = None,
    availability_observation_ids: Sequence[int] = (),
    include_unassigned: bool = False,
    stale_after_minutes: int = 60,
    # Back-compat aliases — never treat job as_of alone as PIT-at-checkpoint.
    as_of: datetime | None = None,
    snapshot_at: datetime | None = None,
) -> list[CoverageCell]:
    """Build cells from exact snapshot quote/availability IDs.

    Quotes are attributed to ``card_id`` only when alias evidence links them.
    Unlinked quotes become ``__unassigned__`` unmatched cells (when
    ``include_unassigned``) instead of falsely marking the card absent.
    """
    recon = match_reconciliation_as_of if match_reconciliation_as_of is not None else as_of
    if recon is None:
        raise ValueError("match_reconciliation_as_of is required")
    snap = quote_snapshot_at if quote_snapshot_at is not None else snapshot_at
    match_as_of, clock_kind, pit = _resolve_match_clocks(
        match_reconciliation_as_of=recon,
        pit_match_as_of=pit_match_as_of,
    )
    clocks = _clock_fields(
        quote_snapshot_at=snap,
        requested_cutoff=requested_cutoff,
        match_reconciliation_as_of=ensure_utc(recon, field="match_reconciliation_as_of"),
        pit_match_as_of=pit,
        match_clock_kind=clock_kind,
    )
    ids = tuple(int(x) for x in quote_ids)
    avail_ids = tuple(int(x) for x in availability_observation_ids)
    bout_ids = _card_bout_ids(session, card_id=card_id)

    quotes = list(
        session.scalars(select(OddsQuote).where(OddsQuote.id.in_(ids))).all()
    ) if ids else []
    # Preserve every in-scope response quote ID (do not drop before classify).
    in_scope: list[OddsQuote] = []
    for quote in quotes:
        if quote.provider != provider or quote.region != region:
            continue
        if quote.provider_market_key != market:
            continue
        if snap is not None and quote.snapshot_at is not None:
            q_snap = quote.snapshot_at
            if q_snap.tzinfo is None:
                q_snap = q_snap.replace(tzinfo=UTC)
            if ensure_utc(q_snap, field="snapshot_at") > ensure_utc(
                snap, field="quote_snapshot_at"
            ):
                continue
        in_scope.append(quote)

    attributed: dict[str, list[OddsQuote]] = {}
    unassigned: dict[str, list[OddsQuote]] = {}
    attributed_evidence: set[tuple[str, str, str, str]] = set()
    for quote in in_scope:
        alias = alias_effective_at(
            session,
            provider=provider,
            external_event_id=quote.external_event_id,
            as_of=match_as_of,
        )
        if alias is None or alias.bout_id not in bout_ids:
            unassigned.setdefault(quote.bookmaker_key, []).append(quote)
            continue
        attributed.setdefault(quote.bookmaker_key, []).append(quote)
        attributed_evidence.add(
            _response_evidence_key(
                external_event_id=quote.external_event_id,
                bookmaker_key=quote.bookmaker_key,
                region=quote.region,
                market=quote.provider_market_key,
            )
        )

    cells: list[CoverageCell] = []
    for bookmaker_key in sorted(attributed):
        book_quotes = attributed[bookmaker_key]
        eligible = 0
        ambiguous = False
        for quote in book_quotes:
            alias = alias_effective_at(
                session,
                provider=provider,
                external_event_id=quote.external_event_id,
                as_of=match_as_of,
            )
            if alias is None or alias.bout_id not in bout_ids:
                ambiguous = True
                continue
            decision = resolve_quote_value_eligibility(
                session,
                quote=quote,
                bout_id=alias.bout_id,
                match_status="matched",
                as_of=match_as_of,
                stale_after_minutes=stale_after_minutes,
            )
            if decision.eligible:
                eligible += 1
        if ambiguous:
            status = "unmatched"
            detail = "ambiguous_card_attribution"
            match_reason = "alias_ambiguous"
            matched = False
            coverage_state: CardCoverageState = "incomplete_attribution"
        else:
            status = "observed"
            detail = "collection_observed_not_value_ready"
            match_reason = "alias_effective_maps_to_card_bout"
            matched = True
            coverage_state = "attributed"
        cells.append(
            CoverageCell(
                card_id=card_id,
                bookmaker_key=bookmaker_key,
                market=market,
                time_label=time_label,
                status=status,
                detail=detail,
                matched=matched,
                quote_count=len(book_quotes),
                quote_eligible_count=eligible,
                match_reason=match_reason,
                quote_ids=tuple(int(q.id) for q in book_quotes),
                card_coverage_state=coverage_state,
                **clocks,
            )
        )

    # Per-book absence/unknown only when response inventory proves it for this
    # card's attributed provider event. Another card's same-bookmaker quote must
    # not suppress absence for a different external_event_id.
    absent_proven = False
    if avail_ids:
        observations = list(
            session.scalars(
                select(OddsAvailabilityObservation).where(
                    OddsAvailabilityObservation.id.in_(avail_ids)
                )
            ).all()
        )
        for obs in observations:
            if obs.provider != provider or obs.region != region:
                continue
            if obs.provider_market_key != market:
                continue
            alias = alias_effective_at(
                session,
                provider=provider,
                external_event_id=obs.external_event_id,
                as_of=match_as_of,
            )
            if alias is None or alias.bout_id not in bout_ids:
                continue
            evidence = _response_evidence_key(
                external_event_id=obs.external_event_id,
                bookmaker_key=obs.bookmaker_key,
                region=obs.region,
                market=obs.provider_market_key,
            )
            if evidence in attributed_evidence:
                continue
            absent_proven = True
            cells.append(
                CoverageCell(
                    card_id=card_id,
                    bookmaker_key=obs.bookmaker_key or "*",
                    market=market,
                    time_label=time_label,
                    status="absent",
                    detail="provider_availability_unknown",
                    match_reason="availability_unknown_no_quote",
                    availability_observation_ids=(int(obs.id),),
                    card_coverage_state="absent_proven",
                    **clocks,
                )
            )

    if include_unassigned:
        for bookmaker_key in sorted(unassigned):
            book_quotes = unassigned[bookmaker_key]
            cells.append(
                CoverageCell(
                    card_id=UNASSIGNED_CARD_ID,
                    bookmaker_key=bookmaker_key,
                    market=market,
                    time_label=time_label,
                    status="unmatched",
                    detail="provider_quote_not_linked_to_card",
                    matched=False,
                    quote_count=len(book_quotes),
                    quote_eligible_count=0,
                    match_reason="alias_missing_or_wrong_card",
                    quote_ids=tuple(int(q.id) for q in book_quotes),
                    card_coverage_state="incomplete_attribution",
                    **clocks,
                )
            )

    card_cells = [cell for cell in cells if cell.card_id == card_id]
    if card_cells:
        return cells

    # In-scope quotes exist but none attribute to this card: emit explicit
    # incomplete metadata (not observed, not absent) so emptiness is not
    # mistaken for complete coverage. Unassigned cells above are batch-level.
    if in_scope:
        incomplete = CoverageCell(
            card_id=card_id,
            bookmaker_key="*",
            market=market,
            time_label=time_label,
            status="unmatched",
            detail="incomplete_attribution_unassigned_only",
            matched=False,
            quote_count=len(in_scope),
            quote_eligible_count=0,
            match_reason="quotes_exist_but_not_linked_to_card",
            quote_ids=tuple(int(q.id) for q in in_scope),
            availability_observation_ids=avail_ids,
            card_coverage_state="incomplete_attribution",
            **clocks,
        )
        # Keep __unassigned__ first when present so batch emitters stay stable.
        if include_unassigned and any(c.card_id == UNASSIGNED_CARD_ID for c in cells):
            return [incomplete, *cells]
        return [incomplete, *cells]

    if absent_proven:
        return cells

    return [
        CoverageCell(
            card_id=card_id,
            bookmaker_key="*",
            market=market,
            time_label=time_label,
            status="absent",
            detail="no_snapshot_quotes",
            match_reason="no_quotes_in_snapshot",
            quote_ids=ids,
            availability_observation_ids=avail_ids,
            card_coverage_state="no_snapshot_quotes",
            **clocks,
        )
    ]


def cells_from_ledger_snapshot(
    session: Session,
    *,
    card_id: str,
    time_label: str,
    market: str,
    provider: str,
    region: str,
    detail: str | None = None,
    match_reconciliation_as_of: datetime | None = None,
    quote_snapshot_at: datetime | None = None,
    requested_cutoff: datetime | None = None,
    pit_match_as_of: datetime | None = None,
    availability_observation_ids: Sequence[int] = (),
    as_of: datetime | None = None,
    snapshot_at: datetime | None = None,
) -> list[CoverageCell]:
    """Idempotent replay: reconstruct cells from ledger-linked quote IDs."""
    recon = match_reconciliation_as_of if match_reconciliation_as_of is not None else as_of
    if recon is None:
        raise ValueError("match_reconciliation_as_of is required")
    return cells_from_snapshot_quotes(
        session,
        card_id=card_id,
        time_label=time_label,
        market=market,
        provider=provider,
        region=region,
        match_reconciliation_as_of=recon,
        quote_ids=_quote_ids_from_ledger_detail(detail),
        quote_snapshot_at=quote_snapshot_at or snapshot_at,
        requested_cutoff=requested_cutoff,
        pit_match_as_of=pit_match_as_of,
        availability_observation_ids=availability_observation_ids,
        include_unassigned=False,
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
    match_reconciliation_as_of: datetime | None = None,
    quote_ids: Sequence[int] = (),
    quote_snapshot_at: datetime | None = None,
    availability_observation_ids: Sequence[int] = (),
    estimated_cost: int = 0,
    actual_cost: int | None = None,
    actual_cost_known: bool = False,
    as_of: datetime | None = None,
    snapshot_at: datetime | None = None,
) -> list[CoverageCell]:
    """Deprecated wrapper: ignores cost args (batch-level only) and scopes by IDs."""
    _ = (estimated_cost, actual_cost, actual_cost_known, region)
    recon = match_reconciliation_as_of or as_of
    if recon is None:
        raise ValueError("match_reconciliation_as_of is required")
    return cells_from_snapshot_quotes(
        session,
        card_id=card_id,
        time_label=time_label,
        market=market,
        provider=provider,
        region=region,
        match_reconciliation_as_of=recon,
        quote_ids=quote_ids,
        quote_snapshot_at=quote_snapshot_at or snapshot_at,
        availability_observation_ids=availability_observation_ids,
        as_of=as_of,
        snapshot_at=snapshot_at,
    )


__all__ = [
    "ALLOWED_COVERAGE_STATUSES",
    "UNASSIGNED_CARD_ID",
    "BatchCostRecord",
    "CardCoverageState",
    "CoverageCell",
    "OddsCoverageReport",
    "PlannedWorkItem",
    "build_odds_coverage_report",
    "cells_from_ledger_snapshot",
    "cells_from_persisted_quotes",
    "cells_from_snapshot_quotes",
    "encode_availability_ids_for_ledger",
    "encode_quote_ids_for_ledger",
]
