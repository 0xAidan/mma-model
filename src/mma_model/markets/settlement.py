"""Pure, deterministic market settlement (DWCS-200).

Returns win/loss/push/void/unresolved plus a reason. No HTTP / DB I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Never

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.markets.rules import (
    SettlementRuleSet,
    SideEffect,
    get_rule_set,
)

WinnerSide = Literal["a", "b"]
ResultClass = Literal["decisive", "draw", "no_contest"]
MethodLabel = Literal[
    "ko_tko",
    "submission",
    "decision",
    "other_stoppage",
    "technical_decision",
]


class SettlementResult(StrEnum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class BoutSettlementFacts:
    """Event-night facts required to settle a selection."""

    scheduled_rounds: int
    cancelled: bool = False
    pending: bool = False
    result_class: ResultClass | None = None
    winner_side: WinnerSide | None = None
    method: MethodLabel | None = None
    ending_round: int | None = None


@dataclass(frozen=True)
class MarketSelection:
    """A single bet selection against a canonical market family."""

    family: MarketFamily
    outcome: OutcomeKey
    line_point: float | None = None


@dataclass(frozen=True)
class SettlementDecision:
    result: SettlementResult
    reason: str
    rule_set_id: str
    rule_set_version: str


def _side_effect_to_result(effect: SideEffect) -> SettlementResult:
    mapping = {
        SideEffect.WIN: SettlementResult.WIN,
        SideEffect.LOSS: SettlementResult.LOSS,
        SideEffect.PUSH: SettlementResult.PUSH,
        SideEffect.VOID: SettlementResult.VOID,
    }
    return mapping[effect]


def _decision(
    result: SettlementResult,
    reason: str,
    rule_set: SettlementRuleSet,
) -> SettlementDecision:
    return SettlementDecision(
        result=result,
        reason=reason,
        rule_set_id=rule_set.rule_set_id,
        rule_set_version=rule_set.version,
    )


def _normalize_method(
    method: MethodLabel | None,
    *,
    technical_counts_as: Literal["decision"] | None,
) -> MethodLabel | None:
    if method is None:
        return None
    if method == "technical_decision" and technical_counts_as == "decision":
        return "decision"
    return method


def _validate_selection(selection: MarketSelection) -> None:
    assert_known_outcome(selection.family, selection.outcome)
    catalog = catalog_for_family(selection.family)
    if not catalog.is_valid_line_point(selection.line_point):
        raise ValueError(
            f"invalid line_point {selection.line_point!r} for family {selection.family!r}"
        )


def _resolved_ending_round(
    facts: BoutSettlementFacts,
    *,
    decision_uses_scheduled: bool,
) -> int | None:
    if facts.ending_round is not None:
        return facts.ending_round
    if (
        decision_uses_scheduled
        and facts.method in {"decision", "technical_decision"}
        and facts.result_class == "decisive"
    ):
        return facts.scheduled_rounds
    if (
        decision_uses_scheduled
        and facts.result_class == "draw"
        and facts.method in {None, "decision", "technical_decision"}
    ):
        return facts.scheduled_rounds
    return None


def _settle_moneyline(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
) -> SettlementDecision:
    rules = rule_set.moneyline
    if facts.cancelled:
        return _decision(
            _side_effect_to_result(rules.cancellation),
            "bout cancelled",
            rule_set,
        )
    if facts.result_class == "no_contest":
        return _decision(
            _side_effect_to_result(rules.no_contest),
            "no_contest",
            rule_set,
        )
    if facts.result_class == "draw":
        return _decision(_side_effect_to_result(rules.draw), "draw", rule_set)
    if facts.result_class != "decisive" or facts.winner_side is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing decisive winner",
            rule_set,
        )
    # technical_decision settles as decision for method labels but moneyline
    # still keys only on winner_side once the bout is decisive.
    _ = rules.technical_decision
    won = (
        (selection.outcome is OutcomeKey.FIGHTER_A and facts.winner_side == "a")
        or (selection.outcome is OutcomeKey.FIGHTER_B and facts.winner_side == "b")
    )
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"winner_side={facts.winner_side}",
        rule_set,
    )


def _distance_realized(
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
) -> Literal["goes_distance", "inside_distance"] | None:
    rules = rule_set.goes_distance
    method = facts.method
    if method == "technical_decision":
        return "goes_distance" if rules.technical_decision_counts_as_goes_distance else None
    if method == "decision":
        return "goes_distance" if rules.decision_counts_as_goes_distance else None
    if facts.result_class == "draw":
        return "goes_distance" if rules.draw_counts_as_goes_distance else None
    if method in {"ko_tko", "submission", "other_stoppage"}:
        return "inside_distance"
    return None


def _settle_goes_distance(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
) -> SettlementDecision:
    rules = rule_set.goes_distance
    if facts.cancelled:
        return _decision(
            _side_effect_to_result(rules.cancellation),
            "bout cancelled",
            rule_set,
        )
    if facts.result_class == "no_contest":
        return _decision(
            _side_effect_to_result(rules.no_contest),
            "no_contest",
            rule_set,
        )
    realized = _distance_realized(facts, rule_set)
    if realized is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "unable to classify distance outcome",
            rule_set,
        )
    won = selection.outcome.value == realized
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"realized={realized}",
        rule_set,
    )


def _settle_totals(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
) -> SettlementDecision:
    rules = rule_set.totals
    if facts.cancelled:
        return _decision(
            _side_effect_to_result(rules.cancellation),
            "bout cancelled",
            rule_set,
        )
    if facts.result_class == "no_contest":
        return _decision(
            _side_effect_to_result(rules.no_contest),
            "no_contest",
            rule_set,
        )
    line = selection.line_point
    if line is None:
        return _decision(SettlementResult.UNRESOLVED, "missing line_point", rule_set)
    ending = _resolved_ending_round(
        facts,
        decision_uses_scheduled=rules.decision_uses_scheduled_rounds_as_ending_round,
    )
    if ending is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing ending_round",
            rule_set,
        )
    is_half = float(line) in rules.half_round_lines or not float(line).is_integer()
    if is_half:
        # over X.5 wins when ending_round >= ceil(X.5)
        threshold = math.ceil(float(line))
        over_wins = ending >= threshold
        if rules.half_round_push:
            # Reserved for exotic half rules; generic contract keeps this false.
            pass
        won = over_wins if selection.outcome is OutcomeKey.OVER else not over_wins
        return _decision(
            SettlementResult.WIN if won else SettlementResult.LOSS,
            f"ending_round={ending} line={line} boundary=ending_round",
            rule_set,
        )
    # Whole-number lines: push when ending_round == line
    if rules.whole_round_push and ending == int(line):
        return _decision(
            SettlementResult.PUSH,
            f"ending_round={ending} equals whole line {line}",
            rule_set,
        )
    over_wins = ending > float(line)
    won = over_wins if selection.outcome is OutcomeKey.OVER else not over_wins
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"ending_round={ending} line={line}",
        rule_set,
    )


def _settle_method_family(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
    *,
    fighter_scoped: bool,
) -> SettlementDecision:
    rules = rule_set.fighter_by_method if fighter_scoped else rule_set.method
    if facts.cancelled:
        return _decision(
            _side_effect_to_result(rules.cancellation),
            "bout cancelled",
            rule_set,
        )
    if facts.result_class == "no_contest":
        return _decision(
            _side_effect_to_result(rules.no_contest),
            "no_contest",
            rule_set,
        )
    if facts.result_class == "draw":
        return _decision(_side_effect_to_result(rules.draw), "draw", rule_set)
    method = _normalize_method(
        facts.method,
        technical_counts_as=rules.technical_decision_counts_as,
    )
    if facts.result_class != "decisive" or method is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing decisive method",
            rule_set,
        )
    if fighter_scoped:
        if facts.winner_side is None:
            return _decision(
                SettlementResult.UNRESOLVED,
                "missing winner_side",
                rule_set,
            )
        realized = f"{facts.winner_side}_{method}"
        won = selection.outcome.value == realized
        return _decision(
            SettlementResult.WIN if won else SettlementResult.LOSS,
            f"realized={realized}",
            rule_set,
        )
    won = selection.outcome.value == method
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"realized_method={method}",
        rule_set,
    )


def _settle_exact_round(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    rule_set: SettlementRuleSet,
) -> SettlementDecision:
    rules = rule_set.exact_round
    if facts.cancelled:
        return _decision(
            _side_effect_to_result(rules.cancellation),
            "bout cancelled",
            rule_set,
        )
    if facts.result_class == "no_contest":
        return _decision(
            _side_effect_to_result(rules.no_contest),
            "no_contest",
            rule_set,
        )
    if facts.result_class == "draw":
        return _decision(_side_effect_to_result(rules.draw), "draw", rule_set)
    if facts.method == "technical_decision":
        return _decision(
            _side_effect_to_result(rules.technical_decision),
            "technical_decision",
            rule_set,
        )
    if facts.method == "decision":
        return _decision(
            _side_effect_to_result(rules.decision),
            "decision",
            rule_set,
        )
    if facts.method not in {"ko_tko", "submission", "other_stoppage"}:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing finish method for exact_round",
            rule_set,
        )
    if facts.ending_round is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing ending_round",
            rule_set,
        )
    realized = f"round_{facts.ending_round}"
    won = selection.outcome.value == realized
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"realized={realized}",
        rule_set,
    )


def settle(
    selection: MarketSelection,
    facts: BoutSettlementFacts,
    *,
    rule_set_id: str | None = None,
    allow_provisional: bool = False,
    rule_set: SettlementRuleSet | None = None,
) -> SettlementDecision:
    """Settle one selection under a versioned rule set.

    Unknown family/outcome combinations hard-fail before settlement. Pending or
    incomplete facts return ``unresolved`` rather than inventing a grade.
    """
    _validate_selection(selection)
    active = rule_set or get_rule_set(
        rule_set_id,
        allow_provisional=allow_provisional,
    )
    if facts.pending:
        return _decision(SettlementResult.UNRESOLVED, "bout pending", active)
    if facts.scheduled_rounds < 1:
        return _decision(
            SettlementResult.UNRESOLVED,
            "invalid scheduled_rounds",
            active,
        )

    family = selection.family
    if family is MarketFamily.MONEYLINE:
        return _settle_moneyline(selection, facts, active)
    if family is MarketFamily.GOES_DISTANCE:
        return _settle_goes_distance(selection, facts, active)
    if family is MarketFamily.TOTALS:
        return _settle_totals(selection, facts, active)
    if family is MarketFamily.METHOD:
        return _settle_method_family(
            selection, facts, active, fighter_scoped=False
        )
    if family is MarketFamily.FIGHTER_BY_METHOD:
        return _settle_method_family(
            selection, facts, active, fighter_scoped=True
        )
    if family is MarketFamily.EXACT_ROUND:
        return _settle_exact_round(selection, facts, active)
    never_family: Never = family
    raise ValueError(f"unhandled market family: {never_family!r}")
