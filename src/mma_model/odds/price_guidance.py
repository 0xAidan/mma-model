"""Sportsbook-agnostic price guidance with optional observed-price EV (DWCS-202)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mma_model.domain.markets import (
    MarketFamily,
    MarketMaturity,
    OutcomeKey,
    RecommendationState,
)
from mma_model.markets.price_targets import (
    PriceThresholds,
    RecommendationClassification,
    classify_recommendation,
    compute_price_thresholds,
)
from mma_model.odds.manual_price import (
    MANUAL_SOURCE_LABEL,
    LineLifecycleState,
    ObservedPrice,
    PriceSourceKind,
    compute_exact_ev,
    validate_market_selection,
)

FALLBACK_LABEL = "non-automated sportsbook-agnostic price targets"


class PriceGuidanceSelectionError(ValueError):
    """Invalid selection catalog identity or observation mismatch."""


@dataclass(frozen=True)
class PriceGuidanceRow:
    """One selection's guidance: thresholds when qualified; EV only if priced."""

    market_family: MarketFamily
    outcome_key: OutcomeKey
    line_point: float | None
    maturity: MarketMaturity
    recommendation: RecommendationClassification
    thresholds: PriceThresholds | None
    exact_ev: float | None
    exact_ev_available: bool
    observed_price: ObservedPrice | None
    source_label: str
    automated_line: bool
    fallback_label: str
    line_lifecycle: LineLifecycleState
    claims_bet365: bool

    def as_dict(self) -> dict[str, Any]:
        thresholds = None
        if self.thresholds is not None:
            thresholds = {
                "fair_decimal": self.thresholds.fair_decimal,
                "actionable_decimal": self.thresholds.actionable_decimal,
                "strong_value_decimal": self.thresholds.strong_value_decimal,
                "actionable_ev_target": self.thresholds.actionable_ev_target,
                "strong_value_ev_target": self.thresholds.strong_value_ev_target,
            }
        return {
            "market_family": self.market_family.value,
            "outcome_key": self.outcome_key.value,
            "line_point": self.line_point,
            "maturity": self.maturity.value,
            "recommendation_state": self.recommendation.state.value,
            "recommendation_reason": self.recommendation.reason,
            "thresholds": thresholds,
            "exact_ev": self.exact_ev,
            "exact_ev_available": self.exact_ev_available,
            "source_label": self.source_label,
            "automated_line": self.automated_line,
            "fallback_label": self.fallback_label,
            "line_lifecycle": self.line_lifecycle.value,
            "claims_bet365": self.claims_bet365,
            "observed_price": (
                None if self.observed_price is None else self.observed_price.as_identity_dict()
            ),
        }


def assert_observation_matches_selection(
    observed: ObservedPrice,
    *,
    family: MarketFamily,
    outcome_key: OutcomeKey,
    line_point: float | None,
) -> None:
    """Require observed market/outcome/line identity to match the guidance row."""
    if not observed.matches_selection(
        family=family,
        outcome_key=outcome_key,
        line_point=line_point,
    ):
        raise PriceGuidanceSelectionError(
            "observed price selection mismatch: "
            f"guidance=({family.value}, {outcome_key.value}, {line_point!r}) "
            f"observed=({observed.market_family.value}, "
            f"{observed.outcome_key.value}, {observed.line_point!r})"
        )


