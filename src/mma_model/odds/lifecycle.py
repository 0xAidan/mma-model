"""DWCS-203 bout/line lifecycle states, precedence, and value eligibility."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import (
    OddsBoutLifecycleObservation,
    OddsProviderEventAlias,
    OddsQuote,
)

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

# Terminal/blocking states cannot be overridden by a bare match→ACTIVE append.
TERMINAL_LIFECYCLES: Final[frozenset[str]] = frozenset(
    {
        "locked",
        "cancelled",
        "replaced",
        "review_blocked",
    }
)

# Explicit evidence kinds allowed to leave a terminal lifecycle.
_TERMINAL_EXIT_EVIDENCE: Final[frozenset[str]] = frozenset(
    {
        "explicit_lifecycle_reactivation",
        "operator_lifecycle_clear",
        "provider_unlock_signal",
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


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware UTC (naive rejected)")
    return value.astimezone(UTC)


def _as_utc_sqlite(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def latest_bout_lifecycle(
    session: Session,
    *,
    bout_id: str,
    provider: str | None = None,
    external_event_id: str | None = None,
) -> OddsBoutLifecycleObservation | None:
    """Latest explicit lifecycle row for a bout (optionally scoped to provider event)."""
    stmt = select(OddsBoutLifecycleObservation).where(
        OddsBoutLifecycleObservation.bout_id == bout_id
    )
    if provider is not None:
        stmt = stmt.where(
            (OddsBoutLifecycleObservation.provider == provider)
            | (OddsBoutLifecycleObservation.provider.is_(None))
        )
    if external_event_id is not None:
        stmt = stmt.where(
            (OddsBoutLifecycleObservation.external_event_id == external_event_id)
            | (OddsBoutLifecycleObservation.external_event_id.is_(None))
        )
    rows = list(session.scalars(stmt).all())
    if not rows:
        return None

    def _sort_key(row: OddsBoutLifecycleObservation) -> tuple[datetime, int]:
        return (_as_utc_sqlite(row.observed_at), int(row.id or 0))

    return max(rows, key=_sort_key)


def active_alias_created_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> datetime | None:
    """Created-at of the active provider-event alias (quote cutoff for reuse)."""
    row = session.scalar(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.provider == provider,
            OddsProviderEventAlias.external_event_id == external_event_id,
            OddsProviderEventAlias.status == "active",
        )
    )
    if row is None:
        return None
    return _as_utc_sqlite(row.created_at)


def quotes_visible_under_active_alias(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> list[OddsQuote]:
    """Quotes belonging to the current alias version.

    When a provider external ID is reused across replacement versions, older
    quotes remain in history but are not exposed under the active alias.
    """
    cutoff = active_alias_created_at(
        session, provider=provider, external_event_id=external_event_id
    )
    rows = list(
        session.scalars(
            select(OddsQuote).where(
                OddsQuote.provider == provider,
                OddsQuote.external_event_id == external_event_id,
            )
        ).all()
    )
    if cutoff is None:
        return rows
    return [row for row in rows if _as_utc_sqlite(row.observed_at) >= cutoff]


def latest_quote_timestamp(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
) -> datetime | None:
    """Latest observed/source timestamp for quotes on the active alias version."""
    rows = quotes_visible_under_active_alias(
        session, provider=provider, external_event_id=external_event_id
    )
    stamps: list[datetime] = []
    for row in rows:
        stamps.append(_as_utc_sqlite(row.observed_at))
        if row.source_updated_at is not None:
            stamps.append(_as_utc_sqlite(row.source_updated_at))
    if not stamps:
        return None
    return max(stamps)


def quote_is_stale(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    observed_at: datetime,
    stale_after_minutes: int,
) -> bool:
    latest = latest_quote_timestamp(
        session, provider=provider, external_event_id=external_event_id
    )
    if latest is None:
        return False
    as_of = _require_aware_utc(observed_at, field="observed_at")
    return as_of - latest > timedelta(minutes=stale_after_minutes)


def resolve_match_lifecycle(
    session: Session,
    *,
    bout_id: str,
    provider: str,
    external_event_id: str,
    observed_at: datetime,
    stale_after_minutes: int,
) -> tuple[OddsBoutLifecycleState, bool]:
    """Resolve effective lifecycle for a successful match without overriding terminals."""
    latest = latest_bout_lifecycle(
        session,
        bout_id=bout_id,
        provider=provider,
        external_event_id=external_event_id,
    )
    if latest is not None and latest.lifecycle in TERMINAL_LIFECYCLES:
        state = OddsBoutLifecycleState(latest.lifecycle)
        return state, False

    if quote_is_stale(
        session,
        provider=provider,
        external_event_id=external_event_id,
        observed_at=observed_at,
        stale_after_minutes=stale_after_minutes,
    ):
        return OddsBoutLifecycleState.STALE, False

    if latest is not None and latest.lifecycle == OddsBoutLifecycleState.STALE.value:
        # Stale remains until a fresher quote exists (quote_is_stale False above).
        return OddsBoutLifecycleState.ACTIVE, True

    if latest is not None and latest.lifecycle in VALUE_BLOCKING_LIFECYCLES:
        return OddsBoutLifecycleState(latest.lifecycle), False

    return OddsBoutLifecycleState.ACTIVE, True


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
            _require_aware_utc(observed_at, field="observed_at").isoformat(),
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
    allow_terminal_override: bool = False,
) -> OddsBoutLifecycleObservation | None:
    """Append an explicit lifecycle observation; never forward-fill prices.

    Returns ``None`` when an ACTIVE transition is refused because a terminal
    lifecycle is already in force and ``allow_terminal_override`` is false.
    """
    kind = (evidence_kind or "").strip()
    if not kind:
        raise ValueError("lifecycle evidence_kind is required (no inferred locks)")
    if price_decimal is not None:
        raise ValueError(
            "refusing lifecycle price_decimal forward-fill; "
            "lifecycle rows never store prices"
        )
    stamp = _require_aware_utc(observed_at, field="observed_at")
    if lifecycle is OddsBoutLifecycleState.LOCKED and kind in {
        "inferred",
        "guess",
        "assumed",
    }:
        raise ValueError("refusing inferred lock without provider evidence")

    latest = latest_bout_lifecycle(
        session,
        bout_id=bout_id,
        provider=provider,
        external_event_id=external_event_id,
    )
    if (
        lifecycle is OddsBoutLifecycleState.ACTIVE
        and latest is not None
        and latest.lifecycle in TERMINAL_LIFECYCLES
        and not allow_terminal_override
        and kind not in _TERMINAL_EXIT_EVIDENCE
    ):
        return None

    dedupe = _lifecycle_dedupe_key(
        bout_id=bout_id,
        lifecycle=lifecycle.value,
        evidence_kind=kind,
        observed_at=stamp,
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
        observed_at=stamp,
    )
    session.add(row)
    session.flush()
    return row
