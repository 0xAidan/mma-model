"""Pure, deterministic market settlement (DWCS-200).

Returns win/loss/push/void/unresolved plus a reason. No HTTP / DB I/O.

Structurally invalid facts raise ``SettlementFactsError``. Genuinely incomplete
or pending facts settle as ``unresolved`` (never invent a grade).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Never

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
    outcomes_for_family,
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

SUPPORTED_SCHEDULED_ROUNDS: frozenset[int] = frozenset({3, 5})
DEFAULT_ROUND_SECONDS: int = 300


class SettlementResult(StrEnum):
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    UNRESOLVED = "unresolved"


class SettlementFactsError(ValueError):
    """Structurally invalid settlement facts (not merely incomplete)."""


@dataclass(frozen=True)
class BoutSettlementFacts:
    """Event-night facts required to settle a selection.

    Totals half-round boundaries require fight duration. Prefer
    ``ending_round`` + ``elapsed_seconds_in_round``, or
    ``total_elapsed_seconds``. Decisions/draws may omit clocks when the active
    rule set uses full scheduled duration.
    """

    scheduled_rounds: int
    cancelled: bool = False
    pending: bool = False
    result_class: ResultClass | None = None
    winner_side: WinnerSide | None = None
    method: MethodLabel | None = None
    ending_round: int | None = None
    elapsed_seconds_in_round: int | None = None
    total_elapsed_seconds: int | None = None


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
    content_hash: str


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
        content_hash=rule_set.contract_content_hash,
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


def clock_pairs_for_total(
    total: int,
    *,
    scheduled_rounds: int,
    round_seconds: int,
) -> frozenset[tuple[int, int]]:
    """Canonical (ending_round, elapsed_in_round) pairs for a total duration.

    Round-boundary equivalence: end of round ``r`` at ``round_seconds`` is also
    representable as start of round ``r+1`` at ``0`` when ``r < scheduled_rounds``.
    """
    if total < 0 or total > scheduled_rounds * round_seconds:
        return frozenset()
    if total == 0:
        return frozenset({(1, 0)})
    full, rem = divmod(total, round_seconds)
    pairs: set[tuple[int, int]] = set()
    if rem == 0:
        # Exactly at end of round ``full``.
        pairs.add((full, round_seconds))
        if full < scheduled_rounds:
            pairs.add((full + 1, 0))
    else:
        ending = full + 1
        if 1 <= ending <= scheduled_rounds:
            pairs.add((ending, rem))
    return frozenset(
        (er, es)
        for er, es in pairs
        if 1 <= er <= scheduled_rounds and 0 <= es <= round_seconds
    )


def _validate_clock_consistency(
    facts: BoutSettlementFacts,
    *,
    round_seconds: int,
) -> None:
    """Fail closed when any provided clock fields contradict each other."""
    ending = facts.ending_round
    in_round = facts.elapsed_seconds_in_round
    total = facts.total_elapsed_seconds

    if ending is not None and (
        ending < 1 or ending > facts.scheduled_rounds
    ):
        raise SettlementFactsError(
            f"ending_round {ending} outside schedule 1..{facts.scheduled_rounds}"
        )
    if in_round is not None and (in_round < 0 or in_round > round_seconds):
        raise SettlementFactsError(
            f"elapsed_seconds_in_round {in_round} outside 0..{round_seconds}"
        )
    if total is not None:
        max_total = facts.scheduled_rounds * round_seconds
        if total < 0 or total > max_total:
            raise SettlementFactsError(
                f"total_elapsed_seconds {total} outside 0..{max_total}"
            )

    if ending is not None and in_round is not None and total is not None:
        allowed = clock_pairs_for_total(
            total,
            scheduled_rounds=facts.scheduled_rounds,
            round_seconds=round_seconds,
        )
        if (ending, in_round) not in allowed:
            raise SettlementFactsError(
                "clock fields disagree: ending_round="
                f"{ending}, elapsed_seconds_in_round={in_round}, "
                f"total_elapsed_seconds={total}"
            )
        return

    if ending is not None and in_round is not None:
        # Unique derived total — always consistent once bounds pass.
        return

    if ending is not None and total is not None:
        allowed = clock_pairs_for_total(
            total,
            scheduled_rounds=facts.scheduled_rounds,
            round_seconds=round_seconds,
        )
        if not any(er == ending for er, _es in allowed):
            raise SettlementFactsError(
                f"ending_round={ending} inconsistent with "
                f"total_elapsed_seconds={total}"
            )
        return

    if in_round is not None and total is not None:
        allowed = clock_pairs_for_total(
            total,
            scheduled_rounds=facts.scheduled_rounds,
            round_seconds=round_seconds,
        )
        if not any(es == in_round for _er, es in allowed):
            raise SettlementFactsError(
                f"elapsed_seconds_in_round={in_round} inconsistent with "
                f"total_elapsed_seconds={total}"
            )


def validate_settlement_facts(
    facts: BoutSettlementFacts,
    *,
    round_seconds: int = DEFAULT_ROUND_SECONDS,
) -> None:
    """Raise ``SettlementFactsError`` for structurally impossible facts."""
    if facts.scheduled_rounds not in SUPPORTED_SCHEDULED_ROUNDS:
        raise SettlementFactsError(
            f"unsupported scheduled_rounds: {facts.scheduled_rounds!r} "
            f"(supported: {sorted(SUPPORTED_SCHEDULED_ROUNDS)})"
        )
    if round_seconds <= 0:
        raise SettlementFactsError(f"round_seconds must be positive, got {round_seconds}")

    _validate_clock_consistency(facts, round_seconds=round_seconds)

    if facts.pending and facts.cancelled:
        raise SettlementFactsError("pending bout cannot also be cancelled")

    if facts.pending:
        # Pending is a pre-result state. Completed result fields are contradictory.
        if facts.result_class is not None:
            raise SettlementFactsError(
                f"pending bout cannot have result_class={facts.result_class!r}"
            )
        if facts.winner_side is not None:
            raise SettlementFactsError("pending bout cannot have winner_side")
        if facts.method is not None:
            raise SettlementFactsError("pending bout cannot have method")
        return

    if facts.cancelled:
        if facts.result_class in {"decisive", "draw", "no_contest"}:
            raise SettlementFactsError(
                f"cancelled bout cannot also have result_class={facts.result_class!r}"
            )
        if facts.winner_side is not None:
            raise SettlementFactsError("cancelled bout cannot have winner_side")
        if facts.method is not None:
            raise SettlementFactsError("cancelled bout cannot have method")
        return

    if facts.result_class == "draw":
        if facts.winner_side is not None:
            raise SettlementFactsError("draw cannot have winner_side")
        if facts.method in {"ko_tko", "submission", "other_stoppage"}:
            raise SettlementFactsError(
                f"draw cannot have finish method {facts.method!r}"
            )

    if facts.result_class == "no_contest" and facts.winner_side is not None:
        raise SettlementFactsError("no_contest cannot have winner_side")


def _validate_selection(
    selection: MarketSelection,
    *,
    scheduled_rounds: int,
) -> None:
    assert_known_outcome(selection.family, selection.outcome)
    catalog = catalog_for_family(selection.family)
    if not catalog.is_valid_line_point(selection.line_point):
        raise ValueError(
            f"invalid line_point {selection.line_point!r} for family {selection.family!r}"
        )
    if selection.family is MarketFamily.EXACT_ROUND:
        allowed = outcomes_for_family(
            MarketFamily.EXACT_ROUND, scheduled_rounds=scheduled_rounds
        )
        if selection.outcome not in allowed:
            raise ValueError(
                f"outcome {selection.outcome!r} is not valid for exact_round with "
                f"scheduled_rounds={scheduled_rounds}"
            )


def resolve_total_elapsed_seconds(
    facts: BoutSettlementFacts,
    *,
    round_seconds: int,
    decision_uses_full_scheduled_duration: bool,
) -> int | None:
    """Resolve fight duration in seconds, or None when clocks are insufficient."""
    if facts.total_elapsed_seconds is not None:
        return facts.total_elapsed_seconds
    if facts.ending_round is not None and facts.elapsed_seconds_in_round is not None:
        return (facts.ending_round - 1) * round_seconds + facts.elapsed_seconds_in_round
    if decision_uses_full_scheduled_duration and not facts.cancelled:
        if facts.result_class == "draw":
            return facts.scheduled_rounds * round_seconds
        if facts.method in {"decision", "technical_decision"} and facts.result_class == "decisive":
            return facts.scheduled_rounds * round_seconds
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
    if float(line) not in rules.half_round_lines:
        raise ValueError(
            f"line_point {line!r} not in rule-set half_round_lines {rules.half_round_lines}"
        )

    total_seconds = resolve_total_elapsed_seconds(
        facts,
        round_seconds=rules.round_seconds,
        decision_uses_full_scheduled_duration=rules.decision_uses_full_scheduled_duration,
    )
    if total_seconds is None:
        return _decision(
            SettlementResult.UNRESOLVED,
            "missing fight duration for totals boundary "
            "(need ending_round+elapsed_seconds_in_round, total_elapsed_seconds, "
            "or decision/draw full-distance duration)",
            rule_set,
        )

    elapsed_rounds = total_seconds / float(rules.round_seconds)
    threshold = float(line)
    if elapsed_rounds == threshold:
        exact = rules.exact_half_result
        if exact == "push":
            return _decision(
                SettlementResult.PUSH,
                f"elapsed_rounds={elapsed_rounds} equals line={line} "
                f"(exact_half_result=push, total_elapsed_seconds={total_seconds})",
                rule_set,
            )
        if exact == "void":
            return _decision(
                SettlementResult.VOID,
                f"elapsed_rounds={elapsed_rounds} equals line={line} "
                f"(exact_half_result=void, total_elapsed_seconds={total_seconds})",
                rule_set,
            )
        if exact == "under":
            won = selection.outcome is OutcomeKey.UNDER
            return _decision(
                SettlementResult.WIN if won else SettlementResult.LOSS,
                f"elapsed_rounds={elapsed_rounds} equals line={line} "
                f"(exact_half_result=under, total_elapsed_seconds={total_seconds})",
                rule_set,
            )
        if exact == "over":
            won = selection.outcome is OutcomeKey.OVER
            return _decision(
                SettlementResult.WIN if won else SettlementResult.LOSS,
                f"elapsed_rounds={elapsed_rounds} equals line={line} "
                f"(exact_half_result=over, total_elapsed_seconds={total_seconds})",
                rule_set,
            )
        never_exact: Never = exact
        raise ValueError(f"unhandled exact_half_result: {never_exact!r}")
    over_wins = elapsed_rounds > threshold
    won = over_wins if selection.outcome is OutcomeKey.OVER else not over_wins
    return _decision(
        SettlementResult.WIN if won else SettlementResult.LOSS,
        f"elapsed_rounds={elapsed_rounds} line={line} "
        f"total_elapsed_seconds={total_seconds} boundary=elapsed_rounds",
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

    Invalid selections / structurally impossible facts hard-fail. Pending or
    incomplete facts return ``unresolved`` rather than inventing a grade.
    """
    active = rule_set or get_rule_set(
        rule_set_id,
        allow_provisional=allow_provisional,
    )
    validate_settlement_facts(facts, round_seconds=active.totals.round_seconds)
    _validate_selection(selection, scheduled_rounds=facts.scheduled_rounds)

    if facts.pending:
        return _decision(SettlementResult.UNRESOLVED, "bout pending", active)

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
