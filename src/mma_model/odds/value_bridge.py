"""Adapters from DWCS-202/203 odds types into DWCS-204 evidence DTOs."""

from __future__ import annotations

from mma_model.db.tables.odds import OddsQuote
from mma_model.odds.lifecycle import QuoteEligibilityDecision
from mma_model.odds.manual_price import ObservedPrice, PriceSourceKind, canonical_selection_identity
from mma_model.value.errors import IneligiblePriceError
from mma_model.value.evidence import (
    ClosingPriceEvidence,
    ManualObservedPriceEvidence,
    PriceObservationRole,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    ValueSelectionContext,
)


def manual_evidence_from_observed(
    observed: ObservedPrice,
    *,
    bound_bout_id: str | None,
    price_role: PriceObservationRole = PriceObservationRole.OPENING,
) -> ManualObservedPriceEvidence:
    """Build manual priced evidence from a DWCS-202 ObservedPrice.

    ``bound_bout_id`` is required before metrics; stored unmatched rows may pass
    ``None`` but cannot produce EV/ROI/CLV until rebound.
    """
    if observed.source_kind is not PriceSourceKind.USER_OBSERVED:
        raise IneligiblePriceError("manual evidence requires user_observed ObservedPrice")
    if observed.price_decimal is None:
        raise IneligiblePriceError("manual evidence requires an available price_decimal")
    selection = observed.selection_identity or canonical_selection_identity(
        observed.market_family, observed.outcome_key, observed.line_point
    )
    return ManualObservedPriceEvidence(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        automated=observed.automated,
        market_family=observed.market_family.value,
        outcome_key=observed.outcome_key.value,
        line_point=observed.line_point,
        selection_identity=selection,
        price_decimal=observed.price_decimal,
        lifecycle=observed.lifecycle.value,
        observed_at=observed.observed_at,
        bookmaker_key=observed.bookmaker_key,
        region=observed.region,
        bound_bout_id=bound_bout_id,
        price_role=price_role,
    )


def quote_evidence_from_row(
    quote: OddsQuote,
    *,
    bout_id: str | None,
    selection_identity: str | None = None,
    price_role: PriceObservationRole = PriceObservationRole.OPENING,
) -> ProviderQuoteEvidence:
    """Build provider quote evidence from a persisted OddsQuote row."""
    if quote.id is None:
        raise IneligiblePriceError("quote row must be persisted (quote.id required)")
    market_id = selection_identity or (
        f"{quote.market_family}:{quote.outcome_key}"
        if quote.line_point is None
        else f"{quote.market_family}:{quote.outcome_key}:{float(quote.line_point)}"
    )
    return ProviderQuoteEvidence(
        quote_id=int(quote.id),
        provider=str(quote.provider),
        bookmaker_key=str(quote.bookmaker_key),
        region=str(quote.region),
        market_family=str(quote.market_family),
        outcome_key=str(quote.outcome_key),
        line_point=quote.line_point,
        selection_identity=market_id,
        price_decimal=float(quote.price_decimal),
        availability=str(quote.availability),
        observed_at=quote.observed_at,
        bout_id=bout_id,
        dedupe_key=str(quote.dedupe_key) if quote.dedupe_key is not None else None,
        external_event_id=(
            str(quote.external_event_id) if quote.external_event_id is not None else None
        ),
        price_role=price_role,
    )


def eligibility_evidence_from_decision(
    decision: QuoteEligibilityDecision,
) -> QuoteEligibilityEvidence:
    """Build eligibility evidence from a DWCS-203 QuoteEligibilityDecision."""
    return QuoteEligibilityEvidence(
        quote_id=int(decision.quote_id),
        eligible=bool(decision.eligible),
        selection_identity=str(decision.selection_identity),
        resolved_bout_id=decision.resolved_bout_id,
        reason=decision.reason.value,
    )


def closing_evidence_from_manual(
    observed: ObservedPrice,
    *,
    bound_bout_id: str,
) -> ClosingPriceEvidence:
    """Closing CLV evidence from a bound user-observed available price."""
    return ClosingPriceEvidence(
        manual_evidence=manual_evidence_from_observed(
            observed,
            bound_bout_id=bound_bout_id,
            price_role=PriceObservationRole.CLOSING,
        )
    )


def closing_evidence_from_provider(
    quote: OddsQuote,
    decision: QuoteEligibilityDecision,
) -> ClosingPriceEvidence:
    """Closing CLV evidence from quote row + eligible DWCS-203 decision."""
    elig = eligibility_evidence_from_decision(decision)
    return ClosingPriceEvidence(
        quote_evidence=quote_evidence_from_row(
            quote,
            bout_id=elig.resolved_bout_id,
            selection_identity=elig.selection_identity,
            price_role=PriceObservationRole.CLOSING,
        ),
        eligibility_evidence=elig,
    )


def value_context_from_parts(
    *,
    bout_id: str,
    market_family: str,
    outcome_key: str,
    line_point: float | None = None,
    event_id: str | None = None,
) -> ValueSelectionContext:
    """Helper to build a validated bout-scoped value selection context."""
    return ValueSelectionContext(
        bout_id=bout_id,
        market_family=market_family,
        outcome_key=outcome_key,
        line_point=line_point,
        event_id=event_id,
    )
