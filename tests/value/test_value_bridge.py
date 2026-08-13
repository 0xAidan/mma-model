"""Bridge adapters derive bout/selection from eligibility (DWCS-204)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mma_model.odds.lifecycle import (
    AvailabilityNote,
    QuoteBlockReason,
    QuoteEligibilityDecision,
    QuoteValueEligibility,
)
from mma_model.odds.value_bridge import (
    eligibility_evidence_from_decision,
    quote_evidence_from_row,
)
from mma_model.value.errors import IneligiblePriceError
from mma_model.value.evidence import PriceObservationRole

T0 = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _quote_row(**overrides: object) -> SimpleNamespace:
    base = {
        "id": 7,
        "provider": "the_odds_api",
        "bookmaker_key": "ref_book",
        "region": "us",
        "market_family": "moneyline",
        "outcome_key": "fighter_a",
        "line_point": None,
        "price_decimal": 2.1,
        "availability": "available",
        "observed_at": T0,
        "dedupe_key": "d1",
        "external_event_id": "ext-1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _decision(**overrides: object) -> QuoteEligibilityDecision:
    kwargs = {
        "quote_id": 7,
        "eligible": True,
        "status": QuoteValueEligibility.ELIGIBLE,
        "reason": QuoteBlockReason.NONE,
        "detail": "",
        "selection_identity": "moneyline:fighter_a",
        "bookmaker_key": "ref_book",
        "region": "us",
        "market_family": "moneyline",
        "outcome_key": "fighter_a",
        "line_point": None,
        "freshness_at": T0,
        "resolved_bout_id": "bout-a",
        "availability_note": AvailabilityNote.NONE,
    }
    kwargs.update(overrides)
    return QuoteEligibilityDecision(**kwargs)  # type: ignore[arg-type]


def test_bridge_derives_bout_and_selection_from_eligibility() -> None:
    elig = eligibility_evidence_from_decision(
        _decision(),
        evaluated_at=T0,
        quote_availability_at_decision="available",
        lifecycle_state_at_decision="active",
    )
    assert elig.evaluated_at == T0
    assert elig.resolved_bout_id == "bout-a"
    quote = quote_evidence_from_row(
        _quote_row(),
        eligibility=elig,
        price_role=PriceObservationRole.OPENING,
    )
    assert quote.bout_id == "bout-a"
    assert quote.selection_identity == "moneyline:fighter_a"
    assert quote.quote_id == 7


def test_bridge_rejects_selection_mismatch_between_quote_and_eligibility() -> None:
    elig = eligibility_evidence_from_decision(
        _decision(selection_identity="moneyline:fighter_b", outcome_key="fighter_b"),
        evaluated_at=T0,
        quote_availability_at_decision="available",
    )
    with pytest.raises(IneligiblePriceError, match="exactly match"):
        quote_evidence_from_row(_quote_row(), eligibility=elig)


def test_bridge_rejects_quote_id_mismatch() -> None:
    elig = eligibility_evidence_from_decision(
        _decision(quote_id=99),
        evaluated_at=T0,
        quote_availability_at_decision="available",
    )
    with pytest.raises(IneligiblePriceError, match="quote.id"):
        quote_evidence_from_row(_quote_row(id=7), eligibility=elig)


def test_bridge_requires_evaluated_at_cutoff() -> None:
    elig = eligibility_evidence_from_decision(
        _decision(),
        evaluated_at=T1,
        quote_availability_at_decision="available",
    )
    assert elig.evaluated_at == T1
    assert elig.decision_identity.startswith("elig_v1:")
