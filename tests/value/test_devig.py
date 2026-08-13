"""De-vig complete-set tests (DWCS-204)."""

from __future__ import annotations

import pytest

from mma_model.value.devig import (
    DEVIG_METHOD,
    DEVIG_VERSION,
    IncompleteMarketSet,
    proportional_devig,
    try_proportional_devig,
)
from mma_model.value.errors import IncompleteMarketSetError


def test_complete_pair_sums_to_one() -> None:
    result = proportional_devig([1.91, 1.91], outcome_keys=("a", "b"))
    assert result.method == DEVIG_METHOD
    assert result.version == DEVIG_VERSION
    assert sum(result.fair_probs) == pytest.approx(1.0)
    assert result.fair_probs[0] == pytest.approx(0.5)
    assert result.overround == pytest.approx(2.0 / 1.91 - 1.0)


def test_proportional_weights_preserve_ratios() -> None:
    result = proportional_devig([2.0, 4.0], outcome_keys=("fav", "dog"))
    # implied 0.5 and 0.25 → fair 2/3 and 1/3
    assert result.as_mapping()["fav"] == pytest.approx(2.0 / 3.0)
    assert result.as_mapping()["dog"] == pytest.approx(1.0 / 3.0)
    result.assert_sum_to_one()


def test_incomplete_set_is_explicit() -> None:
    miss = try_proportional_devig([1.91], outcome_keys=("only",))
    assert isinstance(miss, IncompleteMarketSet)
    assert miss.available_count == 1
    assert miss.method == DEVIG_METHOD
    with pytest.raises(IncompleteMarketSetError):
        proportional_devig([1.91])
