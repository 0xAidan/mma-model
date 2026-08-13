"""Bridge adapters derive temporal fields from DWCS-203 decisions (DWCS-204)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mma_model.domain.quote_eligibility import (
    QUOTE_ELIGIBILITY_DECISION_VERSION,
    compute_quote_eligibility_decision_identity,
)
from mma_model.odds.lifecycle import (
    AvailabilityNote,
    QuoteBlockReason,
    QuoteEligibilityDecision,
    QuoteValueEligibility,
    _build_quote_eligibility_decision,
)
from mma_model.odds.value_bridge import (
    closing_evidence_from_provider,
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


def _decision(
    *,
    evaluated_at: datetime = T0,
    eligible: bool = True,
    reason: QuoteBlockReason = QuoteBlockReason.NONE,
    lifecycle: str = "active",
    availability: str = "available",
    freshness_at: datetime | None = T0,
    resolved_bout_id: str | None = "bout-a",
    selection_identity: str = "moneyline:fighter_a",
    outcome_key: str = "fighter_a",
    quote_id: int = 7,
) -> QuoteEligibilityDecision:
    quote = _quote_row(
        id=quote_id,
        availability=availability,
        outcome_key=outcome_key,
    )
    return _build_quote_eligibility_decision(
        quote=quote,  # type: ignore[arg-type]
        evaluated_at=evaluated_at,
        eligible=eligible,
        status=(
            QuoteValueEligibility.ELIGIBLE
            if eligible
            else QuoteValueEligibility.BLOCKED
        ),
        reason=reason,
        detail="test",
        selection_identity=selection_identity,
        freshness_at=freshness_at,
        lifecycle_state_at_decision=lifecycle,
        resolved_bout_id=resolved_bout_id,
        availability_note=AvailabilityNote.NONE,
    )


def test_bridge_accepts_no_temporal_override_parameters() -> None:
    sig = inspect.signature(eligibility_evidence_from_decision)
    assert list(sig.parameters) == ["decision"]
    close_sig = inspect.signature(closing_evidence_from_provider)
    assert "closing_cutoff" not in close_sig.parameters
    assert "evaluated_at" not in close_sig.parameters
    assert "quote_availability_at_decision" not in close_sig.parameters
    assert "lifecycle_state_at_decision" not in close_sig.parameters
    assert "decision_version" not in close_sig.parameters


def test_bridge_derives_all_temporal_fields_from_decision() -> None:
    decision = _decision(evaluated_at=T1, freshness_at=T0)
    elig = eligibility_evidence_from_decision(decision)
    assert elig.evaluated_at == T1
    assert elig.quote_freshness_at == T0
    assert elig.lifecycle_state_at_decision == "active"
    assert elig.decision_version == QUOTE_ELIGIBILITY_DECISION_VERSION
    assert elig.decision_identity == decision.decision_identity
    assert elig.decision_identity.startswith("qe_v1:")
    quote = quote_evidence_from_row(
        _quote_row(),
        eligibility=elig,
        price_role=PriceObservationRole.OPENING,
    )
    assert quote.bout_id == "bout-a"
    assert quote.selection_identity == "moneyline:fighter_a"


def test_stale_decision_cannot_be_relabeled_as_current_via_bridge() -> None:
    """A decision evaluated at T0 cannot be presented as evaluated at T1."""
    stale = _decision(evaluated_at=T0)
    elig = eligibility_evidence_from_decision(stale)
    assert elig.evaluated_at == T0
    # Caller has no bridge knobs to rewrite evaluated_at; forging evidence fails identity.
    with pytest.raises(IneligiblePriceError, match="decision_identity"):
        from mma_model.value.evidence import QuoteEligibilityEvidence

        QuoteEligibilityEvidence(
            quote_id=elig.quote_id,
            eligible=elig.eligible,
            selection_identity=elig.selection_identity,
            resolved_bout_id=elig.resolved_bout_id,
            reason=elig.reason,
            evaluated_at=T1,  # attempted relabel
            quote_availability_at_decision=elig.quote_availability_at_decision,
            decision_identity=elig.decision_identity,  # old identity
            quote_freshness_at=elig.quote_freshness_at,
            lifecycle_state_at_decision=elig.lifecycle_state_at_decision,
            decision_version=elig.decision_version,
        )
    # Recomputed identity at T1 does not equal the stale decision identity.
    relabeled_id = compute_quote_eligibility_decision_identity(
        quote_id=stale.quote_id,
        evaluated_at=T1,
        eligible=stale.eligible,
        reason=stale.reason.value,
        selection_identity=stale.selection_identity,
        resolved_bout_id=stale.resolved_bout_id,
        quote_availability_at_decision=stale.quote_availability_at_decision,
        quote_freshness_at=stale.freshness_at,
        lifecycle_state_at_decision=stale.lifecycle_state_at_decision,
        decision_version=stale.decision_version,
    )
    assert relabeled_id != stale.decision_identity


def test_closing_bridge_cutoff_equals_decision_evaluated_at() -> None:
    decision = _decision(evaluated_at=T1, freshness_at=T0)
    closing = closing_evidence_from_provider(
        _quote_row(observed_at=T1),  # type: ignore[arg-type]
        decision,
    )
    assert closing.closing_cutoff == T1
    assert closing.eligibility_evidence is not None
    assert closing.eligibility_evidence.evaluated_at == T1
    assert (
        closing.eligibility_evidence.decision_identity == decision.decision_identity
    )


def test_bridge_rejects_selection_mismatch_between_quote_and_eligibility() -> None:
    elig = eligibility_evidence_from_decision(
        _decision(
            selection_identity="moneyline:fighter_b",
            outcome_key="fighter_b",
        )
    )
    with pytest.raises(IneligiblePriceError, match="exactly match"):
        quote_evidence_from_row(_quote_row(), eligibility=elig)


def test_bridge_rejects_quote_id_mismatch() -> None:
    elig = eligibility_evidence_from_decision(_decision(quote_id=99))
    with pytest.raises(IneligiblePriceError, match="quote.id"):
        quote_evidence_from_row(_quote_row(id=7), eligibility=elig)
