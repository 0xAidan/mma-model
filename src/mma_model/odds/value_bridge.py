"""Adapters from DWCS-202/203 odds types into DWCS-204 evidence DTOs."""

from __future__ import annotations

from mma_model.db.tables.odds import OddsQuote
from mma_model.odds.lifecycle import QuoteEligibilityDecision
from mma_model.odds.manual_price import ObservedPrice, PriceSourceKind
from mma_model.value.errors import IneligiblePriceError
from mma_model.value.evidence import (
    ManualObservedPriceEvidence,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    SelectionPriceObservation,
)


def manual_evidence_from_observed(observed: ObservedPrice) -> ManualObservedPriceEvidence:
    """Build manual priced evidence from a DWCS-202 ObservedPrice."""
    if observed.source_kind is not PriceSourceKind.USER_OBSERVED:
        raise IneligiblePriceError("manual evidence requires user_observed ObservedPrice")
    if observed.price_decimal is None:
        raise IneligiblePriceError("manual evidence requires an available price_decimal")
    if observed.selection_identity is None:
        raise IneligiblePriceError("manual evidence requires selection_identity")
    return ManualObservedPriceEvidence(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        automated=observed.automated,
        market_family=observed.market_family.value,
        outcome_key=observed.outcome_key.value,
        line_point=observed.line_point,
        selection_identity=observed.selection_identity,
        price_decimal=observed.price_decimal,
        lifecycle=observed.lifecycle.value,
        observed_at=observed.observed_at,
        bookmaker_key=observed.bookmaker_key,
        region=observed.region,
    )


def quote_evidence_from_row(
    quote: OddsQuote,
    *,
    bout_id: str | None,
    selection_identity: str,
) -> ProviderQuoteEvidence:
    """Build provider quote evidence from a persisted OddsQuote row."""
    if quote.id is None:
        raise IneligiblePriceError("quote row must be persisted (quote.id required)")
    return ProviderQuoteEvidence(
        quote_id=int(quote.id),
        market_family=str(quote.market_family),
        outcome_key=str(quote.outcome_key),
        line_point=quote.line_point,
        selection_identity=selection_identity,
        price_decimal=float(quote.price_decimal),
        availability=str(quote.availability),
        observed_at=quote.observed_at,
        bout_id=bout_id,
        bookmaker_key=str(quote.bookmaker_key),
        region=str(quote.region),
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


def selection_observation_from_manual(
    observed: ObservedPrice,
) -> SelectionPriceObservation:
    """CLV opening/closing observation from a user-observed available price."""
    evidence = manual_evidence_from_observed(observed)
    return SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        market_family=evidence.market_family,
        outcome_key=evidence.outcome_key,
        line_point=evidence.line_point,
        selection_identity=evidence.selection_identity,
        price_decimal=evidence.price_decimal,
        observed_at=evidence.observed_at,
        lifecycle_or_availability=evidence.lifecycle,
    )
