"""Priced-only value metrics gated by typed provenance evidence (DWCS-204).

Unpriced price-target rows never receive EV / CLV / ROI / realized profit / stake.
Caller booleans alone cannot grant metrics. ``PricedValueRequest`` names a
bout-scoped ``ValueSelectionContext``. Manual prices require DWCS-202 evidence
bound to that bout; provider quotes require quote evidence plus matching
DWCS-203 eligibility whose resolved bout equals the target.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import (
    IneligiblePriceError,
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


@dataclass(frozen=True)
class PricedValueRequest:
    """Inputs for gated EV / CLV / profit / ROI / stake computation.

    ``target_context`` is required. Provide either ``manual_evidence`` (DWCS-202)
    or both ``quote_evidence`` and ``eligibility_evidence`` (DWCS-203). Closing
    prices use ``closing_evidence`` with the same provenance gates.
    """

    model_prob: float
    target_context: ValueSelectionContext
    product_eligible: bool = False
    manual_evidence: ManualObservedPriceEvidence | None = None
    quote_evidence: ProviderQuoteEvidence | None = None
    eligibility_evidence: QuoteEligibilityEvidence | None = None
    closing_evidence: ClosingPriceEvidence | None = None
    settlement: SettlementResult | None = None
    bankroll_cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION


@dataclass(frozen=True)
class PricedValueMetrics:
    """Typed priced metrics with per-metric availability reasons."""

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

    def require_available(self) -> PricedValueMetrics:
        if self.available and self.expected_value is not None:
            return self
        if self.reason is MetricsUnavailableReason.UNPRICED_TARGET:
            raise UnpricedMetricsError(self.detail or self.reason.value)
        raise IneligiblePriceError(self.detail or self.reason.value)


def _unavailable(
    reason: MetricsUnavailableReason,
    *,
    detail: str = "",
    target_value_selection_identity: str | None = None,
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
) -> MetricsUnavailableReason | None:
    if int(quote.quote_id) != int(eligibility.quote_id):
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if quote.selection_identity != eligibility.selection_identity:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if quote.selection_identity != target.market_selection_identity:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    if eligibility.selection_identity != target.market_selection_identity:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    if not eligibility.eligible:
        if eligibility.reason in {"unmatched", "latest_match_not_matched"}:
            return MetricsUnavailableReason.MATCH_GATE_ONLY
        return MetricsUnavailableReason.QUOTE_INELIGIBLE
    # eligible path already requires resolved_bout_id + reason=none at DTO level
    assert eligibility.resolved_bout_id is not None
    if eligibility.resolved_bout_id != target.bout_id:
        return MetricsUnavailableReason.CONTEXT_MISMATCH
    if quote.bout_id is not None and quote.bout_id != eligibility.resolved_bout_id:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    return None


def _resolve_opening(
    request: PricedValueRequest,
) -> tuple[SelectionPriceObservation, PriceSourceKind] | PricedValueMetrics:
    target = request.target_context
    manual = request.manual_evidence
    quote = request.quote_evidence
    eligibility = request.eligibility_evidence

    if manual is None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.UNPRICED_TARGET,
            detail="unpriced targets cannot produce EV/ROI/CLV/realized profit/stake",
            target_value_selection_identity=target.value_selection_identity,
        )
    if manual is not None and (quote is not None or eligibility is not None):
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="provide either manual_evidence or quote+eligibility evidence, not both",
            target_value_selection_identity=target.value_selection_identity,
        )
    if quote is not None and eligibility is None:
        return _unavailable(
            MetricsUnavailableReason.QUOTE_INELIGIBLE,
            detail="provider quotes require QuoteEligibilityEvidence from DWCS-203",
            target_value_selection_identity=target.value_selection_identity,
        )
    if eligibility is not None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="eligibility evidence requires matching ProviderQuoteEvidence",
            target_value_selection_identity=target.value_selection_identity,
        )
    if not request.product_eligible:
        return _unavailable(
            MetricsUnavailableReason.PRODUCT_INELIGIBLE,
            detail="selection failed product gates or maturity",
            target_value_selection_identity=target.value_selection_identity,
        )

    if manual is not None:
        if manual.price_role is not PriceObservationRole.OPENING:
            return _unavailable(
                MetricsUnavailableReason.EVIDENCE_MISMATCH,
                detail="opening manual evidence requires price_role=opening",
                target_value_selection_identity=target.value_selection_identity,
            )
        if manual.bound_bout_id is None:
            return _unavailable(
                MetricsUnavailableReason.UNBOUND_MANUAL_PRICE,
                detail=(
                    "manual price is unbound; bind to the target canonical bout "
                    "before exact EV/ROI/CLV"
                ),
                target_value_selection_identity=target.value_selection_identity,
            )
        if manual.bound_bout_id != target.bout_id:
            return _unavailable(
                MetricsUnavailableReason.CONTEXT_MISMATCH,
                detail=(
                    "manual bound_bout_id does not match target context bout "
                    f"({manual.bound_bout_id!r} vs {target.bout_id!r})"
                ),
                target_value_selection_identity=target.value_selection_identity,
            )
        if manual.selection_identity != target.market_selection_identity:
            return _unavailable(
                MetricsUnavailableReason.CONTEXT_MISMATCH,
                detail="manual market selection does not match target context",
                target_value_selection_identity=target.value_selection_identity,
            )
        opening = _observation_from_manual(manual, bout_id=target.bout_id)
        assert_matches_context(opening, target)
        return opening, PriceSourceKind.USER_OBSERVED

    assert quote is not None and eligibility is not None
    if quote.price_role is not PriceObservationRole.OPENING:
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="opening provider evidence requires price_role=opening",
            target_value_selection_identity=target.value_selection_identity,
        )
    mismatch = _validate_provider_pair(quote, eligibility, target=target)
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
        }[mismatch]
        return _unavailable(
            mismatch,
            detail=detail,
            target_value_selection_identity=target.value_selection_identity,
        )
    bout_id = eligibility.resolved_bout_id
    assert bout_id is not None
    opening = _observation_from_quote(quote, bout_id=bout_id)
    assert_matches_context(opening, target)
    return opening, PriceSourceKind.PROVIDER_QUOTE


def _resolve_closing(
    closing: ClosingPriceEvidence,
    *,
    target: ValueSelectionContext,
) -> SelectionPriceObservation | MetricsUnavailableReason:
    if closing.manual_evidence is not None:
        manual = closing.manual_evidence
        if manual.bound_bout_id != target.bout_id:
            return MetricsUnavailableReason.CONTEXT_MISMATCH
        if manual.selection_identity != target.market_selection_identity:
            return MetricsUnavailableReason.CONTEXT_MISMATCH
        obs = _observation_from_manual(manual, bout_id=target.bout_id)
        assert_matches_context(obs, target)
        return obs

    assert closing.quote_evidence is not None
    assert closing.eligibility_evidence is not None
    mismatch = _validate_provider_pair(
        closing.quote_evidence,
        closing.eligibility_evidence,
        target=target,
    )
    if mismatch is not None:
        return mismatch
    bout_id = closing.eligibility_evidence.resolved_bout_id
    assert bout_id is not None
    obs = _observation_from_quote(closing.quote_evidence, bout_id=bout_id)
    assert_matches_context(obs, target)
    return obs


def compute_priced_value_metrics(request: PricedValueRequest) -> PricedValueMetrics:
    """Compute EV/CLV/profit/ROI/stake only from typed eligible observed evidence."""
    model_prob = validate_probability(request.model_prob, field="model_prob")
    target = request.target_context
    resolved = _resolve_opening(request)
    if isinstance(resolved, PricedValueMetrics):
        return resolved
    opening, _source = resolved

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
    if request.closing_evidence is not None:
        closing_resolved = _resolve_closing(
            request.closing_evidence, target=target
        )
        if isinstance(closing_resolved, MetricsUnavailableReason):
            close_reason = closing_resolved
            clv_reason = closing_resolved
        else:
            closing_obs = closing_resolved
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
    )
