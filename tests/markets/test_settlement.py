"""Pure settlement tests for every v1 market family and edge case (DWCS-200)."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.markets.rules import (
    ProvisionalRuleSetError,
    get_rule_set,
    load_settlement_rules,
)
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementResult,
    settle,
)


def _ml(outcome: OutcomeKey) -> MarketSelection:
    return MarketSelection(family=MarketFamily.MONEYLINE, outcome=outcome)


def test_moneyline_win_loss_draw_nc_cancel() -> None:
    facts_a = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=1,
    )
    assert settle(_ml(OutcomeKey.FIGHTER_A), facts_a).result is SettlementResult.WIN
    assert settle(_ml(OutcomeKey.FIGHTER_B), facts_a).result is SettlementResult.LOSS

    draw = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)
    assert settle(_ml(OutcomeKey.FIGHTER_A), draw).result is SettlementResult.PUSH

    nc = BoutSettlementFacts(scheduled_rounds=3, result_class="no_contest")
    assert settle(_ml(OutcomeKey.FIGHTER_A), nc).result is SettlementResult.VOID

    cancelled = BoutSettlementFacts(scheduled_rounds=3, cancelled=True)
    assert settle(_ml(OutcomeKey.FIGHTER_B), cancelled).result is SettlementResult.VOID


def test_moneyline_technical_decision_settles_on_winner() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="b",
        method="technical_decision",
        ending_round=2,
    )
    assert settle(_ml(OutcomeKey.FIGHTER_B), facts).result is SettlementResult.WIN
    assert settle(_ml(OutcomeKey.FIGHTER_A), facts).result is SettlementResult.LOSS


def test_goes_distance_and_inside_distance() -> None:
    finish = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="submission",
        ending_round=2,
    )
    decision = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="decision",
        ending_round=3,
    )
    draw = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)

    goes = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.GOES_DISTANCE
    )
    inside = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.INSIDE_DISTANCE
    )
    assert settle(goes, finish).result is SettlementResult.LOSS
    assert settle(inside, finish).result is SettlementResult.WIN
    assert settle(goes, decision).result is SettlementResult.WIN
    assert settle(inside, decision).result is SettlementResult.LOSS
    assert settle(goes, draw).result is SettlementResult.WIN
    assert settle(inside, draw).result is SettlementResult.LOSS

    tech = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="technical_decision",
        ending_round=2,
    )
    assert settle(goes, tech).result is SettlementResult.WIN


def test_totals_half_round_boundary_no_push() -> None:
    r1 = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=1,
    )
    r2 = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=2,
    )
    decision = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="b",
        method="decision",
        ending_round=3,
    )
    over_15 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under_15 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    over_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.5
    )
    under_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=2.5
    )

    assert settle(over_15, r1).result is SettlementResult.LOSS
    assert settle(under_15, r1).result is SettlementResult.WIN
    assert settle(over_15, r2).result is SettlementResult.WIN
    assert settle(under_15, r2).result is SettlementResult.LOSS
    assert settle(over_25, r2).result is SettlementResult.LOSS
    assert settle(under_25, r2).result is SettlementResult.WIN
    assert settle(over_25, decision).result is SettlementResult.WIN
    assert settle(under_25, decision).result is SettlementResult.LOSS


def test_totals_whole_line_push() -> None:
    # Whole-number line support for push semantics (not in default offered set).
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=2,
    )
    over_2 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.0
    )
    # 2.0 is not in the canonical offered line set → selection validation fails.
    with pytest.raises(ValueError, match="invalid line_point"):
        settle(over_2, facts)


def test_method_and_fighter_by_method() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="submission",
        ending_round=1,
    )
    sub = MarketSelection(family=MarketFamily.METHOD, outcome=OutcomeKey.SUBMISSION)
    ko = MarketSelection(family=MarketFamily.METHOD, outcome=OutcomeKey.KO_TKO)
    assert settle(sub, facts).result is SettlementResult.WIN
    assert settle(ko, facts).result is SettlementResult.LOSS

    a_sub = MarketSelection(
        family=MarketFamily.FIGHTER_BY_METHOD, outcome=OutcomeKey.A_SUBMISSION
    )
    b_sub = MarketSelection(
        family=MarketFamily.FIGHTER_BY_METHOD, outcome=OutcomeKey.B_SUBMISSION
    )
    a_dec = MarketSelection(
        family=MarketFamily.FIGHTER_BY_METHOD, outcome=OutcomeKey.A_DECISION
    )
    assert settle(a_sub, facts).result is SettlementResult.WIN
    assert settle(b_sub, facts).result is SettlementResult.LOSS
    assert settle(a_dec, facts).result is SettlementResult.LOSS

    tech = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="b",
        method="technical_decision",
        ending_round=3,
    )
    b_decision = MarketSelection(
        family=MarketFamily.FIGHTER_BY_METHOD, outcome=OutcomeKey.B_DECISION
    )
    assert settle(b_decision, tech).result is SettlementResult.WIN

    draw = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)
    assert settle(sub, draw).result is SettlementResult.VOID
    assert settle(a_sub, draw).result is SettlementResult.VOID


def test_exact_round_finish_decision_nc() -> None:
    r2 = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=2,
    )
    round_2 = MarketSelection(
        family=MarketFamily.EXACT_ROUND, outcome=OutcomeKey.ROUND_2
    )
    round_1 = MarketSelection(
        family=MarketFamily.EXACT_ROUND, outcome=OutcomeKey.ROUND_1
    )
    assert settle(round_2, r2).result is SettlementResult.WIN
    assert settle(round_1, r2).result is SettlementResult.LOSS

    decision = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="decision",
        ending_round=3,
    )
    assert settle(round_2, decision).result is SettlementResult.LOSS

    nc = BoutSettlementFacts(scheduled_rounds=3, result_class="no_contest")
    assert settle(round_2, nc).result is SettlementResult.VOID


def test_pending_and_missing_facts_unresolved() -> None:
    pending = BoutSettlementFacts(scheduled_rounds=3, pending=True)
    assert (
        settle(_ml(OutcomeKey.FIGHTER_A), pending).result is SettlementResult.UNRESOLVED
    )

    incomplete = BoutSettlementFacts(scheduled_rounds=3, result_class="decisive")
    assert (
        settle(_ml(OutcomeKey.FIGHTER_A), incomplete).result
        is SettlementResult.UNRESOLVED
    )


def test_settlement_is_versioned_and_deterministic() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="decision",
        ending_round=3,
    )
    first = settle(_ml(OutcomeKey.FIGHTER_A), facts)
    second = settle(_ml(OutcomeKey.FIGHTER_A), facts)
    assert first == second
    assert first.rule_set_id == "mma_generic"
    assert first.rule_set_version == "1.0.0"
    assert first.reason


def test_unknown_outcome_fails_before_settle() -> None:
    with pytest.raises(ValueError, match="not valid"):
        settle(
            MarketSelection(family=MarketFamily.MONEYLINE, outcome=OutcomeKey.OVER),
            BoutSettlementFacts(scheduled_rounds=3, pending=True),
        )


def test_provisional_bet365_requires_allow_flag() -> None:
    with pytest.raises(ProvisionalRuleSetError):
        get_rule_set("bet365_mma")
    provisional = get_rule_set("bet365_mma", allow_provisional=True)
    assert provisional.rule_set_id == "bet365_mma"
    assert provisional.status.value == "provisional_pending_approved_source"
    # Extends generic: draw still push
    facts = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)
    decision = settle(
        _ml(OutcomeKey.FIGHTER_A),
        facts,
        rule_set_id="bet365_mma",
        allow_provisional=True,
    )
    assert decision.result is SettlementResult.PUSH
    assert decision.rule_set_id == "bet365_mma"


def test_default_rules_load_from_packaged_contract() -> None:
    contract = load_settlement_rules()
    assert contract.contract_id == "dwcs_settlement"
    assert contract.default_rule_set_id == "mma_generic"
    assert "mma_generic" in contract.rule_sets
    assert "bet365_mma" in contract.rule_sets
