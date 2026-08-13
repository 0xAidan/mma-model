"""Priced-only value metrics gated by typed provenance evidence (DWCS-204).

Unpriced price-target rows never receive EV / CLV / ROI / realized profit / stake.
Caller booleans alone cannot grant metrics. Manual prices require DWCS-202
``ManualObservedPriceEvidence``; provider quotes require quote evidence plus a
matching DWCS-203 ``QuoteEligibilityEvidence``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import IneligiblePriceError, UnpricedMetricsError
from mma_model.value.ev import (
    CLV_UNIT,
    expected_value,
    flat_unit_profit,
    same_selection_closing_ev,
    same_selection_probability_clv,
)
from mma_model.value.evidence import (
    ManualObservedPriceEvidence,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    SelectionPriceObservation,
)
from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    quarter_kelly_fraction,
)
from mma_model.value.odds import VALUE_MATH_METHOD, VALUE_MATH_VERSION, validate_probability
from mma_model.value.portfolio import capped_stake_fraction

PRICED_METRICS_METHOD: Final = "priced_value_metrics"
PRICED_METRICS_VERSION: Final = "1.0.0"


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
    MISSING_CLOSING_PRICE = "missing_closing_price"
    UNRESOLVED_SETTLEMENT = "unresolved_settlement"


@dataclass(frozen=True)
class PricedValueRequest:
    """Inputs for gated EV / CLV / profit / stake computation.

    Provide either ``manual_evidence`` (DWCS-202) or both ``quote_evidence`` and
    ``eligibility_evidence`` (DWCS-203). Boolean shortcuts are not accepted.
    """

    model_prob: float
    product_eligible: bool = False
    manual_evidence: ManualObservedPriceEvidence | None = None
    quote_evidence: ProviderQuoteEvidence | None = None
    eligibility_evidence: QuoteEligibilityEvidence | None = None
    closing_observation: SelectionPriceObservation | None = None
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
) -> PricedValueMetrics:
    return PricedValueMetrics(
        available=False,
        method=PRICED_METRICS_METHOD,
        version=PRICED_METRICS_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        reason=reason,
        expected_value=None,
        expected_value_reason=reason,
        closing_ev=None,
        closing_ev_reason=reason,
        probability_clv=None,
        probability_clv_reason=reason,
        flat_unit_profit=None,
        flat_unit_profit_reason=reason,
        quarter_kelly_fraction=None,
        stake_fraction=None,
        stake_reason=reason,
        detail=detail,
    )


def _opening_observation_from_manual(
    evidence: ManualObservedPriceEvidence,
) -> SelectionPriceObservation:
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


def _opening_observation_from_quote(
    evidence: ProviderQuoteEvidence,
) -> SelectionPriceObservation:
    return SelectionPriceObservation(
        provenance=PriceProvenanceKind.PROVIDER_QUOTE,
        market_family=evidence.market_family,
        outcome_key=evidence.outcome_key,
        line_point=evidence.line_point,
        selection_identity=evidence.selection_identity,
        price_decimal=evidence.price_decimal,
        observed_at=evidence.observed_at,
        lifecycle_or_availability=evidence.availability,
    )


def _validate_provider_pair(
    quote: ProviderQuoteEvidence,
    eligibility: QuoteEligibilityEvidence,
) -> MetricsUnavailableReason | None:
    if int(quote.quote_id) != int(eligibility.quote_id):
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if quote.selection_identity != eligibility.selection_identity:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if (
        eligibility.resolved_bout_id is not None
        and quote.bout_id is not None
        and eligibility.resolved_bout_id != quote.bout_id
    ):
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    if not eligibility.eligible:
        # Distinguish "blocked despite somehow claiming match" via reason text.
        if eligibility.reason in {"unmatched", "latest_match_not_matched"}:
            return MetricsUnavailableReason.MATCH_GATE_ONLY
        return MetricsUnavailableReason.QUOTE_INELIGIBLE
    if eligibility.resolved_bout_id is None:
        return MetricsUnavailableReason.MATCH_GATE_ONLY
    if quote.bout_id is not None and quote.bout_id != eligibility.resolved_bout_id:
        return MetricsUnavailableReason.EVIDENCE_MISMATCH
    return None


def compute_priced_value_metrics(request: PricedValueRequest) -> PricedValueMetrics:
    """Compute EV/CLV/profit/stake only from typed eligible observed evidence."""
    model_prob = validate_probability(request.model_prob, field="model_prob")

    manual = request.manual_evidence
    quote = request.quote_evidence
    eligibility = request.eligibility_evidence

    if manual is None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.UNPRICED_TARGET,
            detail="unpriced targets cannot produce EV/ROI/CLV/realized profit/stake",
        )
    if manual is not None and (quote is not None or eligibility is not None):
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="provide either manual_evidence or quote+eligibility evidence, not both",
        )
    if quote is not None and eligibility is None:
        return _unavailable(
            MetricsUnavailableReason.QUOTE_INELIGIBLE,
            detail="provider quotes require QuoteEligibilityEvidence from DWCS-203",
        )
    if eligibility is not None and quote is None:
        return _unavailable(
            MetricsUnavailableReason.EVIDENCE_MISMATCH,
            detail="eligibility evidence requires matching ProviderQuoteEvidence",
        )

    if not request.product_eligible:
        return _unavailable(
            MetricsUnavailableReason.PRODUCT_INELIGIBLE,
            detail="selection failed product gates or maturity",
        )

    if manual is not None:
        opening = _opening_observation_from_manual(manual)
        source = PriceSourceKind.USER_OBSERVED
    else:
        assert quote is not None and eligibility is not None
        mismatch = _validate_provider_pair(quote, eligibility)
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
                    "quote and eligibility evidence do not match "
                    "(quote_id/selection/bout)"
                ),
            }[mismatch]
            return _unavailable(mismatch, detail=detail)
        # Eligible quote must bind to the eligibility bout when quote bout is unset.
        if quote.bout_id is None:
            quote = ProviderQuoteEvidence(
                quote_id=quote.quote_id,
                market_family=quote.market_family,
                outcome_key=quote.outcome_key,
                line_point=quote.line_point,
                selection_identity=quote.selection_identity,
                price_decimal=quote.price_decimal,
                availability=quote.availability,
                observed_at=quote.observed_at,
                bout_id=eligibility.resolved_bout_id,
                bookmaker_key=quote.bookmaker_key,
                region=quote.region,
            )
        opening = _opening_observation_from_quote(quote)
        source = PriceSourceKind.PROVIDER_QUOTE

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
    if request.closing_observation is not None:
        close_ev = same_selection_closing_ev(
            model_prob=model_prob,
            opening=opening,
            closing=request.closing_observation,
        )
        clv = same_selection_probability_clv(
            opening=opening,
            closing=request.closing_observation,
        )
        close_reason = MetricsUnavailableReason.NONE
        clv_reason = MetricsUnavailableReason.NONE

    profit: float | None = None
    profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    if request.settlement is None:
        profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    elif request.settlement is SettlementResult.UNRESOLVED:
        profit = None
        profit_reason = MetricsUnavailableReason.UNRESOLVED_SETTLEMENT
    else:
        profit = flat_unit_profit(
            settlement=request.settlement,
            offered_decimal=offered,
        )
        profit_reason = MetricsUnavailableReason.NONE

    _ = source  # provenance recorded via opening observation
    return PricedValueMetrics(
        available=True,
        method=PRICED_METRICS_METHOD,
        version=PRICED_METRICS_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        reason=MetricsUnavailableReason.NONE,
        expected_value=ev,
        expected_value_reason=MetricsUnavailableReason.NONE,
        closing_ev=close_ev,
        closing_ev_reason=close_reason,
        probability_clv=clv,
        probability_clv_reason=clv_reason,
        probability_clv_unit=CLV_UNIT,
        flat_unit_profit=profit,
        flat_unit_profit_reason=profit_reason,
        quarter_kelly_fraction=qk,
        stake_fraction=stake,
        stake_reason=MetricsUnavailableReason.NONE,
        detail="",
    )