def build_price_guidance(
    *,
    family: MarketFamily,
    outcome_key: OutcomeKey,
    maturity: MarketMaturity,
    p50: float,
    p25: float,
    gates_pass: bool,
    observed: ObservedPrice | None = None,
    prob_ev_positive: float | None = None,
    line_point: float | None = None,
) -> PriceGuidanceRow:
    """Build guidance for one selection.

    Selection identity is always validated against the DWCS-200 catalog.
    When an observation is supplied it must match family/outcome/line_point
    exactly before classification or exact EV. Exact EV is suppressed for
    non-qualified or failed-gate selections.
    """
    try:
        validate_market_selection(family, outcome_key, line_point)
    except ValueError as exc:
        raise PriceGuidanceSelectionError(str(exc)) from exc

    if observed is not None:
        assert_observation_matches_selection(
            observed,
            family=family,
            outcome_key=outcome_key,
            line_point=line_point,
        )

    offered_for_classification: float | None = None
    lifecycle = LineLifecycleState.UNKNOWN
    if observed is not None:
        lifecycle = observed.lifecycle
        if (
            observed.lifecycle is LineLifecycleState.AVAILABLE
            and observed.price_decimal is not None
        ):
            offered_for_classification = observed.price_decimal

    recommendation = classify_recommendation(
        family=family,
        maturity=maturity,
        p50=p50,
        p25=p25,
        gates_pass=gates_pass,
        offered_decimal=offered_for_classification,
        prob_ev_positive=prob_ev_positive,
    )

    thresholds = recommendation.thresholds
    if thresholds is None and gates_pass and maturity is MarketMaturity.QUALIFIED:
        thresholds = compute_price_thresholds(p50, p25, family=family)
        if observed is not None and observed.lifecycle is not LineLifecycleState.AVAILABLE:
            recommendation = RecommendationClassification(
                state=RecommendationState.PRICE_TARGET,
                reason=(
                    f"line lifecycle={observed.lifecycle.value}; publish "
                    "sportsbook-agnostic thresholds without exact EV"
                ),
                thresholds=thresholds,
                offered_decimal=None,
            )

    product_eligible = gates_pass and maturity is MarketMaturity.QUALIFIED
    exact_ev: float | None = None
    exact_ev_available = False
    if (
        product_eligible
        and observed is not None
        and observed.lifecycle is LineLifecycleState.AVAILABLE
        and observed.price_decimal is not None
    ):
        exact_ev = compute_exact_ev(p50, observed.price_decimal)
        exact_ev_available = True

    source_label = "sportsbook_agnostic"
    automated_line = False
    claims_bet365 = False
    if observed is not None:
        if observed.source_kind is PriceSourceKind.USER_OBSERVED:
            source_label = MANUAL_SOURCE_LABEL
            automated_line = False
        elif observed.source_kind is PriceSourceKind.REFERENCE_PROVIDER:
            source_label = observed.provider or "reference_provider"
            automated_line = True
        claims_bet365 = observed.bookmaker_key.lower().startswith("bet365")
        if (
            observed.source_kind is PriceSourceKind.REFERENCE_PROVIDER
            and not observed.bookmaker_key.lower().startswith("bet365")
        ):
            claims_bet365 = False

    return PriceGuidanceRow(
        market_family=family,
        outcome_key=outcome_key,
        line_point=line_point,
        maturity=maturity,
        recommendation=recommendation,
        thresholds=thresholds,
        exact_ev=exact_ev,
        exact_ev_available=exact_ev_available,
        observed_price=observed,
        source_label=source_label,
        automated_line=automated_line,
        fallback_label=FALLBACK_LABEL,
        line_lifecycle=lifecycle if observed is not None else LineLifecycleState.UNKNOWN,
        claims_bet365=claims_bet365,
    )


def build_unpriced_price_targets(
    selections: Sequence[Mapping[str, Any]],
) -> list[PriceGuidanceRow]:
    """Emit fair/actionable/strong-value rows for qualified unpriced selections."""
    rows: list[PriceGuidanceRow] = []
    for item in selections:
        line_raw = item.get("line_point")
        line_point = None if line_raw in (None, "") else float(line_raw)
        rows.append(
            build_price_guidance(
                family=_as_family(item["market_family"]),
                outcome_key=_as_outcome(item["outcome_key"]),
                maturity=_as_maturity(item.get("maturity", MarketMaturity.QUALIFIED)),
                p50=float(item["p50"]),
                p25=float(item["p25"]),
                gates_pass=bool(item.get("gates_pass", True)),
                observed=None,
                line_point=line_point,
                prob_ev_positive=item.get("prob_ev_positive"),
            )
        )
    return rows


def _as_family(value: MarketFamily | str) -> MarketFamily:
    return value if isinstance(value, MarketFamily) else MarketFamily(str(value))


def _as_outcome(value: OutcomeKey | str) -> OutcomeKey:
    return value if isinstance(value, OutcomeKey) else OutcomeKey(str(value))


def _as_maturity(value: MarketMaturity | str) -> MarketMaturity:
    return value if isinstance(value, MarketMaturity) else MarketMaturity(str(value))
