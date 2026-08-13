"""Priced-only value metrics gated by provenance and eligibility (DWCS-204).

Unpriced price-target rows never receive EV / CLV / ROI / realized profit / stake.
Provider quotes require quote-level eligibility from DWCS-203; the bout match gate
alone is insufficient. User-observed prices use product eligibility (DWCS-202).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from mma_model.markets.settlement import SettlementResult
from mma_model.value.errors import IneligiblePriceError, UnpricedMetricsError
from mma_model.value.ev import (
    closing_ev,
    expected_value,
    flat_unit_profit,
    same_line_probability_clv,
)
from mma_model.value.kelly import (
    DEFAULT_BANKROLL_CAP_FRACTION,
    quarter_kelly_fraction,
)
from mma_model.value.odds import VALUE_MATH_METHOD, VALUE_MATH_VERSION
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
    MISSING_CLOSING_PRICE = "missing_closing_price"
    UNRESOLVED_SETTLEMENT = "unresolved_settlement"


@dataclass(frozen=True)
class PricedValueRequest:
    """Inputs for gated EV / CLV / profit / stake computation."""

    model_prob: float
    source_kind: PriceSourceKind
    offered_decimal: float | None = None
    has_timestamped_price: bool = False
    product_eligible: bool = False
    quote_eligible: bool | None = None
    match_gate_ok: bool = False
    closing_decimal: float | None = None
    settlement: SettlementResult | None = None
    bankroll_cap_fraction: float = DEFAULT_BANKROLL_CAP_FRACTION


@dataclass(frozen=True)
class PricedValueMetrics:
    """Typed priced metrics or an explicit unavailable outcome."""

    available: bool
    method: str
    version: str
    value_math_method: str
    value_math_version: str
    reason: MetricsUnavailableReason
    expected_value: float | None = None
    closing_ev: float | None = None
    probability_clv: float | None = None
    flat_unit_profit: float | None = None
    quarter_kelly_fraction: float | None = None
    stake_fraction: float | None = None
    detail: str = ""

    def require_available(self) -> PricedValueMetrics:
        if self.available:
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
        detail=detail,
    )


def compute_priced_value_metrics(request: PricedValueRequest) -> PricedValueMetrics:
    """Compute EV/CLV/profit/stake only for eligible observed prices."""
    if (
        request.source_kind is PriceSourceKind.UNPRICED
        or not request.has_timestamped_price
        or request.offered_decimal is None
    ):
        return _unavailable(
            MetricsUnavailableReason.UNPRICED_TARGET,
            detail="unpriced targets cannot produce EV/ROI/CLV/realized profit/stake",
        )

    if not request.product_eligible:
        return _unavailable(
            MetricsUnavailableReason.PRODUCT_INELIGIBLE,
            detail="selection failed product gates or maturity",
        )

    if request.source_kind is PriceSourceKind.PROVIDER_QUOTE:
        # Match gate alone is insufficient — require quote-level eligibility.
        if request.quote_eligible is not True:
            if request.match_gate_ok and request.quote_eligible is not True:
                return _unavailable(
                    MetricsUnavailableReason.MATCH_GATE_ONLY,
                    detail=(
                        "bout match gate alone is insufficient; "
                        "quote-level eligibility from DWCS-203 is required"
                    ),
                )
            return _unavailable(
                MetricsUnavailableReason.QUOTE_INELIGIBLE,
                detail="provider quote failed quote-level value eligibility",
            )
    elif request.source_kind is PriceSourceKind.USER_OBSERVED:
        # User-observed path: product eligibility + timestamped price only.
        pass
    else:
        never: PriceSourceKind = request.source_kind
        return _unavailable(
            MetricsUnavailableReason.UNPRICED_TARGET,
            detail=f"unsupported price source: {never!r}",
        )

    offered = float(request.offered_decimal)
    ev = expected_value(request.model_prob, offered)
    qk = quarter_kelly_fraction(
        request.model_prob,
        offered,
        cap=request.bankroll_cap_fraction,
    )
    stake = capped_stake_fraction(qk, cap_fraction=request.bankroll_cap_fraction)

    close_ev: float | None = None
    clv: float | None = None
    if request.closing_decimal is not None:
        close_ev = closing_ev(request.model_prob, request.closing_decimal)
        clv = same_line_probability_clv(
            bet_decimal=offered,
            close_decimal=request.closing_decimal,
        )

    profit: float | None = None
    if request.settlement is not None:
        if request.settlement is SettlementResult.UNRESOLVED:
            return _unavailable(
                MetricsUnavailableReason.UNRESOLVED_SETTLEMENT,
                detail="unresolved settlement cannot produce realized profit",
            )
        profit = flat_unit_profit(
            settlement=request.settlement,
            offered_decimal=offered,
        )

    return PricedValueMetrics(
        available=True,
        method=PRICED_METRICS_METHOD,
        version=PRICED_METRICS_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        reason=MetricsUnavailableReason.NONE,
        expected_value=ev,
        closing_ev=close_ev,
        probability_clv=clv,
        flat_unit_profit=profit,
        quarter_kelly_fraction=qk,
        stake_fraction=stake,
        detail="",
    )
