"""Complete-set proportional de-vig with DWCS-200 market identity (DWCS-204).

Canonical completeness requires family (+ scheduled rounds / line where relevant)
and the exact catalog outcome-key set. A generic API may de-vig only when an
explicit expected outcome set is supplied; it never claims DWCS-200 completeness
without a family. Totals lines are separate over/under complete sets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    catalog_for_family,
    outcomes_for_family,
)
from mma_model.value.errors import (
    IncompleteMarketSetError,
    InvalidMarketSetSpecError,
    InvalidOddsError,
)
from mma_model.value.odds import (
    VALUE_MATH_METHOD,
    VALUE_MATH_VERSION,
    decimal_to_implied_prob,
    validate_decimal_odds,
)

DEVIG_METHOD: Final = "proportional_complete_set"
DEVIG_VERSION: Final = "1.0.0"
OVERROUND_UNIT: Final = "probability_mass"  # sum(implied) - 1

_SUM_TOLERANCE: Final = 1e-12


@dataclass(frozen=True)
class DevigResult:
    """Fair probabilities from a complete market set after proportional de-vig."""

    outcome_keys: tuple[str, ...]
    decimal_odds: tuple[float, ...]
    implied_probs: tuple[float, ...]
    fair_probs: tuple[float, ...]
    overround: float
    overround_unit: str
    method: str
    version: str
    value_math_method: str
    value_math_version: str
    family: MarketFamily | None
    line_point: float | None
    scheduled_rounds: int | None
    canonical_complete: bool

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
    expected_count: int | None
    missing_keys: tuple[str, ...]
    extra_keys: tuple[str, ...]
    method: str
    version: str
    value_math_method: str
    value_math_version: str
    family: MarketFamily | None
    line_point: float | None
    scheduled_rounds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "incomplete",
            "reason": self.reason,
            "available_count": self.available_count,
            "expected_count": self.expected_count,
            "missing_keys": list(self.missing_keys),
            "extra_keys": list(self.extra_keys),
            "method": self.method,
            "version": self.version,
            "value_math_method": self.value_math_method,
            "value_math_version": self.value_math_version,
            "family": None if self.family is None else self.family.value,
            "line_point": self.line_point,
            "scheduled_rounds": self.scheduled_rounds,
        }


def _normalize_key(key: object) -> str:
    text = key.value if isinstance(key, OutcomeKey) else str(key).strip()
    if not text:
        raise InvalidMarketSetSpecError("outcome keys must be non-blank")
    return text


def _normalize_prices(
    prices_by_outcome: Mapping[object, float],
) -> dict[str, float]:
    if not isinstance(prices_by_outcome, Mapping):
        raise InvalidMarketSetSpecError("prices_by_outcome must be a mapping")
    normalized: dict[str, float] = {}
    for raw_key, raw_price in prices_by_outcome.items():
        key = _normalize_key(raw_key)
        if key in normalized:
            raise InvalidMarketSetSpecError(f"duplicate outcome key: {key!r}")
        normalized[key] = validate_decimal_odds(raw_price, field=f"price[{key}]")
    return normalized


def _expected_keys_for_family(
    family: MarketFamily,
    *,
    scheduled_rounds: int | None,
    line_point: float | None,
) -> tuple[str, ...]:
    catalog = catalog_for_family(family)
    if family is MarketFamily.TOTALS:
        if line_point is None or not catalog.is_valid_line_point(line_point):
            raise InvalidMarketSetSpecError(
                "totals de-vig requires a catalog line_point "
                f"(got {line_point!r}; allowed={list(catalog.line_points)})"
            )
        return tuple(o.value for o in catalog.outcomes)
    if line_point is not None:
        raise InvalidMarketSetSpecError(
            f"{family.value} de-vig must not supply line_point"
        )
    if family is MarketFamily.EXACT_ROUND:
        if scheduled_rounds is None:
            raise InvalidMarketSetSpecError(
                "exact_round de-vig requires scheduled_rounds (3 or 5)"
            )
        return tuple(
            o.value
            for o in outcomes_for_family(family, scheduled_rounds=scheduled_rounds)
        )
    if scheduled_rounds is not None:
        raise InvalidMarketSetSpecError(
            f"{family.value} de-vig must not supply scheduled_rounds"
        )
    return tuple(o.value for o in catalog.outcomes)


def _resolve_expected_keys(
    *,
    family: MarketFamily | None,
    scheduled_rounds: int | None,
    line_point: float | None,
    expected_outcome_keys: Sequence[object] | None,
) -> tuple[tuple[str, ...], bool]:
    """Return (expected_keys, canonical_complete)."""
    if family is not None:
        if expected_outcome_keys is not None:
            raise InvalidMarketSetSpecError(
                "do not pass expected_outcome_keys with a canonical family; "
                "the DWCS-200 catalog is authoritative"
            )
        return (
            _expected_keys_for_family(
                family,
                scheduled_rounds=scheduled_rounds,
                line_point=line_point,
            ),
            True,
        )

    if scheduled_rounds is not None or line_point is not None:
        raise InvalidMarketSetSpecError(
            "scheduled_rounds/line_point require a canonical family"
        )
    if expected_outcome_keys is None:
        raise InvalidMarketSetSpecError(
            "generic de-vig requires explicit expected_outcome_keys; "
            "refusing to claim completeness from price count alone"
        )
    keys = tuple(_normalize_key(k) for k in expected_outcome_keys)
    if len(keys) < 2:
        raise InvalidMarketSetSpecError(
            "expected_outcome_keys must contain at least 2 outcomes"
        )
    if len(set(keys)) != len(keys):
        raise InvalidMarketSetSpecError("expected_outcome_keys must be unique")
    return keys, False


def try_proportional_devig(
    prices_by_outcome: Mapping[object, float],
    *,
    family: MarketFamily | None = None,
    scheduled_rounds: int | None = None,
    line_point: float | None = None,
    expected_outcome_keys: Sequence[object] | None = None,
) -> DevigResult | IncompleteMarketSet:
    """Proportional de-vig when the outcome set is complete; else incomplete.

    Invalid / non-finite odds raise ``InvalidOddsError``. Blank keys and invalid
    market identity raise ``InvalidMarketSetSpecError``. Missing/extra outcomes
    return ``IncompleteMarketSet`` (never silent success).
    """
    expected, canonical = _resolve_expected_keys(
        family=family,
        scheduled_rounds=scheduled_rounds,
        line_point=line_point,
        expected_outcome_keys=expected_outcome_keys,
    )
    prices = _normalize_prices(prices_by_outcome)
    available_keys = set(prices)
    expected_set = set(expected)
    missing = tuple(sorted(expected_set - available_keys))
    extra = tuple(sorted(available_keys - expected_set))
    if missing or extra or len(prices) != len(expected):
        return IncompleteMarketSet(
            reason=(
                "outcome set is not the exact complete expected set "
                f"(missing={list(missing)}, extra={list(extra)})"
            ),
            available_count=len(prices),
            expected_count=len(expected),
            missing_keys=missing,
            extra_keys=extra,
            method=DEVIG_METHOD,
            version=DEVIG_VERSION,
            value_math_method=VALUE_MATH_METHOD,
            value_math_version=VALUE_MATH_VERSION,
            family=family,
            line_point=None if family is not MarketFamily.TOTALS else line_point,
            scheduled_rounds=(
                scheduled_rounds if family is MarketFamily.EXACT_ROUND else None
            ),
        )

    ordered_keys = expected
    validated = tuple(prices[k] for k in ordered_keys)
    implied = tuple(decimal_to_implied_prob(x) for x in validated)
    total_implied = sum(implied)
    if total_implied <= 0.0:
        raise InvalidOddsError("implied probability mass must be positive and finite")

    fair = tuple(p / total_implied for p in implied)
    totals_line = (
        float(line_point)
        if family is MarketFamily.TOTALS and line_point is not None
        else None
    )
    result = DevigResult(
        outcome_keys=ordered_keys,
        decimal_odds=validated,
        implied_probs=implied,
        fair_probs=fair,
        overround=total_implied - 1.0,
        overround_unit=OVERROUND_UNIT,
        method=DEVIG_METHOD,
        version=DEVIG_VERSION,
        value_math_method=VALUE_MATH_METHOD,
        value_math_version=VALUE_MATH_VERSION,
        family=family,
        line_point=totals_line,
        scheduled_rounds=(
            scheduled_rounds if family is MarketFamily.EXACT_ROUND else None
        ),
        canonical_complete=canonical,
    )
    result.assert_sum_to_one()
    return result


def proportional_devig(
    prices_by_outcome: Mapping[object, float],
    *,
    family: MarketFamily | None = None,
    scheduled_rounds: int | None = None,
    line_point: float | None = None,
    expected_outcome_keys: Sequence[object] | None = None,
) -> DevigResult:
    """Complete-set proportional de-vig; raises on incomplete sets."""
    result = try_proportional_devig(
        prices_by_outcome,
        family=family,
        scheduled_rounds=scheduled_rounds,
        line_point=line_point,
        expected_outcome_keys=expected_outcome_keys,
    )
    if isinstance(result, IncompleteMarketSet):
        raise IncompleteMarketSetError(result.reason)
    return result
