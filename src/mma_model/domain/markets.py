"""Typed market, outcome, maturity, and price-guidance contracts (DWCS-200).

Sportsbook-agnostic price thresholds and recommendation states are first-class.
Exact bookmaker lines are optional enrichment handled by later tickets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class MarketFamily(StrEnum):
    """v1 betting market families."""

    MONEYLINE = "moneyline"
    TOTALS = "totals"
    GOES_DISTANCE = "goes_distance"
    METHOD = "method"
    FIGHTER_BY_METHOD = "fighter_by_method"
    EXACT_ROUND = "exact_round"


class MarketMaturity(StrEnum):
    """Whether a family may emit confirmed-value or price-target guidance."""

    QUALIFIED = "qualified"
    EXPERIMENTAL = "experimental"
    BLOCKED = "blocked"


class OutcomeKey(StrEnum):
    """Canonical outcome keys across all v1 market families."""

    FIGHTER_A = "fighter_a"
    FIGHTER_B = "fighter_b"
    GOES_DISTANCE = "goes_distance"
    INSIDE_DISTANCE = "inside_distance"
    OVER = "over"
    UNDER = "under"
    KO_TKO = "ko_tko"
    SUBMISSION = "submission"
    DECISION = "decision"
    OTHER_STOPPAGE = "other_stoppage"
    A_KO_TKO = "a_ko_tko"
    A_SUBMISSION = "a_submission"
    A_OTHER_STOPPAGE = "a_other_stoppage"
    A_DECISION = "a_decision"
    B_KO_TKO = "b_ko_tko"
    B_SUBMISSION = "b_submission"
    B_OTHER_STOPPAGE = "b_other_stoppage"
    B_DECISION = "b_decision"
    ROUND_1 = "round_1"
    ROUND_2 = "round_2"
    ROUND_3 = "round_3"
    ROUND_4 = "round_4"
    ROUND_5 = "round_5"


class PriceThresholdKind(StrEnum):
    """Sportsbook-agnostic model price thresholds (always computable)."""

    FAIR = "fair"
    ACTIONABLE = "actionable"
    STRONG_VALUE = "strong_value"


class RecommendationState(StrEnum):
    """Product classification independent of any specific bookmaker feed."""

    CONFIRMED_VALUE = "confirmed_value"
    PRICE_TARGET = "price_target"
    NO_BET = "no_bet"


V1_MARKET_FAMILIES: Final[tuple[MarketFamily, ...]] = tuple(MarketFamily)

# Settlement voids these families on draw / technical draw.
VOID_ON_DRAW_FAMILIES: Final[frozenset[MarketFamily]] = frozenset(
    {
        MarketFamily.MONEYLINE,
        MarketFamily.METHOD,
        MarketFamily.FIGHTER_BY_METHOD,
    }
)

MONEYLINE_OUTCOMES: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.FIGHTER_A,
    OutcomeKey.FIGHTER_B,
)
GOES_DISTANCE_OUTCOMES: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.GOES_DISTANCE,
    OutcomeKey.INSIDE_DISTANCE,
)
TOTALS_OUTCOMES: Final[tuple[OutcomeKey, ...]] = (OutcomeKey.OVER, OutcomeKey.UNDER)
TOTALS_LINE_POINTS: Final[tuple[float, ...]] = (1.5, 2.5)
METHOD_OUTCOMES: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.KO_TKO,
    OutcomeKey.SUBMISSION,
    OutcomeKey.DECISION,
    OutcomeKey.OTHER_STOPPAGE,
)
FIGHTER_BY_METHOD_OUTCOMES: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.A_KO_TKO,
    OutcomeKey.A_SUBMISSION,
    OutcomeKey.A_OTHER_STOPPAGE,
    OutcomeKey.A_DECISION,
    OutcomeKey.B_KO_TKO,
    OutcomeKey.B_SUBMISSION,
    OutcomeKey.B_OTHER_STOPPAGE,
    OutcomeKey.B_DECISION,
)
EXACT_ROUND_OUTCOMES_THREE: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.ROUND_1,
    OutcomeKey.ROUND_2,
    OutcomeKey.ROUND_3,
)
EXACT_ROUND_OUTCOMES_FIVE: Final[tuple[OutcomeKey, ...]] = (
    OutcomeKey.ROUND_1,
    OutcomeKey.ROUND_2,
    OutcomeKey.ROUND_3,
    OutcomeKey.ROUND_4,
    OutcomeKey.ROUND_5,
)


@dataclass(frozen=True)
class MarketOutcomeCatalog:
    """Complete outcome set (and optional line points) for one market family."""

    family: MarketFamily
    outcomes: tuple[OutcomeKey, ...]
    line_points: tuple[float, ...] = ()
    default_maturity: MarketMaturity = MarketMaturity.EXPERIMENTAL

    def requires_line_point(self) -> bool:
        return self.family is MarketFamily.TOTALS

    def is_complete_outcome(self, outcome: OutcomeKey) -> bool:
        return outcome in self.outcomes

    def is_valid_line_point(self, line_point: float | None) -> bool:
        if not self.requires_line_point():
            return line_point is None
        return line_point is not None and float(line_point) in self.line_points


_CATALOGS: Final[Mapping[MarketFamily, MarketOutcomeCatalog]] = {
    MarketFamily.MONEYLINE: MarketOutcomeCatalog(
        family=MarketFamily.MONEYLINE,
        outcomes=MONEYLINE_OUTCOMES,
        default_maturity=MarketMaturity.QUALIFIED,
    ),
    MarketFamily.TOTALS: MarketOutcomeCatalog(
        family=MarketFamily.TOTALS,
        outcomes=TOTALS_OUTCOMES,
        line_points=TOTALS_LINE_POINTS,
    ),
    MarketFamily.GOES_DISTANCE: MarketOutcomeCatalog(
        family=MarketFamily.GOES_DISTANCE,
        outcomes=GOES_DISTANCE_OUTCOMES,
    ),
    MarketFamily.METHOD: MarketOutcomeCatalog(
        family=MarketFamily.METHOD,
        outcomes=METHOD_OUTCOMES,
    ),
    MarketFamily.FIGHTER_BY_METHOD: MarketOutcomeCatalog(
        family=MarketFamily.FIGHTER_BY_METHOD,
        outcomes=FIGHTER_BY_METHOD_OUTCOMES,
    ),
    MarketFamily.EXACT_ROUND: MarketOutcomeCatalog(
        family=MarketFamily.EXACT_ROUND,
        outcomes=EXACT_ROUND_OUTCOMES_FIVE,
    ),
}


def catalog_for_family(family: MarketFamily) -> MarketOutcomeCatalog:
    """Return the frozen outcome catalog for a v1 market family."""
    try:
        return _CATALOGS[family]
    except KeyError as exc:  # pragma: no cover - StrEnum prevents unknown members
        raise ValueError(f"unknown market family: {family!r}") from exc


def outcomes_for_family(
    family: MarketFamily,
    *,
    scheduled_rounds: int | None = None,
) -> tuple[OutcomeKey, ...]:
    """Canonical outcomes; exact-round set shrinks to scheduled rounds when given."""
    catalog = catalog_for_family(family)
    if family is not MarketFamily.EXACT_ROUND or scheduled_rounds is None:
        return catalog.outcomes
    if scheduled_rounds == 3:
        return EXACT_ROUND_OUTCOMES_THREE
    if scheduled_rounds == 5:
        return EXACT_ROUND_OUTCOMES_FIVE
    raise ValueError(f"unsupported scheduled_rounds for exact_round: {scheduled_rounds}")


def assert_known_outcome(family: MarketFamily, outcome: OutcomeKey) -> None:
    """Hard-fail when an outcome is not in the family's complete set."""
    catalog = catalog_for_family(family)
    if outcome not in catalog.outcomes:
        raise ValueError(f"outcome {outcome!r} is not valid for market family {family!r}")
