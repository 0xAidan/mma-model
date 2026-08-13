"""Adapters from DWCS-202/203 odds types into DWCS-204 evidence DTOs."""

from __future__ import annotations

from datetime import datetime

from mma_model.db.tables.odds import OddsQuote
from mma_model.odds.lifecycle import QuoteEligibilityDecision
from mma_model.odds.manual_price import ObservedPrice, PriceSourceKind, canonical_selection_identity
from mma_model.value.errors import IneligiblePriceError
from mma_model.value.evidence import (
    ClosingPriceEvidence,
    ManualBoutBindingAssertion,
    ManualObservedPriceEvidence,
    PriceObservationRole,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    ValueSelectionContext,
    validate_catalog_selection,
)


def manual_evidence_from_observed(
    observed: ObservedPrice,
    *,
    bout_binding: ManualBoutBindingAssertion | None,
    price_role: PriceObservationRole = PriceObservationRole.OPENING,
) -> ManualObservedPriceEvidence:
    """Build manual priced evidence from a DWCS-202 ObservedPrice.

    Bout binding is an auditable ``ManualBoutBindingAssertion`` (actor/time/source).
    Unmatched stored rows may pass ``bout_binding=None`` but cannot produce metrics.
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
        bout_binding=bout_binding,
        price_role=price_role,
    )


def eligibility_evidence_from_decision(
    decision: QuoteEligibilityDecision,
) -> QuoteEligibilityEvidence:
    """Build eligibility evidence solely from an authoritative DWCS-203 decision.

    Temporal/state fields (``evaluated_at``, availability, lifecycle, version,
    identity) are copied from the decision. Callers cannot override or relabel
    a stale decision as current.
    """
    return QuoteEligibilityEvidence(
        quote_id=int(decision.quote_id),
        eligible=bool(decision.eligible),
        selection_identity=str(decision.selection_identity),
        resolved_bout_id=decision.resolved_bout_id,
        reason=decision.reason.value,
        evaluated_at=decision.evaluated_at,
        quote_availability_at_decision=decision.quote_availability_at_decision,
        decision_identity=decision.decision_identity,
        quote_freshness_at=decision.freshness_at,
        lifecycle_state_at_decision=decision.lifecycle_state_at_decision,
        decision_version=decision.decision_version,
    )


def quote_evidence_from_row(
    quote: OddsQuote,
    *,
    eligibility: QuoteEligibilityEvidence,
    price_role: PriceObservationRole = PriceObservationRole.OPENING,
) -> ProviderQuoteEvidence:
    """Build provider quote evidence deriving bout/selection from quote+eligibility.

    Caller-supplied bout/selection identity is not accepted. Selection is derived
    from quote catalog fields and must exactly match ``eligibility.selection_identity``.
    Bout is taken from ``eligibility.resolved_bout_id`` when eligible.
    """
    if quote.id is None:
        raise IneligiblePriceError("quote row must be persisted (quote.id required)")
    if int(quote.id) != int(eligibility.quote_id):
        raise IneligiblePriceError(
            "quote.id must equal eligibility.quote_id "
            f"(got {quote.id!r} vs {eligibility.quote_id!r})"
        )
    _family, _outcome, market_id = validate_catalog_selection(
        str(quote.market_family),
        str(quote.outcome_key),
        quote.line_point,
    )
    if market_id != eligibility.selection_identity:
        raise IneligiblePriceError(
            "quote-derived selection must exactly match eligibility.selection_identity: "
            f"{market_id!r} vs {eligibility.selection_identity!r}"
        )
    bout_id = eligibility.resolved_bout_id if eligibility.eligible else None
    return ProviderQuoteEvidence(
        quote_id=int(quote.id),
        provider=str(quote.provider),
        bookmaker_key=str(quote.bookmaker_key),
        region=str(quote.region),
        market_family=_family.value,
        outcome_key=_outcome.value,
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


def closing_evidence_from_manual(
    observed: ObservedPrice,
    *,
    bout_binding: ManualBoutBindingAssertion,
    closing_cutoff: datetime | None = None,
) -> ClosingPriceEvidence:
    """Closing CLV evidence from a bound user-observed available price."""
    return ClosingPriceEvidence(
        manual_evidence=manual_evidence_from_observed(
            observed,
            bout_binding=bout_binding,
            price_role=PriceObservationRole.CLOSING,
        ),
        closing_cutoff=closing_cutoff,
    )


def closing_evidence_from_provider(
    quote: OddsQuote,
    decision: QuoteEligibilityDecision,
    *,
    allow_cross_book: bool = False,
) -> ClosingPriceEvidence:
    """Closing CLV evidence from quote row + DWCS-203 decision (cutoff from decision)."""
    elig = eligibility_evidence_from_decision(decision)
    return ClosingPriceEvidence(
        quote_evidence=quote_evidence_from_row(
            quote,
            eligibility=elig,
            price_role=PriceObservationRole.CLOSING,
        ),
        eligibility_evidence=elig,
        closing_cutoff=decision.evaluated_at,
        allow_cross_book=allow_cross_book,
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
