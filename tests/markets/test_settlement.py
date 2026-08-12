"""Pure settlement tests for every v1 market family and edge case (DWCS-200)."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.markets.rules import (
    PINNED_SETTLEMENT_HASH,
    ProvisionalRuleSetError,
    get_rule_set,
    load_settlement_rules,
)
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementFactsError,
    SettlementResult,
    settle,
)


def _ml(outcome: OutcomeKey) -> MarketSelection:
    return MarketSelection(family=MarketFamily.MONEYLINE, outcome=outcome)


def _finish(
    *,
    ending_round: int,
    elapsed_seconds_in_round: int,
    winner_side: str = "a",
    method: str = "ko_tko",
    scheduled_rounds: int = 3,
) -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=scheduled_rounds,
        result_class="decisive",
        winner_side=winner_side,  # type: ignore[arg-type]
        method=method,  # type: ignore[arg-type]
        ending_round=ending_round,
        elapsed_seconds_in_round=elapsed_seconds_in_round,
    )


def test_moneyline_win_loss_draw_nc_cancel() -> None:
    facts_a = _finish(ending_round=1, elapsed_seconds_in_round=45)
    assert settle(_ml(OutcomeKey.FIGHTER_A), facts_a).result is SettlementResult.WIN
    assert settle(_ml(OutcomeKey.FIGHTER_B), facts_a).result is SettlementResult.LOSS

    draw = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)
    assert settle(_ml(OutcomeKey.FIGHTER_A), draw).result is SettlementResult.VOID

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
        elapsed_seconds_in_round=100,
    )
    assert settle(_ml(OutcomeKey.FIGHTER_B), facts).result is SettlementResult.WIN
    assert settle(_ml(OutcomeKey.FIGHTER_A), facts).result is SettlementResult.LOSS


def test_goes_distance_and_inside_distance() -> None:
    finish = _finish(ending_round=2, elapsed_seconds_in_round=60, method="submission")
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

    # Early technical decision ends before scheduled rounds → inside_distance
    # (Sky/Paddy/bet365/Bodog Yes requires full stated rounds).
    tech = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="technical_decision",
        ending_round=2,
        elapsed_seconds_in_round=90,
    )
    assert settle(goes, tech).result is SettlementResult.LOSS
    assert settle(inside, tech).result is SettlementResult.WIN


def test_goes_distance_full_distance_decision_still_yes() -> None:
    goes = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.GOES_DISTANCE
    )
    inside = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.INSIDE_DISTANCE
    )
    decision = BoutSettlementFacts(
        scheduled_rounds=5,
        result_class="decisive",
        winner_side="b",
        method="decision",
        ending_round=5,
    )
    assert settle(goes, decision).result is SettlementResult.WIN
    assert settle(inside, decision).result is SettlementResult.LOSS


def test_goes_distance_early_technical_draw_is_inside() -> None:
    goes = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.GOES_DISTANCE
    )
    inside = MarketSelection(
        family=MarketFamily.GOES_DISTANCE, outcome=OutcomeKey.INSIDE_DISTANCE
    )
    tech_draw = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        method="technical_draw",
        ending_round=1,
        elapsed_seconds_in_round=200,
    )
    assert settle(goes, tech_draw).result is SettlementResult.LOSS
    assert settle(inside, tech_draw).result is SettlementResult.WIN


@pytest.mark.parametrize(
    ("ending_round", "elapsed", "over_15", "under_15"),
    [
        (2, 149, SettlementResult.LOSS, SettlementResult.WIN),  # 2:29 — under
        (2, 150, SettlementResult.LOSS, SettlementResult.WIN),  # 2:30 — under (Sky/PP)
        (2, 151, SettlementResult.WIN, SettlementResult.LOSS),  # 2:31 — over
    ],
)
def test_totals_1_5_boundary_before_at_after(
    ending_round: int,
    elapsed: int,
    over_15: SettlementResult,
    under_15: SettlementResult,
) -> None:
    facts = _finish(ending_round=ending_round, elapsed_seconds_in_round=elapsed)
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    assert settle(over, facts).result is over_15
    assert settle(under, facts).result is under_15


@pytest.mark.parametrize(
    ("ending_round", "elapsed", "over_25", "under_25"),
    [
        (3, 149, SettlementResult.LOSS, SettlementResult.WIN),  # 2:29 of R3 — under 2.5
        (3, 150, SettlementResult.LOSS, SettlementResult.WIN),  # 2:30 of R3 — under
        (3, 151, SettlementResult.WIN, SettlementResult.LOSS),  # 2:31 of R3 — over 2.5
    ],
)
def test_totals_2_5_boundary_before_at_after(
    ending_round: int,
    elapsed: int,
    over_25: SettlementResult,
    under_25: SettlementResult,
) -> None:
    facts = _finish(ending_round=ending_round, elapsed_seconds_in_round=elapsed)
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=2.5
    )
    assert settle(over, facts).result is over_25
    assert settle(under, facts).result is under_25


def test_totals_round1_stoppage_is_under_1_5() -> None:
    facts = _finish(ending_round=1, elapsed_seconds_in_round=1)
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    assert settle(over, facts).result is SettlementResult.LOSS
    assert settle(under, facts).result is SettlementResult.WIN


def test_totals_early_round2_stoppage_is_under_1_5() -> None:
    """Stoppage at 0:01 of round 2 is NOT over 1.5."""
    facts = _finish(ending_round=2, elapsed_seconds_in_round=1)
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    assert settle(over, facts).result is SettlementResult.LOSS
    assert settle(under, facts).result is SettlementResult.WIN


def test_totals_decision_settles_as_full_distance() -> None:
    decision = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="b",
        method="decision",
    )
    over_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.5
    )
    under_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=2.5
    )
    assert settle(over_25, decision).result is SettlementResult.WIN
    assert settle(under_25, decision).result is SettlementResult.LOSS


def test_totals_ordinary_draw_settles_as_full_distance() -> None:
    draw = BoutSettlementFacts(scheduled_rounds=3, result_class="draw")
    over_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.5
    )
    under_25 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=2.5
    )
    assert settle(over_25, draw).result is SettlementResult.WIN
    assert settle(under_25, draw).result is SettlementResult.LOSS


def test_totals_technical_decision_without_clocks_unresolved() -> None:
    """Tech decision must not inherit full_scheduled inventing stoppage time."""
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="technical_decision",
    )
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    assert settle(over, facts).result is SettlementResult.UNRESOLVED


def test_totals_early_technical_draw_uses_stoppage_clocks() -> None:
    before = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        method="technical_draw",
        ending_round=2,
        elapsed_seconds_in_round=100,
    )
    after = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        method="technical_draw",
        ending_round=2,
        elapsed_seconds_in_round=200,
    )
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    assert settle(over, before).result is SettlementResult.LOSS
    assert settle(under, before).result is SettlementResult.WIN
    assert settle(over, after).result is SettlementResult.WIN
    assert settle(under, after).result is SettlementResult.LOSS


def test_moneyline_and_method_technical_draw_void() -> None:
    tech_draw = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        method="technical_draw",
        ending_round=2,
        elapsed_seconds_in_round=90,
    )
    assert settle(_ml(OutcomeKey.FIGHTER_A), tech_draw).result is SettlementResult.VOID
    method = MarketSelection(family=MarketFamily.METHOD, outcome=OutcomeKey.DECISION)
    assert settle(method, tech_draw).result is SettlementResult.VOID
    exact = MarketSelection(family=MarketFamily.EXACT_ROUND, outcome=OutcomeKey.ROUND_2)
    assert settle(exact, tech_draw).result is SettlementResult.LOSS


def test_totals_missing_duration_unresolved() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=2,
        # missing elapsed_seconds_in_round and total_elapsed_seconds
    )
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    decision = settle(over, facts)
    assert decision.result is SettlementResult.UNRESOLVED
    assert "duration" in decision.reason


def test_totals_rejects_non_canonical_whole_number_line() -> None:
    facts = _finish(ending_round=2, elapsed_seconds_in_round=60)
    over_2 = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=2.0
    )
    with pytest.raises(ValueError, match="invalid line_point"):
        settle(over_2, facts)


def test_method_and_fighter_by_method() -> None:
    facts = _finish(ending_round=1, elapsed_seconds_in_round=30, method="submission")
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
    r2 = _finish(ending_round=2, elapsed_seconds_in_round=40)
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


def test_exact_round_rejects_round_beyond_schedule() -> None:
    three = _finish(ending_round=2, elapsed_seconds_in_round=10, scheduled_rounds=3)
    with pytest.raises(ValueError, match="scheduled_rounds=3"):
        settle(
            MarketSelection(family=MarketFamily.EXACT_ROUND, outcome=OutcomeKey.ROUND_5),
            three,
        )

    five = _finish(
        ending_round=5,
        elapsed_seconds_in_round=20,
        scheduled_rounds=5,
    )
    assert (
        settle(
            MarketSelection(family=MarketFamily.EXACT_ROUND, outcome=OutcomeKey.ROUND_5),
            five,
        ).result
        is SettlementResult.WIN
    )


def test_ending_round_beyond_schedule_is_invalid_fact() -> None:
    with pytest.raises(SettlementFactsError, match="ending_round"):
        settle(
            _ml(OutcomeKey.FIGHTER_A),
            BoutSettlementFacts(
                scheduled_rounds=3,
                result_class="decisive",
                winner_side="a",
                method="ko_tko",
                ending_round=4,
                elapsed_seconds_in_round=10,
            ),
        )


def test_pending_and_missing_facts_unresolved() -> None:
    # Clean pending (no completed result fields) is a valid transitional state.
    pending = BoutSettlementFacts(scheduled_rounds=3, pending=True)
    assert (
        settle(_ml(OutcomeKey.FIGHTER_A), pending).result is SettlementResult.UNRESOLVED
    )

    incomplete = BoutSettlementFacts(scheduled_rounds=3, result_class="decisive")
    assert (
        settle(_ml(OutcomeKey.FIGHTER_A), incomplete).result
        is SettlementResult.UNRESOLVED
    )


def test_invalid_fact_combinations_raise() -> None:
    with pytest.raises(SettlementFactsError, match="unsupported scheduled_rounds"):
        settle(_ml(OutcomeKey.FIGHTER_A), BoutSettlementFacts(scheduled_rounds=4))

    with pytest.raises(SettlementFactsError, match="elapsed_seconds_in_round"):
        settle(
            _ml(OutcomeKey.FIGHTER_A),
            _finish(ending_round=1, elapsed_seconds_in_round=301),
        )

    with pytest.raises(SettlementFactsError, match="cancelled bout cannot"):
        settle(
            _ml(OutcomeKey.FIGHTER_A),
            BoutSettlementFacts(
                scheduled_rounds=3,
                cancelled=True,
                result_class="decisive",
                winner_side="a",
            ),
        )

    with pytest.raises(SettlementFactsError, match="draw cannot have winner_side"):
        settle(
            _ml(OutcomeKey.FIGHTER_A),
            BoutSettlementFacts(
                scheduled_rounds=3,
                result_class="draw",
                winner_side="a",
            ),
        )

    with pytest.raises(SettlementFactsError, match="clock fields disagree"):
        settle(
            MarketSelection(
                family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
            ),
            BoutSettlementFacts(
                scheduled_rounds=3,
                result_class="decisive",
                winner_side="a",
                method="ko_tko",
                ending_round=2,
                elapsed_seconds_in_round=150,
                total_elapsed_seconds=400,
            ),
        )


def test_settlement_is_versioned_hashed_and_deterministic() -> None:
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
    assert first.rule_set_version == "1.3.0"
    assert first.content_hash == PINNED_SETTLEMENT_HASH
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
    facts = BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3)
    decision = settle(
        _ml(OutcomeKey.FIGHTER_A),
        facts,
        rule_set_id="bet365_mma",
        allow_provisional=True,
    )
    assert decision.result is SettlementResult.VOID
    assert decision.rule_set_id == "bet365_mma"


def test_default_rules_are_externally_sourced_public_house_rules() -> None:
    contract = load_settlement_rules()
    assert contract.contract_id == "dwcs_settlement"
    assert contract.contract_version == "1.3.0"
    assert contract.default_rule_set_id == "mma_generic"
    generic = contract.rule_sets["mma_generic"]
    assert generic.status.value == "externally_sourced"
    assert "not universal sportsbook rules" in generic.source.citation.lower()
    https_refs = [r for r in generic.source.references if r.locator.startswith("https://")]
    assert https_refs
    assert "bet365_mma" in contract.rule_sets
    assert "bodog_mma" in contract.rule_sets
