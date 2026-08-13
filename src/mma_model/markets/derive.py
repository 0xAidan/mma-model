"""Derive v1 market probabilities from one competing-risks terminal distribution.

Every family is an exact sum of named fine atoms. There is no independent
totals, method, or round model. Draw is an explicit leftover on moneyline
(fighter A + fighter B may be < 1). Method excludes draw rather than pooling
it into decision (settlement voids draw for method). Decisions and draws are
not exact-round wins.

Totals 1.5 / 2.5 follow right-closed half-round bins from DWCS-300
(``half_round_duration``) and DWCS-200 ``exact_half_result=under``:

- Interval ``i`` covers elapsed seconds ``(i * 150, (i + 1) * 150]``
  (elapsed 0 maps to interval 0). Interval 2 includes exact 1.5 rounds
  (450s); interval 4 includes exact 2.5 rounds (750s).
- UNDER 1.5 = finish atoms in intervals {0, 1, 2}; OVER 1.5 = remaining
  finish atoms plus decision/draw survival.
- UNDER 2.5 = finish atoms in intervals {0, 1, 2, 3, 4}; OVER 2.5 = the rest.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Never, cast

from mma_model.domain.markets import (
    FIGHTER_BY_METHOD_OUTCOMES,
    GOES_DISTANCE_OUTCOMES,
    METHOD_OUTCOMES,
    MONEYLINE_OUTCOMES,
    TOTALS_LINE_POINTS,
    MarketFamily,
    OutcomeKey,
    outcomes_for_family,
)
from mma_model.evaluation.contract import TerminalAtom
from mma_model.markets.settlement import SUPPORTED_SCHEDULED_ROUNDS

FINISH_CAUSES: Final[tuple[str, ...]] = ("ko_tko", "submission", "other_stoppage")
FINISH_SIDES: Final[tuple[str, ...]] = ("a", "b")
DECISION_ATOM_KEYS: Final[tuple[str, ...]] = ("a_decision", "b_decision", "draw")
METHOD_DRAW_TREATMENT: Final = (
    "Draw is excluded from method probabilities; it is not pooled into "
    "decision. Method outcomes may sum to less than 1 by the draw mass."
)
# Matches settlement mma_generic exact_half_result=under on right-closed bins.
TOTALS_UNDER_INTERVALS: Final[Mapping[float, frozenset[int]]] = {
    1.5: frozenset({0, 1, 2}),
    2.5: frozenset({0, 1, 2, 3, 4}),
}
FINE_FINISH_ATOM_RE: Final = re.compile(
    r"^(?P<side>a|b)_(?P<cause>ko_tko|submission|other_stoppage)"
    r"_r(?P<round>\d+)_i(?P<interval>\d+)$"
)
ATOM_SUM_ATOL: Final = 1e-10


class DerivedMarketError(ValueError):
    """Derived market probabilities are structurally invalid."""


class UnsupportedScheduleError(ValueError):
    """Scheduled rounds are missing or outside the modeled {3, 5} set."""


FinishSide = Literal["a", "b"]
FinishCauseName = Literal["ko_tko", "submission", "other_stoppage"]


@dataclass(frozen=True)
class FineFinishAtom:
    """Parsed finish atom: cause, side, round, and half-round interval."""

    key: str
    side: FinishSide
    cause: FinishCauseName
    round_no: int
    interval: int


@dataclass(frozen=True)
class DerivedMarketProbabilities:
    """Typed probabilities keyed by MarketFamily / OutcomeKey where possible."""

    scheduled_rounds: int
    moneyline: Mapping[OutcomeKey, float]
    draw: float
    goes_distance: Mapping[OutcomeKey, float]
    method: Mapping[OutcomeKey, float]
    fighter_by_method: Mapping[OutcomeKey, float]
    exact_round: Mapping[OutcomeKey, float]
    totals: Mapping[float, Mapping[OutcomeKey, float]]
    method_draw_treatment: str = METHOD_DRAW_TREATMENT

    def as_family_map(self) -> Mapping[MarketFamily, Mapping[OutcomeKey, float]]:
        return {
            MarketFamily.MONEYLINE: dict(self.moneyline),
            MarketFamily.GOES_DISTANCE: dict(self.goes_distance),
            MarketFamily.METHOD: dict(self.method),
            MarketFamily.FIGHTER_BY_METHOD: dict(self.fighter_by_method),
            MarketFamily.EXACT_ROUND: dict(self.exact_round),
        }


def interval_count_for_schedule(scheduled_rounds: int) -> int:
    if scheduled_rounds not in SUPPORTED_SCHEDULED_ROUNDS:
        raise UnsupportedScheduleError(
            f"unsupported scheduled_rounds {scheduled_rounds!r}; only 3 or 5 are modeled"
        )
    return int(scheduled_rounds) * 2


def interval_to_round(interval: int) -> int:
    if interval < 0:
        raise DerivedMarketError(f"interval index must be >= 0, got {interval}")
    return interval // 2 + 1


def finish_atom_key(*, side: str, cause: str, interval: int) -> str:
    if side not in FINISH_SIDES:
        raise DerivedMarketError(f"unsupported finish side {side!r}")
    if cause not in FINISH_CAUSES:
        raise DerivedMarketError(f"unsupported finish cause {cause!r}")
    round_no = interval_to_round(interval)
    return f"{side}_{cause}_r{round_no}_i{interval}"


def fine_atom_keys(scheduled_rounds: int) -> tuple[str, ...]:
    n_intervals = interval_count_for_schedule(scheduled_rounds)
    keys: list[str] = []
    for interval in range(n_intervals):
        for side in FINISH_SIDES:
            for cause in FINISH_CAUSES:
                keys.append(finish_atom_key(side=side, cause=cause, interval=interval))
    keys.extend(DECISION_ATOM_KEYS)
    return tuple(keys)


def parse_finish_atom(key: str) -> FineFinishAtom | None:
    match = FINE_FINISH_ATOM_RE.fullmatch(key)
    if match is None:
        return None
    side = match.group("side")
    cause = match.group("cause")
    if side not in FINISH_SIDES or cause not in FINISH_CAUSES:
        return None
    return FineFinishAtom(
        key=key,
        side=cast(FinishSide, side),
        cause=cast(FinishCauseName, cause),
        round_no=int(match.group("round")),
        interval=int(match.group("interval")),
    )


def _require_unit(value: float, *, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise DerivedMarketError(f"{name} must be finite and in [0, 1], got {number!r}")
    return number


def _sum_atoms(fine: Mapping[str, float], keys: tuple[str, ...], *, name: str) -> float:
    total = 0.0
    for key in keys:
        if key not in fine:
            raise DerivedMarketError(f"{name} missing required atom {key!r}")
        total += _require_unit(fine[key], name=key)
    if not math.isfinite(total):
        raise DerivedMarketError(f"{name} sum is not finite")
    return total


def _require_supported_schedule(scheduled_rounds: int) -> int:
    if scheduled_rounds not in SUPPORTED_SCHEDULED_ROUNDS:
        raise UnsupportedScheduleError(
            f"unsupported scheduled_rounds {scheduled_rounds!r}; only 3 or 5 are modeled"
        )
    return int(scheduled_rounds)


def swap_fine_atom_key(key: str) -> str:
    """Map an A/B fine atom onto its swapped counterpart; draw is unchanged."""
    if key == "draw":
        return "draw"
    if key == "a_decision":
        return "b_decision"
    if key == "b_decision":
        return "a_decision"
    parsed = parse_finish_atom(key)
    if parsed is None:
        raise DerivedMarketError(f"unrecognized fine atom key {key!r}")
    if parsed.side == "a":
        swapped_side = "b"
    elif parsed.side == "b":
        swapped_side = "a"
    else:
        never_side: Never = parsed.side  # type: ignore[assignment]
        raise DerivedMarketError(f"unhandled finish side: {never_side!r}")
    return finish_atom_key(side=swapped_side, cause=parsed.cause, interval=parsed.interval)


def swap_fine_atoms(fine: Mapping[str, float]) -> dict[str, float]:
    return {swap_fine_atom_key(key): float(value) for key, value in fine.items()}


def aggregate_frozen_atoms(fine: Mapping[str, float]) -> dict[TerminalAtom, float]:
    """Exact sums onto the frozen evaluation-contract TerminalAtom enum."""
    frozen = {atom: 0.0 for atom in TerminalAtom}
    for key, raw in fine.items():
        value = _require_unit(raw, name=key)
        if key == "a_decision":
            frozen[TerminalAtom.A_DECISION] += value
            continue
        if key == "b_decision":
            frozen[TerminalAtom.B_DECISION] += value
            continue
        if key == "draw":
            frozen[TerminalAtom.DRAW] += value
            continue
        parsed = parse_finish_atom(key)
        if parsed is None:
            raise DerivedMarketError(f"unrecognized fine atom key {key!r}")
        atom = _finish_to_frozen(parsed)
        frozen[atom] += value
    _require_unit_sum(frozen, name="frozen terminal atoms")
    return frozen


def _finish_to_frozen(parsed: FineFinishAtom) -> TerminalAtom:
    if parsed.side == "a":
        if parsed.cause == "ko_tko":
            return TerminalAtom.A_KO_TKO
        if parsed.cause == "submission":
            return TerminalAtom.A_SUBMISSION
        if parsed.cause == "other_stoppage":
            return TerminalAtom.A_OTHER_STOPPAGE
        never_cause: Never = parsed.cause  # type: ignore[assignment]
        raise DerivedMarketError(f"unhandled finish cause: {never_cause!r}")
    if parsed.side == "b":
        if parsed.cause == "ko_tko":
            return TerminalAtom.B_KO_TKO
        if parsed.cause == "submission":
            return TerminalAtom.B_SUBMISSION
        if parsed.cause == "other_stoppage":
            return TerminalAtom.B_OTHER_STOPPAGE
        never_cause_b: Never = parsed.cause  # type: ignore[assignment]
        raise DerivedMarketError(f"unhandled finish cause: {never_cause_b!r}")
    never_side: Never = parsed.side  # type: ignore[assignment]
    raise DerivedMarketError(f"unhandled finish side: {never_side!r}")


def _require_unit_sum(values: Mapping[object, float], *, name: str) -> None:
    total = 0.0
    for key, raw in values.items():
        total += _require_unit(raw, name=f"{name}:{key}")
    if abs(total - 1.0) > ATOM_SUM_ATOL:
        raise DerivedMarketError(
            f"{name} must sum to 1 ± {ATOM_SUM_ATOL}, got {total}"
        )


def _finish_keys_for(
    fine: Mapping[str, float],
    *,
    scheduled_rounds: int,
    side: str | None = None,
    cause: str | None = None,
    intervals: frozenset[int] | None = None,
    round_no: int | None = None,
) -> tuple[str, ...]:
    n_intervals = interval_count_for_schedule(scheduled_rounds)
    keys: list[str] = []
    for key in fine:
        parsed = parse_finish_atom(key)
        if parsed is None:
            continue
        if parsed.interval < 0 or parsed.interval >= n_intervals:
            raise DerivedMarketError(
                f"atom {key!r} interval is outside schedule {scheduled_rounds}"
            )
        if parsed.round_no != interval_to_round(parsed.interval):
            raise DerivedMarketError(f"atom {key!r} round disagrees with interval")
        if side is not None and parsed.side != side:
            continue
        if cause is not None and parsed.cause != cause:
            continue
        if intervals is not None and parsed.interval not in intervals:
            continue
        if round_no is not None and parsed.round_no != round_no:
            continue
        keys.append(key)
    return tuple(keys)


def _moneyline_keys(
    fine: Mapping[str, float],
    *,
    scheduled_rounds: int,
    side: str,
) -> tuple[str, ...]:
    finish = _finish_keys_for(fine, scheduled_rounds=scheduled_rounds, side=side)
    decision = "a_decision" if side == "a" else "b_decision"
    return finish + (decision,)


def derive_markets(
    fine: Mapping[str, float],
    *,
    scheduled_rounds: int,
) -> DerivedMarketProbabilities:
    """Pure market map from one fine terminal distribution."""
    rounds = _require_supported_schedule(scheduled_rounds)
    expected = set(fine_atom_keys(rounds))
    got = set(fine)
    missing = expected - got
    extra = got - expected
    if missing or extra:
        raise DerivedMarketError(
            f"fine atom key mismatch for {rounds}-round schedule "
            f"(missing={sorted(missing)[:8]!r} extra={sorted(extra)[:8]!r})"
        )
    _require_unit_sum(fine, name="fine terminal atoms")

    p_a = _sum_atoms(
        fine,
        _moneyline_keys(fine, scheduled_rounds=rounds, side="a"),
        name="moneyline.fighter_a",
    )
    p_b = _sum_atoms(
        fine,
        _moneyline_keys(fine, scheduled_rounds=rounds, side="b"),
        name="moneyline.fighter_b",
    )
    p_draw = _require_unit(fine["draw"], name="draw")
    moneyline = {
        OutcomeKey.FIGHTER_A: p_a,
        OutcomeKey.FIGHTER_B: p_b,
    }
    _validate_outcome_keys(MarketFamily.MONEYLINE, moneyline, scheduled_rounds=rounds)

    finish_keys = _finish_keys_for(fine, scheduled_rounds=rounds)
    p_inside = _sum_atoms(fine, finish_keys, name="inside_distance")
    p_goes = _sum_atoms(fine, DECISION_ATOM_KEYS, name="goes_distance")
    goes = {
        OutcomeKey.GOES_DISTANCE: p_goes,
        OutcomeKey.INSIDE_DISTANCE: p_inside,
    }
    _validate_outcome_keys(MarketFamily.GOES_DISTANCE, goes, scheduled_rounds=rounds)

    p_ko = _sum_atoms(
        fine,
        _finish_keys_for(fine, scheduled_rounds=rounds, cause="ko_tko"),
        name="method.ko_tko",
    )
    p_sub = _sum_atoms(
        fine,
        _finish_keys_for(fine, scheduled_rounds=rounds, cause="submission"),
        name="method.submission",
    )
    p_other = _sum_atoms(
        fine,
        _finish_keys_for(fine, scheduled_rounds=rounds, cause="other_stoppage"),
        name="method.other_stoppage",
    )
    p_decision = _require_unit(fine["a_decision"], name="a_decision") + _require_unit(
        fine["b_decision"], name="b_decision"
    )
    method = {
        OutcomeKey.KO_TKO: p_ko,
        OutcomeKey.SUBMISSION: p_sub,
        OutcomeKey.DECISION: p_decision,
        OutcomeKey.OTHER_STOPPAGE: p_other,
    }
    _validate_outcome_keys(MarketFamily.METHOD, method, scheduled_rounds=rounds)

    frozen = aggregate_frozen_atoms(fine)
    fighter_by_method = {
        OutcomeKey.A_KO_TKO: frozen[TerminalAtom.A_KO_TKO],
        OutcomeKey.A_SUBMISSION: frozen[TerminalAtom.A_SUBMISSION],
        OutcomeKey.A_OTHER_STOPPAGE: frozen[TerminalAtom.A_OTHER_STOPPAGE],
        OutcomeKey.A_DECISION: frozen[TerminalAtom.A_DECISION],
        OutcomeKey.B_KO_TKO: frozen[TerminalAtom.B_KO_TKO],
        OutcomeKey.B_SUBMISSION: frozen[TerminalAtom.B_SUBMISSION],
        OutcomeKey.B_OTHER_STOPPAGE: frozen[TerminalAtom.B_OTHER_STOPPAGE],
        OutcomeKey.B_DECISION: frozen[TerminalAtom.B_DECISION],
    }
    _validate_outcome_keys(
        MarketFamily.FIGHTER_BY_METHOD, fighter_by_method, scheduled_rounds=rounds
    )

    exact_round = _exact_round_probs(fine, scheduled_rounds=rounds)
    totals = _totals_probs(fine, scheduled_rounds=rounds)
    return DerivedMarketProbabilities(
        scheduled_rounds=rounds,
        moneyline=moneyline,
        draw=p_draw,
        goes_distance=goes,
        method=method,
        fighter_by_method=fighter_by_method,
        exact_round=exact_round,
        totals=totals,
    )


def _exact_round_probs(
    fine: Mapping[str, float],
    *,
    scheduled_rounds: int,
) -> dict[OutcomeKey, float]:
    catalog = outcomes_for_family(MarketFamily.EXACT_ROUND, scheduled_rounds=scheduled_rounds)
    mapping: dict[OutcomeKey, float] = {}
    for outcome in catalog:
        round_no = _outcome_round(outcome)
        keys = _finish_keys_for(fine, scheduled_rounds=scheduled_rounds, round_no=round_no)
        mapping[outcome] = _sum_atoms(fine, keys, name=f"exact_round.{outcome.value}")
    _validate_outcome_keys(
        MarketFamily.EXACT_ROUND, mapping, scheduled_rounds=scheduled_rounds
    )
    return mapping


def _outcome_round(outcome: OutcomeKey) -> int:
    if outcome is OutcomeKey.ROUND_1:
        return 1
    if outcome is OutcomeKey.ROUND_2:
        return 2
    if outcome is OutcomeKey.ROUND_3:
        return 3
    if outcome is OutcomeKey.ROUND_4:
        return 4
    if outcome is OutcomeKey.ROUND_5:
        return 5
    raise DerivedMarketError(f"outcome {outcome!r} is not an exact-round key")


def _totals_probs(
    fine: Mapping[str, float],
    *,
    scheduled_rounds: int,
) -> dict[float, dict[OutcomeKey, float]]:
    n_intervals = interval_count_for_schedule(scheduled_rounds)
    all_intervals = frozenset(range(n_intervals))
    out: dict[float, dict[OutcomeKey, float]] = {}
    for line in TOTALS_LINE_POINTS:
        under_intervals = TOTALS_UNDER_INTERVALS[line]
        over_intervals = all_intervals - under_intervals
        p_under = _sum_atoms(
            fine,
            _finish_keys_for(fine, scheduled_rounds=scheduled_rounds, intervals=under_intervals),
            name=f"totals.under_{line}",
        )
        p_over_finish = _sum_atoms(
            fine,
            _finish_keys_for(fine, scheduled_rounds=scheduled_rounds, intervals=over_intervals),
            name=f"totals.over_finish_{line}",
        )
        p_over = p_over_finish + _sum_atoms(
            fine, DECISION_ATOM_KEYS, name=f"totals.over_survival_{line}"
        )
        mapping = {OutcomeKey.OVER: p_over, OutcomeKey.UNDER: p_under}
        for outcome, value in mapping.items():
            _require_unit(value, name=f"totals.{line}.{outcome.value}")
        out[line] = mapping
    return out


def _validate_outcome_keys(
    family: MarketFamily,
    mapping: Mapping[OutcomeKey, float],
    *,
    scheduled_rounds: int,
) -> None:
    expected = outcomes_for_family(family, scheduled_rounds=scheduled_rounds)
    if family is MarketFamily.EXACT_ROUND:
        allowed = expected
    elif family is MarketFamily.MONEYLINE:
        allowed = MONEYLINE_OUTCOMES
    elif family is MarketFamily.GOES_DISTANCE:
        allowed = GOES_DISTANCE_OUTCOMES
    elif family is MarketFamily.METHOD:
        allowed = METHOD_OUTCOMES
    elif family is MarketFamily.FIGHTER_BY_METHOD:
        allowed = FIGHTER_BY_METHOD_OUTCOMES
    elif family is MarketFamily.TOTALS:
        allowed = (OutcomeKey.OVER, OutcomeKey.UNDER)
    else:
        never_family: Never = family
        raise DerivedMarketError(f"unhandled market family: {never_family!r}")
    extra = set(mapping) - set(allowed)
    if extra:
        raise DerivedMarketError(f"{family.value} has unexpected outcomes {sorted(extra)!r}")
    for outcome, value in mapping.items():
        _require_unit(value, name=f"{family.value}.{outcome.value}")
    if family is MarketFamily.EXACT_ROUND:
        missing = [item for item in expected if item not in mapping]
        if missing:
            raise DerivedMarketError(f"exact_round missing {missing!r}")
