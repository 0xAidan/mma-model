"""Schema validation rules for dashboard contracts (DWCS-500)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mma_model.publish.schema import (
    ConfirmedPriceMetrics,
    HistoryPoint,
    MatchupPrices,
    ObservedPriceView,
    PerformanceLaneView,
    PriceAvailability,
    PriceTargetOnlyMetrics,
    QuoteSourceTypeView,
    validate_document,
)


def test_exact_ev_without_observed_price_rejected() -> None:
    with pytest.raises(ValidationError, match="exact_ev"):
        MatchupPrices(
            model_fair_probability=0.55,
            fair_decimal=2.0,
            fair_american=100.0,
            actionable_decimal=2.1,
            actionable_american=110.0,
            strong_value_decimal=2.2,
            strong_value_american=120.0,
            observed=None,
            exact_ev=0.1,
            price_availability=PriceAvailability.AVAILABLE,
        )


def test_exact_ev_with_observed_price_ok() -> None:
    prices = MatchupPrices(
        model_fair_probability=0.55,
        fair_decimal=2.0,
        fair_american=100.0,
        actionable_decimal=2.1,
        actionable_american=110.0,
        strong_value_decimal=2.2,
        strong_value_american=120.0,
        observed=ObservedPriceView(
            decimal_odds=2.4,
            american_odds=140.0,
            sportsbook="fixture_book",
            source_type=QuoteSourceTypeView.AUTOMATIC,
            timestamp="2026-08-11T17:00:05Z",
        ),
        exact_ev=0.32,
        price_availability=PriceAvailability.AVAILABLE,
    )
    assert prices.exact_ev == 0.32


def test_roi_clv_illegal_on_price_target_history() -> None:
    with pytest.raises(ValidationError, match="roi/clv"):
        HistoryPoint(
            at="2026-08-11T17:00:05Z",
            label="pt-only",
            bucket="price_target_only",
            lane=PerformanceLaneView.PAPER,
            flat_unit_roi=0.1,
            clv=0.02,
        )


def test_confirmed_price_metrics_allow_roi_clv() -> None:
    metrics = ConfirmedPriceMetrics(
        pick_count=3,
        hit_rate=0.5,
        flat_unit_roi=0.12,
        clv=0.03,
        drawdown=-0.2,
    )
    assert metrics.flat_unit_roi == 0.12
    # Price-target-only model has no roi/clv fields.
    assert "flat_unit_roi" not in PriceTargetOnlyMetrics.model_fields
    assert "clv" not in PriceTargetOnlyMetrics.model_fields


def test_extra_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_document(
            "release.json",
            {
                "schema_version": 1,
                "contract_id": "dwcs_dashboard",
                "contract_version": "1.0.0",
                "ticket": "DWCS-500",
                "series": "dwcs",
                "release_id": "r1",
                "as_of": "2026-08-11T17:00:05Z",
                "files": [],
                "unexpected": True,
            },
        )
