"""Swap transform and spec hashing."""

from __future__ import annotations

from mma_model.features.spec import FEATURE_NAMES, spec_hash, swap_values, vector_from_mapping


def test_spec_hash_is_stable() -> None:
    first = spec_hash()
    second = spec_hash()
    assert first == second
    assert len(first) == 64
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))


def test_swap_negates_diffs_and_swaps_pairs() -> None:
    values = vector_from_mapping({name: float(idx + 1) for idx, name in enumerate(FEATURE_NAMES)})
    swapped = swap_values(values)
    twice = swap_values(swapped)
    assert twice == values
    by_name = dict(zip(FEATURE_NAMES, values, strict=True))
    swapped_map = dict(zip(FEATURE_NAMES, swapped, strict=True))
    assert swapped_map["rating_diff"] == -by_name["rating_diff"]
    assert swapped_map["rating_a"] == by_name["rating_b"]
    assert swapped_map["rating_b"] == by_name["rating_a"]
    assert swapped_map["scheduled_rounds"] == by_name["scheduled_rounds"]
    assert swapped_map["data_completeness"] == by_name["data_completeness"]
