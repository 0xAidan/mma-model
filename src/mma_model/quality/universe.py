"""Load and fail-close on frozen DWCS universe contracts."""

from __future__ import annotations

from dataclasses import dataclass

from mma_model.dwcs.manifest import (
    PINNED_EXPECTED_UNIVERSE_HASH,
    load_dwcs_bout_manifest,
    load_dwcs_event_manifest,
    validate_expected_universe,
)
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, load_evaluation_contract


class UniverseContractError(ValueError):
    """Raised when evaluation and expected-universe contracts disagree."""


@dataclass(frozen=True)
class UniverseContract:
    cards: int
    bouts: int
    standard_cards: int
    standard_bouts: int
    brazil_cards: int
    brazil_bouts: int
    expected_universe_hash: str
    evaluation_contract_hash: str


def load_universe_contract() -> UniverseContract:
    events = load_dwcs_event_manifest()
    bouts = load_dwcs_bout_manifest()
    expected = validate_expected_universe(events=events, bouts=bouts)
    contract = load_evaluation_contract()
    eval_cards = int(contract.universe.all_dwcs.cards)
    eval_bouts = int(contract.universe.all_dwcs.bouts)
    eval_standard_cards = int(contract.universe.standard_only.cards)
    eval_standard_bouts = int(contract.universe.standard_only.bouts)
    eval_brazil_cards = int(contract.universe.brazil.cards)
    eval_brazil_bouts = int(contract.universe.brazil.bouts)
    expected_cards = int(expected["cards"]["all"])
    expected_bouts = int(expected["bouts"]["all"])
    if (
        eval_cards != expected_cards
        or eval_bouts != expected_bouts
        or eval_standard_cards != int(expected["cards"]["standard"])
        or eval_standard_bouts != int(expected["bouts"]["standard"])
        or eval_brazil_cards != int(expected["cards"]["brazil"])
        or eval_brazil_bouts != int(expected["bouts"]["brazil"])
    ):
        raise UniverseContractError(
            "evaluation contract universe disagrees with expected-universe contract"
        )
    return UniverseContract(
        cards=eval_cards,
        bouts=eval_bouts,
        standard_cards=eval_standard_cards,
        standard_bouts=eval_standard_bouts,
        brazil_cards=eval_brazil_cards,
        brazil_bouts=eval_brazil_bouts,
        expected_universe_hash=PINNED_EXPECTED_UNIVERSE_HASH,
        evaluation_contract_hash=PINNED_CONTRACT_HASH,
    )
