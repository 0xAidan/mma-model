"""Exhaustive market family / outcome catalog tests (DWCS-200)."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import (
    EXACT_ROUND_OUTCOMES_FIVE,
    EXACT_ROUND_OUTCOMES_THREE,
    FIGHTER_BY_METHOD_OUTCOMES,
    GOES_DISTANCE_OUTCOMES,
    METHOD_OUTCOMES,
    MONEYLINE_OUTCOMES,
    TOTALS_LINE_POINTS,
    TOTALS_OUTCOMES,
    V1_MARKET_FAMILIES,
    MarketFamily,
    MarketMaturity,
    OutcomeKey,
    PriceThresholdKind,
    RecommendationState,
    assert_known_outcome,
    catalog_for_family,
    outcomes_for_family,
)


def test_v1_families_are_exhaustive() -> None:
    assert set(V1_MARKET_FAMILIES) == {
        MarketFamily.MONEYLINE,
        MarketFamily.TOTALS,
        MarketFamily.GOES_DISTANCE,
        MarketFamily.METHOD,
        MarketFamily.FIGHTER_BY_METHOD,
        MarketFamily.EXACT_ROUND,
    }
    assert len(MarketFamily) == 6


def test_every_family_has_canonical_outcomes() -> None:
    expected = {
        MarketFamily.MONEYLINE: MONEYLINE_OUTCOMES,
        MarketFamily.TOTALS: TOTALS_OUTCOMES,
        MarketFamily.GOES_DISTANCE: GOES_DISTANCE_OUTCOMES,
        MarketFamily.METHOD: METHOD_OUTCOMES,
        MarketFamily.FIGHTER_BY_METHOD: FIGHTER_BY_METHOD_OUTCOMES,
        MarketFamily.EXACT_ROUND: EXACT_ROUND_OUTCOMES_FIVE,
    }
    for family in MarketFamily:
        catalog = catalog_for_family(family)
        assert catalog.family is family
        assert catalog.outcomes == expected[family]
        assert catalog.outcomes, f"{family} must have outcomes"
        assert outcomes_for_family(family) == catalog.outcomes


def test_totals_line_points() -> None:
    catalog = catalog_for_family(MarketFamily.TOTALS)
    assert catalog.line_points == TOTALS_LINE_POINTS == (1.5, 2.5)
    assert catalog.requires_line_point() is True
    assert catalog.is_valid_line_point(1.5) is True
    assert catalog.is_valid_line_point(2.5) is True
    assert catalog.is_valid_line_point(3.5) is False
    assert catalog.is_valid_line_point(None) is False


def test_non_totals_reject_line_points() -> None:
    for family in MarketFamily:
        if family is MarketFamily.TOTALS:
            continue
        catalog = catalog_for_family(family)
        assert catalog.requires_line_point() is False
        assert catalog.is_valid_line_point(None) is True
        assert catalog.is_valid_line_point(1.5) is False


def test_exact_round_shrinks_to_scheduled_rounds() -> None:
    assert outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=3) == (
        EXACT_ROUND_OUTCOMES_THREE
    )
    assert outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=5) == (
        EXACT_ROUND_OUTCOMES_FIVE
    )
    with pytest.raises(ValueError, match="unsupported scheduled_rounds"):
        outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=4)


def test_unknown_outcome_hard_fails() -> None:
    with pytest.raises(ValueError, match="not valid"):
        assert_known_outcome(MarketFamily.MONEYLINE, OutcomeKey.OVER)


def test_unknown_enum_variants_fail() -> None:
    with pytest.raises(ValueError):
        MarketFamily("not_a_market")
    with pytest.raises(ValueError):
        OutcomeKey("not_an_outcome")
    with pytest.raises(ValueError):
        MarketMaturity("maybe")
    with pytest.raises(ValueError):
        RecommendationState("hedge")
    with pytest.raises(ValueError):
        PriceThresholdKind("mid")


def test_moneyline_default_maturity_qualified() -> None:
    assert (
        catalog_for_family(MarketFamily.MONEYLINE).default_maturity
        is MarketMaturity.QUALIFIED
    )
    assert (
        catalog_for_family(MarketFamily.METHOD).default_maturity
        is MarketMaturity.EXPERIMENTAL
    )


def test_outcome_keys_cover_all_catalog_members() -> None:
    seen: set[OutcomeKey] = set()
    for family in MarketFamily:
        seen.update(catalog_for_family(family).outcomes)
    # Every catalog outcome must be a declared OutcomeKey member.
    assert seen <= set(OutcomeKey)
