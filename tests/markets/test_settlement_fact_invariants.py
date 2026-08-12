"""Incomplete/contradictory method+result facts must never confidently grade."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementFactsError,
    SettlementResult,
    settle,
    validate_settlement_facts,
)

CONFIDENT = frozenset(
    {
        SettlementResult.WIN,
        SettlementResult.LOSS,
        SettlementResult.PUSH,
        SettlementResult.VOID,
    }
)

FAMILIES: tuple[tuple[MarketFamily, OutcomeKey, float | None], ...] = (
    (MarketFamily.MONEYLINE, OutcomeKey.FIGHTER_A, None),
    (MarketFamily.TOTALS, OutcomeKey.OVER, 1.5),
    (MarketFamily.GOES_DISTANCE, OutcomeKey.GOES_DISTANCE, None),
    (MarketFamily.METHOD, OutcomeKey.DECISION, None),
    (MarketFamily.FIGHTER_BY_METHOD, OutcomeKey.A_DECISION, None),
    (MarketFamily.EXACT_ROUND, OutcomeKey.ROUND_2, None),
)


def _selection(
    family: MarketFamily,
    outcome: OutcomeKey,
    line: float | None,
) -> MarketSelection:
    return MarketSelection(family=family, outcome=outcome, line_point=line)


def _assert_no_confident_grade(facts: BoutSettlementFacts) -> None:
    """Either hard-fail validation or settle unresolved — never win/loss/push/void."""
    try:
        validate_settlement_facts(facts)
    except SettlementFactsError:
        for family, outcome, line in FAMILIES:
            with pytest.raises(SettlementFactsError):
                settle(_selection(family, outcome, line), facts)
        return

    for family, outcome, line in FAMILIES:
        decision = settle(_selection(family, outcome, line), facts)
        assert decision.result not in CONFIDENT, (
            f"{family.value} graded {decision.result.value} for incomplete facts "
            f"(reason={decision.reason!r})"
        )
        assert decision.result is SettlementResult.UNRESOLVED


@pytest.mark.parametrize(
    "method",
    [
        "technical_draw",
        "technical_decision",
        "decision",
        "ko_tko",
        "submission",
        "other_stoppage",
    ],
)
def test_method_without_result_class_never_confidently_grades(method: str) -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        method=method,  # type: ignore[arg-type]
        ending_round=2,
        elapsed_seconds_in_round=90,
        winner_side="a" if method != "technical_draw" else None,
    )
    with pytest.raises(SettlementFactsError):
        validate_settlement_facts(facts)
    _assert_no_confident_grade(facts)


def test_technical_draw_requires_draw_result_class() -> None:
    with pytest.raises(SettlementFactsError, match="technical_draw requires result_class"):
        validate_settlement_facts(
            BoutSettlementFacts(
                scheduled_rounds=3,
                method="technical_draw",
                ending_round=1,
                elapsed_seconds_in_round=100,
            )
        )
    with pytest.raises(SettlementFactsError, match="technical_draw requires result_class"):
        validate_settlement_facts(
            BoutSettlementFacts(
                scheduled_rounds=3,
                result_class="decisive",
                winner_side="a",
                method="technical_draw",
            )
        )


def test_decisive_methods_require_decisive_result_class() -> None:
    for method in ("decision", "technical_decision", "ko_tko", "submission"):
        with pytest.raises(SettlementFactsError, match="requires result_class='decisive'"):
            validate_settlement_facts(
                BoutSettlementFacts(
                    scheduled_rounds=3,
                    result_class="draw",
                    method=method,  # type: ignore[arg-type]
                )
            )


def test_incomplete_result_without_method_unresolved_across_families() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        ending_round=2,
        elapsed_seconds_in_round=100,
    )
    _assert_no_confident_grade(facts)


def test_clocks_without_result_class_unresolved_across_families() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        ending_round=2,
        elapsed_seconds_in_round=200,
    )
    _assert_no_confident_grade(facts)


def test_complete_technical_draw_still_grades_deterministically() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        method="technical_draw",
        ending_round=2,
        elapsed_seconds_in_round=90,
    )
    assert settle(_selection(*FAMILIES[0]), facts).result is SettlementResult.VOID
    assert (
        settle(
            MarketSelection(
                family=MarketFamily.GOES_DISTANCE,
                outcome=OutcomeKey.INSIDE_DISTANCE,
            ),
            facts,
        ).result
        is SettlementResult.WIN
    )
    assert settle(_selection(*FAMILIES[3]), facts).result is SettlementResult.VOID
    assert settle(_selection(*FAMILIES[5]), facts).result is SettlementResult.LOSS


@pytest.mark.parametrize(
    ("cancelled", "result_class", "method", "winner", "match"),
    [
        (True, "decisive", None, None, "cancelled bout cannot also have result_class"),
        (True, None, "ko_tko", None, "cancelled bout cannot have method"),
        (True, None, None, "a", "cancelled bout cannot have winner_side"),
        (False, "no_contest", "ko_tko", None, "no_contest cannot have method"),
        (False, "no_contest", None, "a", "no_contest cannot have winner_side"),
        (False, "no_contest", "decision", "a", "no_contest cannot have"),
    ],
)
def test_cancelled_and_no_contest_reject_retained_outcome_fields(
    cancelled: bool,
    result_class: str | None,
    method: str | None,
    winner: str | None,
    match: str,
) -> None:
    with pytest.raises(SettlementFactsError, match=match):
        validate_settlement_facts(
            BoutSettlementFacts(
                scheduled_rounds=3,
                cancelled=cancelled,
                result_class=result_class,  # type: ignore[arg-type]
                method=method,  # type: ignore[arg-type]
                winner_side=winner,  # type: ignore[arg-type]
            )
        )


def test_clean_no_contest_and_cancel_void_without_method() -> None:
    nc = BoutSettlementFacts(scheduled_rounds=3, result_class="no_contest")
    cancelled = BoutSettlementFacts(scheduled_rounds=3, cancelled=True)
    for family, outcome, line in FAMILIES:
        assert settle(_selection(family, outcome, line), nc).result is SettlementResult.VOID
        assert (
            settle(_selection(family, outcome, line), cancelled).result
            is SettlementResult.VOID
        )


def test_incomplete_matrix_parametrized_by_family() -> None:
    incomplete_cases = [
        BoutSettlementFacts(scheduled_rounds=3, method="technical_draw"),
        BoutSettlementFacts(
            scheduled_rounds=3,
            method="technical_decision",
            winner_side="a",
            ending_round=2,
            elapsed_seconds_in_round=50,
        ),
        BoutSettlementFacts(
            scheduled_rounds=3,
            method="decision",
            winner_side="b",
        ),
        BoutSettlementFacts(
            scheduled_rounds=3,
            method="ko_tko",
            winner_side="a",
            ending_round=1,
            elapsed_seconds_in_round=30,
        ),
        BoutSettlementFacts(scheduled_rounds=3, result_class="decisive"),
        BoutSettlementFacts(
            scheduled_rounds=3,
            ending_round=2,
            elapsed_seconds_in_round=150,
        ),
    ]
    for facts in incomplete_cases:
        _assert_no_confident_grade(facts)
