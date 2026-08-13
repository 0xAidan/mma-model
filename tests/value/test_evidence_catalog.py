"""Catalog validation for value evidence DTOs (DWCS-204)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mma_model.value.errors import IneligiblePriceError
from mma_model.value.evidence import (
    ManualObservedPriceEvidence,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    ValueSelectionContext,
    validate_catalog_selection,
)

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)


def test_validate_catalog_rejects_nonsense_family_outcome() -> None:
    with pytest.raises(IneligiblePriceError, match="unknown market_family"):
        validate_catalog_selection("foo", "bar", None)
    with pytest.raises(IneligiblePriceError, match="unknown outcome_key"):
        validate_catalog_selection("moneyline", "bar", None)
    with pytest.raises(IneligiblePriceError):
        validate_catalog_selection("moneyline", "over", None)


def test_totals_requires_canonical_line_point() -> None:
    with pytest.raises(IneligiblePriceError, match="line_point"):
        validate_catalog_selection("totals", "over", None)
    with pytest.raises(IneligiblePriceError, match="line_point"):
        validate_catalog_selection("totals", "over", 3.5)
    family, outcome, market_id = validate_catalog_selection("totals", "under", 2.5)
    assert family.value == "totals"
    assert outcome.value == "under"
    assert market_id == "totals:under:2.5"


def test_exact_round_rejects_invalid_outcome_for_context() -> None:
    with pytest.raises(IneligiblePriceError):
        ValueSelectionContext(
            bout_id="bout-1",
            market_family="exact_round",
            outcome_key="fighter_a",
        )
    ctx = ValueSelectionContext(
        bout_id="bout-1",
        market_family="exact_round",
        outcome_key="round_1",
    )
    assert ctx.market_selection_identity == "exact_round:round_1"


def test_manual_evidence_rejects_self_consistent_nonsense() -> None:
    with pytest.raises(IneligiblePriceError, match="unknown market_family"):
        ManualObservedPriceEvidence(
            provenance=PriceProvenanceKind.USER_OBSERVED,
            automated=False,
            market_family="foo",
            outcome_key="bar",
            line_point=None,
            selection_identity="foo:bar",
            price_decimal=2.0,
            lifecycle="available",
            observed_at=T0,
            bookmaker_key="book",
            region="us",
            bound_bout_id="bout-1",
        )


def test_provider_evidence_rejects_bad_totals_line() -> None:
    with pytest.raises(IneligiblePriceError, match="line_point"):
        ProviderQuoteEvidence(
            quote_id=1,
            provider="the_odds_api",
            bookmaker_key="book",
            region="us",
            market_family="totals",
            outcome_key="over",
            line_point=9.5,
            selection_identity="totals:over:9.5",
            price_decimal=1.9,
            availability="available",
            observed_at=T0,
            bout_id="bout-1",
        )


def test_eligibility_rejects_non_catalog_selection_identity() -> None:
    with pytest.raises(IneligiblePriceError):
        QuoteEligibilityEvidence(
            quote_id=1,
            eligible=False,
            selection_identity="foo:bar",
            resolved_bout_id=None,
            reason="unmatched",
        )
