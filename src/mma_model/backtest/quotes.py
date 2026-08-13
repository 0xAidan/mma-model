"""DWCS-203 quote loading for walk-forward (no forged eligibility).

Join filters to observations at/before the card cutoff first so a later
post-cutoff quote cannot poison a valid earlier line. Closing evidence is a
distinct later eligible quote for the same selection and book. Protocol
fixtures may attach explicit quotes but must label fixture provenance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsProviderEventAlias, OddsQuote
from mma_model.features.as_of import ensure_utc
from mma_model.modeling.splits import EventGroup
from mma_model.odds.lifecycle import (
    alias_effective_at,
    latest_match_observation_at,
    quotes_visible_under_alias_at,
    resolve_quote_value_eligibility,
)
from mma_model.odds.matching import MATCH_STATUS_MATCHED, load_matching_contract
from mma_model.odds.value_bridge import (
    eligibility_evidence_from_decision,
    quote_evidence_from_row,
)
from mma_model.quality.schema import sha256_canonical
from mma_model.value.errors import IneligiblePriceError, InvalidOddsError
from mma_model.value.evidence import ProviderQuoteEvidence, QuoteEligibilityEvidence
from mma_model.value.priced import PriceObservationRole


@dataclass(frozen=True)
class LoadedQuoteRow:
    """One quote resolved at a specific card cutoff with authoritative eligibility."""

    bout_id: str
    cutoff: datetime
    event_start: datetime
    quote: OddsQuote
    eligible: bool
    eligibility_reason: str
    eligibility: QuoteEligibilityEvidence
    quote_evidence: ProviderQuoteEvidence | None
    later_ignored: int = 0


def quote_content_payload(
    quote: OddsQuote, *, eligibility: QuoteEligibilityEvidence
) -> dict[str, Any]:
    return {
        "availability": quote.availability,
        "bookmaker_key": quote.bookmaker_key,
        "bout_id": eligibility.resolved_bout_id,
        "decision_identity": eligibility.decision_identity,
        "decision_version": eligibility.decision_version,
        "evaluated_at": eligibility.evaluated_at.isoformat(),
        "line_point": quote.line_point,
        "market_family": quote.market_family,
        "observed_at": quote.observed_at.isoformat(),
        "outcome_key": quote.outcome_key,
        "price_decimal": float(quote.price_decimal),
        "provider": quote.provider,
        "quote_id": int(quote.id) if quote.id is not None else None,
        "region": quote.region,
        "selection_identity": eligibility.selection_identity,
        "source_updated_at": (
            None if quote.source_updated_at is None else quote.source_updated_at.isoformat()
        ),
    }


def quote_inventory_hash(rows: Sequence[LoadedQuoteRow]) -> str:
    payload = [
        quote_content_payload(row.quote, eligibility=row.eligibility)
        for row in sorted(
            rows,
            key=lambda item: (
                item.bout_id,
                int(item.quote.id or 0),
                item.cutoff.isoformat(),
            ),
        )
    ]
    return sha256_canonical({"quotes": payload})


def _stale_after_minutes() -> int:
    return int(load_matching_contract().stale_after_minutes)


def _resolve_row(
    session: Session,
    *,
    quote: OddsQuote,
    bout_id: str,
    cutoff: datetime,
    event_start: datetime,
    stale_after: int,
    price_role: PriceObservationRole,
) -> LoadedQuoteRow | None:
    cutoff = ensure_utc(cutoff)
    event_start = ensure_utc(event_start)
    latest = latest_match_observation_at(
        session,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
        as_of=cutoff,
    )
    match_status = latest.match_status if latest is not None else ""
    decision = resolve_quote_value_eligibility(
        session,
        quote=quote,
        bout_id=bout_id,
        match_status=match_status or MATCH_STATUS_MATCHED,
        as_of=cutoff,
        stale_after_minutes=stale_after,
    )
    try:
        eligibility = eligibility_evidence_from_decision(decision)
    except IneligiblePriceError:
        return None
    evidence: ProviderQuoteEvidence | None = None
    try:
        evidence = quote_evidence_from_row(
            quote,
            eligibility=eligibility,
            price_role=price_role,
        )
    except (IneligiblePriceError, InvalidOddsError):
        evidence = None
    return LoadedQuoteRow(
        bout_id=bout_id,
        cutoff=cutoff,
        event_start=event_start,
        quote=quote,
        eligible=bool(decision.eligible),
        eligibility_reason=decision.reason.value,
        eligibility=eligibility,
        quote_evidence=evidence,
    )


def load_quotes_at_cutoff(
    session: Session,
    *,
    bout_ids: Sequence[str],
    cutoff: datetime,
    event_start: datetime,
) -> tuple[LoadedQuoteRow, ...]:
    """Resolve DWCS-203 eligibility at ``cutoff`` for quotes visible on these bouts."""
    wanted = set(bout_ids)
    if not wanted:
        return ()
    cutoff = ensure_utc(cutoff)
    event_start = ensure_utc(event_start)
    stale_after = _stale_after_minutes()
    alias_rows = session.scalars(
        select(OddsProviderEventAlias).where(OddsProviderEventAlias.bout_id.in_(tuple(wanted)))
    ).all()
    seen_events: set[tuple[str, str]] = set()
    loaded: list[LoadedQuoteRow] = []
    for alias in alias_rows:
        key = (str(alias.provider), str(alias.external_event_id))
        if key in seen_events:
            continue
        seen_events.add(key)
        effective = alias_effective_at(
            session,
            provider=alias.provider,
            external_event_id=alias.external_event_id,
            as_of=cutoff,
        )
        if effective is None or effective.bout_id not in wanted:
            continue
        visible = quotes_visible_under_alias_at(
            session,
            provider=alias.provider,
            external_event_id=alias.external_event_id,
            as_of=cutoff,
        )
        stored = session.scalars(
            select(OddsQuote).where(
                OddsQuote.provider == alias.provider,
                OddsQuote.external_event_id == alias.external_event_id,
            )
        ).all()
        later_by_selection: dict[tuple[str, str, float | None], int] = {}
        for quote in stored:
            if ensure_utc(quote.observed_at) <= cutoff:
                continue
            sel = (str(quote.market_family), str(quote.outcome_key), quote.line_point)
            later_by_selection[sel] = later_by_selection.get(sel, 0) + 1
        for quote in visible:
            if ensure_utc(quote.observed_at) > cutoff:
                continue
            row = _resolve_row(
                session,
                quote=quote,
                bout_id=str(effective.bout_id),
                cutoff=cutoff,
                event_start=event_start,
                stale_after=stale_after,
                price_role=PriceObservationRole.OPENING,
            )
            if row is None:
                continue
            sel = (str(quote.market_family), str(quote.outcome_key), quote.line_point)
            loaded.append(
                LoadedQuoteRow(
                    bout_id=row.bout_id,
                    cutoff=row.cutoff,
                    event_start=event_start,
                    quote=row.quote,
                    eligible=row.eligible,
                    eligibility_reason=row.eligibility_reason,
                    eligibility=row.eligibility,
                    quote_evidence=row.quote_evidence,
                    later_ignored=later_by_selection.get(sel, 0),
                )
            )
    return tuple(loaded)


def load_quotes_for_groups(
    session: Session,
    groups: Sequence[EventGroup],
) -> tuple[LoadedQuoteRow, ...]:
    rows: list[LoadedQuoteRow] = []
    for group in groups:
        rows.extend(
            load_quotes_at_cutoff(
                session,
                bout_ids=group.bout_ids,
                cutoff=group.cutoff.cutoff,
                event_start=group.event_start,
            )
        )
    return tuple(rows)


def select_closing_row(
    session: Session,
    *,
    opening: LoadedQuoteRow,
    event_start: datetime,
) -> LoadedQuoteRow | None:
    """Distinct later eligible quote, same selection and book, before commence/lock."""
    quote = opening.quote
    stale_after = _stale_after_minutes()
    later = session.scalars(
        select(OddsQuote).where(
            OddsQuote.provider == quote.provider,
            OddsQuote.external_event_id == quote.external_event_id,
            OddsQuote.bookmaker_key == quote.bookmaker_key,
            OddsQuote.region == quote.region,
            OddsQuote.market_family == quote.market_family,
            OddsQuote.outcome_key == quote.outcome_key,
            OddsQuote.id != quote.id,
        )
    ).all()
    open_at = ensure_utc(quote.observed_at)
    lock_at = ensure_utc(event_start)
    later = [
        row
        for row in later
        if open_at < ensure_utc(row.observed_at) <= lock_at
    ]
    if quote.line_point is None:
        later = [row for row in later if row.line_point is None]
    else:
        later = [
            row
            for row in later
            if row.line_point is not None and float(row.line_point) == float(quote.line_point)
        ]
    if not later:
        return None
    chosen = max(later, key=lambda row: (row.observed_at, int(row.id or 0)))
    resolved = _resolve_row(
        session,
        quote=chosen,
        bout_id=opening.bout_id,
        cutoff=ensure_utc(chosen.observed_at),
        event_start=event_start,
        stale_after=stale_after,
        price_role=PriceObservationRole.CLOSING,
    )
    if resolved is None or not resolved.eligible:
        return None
    return resolved


__all__ = [
    "LoadedQuoteRow",
    "load_quotes_at_cutoff",
    "load_quotes_for_groups",
    "quote_content_payload",
    "quote_inventory_hash",
    "select_closing_row",
]
