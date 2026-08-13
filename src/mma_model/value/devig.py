"""Complete-set proportional de-vig (DWCS-204)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mma_model.value.errors import IncompleteMarketSetError, InvalidOddsError
from mma_model.value.odds import (
    VALUE_MATH_METHOD,
    VALUE_MATH_VERSION,
    decimal_to_implied_prob,
    validate_decimal_odds,
)

DEVIG_METHOD: Final = "proportional_complete_set"
DEVIG_VERSION: Final = "1.0.0"

_SUM_TOLERANCE: Final = 1e-12


@dataclass(frozen=True)
class DevigResult:
    """Fair probabilities from a complete market set after proportional de-vig."""

    outcome_keys: tuple[str, ...]
    decimal_odds: tuple[float, ...]
    implied_probs: tuple[float, ...]
    fair_probs: tuple[float, ...]
    overround: float
    method: str
    version: str
    value_math_method: str
    value_math_version: str

    def as_mapping(self) -> Mapping[str, float]:
        return dict(zip(self.outcome_keys, self.fair_probs, strict=True))

    def assert_sum_to_one(self) -> None:
        total = sum(self.fair_probs)
        if abs(total - 1.0) > _SUM_TOLERANCE:
            raise AssertionError(
                f"de-vig fair probs must sum to 1 within {_SUM_TOLERANCE}: got {total}"
            )


@dataclass(frozen=True)
class IncompleteMarketSet:
    """Explicit failure for incomplete outcome sets (no silent de-vig)."""

    reason: str
    available_count: int
    method: str
    version: str
    value_math_method: str
    value_math_version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "incomplete",
            "reason": self.reason,
            "available_count": self.available_count,
            "method": self.method,
            "version": self.version,
            "value_math_method": self.value_math_method,
            "value_math_version": self.value_math_version,
        }


def try_proportional_devig(
    decimal_odds: Sequence[float],
    *,
    outcome_keys: Sequence[str] | None = None,
    min_outcomes: int = 2,
) -> DevigResult | IncompleteMarketSet:
    """Proportional de-vig when the set is complete; otherwise an explicit miss."""
    odds_list = list(decimal_odds)
    count = len(odds_list)
    if count < min_outcomes:
        return IncompleteMarketSet(
            reason=f"need at least {min_outcomes} outcomes for a complete set",
            available_count=count,
            method=DEVIG_METHOD,
            version=DEVIG_VERSION,
            value_math_method=VALUE_MATH_METHOD,
            value_math_version=VALUE_MATH_VERSION,
        )

    if outcome_keys is None:
        keys: tuple[str, ...] = tuple(f"outcome_{i}" for i in range(count))
    else:
        keys = tuple(str(k) for k in outcome_keys)
        if len(keys) != count:
            return IncompleteMarketSet(
                reason="outcome_keys length must match decimal_odds length",
                available_count=count,
                method=DEVIG_METHOD,
                version=DEVIG_VERSION,
                value_math_method=VALUE_MATH_METHOD,
                value_math_version=VALUE_MATH_VERSION,
            )
        if len(set(keys)) != len(keys):
            return IncompleteMarketSet(
                reason="outcome_keys must be unique",
                available_count=count,
                method=DEVIG_METHOD,
                version=DEVIG_VERSION,
                value_math_method=VALUE_MATH_METHOD,
                value_math_version=VALUE_MATH_VERSION,
            )

    try:
        validated = tuple(validate_decimal_odds(x) for x in odds_list)
        implied = tuple(decimal_to_implied_prob(x) for x in validated)
    except InvalidOddsError as exc:
        return IncompleteMarketSet(
            reason=str(exc),
            available_count=count,
            method=DEVIG_METHOD,
            version=DEVIG_VERSION,
            value_math_method=VALUE_MATH_METHOD,
            value_math_version=VALUE_MATH_VERSION,
        )

    total_implied = sum(implied)
    if total_implied <= 0.0:
        return IncompleteMarketSet(
            reason="implied probability mass must be positive",
            available_count=count,
            method=DEVIG_METHOD,
            version=DEVIG_VERSION,
            value_math_method=VALUE_MATH_METHOD,
            value_math_version=VALUE_MATH_VERSION,
        )

    fair = tuple(p / total_implied for p in implied)
    result = DevigResult(
        outcome_keys=keys,
        decimal_odds=validated,
        implied_probs=implied,
        fair_probs=fair,
        overround=total_implied - 1.0,
        method=DEVIG_METHOD,
        version=DEVIG_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
    )
    result.assert_sum_to_one()
    return result


def proportional_devig(
    decimal_odds: Sequence[float],
    *,
    outcome_keys: Sequence[str] | None = None,
    min_outcomes: int = 2,
) -> DevigResult:
    """Complete-set proportional de-vig; raises on incomplete / invalid sets."""
    result = try_proportional_devig(
        decimal_odds,
        outcome_keys=outcome_keys,
        min_outcomes=min_outcomes,
    )
    if isinstance(result, IncompleteMarketSet):
        raise IncompleteMarketSetError(result.reason)
    return result
