"""De-vig complete-set tests across DWCS-200 families (DWCS-204)."""

from __future__ import annotations

import math

import pytest

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    outcomes_for_family,
)
from mma_model.value.devig import (
    DEVIG_METHOD,
    NO_FINISH_OUTCOME_ATOMS,
    OVERROUND_UNIT,
    PROBABILITY_CONDITIONING_UNCONDITIONAL_CATALOG,
    PROBABILITY_CONDITIONING_UNCONDITIONAL_EXPLICIT,
    IncompleteMarketSet,
    proportional_devig,
    try_proportional_devig,
)
from mma_model.value.errors import (
    IncompleteMarketSetError,
    InvalidMarketSetSpecError,
    InvalidOddsError,
)


def _even_prices(keys: tuple[str, ...], decimal: float = 3.0) -> dict[str, float]:
    return {k: decimal for k in keys}


def test_moneyline_complete_sums_to_one() -> None:
    result = proportional_devig(
        {"fighter_a": 1.91, "fighter_b": 1.91},
        family=MarketFamily.MONEYLINE,
    )
    assert result.canonical_complete is True
    assert (
        result.probability_conditioning
        == PROBABILITY_CONDITIONING_UNCONDITIONAL_CATALOG
    )
    assert result.overround_unit == OVERROUND_UNIT
    assert result.method == DEVIG_METHOD
    assert sum(result.fair_probs) == pytest.approx(1.0)
    assert result.fair_probs[0] == pytest.approx(0.5)


def test_totals_line_is_separate_complete_set() -> None:
    result = proportional_devig(
        {"over": 1.90, "under": 1.90},
        family=MarketFamily.TOTALS,
        line_point=2.5,
    )
    assert result.line_point == pytest.approx(2.5)
    assert result.canonical_complete is True
    result.assert_sum_to_one()
    miss = try_proportional_devig(
        {"over": 1.90, "under": 1.90},
        family=MarketFamily.TOTALS,
        line_point=1.5,
    )
    # complete for 1.5 as well when both sides present
    assert not isinstance(miss, IncompleteMarketSet)


@pytest.mark.parametrize(
    ("family", "scheduled_rounds", "line_point"),
    [
        (MarketFamily.MONEYLINE, None, None),
        (MarketFamily.GOES_DISTANCE, None, None),
        (MarketFamily.METHOD, None, None),
        (MarketFamily.FIGHTER_BY_METHOD, None, None),
        (MarketFamily.TOTALS, None, 1.5),
        (MarketFamily.TOTALS, None, 2.5),
    ],
)
def test_exhaustive_families_complete_and_incomplete(
    family: MarketFamily,
    scheduled_rounds: int | None,
    line_point: float | None,
) -> None:
    keys = tuple(
        o.value
        for o in outcomes_for_family(family, scheduled_rounds=scheduled_rounds)
    )
    complete = proportional_devig(
        _even_prices(keys),
        family=family,
        scheduled_rounds=scheduled_rounds,
        line_point=line_point,
    )
    assert complete.canonical_complete is True
    assert (
        complete.probability_conditioning
        == PROBABILITY_CONDITIONING_UNCONDITIONAL_CATALOG
    )
    complete.assert_sum_to_one()

    incomplete_prices = _even_prices(keys[:-1]) if len(keys) > 1 else {}
    miss = try_proportional_devig(
        incomplete_prices,
        family=family,
        scheduled_rounds=scheduled_rounds,
        line_point=line_point,
    )
    assert isinstance(miss, IncompleteMarketSet)
    assert miss.canonical_complete is False
    assert miss.missing_keys
    with pytest.raises(IncompleteMarketSetError):
        proportional_devig(
            incomplete_prices,
            family=family,
            scheduled_rounds=scheduled_rounds,
            line_point=line_point,
        )


def test_method_two_price_subset_is_incomplete() -> None:
    miss = try_proportional_devig(
        {"ko_tko": 3.0, "submission": 3.0},
        family=MarketFamily.METHOD,
    )
    assert isinstance(miss, IncompleteMarketSet)
    assert OutcomeKey.DECISION.value in miss.missing_keys


