"""DWCS-203 bout/line lifecycle states and value eligibility."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsBoutLifecycleObservation

VALUE_BLOCKING_LIFECYCLES: Final[frozenset[str]] = frozenset(
    {
        "stale",
        "missing_unknown",
        "locked",
        "cancelled",
        "replaced",
        "review_blocked",
    }
)


class OddsBoutLifecycleState(StrEnum):
    """Explicit bout/line lifecycle for matched odds (no inferred locks)."""

    ACTIVE = "active"
    STALE = "stale"
    MISSING_UNKNOWN = "missing_unknown"
    LOCKED = "locked"
    CANCELLED = "cancelled"
    REPLACED = "replaced"
    REVIEW_BLOCKED = "review_blocked"


class QuoteValueEligibility(StrEnum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


def classify_quote_value_eligibility(
    *,
    match_status: str,
    lifecycle: OddsBoutLifecycleState | str,
) -> QuoteValueEligibility:
    """Only matched + active lifecycles may reach value calculations."""
    life = (
        lifecycle.value
        if isinstance(lifecycle, OddsBoutLifecycleState)
        else str(lifecycle)
    )
    if match_status != "matched":
        return QuoteValueEligibility.BLOCKED
    if life != OddsBoutLifecycleState.ACTIVE.value:
        return QuoteValueEligibility.BLOCKED
    return QuoteValueEligibility.ELIGIBLE


def _lifecycle_dedupe_key(
    *,
    bout_id: str,
    lifecycle: str,
    evidence_kind: str,
    observed_at: datetime,
    provider: str | None,
    external_event_id: str | None,
) -> str:
    material = "|".join(
        [
            bout_id,
            lifecycle,
            evidence_kind,
            provider or "",
            external_event_id or "",
            observed_at.astimezone(UTC).isoformat(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def apply_bout_lifecycle(
    session: Session,
    *,
    bout_id: str,
    lifecycle: OddsBoutLifecycleState,
    evidence_kind: str,
    observed_at: datetime,
    provider: str | None = None,
    external_event_id: str | None = None,
    detail: str | None = None,
    price_decimal: float | None = None,
) -> OddsBoutLifecycleObservation:
    """Append an explicit lifecycle observation; never forward-fill prices."""
    kind = (evidence_kind or "").strip()
    if not kind:
        raise ValueError("lifecycle evidence_kind is required (no inferred locks)")
    if price_decimal is not None:
        raise ValueError(
            "refusing lifecycle price_decimal forward-fill; "
            "lifecycle rows never store prices"
        )
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if lifecycle is OddsBoutLifecycleState.LOCKED and kind in {
        "inferred",
        "guess",
        "assumed",
    }:
        raise ValueError("refusing inferred lock without provider evidence")

    dedupe = _lifecycle_dedupe_key(
        bout_id=bout_id,
        lifecycle=lifecycle.value,
        evidence_kind=kind,
        observed_at=observed_at,
        provider=provider,
        external_event_id=external_event_id,
    )
    existing = session.scalar(
        select(OddsBoutLifecycleObservation).where(
            OddsBoutLifecycleObservation.dedupe_key == dedupe
        )
    )
    if existing is not None:
        return existing

    row = OddsBoutLifecycleObservation(
        dedupe_key=dedupe,
        bout_id=bout_id,
        provider=provider,
        external_event_id=external_event_id,
        lifecycle=lifecycle.value,
        evidence_kind=kind,
        detail=detail,
        price_decimal=None,
        observed_at=observed_at.astimezone(UTC),
    )
    session.add(row)
    session.flush()
    return row
