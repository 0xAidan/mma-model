"""Clock consistency, pending-state, and source-governance settlement tests."""

from __future__ import annotations

import pytest

from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.markets.rules import (
    RuleSetStatus,
    default_settlement_rules,
    get_rule_set,
    load_settlement_rules,
)
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementFactsError,
    SettlementResult,
    clock_pairs_for_total,
    settle,
    validate_settlement_facts,
)


@pytest.mark.parametrize(
    ("ending", "in_round", "total", "ok"),
    [
        # Consistent two-field: ending + in_round
        (2, 150, None, True),
        # Consistent two-field: ending + total (exact half of R2)
        (2, None, 450, True),
        # Consistent two-field: in_round + total
        (None, 150, 450, True),
        # Consistent three-field
        (2, 150, 450, True),
        # Round-boundary dual forms for total=300 (end R1)
        (1, 300, 300, True),
        (2, 0, 300, True),
        (1, None, 300, True),
        (2, None, 300, True),
        (None, 300, 300, True),
        (None, 0, 300, True),
        # Inconsistent: ending_round=1 with large total
        (1, None, 700, False),
        # Inconsistent: in_round alone cannot imply that total
        (None, 10, 700, False),
        # Inconsistent three-field
        (2, 150, 400, False),
        # Inconsistent ending+in_round vs total at boundary misuse
        (1, 0, 300, False),
    ],
)
def test_clock_field_combinations(
    ending: int | None,
    in_round: int | None,
    total: int | None,
    ok: bool,
) -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=ending,
        elapsed_seconds_in_round=in_round,
        total_elapsed_seconds=total,
    )
    if ok:
        validate_settlement_facts(facts)
    else:
        with pytest.raises(SettlementFactsError):
            validate_settlement_facts(facts)


def test_round_boundary_pairs_for_total() -> None:
    assert clock_pairs_for_total(300, scheduled_rounds=3, round_seconds=300) == frozenset(
        {(1, 300), (2, 0)}
    )
    assert clock_pairs_for_total(450, scheduled_rounds=3, round_seconds=300) == frozenset(
        {(2, 150)}
    )


def test_pending_with_completed_result_rejected() -> None:
    with pytest.raises(SettlementFactsError, match="pending bout cannot have result_class"):
        settle(
            MarketSelection(family=MarketFamily.MONEYLINE, outcome=OutcomeKey.FIGHTER_A),
            BoutSettlementFacts(
                scheduled_rounds=3,
                pending=True,
                result_class="decisive",
                winner_side="a",
                method="ko_tko",
            ),
        )


def test_pending_with_method_or_winner_rejected() -> None:
    with pytest.raises(SettlementFactsError, match="pending bout cannot have winner_side"):
        validate_settlement_facts(
            BoutSettlementFacts(scheduled_rounds=3, pending=True, winner_side="a")
        )
    with pytest.raises(SettlementFactsError, match="pending bout cannot have method"):
        validate_settlement_facts(
            BoutSettlementFacts(scheduled_rounds=3, pending=True, method="decision")
        )


def test_pending_and_cancelled_rejected() -> None:
    with pytest.raises(SettlementFactsError, match="pending bout cannot also be cancelled"):
        validate_settlement_facts(
            BoutSettlementFacts(scheduled_rounds=3, pending=True, cancelled=True)
        )


def test_clean_pending_settles_unresolved() -> None:
    decision = settle(
        MarketSelection(family=MarketFamily.MONEYLINE, outcome=OutcomeKey.FIGHTER_A),
        BoutSettlementFacts(scheduled_rounds=3, pending=True),
    )
    assert decision.result is SettlementResult.UNRESOLVED


def test_externally_sourced_default_has_https_citations() -> None:
    contract = load_settlement_rules()
    generic = contract.rule_sets["mma_generic"]
    assert generic.status is RuleSetStatus.EXTERNALLY_SOURCED
    https_refs = [r for r in generic.source.references if r.locator.startswith("https://")]
    assert len(https_refs) >= 2
    assert all(r.accessed for r in https_refs)
    assert contract.rule_sets["bodog_mma"].status is RuleSetStatus.EXTERNALLY_SOURCED
    assert contract.rule_sets["bet365_mma"].status is (
        RuleSetStatus.PROVISIONAL_PENDING_APPROVED_SOURCE
    )


def test_bodog_override_voids_exact_half() -> None:
    facts = BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side="a",
        method="ko_tko",
        ending_round=2,
        elapsed_seconds_in_round=150,
    )
    over = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.OVER, line_point=1.5
    )
    under = MarketSelection(
        family=MarketFamily.TOTALS, outcome=OutcomeKey.UNDER, line_point=1.5
    )
    assert settle(over, facts).result is SettlementResult.LOSS  # generic: under wins
    assert settle(under, facts).result is SettlementResult.WIN
    bodog = get_rule_set("bodog_mma")
    assert settle(over, facts, rule_set=bodog).result is SettlementResult.VOID
    assert settle(under, facts, rule_set=bodog).result is SettlementResult.VOID


def test_default_is_not_provisional() -> None:
    contract = default_settlement_rules()
    assert (
        contract.rule_sets[contract.default_rule_set_id].status
        is not RuleSetStatus.PROVISIONAL_PENDING_APPROVED_SOURCE
    )