def test_canonical_exact_round_fails_as_non_exhaustive() -> None:
    keys3 = tuple(
        o.value for o in outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=3)
    )
    with pytest.raises(InvalidMarketSetSpecError, match="not mutually exhaustive"):
        try_proportional_devig(
            _even_prices(keys3),
            family=MarketFamily.EXACT_ROUND,
            scheduled_rounds=3,
        )
    keys5 = tuple(
        o.value for o in outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=5)
    )
    with pytest.raises(InvalidMarketSetSpecError, match="not mutually exhaustive"):
        proportional_devig(
            _even_prices(keys5),
            family=MarketFamily.EXACT_ROUND,
            scheduled_rounds=5,
        )


def test_exact_round_requires_scheduled_rounds_before_non_exhaustive() -> None:
    with pytest.raises(InvalidMarketSetSpecError, match="scheduled_rounds"):
        try_proportional_devig(
            {"round_1": 5.0, "round_2": 5.0, "round_3": 5.0},
            family=MarketFamily.EXACT_ROUND,
        )


def test_round_only_generic_set_rejected_without_no_finish_atom() -> None:
    with pytest.raises(InvalidMarketSetSpecError, match="round-only"):
        try_proportional_devig(
            {"round_1": 4.0, "round_2": 4.0, "round_3": 4.0},
            expected_outcome_keys=("round_1", "round_2", "round_3"),
        )


@pytest.mark.parametrize("no_finish", sorted(NO_FINISH_OUTCOME_ATOMS))
def test_exact_round_with_explicit_no_finish_is_unconditional_non_canonical(
    no_finish: str,
) -> None:
    """Decision-heavy / no-finish complete sets may de-vig without overstating rounds."""
    keys = ("round_1", "round_2", "round_3", no_finish)
    # Heavy decision/no-finish price mass → round fair probs must shrink.
    prices = {
        "round_1": 6.0,
        "round_2": 6.0,
        "round_3": 6.0,
        no_finish: 1.40,
    }
    result = proportional_devig(prices, expected_outcome_keys=keys)
    assert result.canonical_complete is False
    assert (
        result.probability_conditioning
        == PROBABILITY_CONDITIONING_UNCONDITIONAL_EXPLICIT
    )
    result.assert_sum_to_one()
    mapping = result.as_mapping()
    assert mapping[no_finish] > mapping["round_1"]
    assert mapping["round_1"] < 1.0 / 3.0  # not silently inflated as if rounds-only


def test_generic_api_requires_explicit_expected_keys_and_marks_non_canonical() -> None:
    with pytest.raises(InvalidMarketSetSpecError, match="explicit expected_outcome_keys"):
        try_proportional_devig({"a": 2.0, "b": 2.0})
    result = proportional_devig(
        {"a": 2.0, "b": 4.0},
        expected_outcome_keys=("a", "b"),
    )
    assert result.canonical_complete is False
    assert (
        result.probability_conditioning
        == PROBABILITY_CONDITIONING_UNCONDITIONAL_EXPLICIT
    )
    assert result.as_mapping()["a"] == pytest.approx(2.0 / 3.0)


def test_invalid_odds_raise_invalid_odds_error_not_incomplete() -> None:
    with pytest.raises(InvalidOddsError):
        try_proportional_devig(
            {"fighter_a": 1.0, "fighter_b": 2.0},
            family=MarketFamily.MONEYLINE,
        )
    with pytest.raises(InvalidOddsError):
        try_proportional_devig(
            {"fighter_a": math.nan, "fighter_b": 2.0},
            family=MarketFamily.MONEYLINE,
        )
    with pytest.raises(InvalidOddsError):
        proportional_devig(
            {"fighter_a": math.inf, "fighter_b": 2.0},
            family=MarketFamily.MONEYLINE,
        )


def test_blank_keys_and_short_expected_set_are_typed_validation_errors() -> None:
    with pytest.raises(InvalidMarketSetSpecError, match="non-blank"):
        try_proportional_devig(
            {"": 2.0, "b": 2.0},
            expected_outcome_keys=("", "b"),
        )
    with pytest.raises(InvalidMarketSetSpecError, match="at least 2"):
        try_proportional_devig(
            {"a": 2.0},
            expected_outcome_keys=("a",),
        )
