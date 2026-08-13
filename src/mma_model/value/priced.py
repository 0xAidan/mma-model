"""Priced-only value metrics gated by typed provenance evidence (DWCS-204).

Unpriced price-target rows never receive EV / CLV / ROI / realized profit / stake.
Caller booleans alone cannot grant metrics. ``PricedValueRequest`` names a
bout-scoped ``ValueSelectionContext`` and a ``valuation_cutoff``. Manual prices
require DWCS-202 evidence with an auditable bout binding; provider quotes require
quote evidence plus matching DWCS-203 eligibility evaluated at that cutoff
(never older / replayed). Closing evidence uses its own closing cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import (
    IneligiblePriceError,
    InvalidOddsError,
    SelectionMismatchError,
    UnpricedMetricsError,
)
from mma_model.value.ev import (
    CLV_UNIT,
    expected_value,
    flat_unit_profit,
    same_selection_closing_ev,
    same_selection_probability_clv,
)
from mma_model.value.evidence import (
    ClosingPriceEvidence,
    ManualObservedPriceEvidence,
    PriceObservationRole,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    SelectionPriceObservation,
    ValueSelectionContext,
    assert_matches_context,
)
from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    quarter_kelly_fraction,
)
from mma_model.value.odds import VALUE_MATH_METHOD, VALUE_MATH_VERSION, validate_probability
from mma_model.value.portfolio import capped_stake_fraction

PRICED_METRICS_METHOD: Final = "priced_value_metrics"
PRICED_METRICS_VERSION: Final = "1.0.0"
ROI_UNIT: Final = "unit_profit_per_unit_stake"
MODEL_PROBABILITY_UNIT: Final = "probability_open_unit_interval"


class PriceSourceKind(StrEnum):
    """Provenance for priced metrics (not Bet365 / reference mislabels)."""

    USER_OBSERVED = "user_observed"
    PROVIDER_QUOTE = "provider_quote"
    UNPRICED = "unpriced"


class MetricsUnavailableReason(StrEnum):
    NONE = "none"
    UNPRICED_TARGET = "unpriced_target"
    PRODUCT_INELIGIBLE = "product_ineligible"
    QUOTE_INELIGIBLE = "quote_ineligible"
    MATCH_GATE_ONLY = "match_gate_insufficient"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    CONTEXT_MISMATCH = "context_mismatch"
    UNBOUND_MANUAL_PRICE = "unbound_manual_price"
    MISSING_CLOSING_PRICE = "missing_closing_price"
    NON_CLOSING_SAME_TIMESTAMP = "non_closing_same_timestamp"
    UNRESOLVED_SETTLEMENT = "unresolved_settlement"
    STALE_ELIGIBILITY_EVIDENCE = "stale_eligibility_evidence"
    ELIGIBILITY_CUTOFF_MISMATCH = "eligibility_cutoff_mismatch"
    CROSS_BOOK_CLOSING_DISALLOWED = "cross_book_closing_disallowed"
    MISSING_VALUATION_CUTOFF = "missing_valuation_cutoff"


@dataclass(frozen=True)
class PriceProvenanceSummary:
    """Safe reproducibility summary (no secrets / full payloads)."""

    role: str
    source_kind: str
    quote_id: int | None
    provider: str | None
    bookmaker_key: str | None
    region: str | None
    observed_at: str | None
    eligibility_evaluated_at: str | None
    eligibility_reason: str | None
    eligibility_decision_identity: str | None
    eligibility_decision_version: str | None = None
    manual_binding_actor: str | None = None
    manual_binding_source: str | None = None
    manual_binding_asserted_at: str | None = None
    cross_book_closing: bool | None = None


@dataclass(frozen=True)
class PricedValueRequest:
    """Inputs for gated EV / CLV / profit / ROI / stake computation.

    ``target_context`` and ``valuation_cutoff`` are required for provider paths.
    Manual paths also require ``valuation_cutoff`` (>= observation time).
    Provide either ``manual_evidence`` (DWCS-202) or both ``quote_evidence`` and
    ``eligibility_evidence`` (DWCS-203). Closing prices use ``closing_evidence``.
    """

    model_prob: float
    target_context: ValueSelectionContext
    valuation_cutoff: datetime
    product_eligible: bool = False
    manual_evidence: ManualObservedPriceEvidence | None = None
    quote_evidence: ProviderQuoteEvidence | None = None
    eligibility_evidence: QuoteEligibilityEvidence | None = None
    closing_evidence: ClosingPriceEvidence | None = None
    settlement: SettlementResult | None = None
    bankroll_cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION

    def __post_init__(self) -> None:
        if self.valuation_cutoff.tzinfo is None:
            raise InvalidOddsError("valuation_cutoff must be timezone-aware")
        object.__setattr__(
            self, "valuation_cutoff", self.valuation_cutoff.astimezone(UTC)
        )


@dataclass(frozen=True)
class PricedValueMetrics:
    """Typed priced metrics with per-metric availability and provenance summary."""

    available: bool
    method: str
    version: str
    value_math_method: str
    value_math_version: str
    reason: MetricsUnavailableReason
    target_value_selection_identity: str | None = None
    expected_value: float | None = None
    expected_value_reason: MetricsUnavailableReason = MetricsUnavailableReason.NONE
    closing_ev: float | None = None
    closing_ev_reason: MetricsUnavailableReason = MetricsUnavailableReason.MISSING_CLOSING_PRICE
    probability_clv: float | None = None
    probability_clv_reason: MetricsUnavailableReason = (
        MetricsUnavailableReason.MISSING_CLOSING_PRICE
    )
    probability_clv_unit: str = CLV_UNIT
    flat_unit_profit: float | None = None
    flat_unit_profit_reason: MetricsUnavailableReason = (
        MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    )
    realized_roi: float | None = None
    realized_roi_reason: MetricsUnavailableReason = (
        MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    )
    realized_roi_unit: str = ROI_UNIT
    quarter_kelly_fraction: float | None = None
    stake_fraction: float | None = None
    stake_reason: MetricsUnavailableReason = MetricsUnavailableReason.NONE
    detail: str = ""
    opening_provenance: PriceProvenanceSummary | None = None
    closing_provenance: PriceProvenanceSummary | None = None
    model_probability_unit: str = MODEL_PROBABILITY_UNIT
    bankroll_cap_fraction: float | None = None
    valuation_cutoff: str | None = None

    def require_available(self) -> PricedValueMetrics:
        if self.available and self.expected_value is not None:
            return self
        if self.reason is MetricsUnavailableReason.UNPRICED_TARGET:
            raise UnpricedMetricsError(self.detail or self.reason.value)
        raise IneligiblePriceError(self.detail or self.reason.value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _unavailable(
    reason: MetricsUnavailableReason,
    *,
    detail: str = "",
    target_value_selection_identity: str | None = None,
    valuation_cutoff: datetime | None = None,
) -> PricedValueMetrics:
    return PricedValueMetrics(
        available=False,
        method=PRICED_METRICS_METHOD,
        version=PRICED_METRICS_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        reason=reason,
        target_value_selection_identity=target_value_selection_identity,
        expected_value=None,
        expected_value_reason=reason,
        closing_ev=None,
        closing_ev_reason=reason,
        probability_clv=None,
        probability_clv_reason=reason,
        flat_unit_profit=None,
        flat_unit_profit_reason=reason,
        realized_roi=None,
        realized_roi_reason=reason,
        quarter_kelly_fraction=None,
        stake_fraction=None,
        stake_reason=reason,
        detail=detail,
        valuation_cutoff=_iso(valuation_cutoff),
    )


def _manual_provenance(
    evidence: ManualObservedPriceEvidence,
    *,
    role: str,
) -> PriceProvenanceSummary:
    binding = evidence.bout_binding
    return PriceProvenanceSummary(
        role=role,
        source_kind=PriceSourceKind.USER_OBSERVED.value,
        quote_id=None,
        provider=None,
        bookmaker_key=evidence.bookmaker_key,
        region=evidence.region,
        observed_at=_iso(evidence.observed_at),
        eligibility_evaluated_at=None,
        eligibility_reason=None,
        eligibility_decision_identity=None,
        manual_binding_actor=None if binding is None else binding.asserted_by,
        manual_binding_source=None if binding is None else binding.source.value,
        manual_binding_asserted_at=None if binding is None else _iso(binding.asserted_at),
        cross_book_closing=None,
    )


def _provider_provenance(
    quote: ProviderQuoteEvidence,
    eligibility: QuoteEligibilityEvidence,
    *,
    role: str,
    cross_book_closing: bool | None = None,
) -> PriceProvenanceSummary:
    return PriceProvenanceSummary(
        role=role,
        source_kind=PriceSourceKind.PROVIDER_QUOTE.value,
        quote_id=quote.quote_id,
        provider=quote.provider,
        bookmaker_key=quote.bookmaker_key,
        region=quote.region,
        observed_at=_iso(quote.observed_at),
        eligibility_evaluated_at=_iso(eligibility.evaluated_at),
        eligibility_reason=eligibility.reason,
        eligibility_decision_identity=eligibility.decision_identity,
        eligibility_decision_version=eligibility.decision_version,
        cross_book_closing=cross_book_closing,
    )


def _observation_from_manual(
    evidence: ManualObservedPriceEvidence,
    *,
    bout_id: str,
) -> SelectionPriceObservation:
    return SelectionPriceObservation(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        bout_id=bout_id,
        market_family=evidence.market_family,
        outcome_key=evidence.outcome_key,
        line_point=evidence.line_point,
        market_selection_identity=evidence.selection_identity,
        value_selection_identity=f"{bout_id}|{evidence.selection_identity}",
        price_decimal=evidence.price_decimal,
        observed_at=evidence.observed_at,
        lifecycle_or_availability=evidence.lifecycle,
        price_role=evidence.price_role,
        provider=None,
        bookmaker_key=evidence.bookmaker_key,
        region=evidence.region,
        quote_id=None,
    )


def _observation_from_quote(
    evidence: ProviderQuoteEvidence,
    *,
    bout_id: str,
) -> SelectionPriceObservation:
    return SelectionPriceObservation(
        provenance=PriceProvenanceKind.PROVIDER_QUOTE,
        bout_id=bout_id,
        market_family=evidence.market_family,
        outcome_key=evidence.outcome_key,
        line_point=evidence.line_point,
        market_selection_identity=evidence.selection_identity,
        value_selection_identity=f"{bout_id}|{evidence.selection_identity}",
        price_decimal=evidence.price_decimal,
        observed_at=evidence.observed_at,
        lifecycle_or_availability=evidence.availability,
        price_role=evidence.price_role,
        provider=evidence.provider,
        bookmaker_key=evidence.bookmaker_key,
        region=evidence.region,
        quote_id=evidence.quote_id,
    )


def _validate_provider_pair(
    quote: ProviderQuoteEvidence,
    eligibility: QuoteEligibilityEvidence,
    *,
    target: ValueSelectionContext,
    required_cutoff: datetime,
) -> MetricsUnavailableReason | None:
    if int(quote.quote_id) != int(eligibility.quote_id):
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if quote.selection_identity != eligibility.selection_identity:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if quote.selection_identity != target.market_selection_identity:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    if eligibility.selection_identity != target.market_selection_identity:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    if eligibility.evaluated_at != required_cutoff:
        return MetricsUnavailableReason.ELIGIBILITY_CUTOFF_MISMATCH
    if quote.observed_at > required_cutoff:
        return MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE
    if eligibility.eligible and eligibility.quote_availability_at_decision != "available":
        return MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE
    if eligibility.eligible and quote.availability != "available":
        return MetricsUnavailableReason.QUOTE_INELIGIBLE
    # Blocking lifecycle / availability reasons after an older eligible decision.
    if not eligibility.eligible:
        if eligibility.reason in {"unmatched", "latest_match_not_matched"}:
            return MetricsUnavailableReason.MATCH_GATE_ONLY
        if eligibility.reason in {
            "stale",
            "locked",
            "selection_locked",
            "replaced",
            "review_blocked",
            "unknown_availability",
            "market_unknown",
            "quote_unavailable",
            "bout_terminal",
            "terminal_lifecycle",
            "not_visible",
        }:
            return MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE
        return MetricsUnavailableReason.QUOTE_INELIGIBLE
    assert eligibility.resolved_bout_id is not None
    if eligibility.resolved_bout_id != target.bout_id:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    # Bout must be derived from eligibility; quote bout if present must match.
    if quote.bout_id is not None and quote.bout_id != eligibility.resolved_bout_id:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    return None


def _resolve_opening(
    request: PricedValueRequest,
) -> (
    tuple[SelectionPriceObservation, PriceSourceKind, PriceProvenanceSummary]
    | PricedValueMetrics
):
    target = request.target_context
    cutoff = request.valuation_cutoff
    manual = request.manual_evidence
    quote = request.quote_evidence
    eligibility = request.eligibility_evidence

    if manual is None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.UNPRICED_TARGET,
            detail="unpriced targets cannot produce EV/ROI/CLV/realized profit/stake",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    if manual is not None and (quote is not None or eligibility is not None):
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="provide either manual_evidence or quote+eligibility evidence, not both",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    if quote is not None and eligibility is None:
        return _unavailable(
            MetricsUnavailableReason.QUOTE_INELIGIBLE,
            detail="provider quotes require QuoteEligibilityEvidence from DWCS-203",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    if eligibility is not None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="eligibility evidence requires matching ProviderQuoteEvidence",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    if not request.product_eligible:
        return _unavailable(
            MetricsUnavailableReason.PRODUCT_INELIGIBLE,
            detail="selection failed product gates or maturity",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )

    if manual is not None:
        if manual.price_role is not PriceObservationRole.OPENING:
            return _unavailable(
                MetricsUnavailableReason.EVIDENCE_MISMATCH,
                detail="opening manual evidence requires price_role=opening",
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        if manual.bout_binding is None:
            return _unavailable(
                MetricsUnavailableReason.UNBOUND_MANUAL_PRICE,
                detail=(
                    "manual price is unbound; require ManualBoutBindingAssertion "
                    "(user_assertion|operator_assertion) before exact EV/ROI/CLV"
                ),
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        if manual.bound_bout_id != target.bout_id:
            return _unavailable(
                MetricsUnavailableReason.CONTEXT_MISMATCH,
                detail=(
                    "manual bout binding does not match target context bout "
                    f"({manual.bound_bout_id!r} vs {target.bout_id!r})"
                ),
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        if manual.selection_identity != target.market_selection_identity:
            return _unavailable(
                MetricsUnavailableReason.CONTEXT_MISMATCH,
                detail="manual market selection does not match target context",
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        if cutoff < manual.observed_at:
            return _unavailable(
                MetricsUnavailableReason.ELIGIBILITY_CUTOFF_MISMATCH,
                detail="valuation_cutoff must be >= manual opening observed_at",
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        assert manual.bout_binding is not None
        if manual.bout_binding.asserted_at > cutoff:
            return _unavailable(
                MetricsUnavailableReason.ELIGIBILITY_CUTOFF_MISMATCH,
                detail=(
                    "manual bout binding asserted_at must be <= valuation_cutoff "
                    "(future assertions cannot authorize historical valuation)"
                ),
                target_value_selection_identity=target.value_selection_identity,
                valuation_cutoff=cutoff,
            )
        opening = _observation_from_manual(manual, bout_id=target.bout_id)
        assert_matches_context(opening, target)
        return (
            opening,
            PriceSourceKind.USER_OBSERVED,
            _manual_provenance(manual, role="opening"),
        )

    assert quote is not None and eligibility is not None
    if quote.price_role is not PriceObservationRole.OPENING:
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="opening provider evidence requires price_role=opening",
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    mismatch = _validate_provider_pair(
        quote, eligibility, target=target, required_cutoff=cutoff
    )
    if mismatch is not None:
        detail = {
            MetricsUnavailableReason.MATCH_GATE_ONLY: (
                "bout match gate alone is insufficient; "
                "quote-level eligibility from DWCS-203 is required"
            ),
            MetricsUnavailableReason.QUOTE_INELIGIBLE: (
                "provider quote failed quote-level value eligibility"
            ),
            MetricsUnavailableReason.EVIDENCE_MISMATCH: (
                "quote and eligibility evidence do not match (quote_id/selection/bout)"
            ),
            MetricsUnavailableReason.CONTEXT_MISMATCH: (
                "provider evidence bout/selection does not match target context"
            ),
            MetricsUnavailableReason.ELIGIBILITY_CUTOFF_MISMATCH: (
                "eligibility evaluated_at must equal valuation_cutoff "
                "(rejecting older/replayed eligibility)"
            ),
            MetricsUnavailableReason.STALE_ELIGIBILITY_EVIDENCE: (
                "eligibility evidence is stale relative to valuation cutoff "
                "(lock/unknown/replaced/review-blocked or quote after cutoff)"
            ),
        }[mismatch]
        return _unavailable(
            mismatch,
            detail=detail,
            target_value_selection_identity=target.value_selection_identity,
            valuation_cutoff=cutoff,
        )
    bout_id = eligibility.resolved_bout_id
    assert bout_id is not None
    opening = _observation_from_quote(quote, bout_id=bout_id)
    assert_matches_context(opening, target)
    return (
        opening,
        PriceSourceKind.PROVIDER_QUOTE,
        _provider_provenance(quote, eligibility, role="opening"),
    )


def _resolve_closing(
    closing: ClosingPriceEvidence,
    *,
    target: ValueSelectionContext,
    opening: SelectionPriceObservation,
) -> (
    tuple[SelectionPriceObservation, PriceProvenanceSummary]
    | MetricsUnavailableReason
):
    if closing.manual_evidence is not None:
        manual = closing.manual_evidence
        if manual.bound_bout_id != target.bout_id:
            return MetricsUnavailableReason.CONTEXT_MISMATCH
        if manual.selection_identity != target.market_selection_identity:
            return MetricsUnavailableReason.CONTEXT_MISMATCH
        if (
            opening.bookmaker_key is not None
            and manual.bookmaker_key != opening.bookmaker_key
            and not closing.allow_cross_book
        ):
            return MetricsUnavailableReason.CROSS_BOOK_CLOSING_DISALLOWED
        if (
            opening.region is not None
            and manual.region != opening.region
            and not closing.allow_cross_book
        ):
            return MetricsUnavailableReason.CROSS_BOOK_CLOSING_DISALLOWED
        obs = _observation_from_manual(manual, bout_id=target.bout_id)
        assert_matches_context(obs, target)
        cross = bool(
            opening.bookmaker_key is not None
            and (
                manual.bookmaker_key != opening.bookmaker_key
                or manual.region != opening.region
            )
        )
        base = _manual_provenance(manual, role="closing")
        summary = PriceProvenanceSummary(
            role=base.role,
            source_kind=base.source_kind,
            quote_id=base.quote_id,
            provider=base.provider,
            bookmaker_key=base.bookmaker_key,
            region=base.region,
            observed_at=base.observed_at,
            eligibility_evaluated_at=base.eligibility_evaluated_at,
            eligibility_reason=base.eligibility_reason,
            eligibility_decision_identity=base.eligibility_decision_identity,
            eligibility_decision_version=base.eligibility_decision_version,
            manual_binding_actor=base.manual_binding_actor,
            manual_binding_source=base.manual_binding_source,
            manual_binding_asserted_at=base.manual_binding_asserted_at,
            cross_book_closing=cross if closing.allow_cross_book else False,
        )
        return obs, summary

    assert closing.quote_evidence is not None
    assert closing.eligibility_evidence is not None
    assert closing.closing_cutoff is not None
    mismatch = _validate_provider_pair(
        closing.quote_evidence,
        closing.eligibility_evidence,
        target=target,
        required_cutoff=closing.closing_cutoff,
    )
    if mismatch is not None:
        return mismatch
    close_quote = closing.quote_evidence
    if (
        opening.bookmaker_key is not None
        and (
            close_quote.bookmaker_key != opening.bookmaker_key
            or close_quote.region != opening.region
        )
        and not closing.allow_cross_book
    ):
        return MetricsUnavailableReason.CROSS_BOOK_CLOSING_DISALLOWED
    bout_id = closing.eligibility_evidence.resolved_bout_id
    assert bout_id is not None
    obs = _observation_from_quote(close_quote, bout_id=bout_id)
    assert_matches_context(obs, target)
    cross = bool(
        opening.bookmaker_key is not None
        and (
            close_quote.bookmaker_key != opening.bookmaker_key
            or close_quote.region != opening.region
        )
    )
    summary = _provider_provenance(
        close_quote,
        closing.eligibility_evidence,
        role="closing",
        cross_book_closing=cross if closing.allow_cross_book else False,
    )
    return obs, summary


def compute_priced_value_metrics(request: PricedValueRequest) -> PricedValueMetrics:
    """Compute EV/CLV/profit/ROI/stake only from typed eligible observed evidence."""
    model_prob = validate_probability(request.model_prob, field="model_prob")
    target = request.target_context
    resolved = _resolve_opening(request)
    if isinstance(resolved, PricedValueMetrics):
        return resolved
    opening, _source, opening_prov = resolved

    offered = opening.price_decimal
    ev = expected_value(model_prob, offered)
    qk = quarter_kelly_fraction(
        model_prob,
        offered,
        cap=request.bankroll_cap_fraction,
    )
    stake = capped_stake_fraction(qk, cap_fraction=request.bankroll_cap_fraction)

    close_ev: float | None = None
    clv: float | None = None
    close_reason = MetricsUnavailableReason.MISSING_CLOSING_PRICE
    clv_reason = MetricsUnavailableReason.MISSING_CLOSING_PRICE
    closing_prov: PriceProvenanceSummary | None = None
    if request.closing_evidence is not None:
        closing_resolved = _resolve_closing(
            request.closing_evidence, target=target, opening=opening
        )
        if isinstance(closing_resolved, MetricsUnavailableReason):
            close_reason = closing_resolved
            clv_reason = closing_resolved
        else:
            closing_obs, closing_prov = closing_resolved
            if closing_obs.observed_at == opening.observed_at:
                close_reason = MetricsUnavailableReason.NON_CLOSING_SAME_TIMESTAMP
                clv_reason = MetricsUnavailableReason.NON_CLOSING_SAME_TIMESTAMP
            elif closing_obs.observed_at < opening.observed_at:
                close_reason = MetricsUnavailableReason.EVIDENCE_MISMATCH
                clv_reason = MetricsUnavailableReason.EVIDENCE_MISMATCH
            else:
                try:
                    close_ev = same_selection_closing_ev(
                        model_prob=model_prob,
                        opening=opening,
                        closing=closing_obs,
                    )
                    clv = same_selection_probability_clv(
                        opening=opening,
                        closing=closing_obs,
                    )
                    close_reason = MetricsUnavailableReason.NONE
                    clv_reason = MetricsUnavailableReason.NONE
                except SelectionMismatchError:
                    close_reason = MetricsUnavailableReason.CONTEXT_MISMATCH
                    clv_reason = MetricsUnavailableReason.CONTEXT_MISMATCH

    profit: float | None = None
    profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    roi: float | None = None
    roi_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    if request.settlement is None:
        profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
        roi_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    elif request.settlement is SettlementResult.UNRESOLVED:
        profit = None
        roi = None
        profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
        roi_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    else:
        profit = flat_unit_profit(
            settlement=request.settlement,
            offered_decimal=offered,
        )
        # Flat 1-unit stake ⇒ realized ROI equals unit profit.
        roi = profit
        profit_reason = MetricsUnavailableReason.NONE
        roi_reason = MetricsUnavailableReason.NONE

    return PricedValueMetrics(
        available=True,
        method=PRICED_METRICS_METHOD,
        version=PRICED_METRICS_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        reason=MetricsUnavailableReason.NONE,
        target_value_selection_identity=target.value_selection_identity,
        expected_value=ev,
        expected_value_reason=MetricsUnavailableReason.NONE,
        closing_ev=close_ev,
        closing_ev_reason=close_reason,
        probability_clv=clv,
        probability_clv_reason=clv_reason,
        probability_clv_unit=CLV_UNIT,
        flat_unit_profit=profit,
        flat_unit_profit_reason=profit_reason,
        realized_roi=roi,
        realized_roi_reason=roi_reason,
        realized_roi_unit=ROI_UNIT,
        quarter_kelly_fraction=qk,
        stake_fraction=stake,
        stake_reason=MetricsUnavailableReason.NONE,
        detail="",
        opening_provenance=opening_prov,
        closing_provenance=closing_prov,
        model_probability_unit=MODEL_PROBABILITY_UNIT,
        bankroll_cap_fraction=request.bankroll_cap_fraction,
        valuation_cutoff=_iso(request.valuation_cutoff),
    )
