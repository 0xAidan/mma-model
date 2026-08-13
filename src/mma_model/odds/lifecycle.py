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

# Fail-closed allowlists: unknown evidence kinds are rejected.
LIFECYCLE_EVIDENCE_KINDS: Final[dict[str, frozenset[str]]] = {
    "active": frozenset(
        {
            "match_provider_id",
            "match_participant_pair",
            "match_manual_review",
            "odds_bout_match_review_approved",
            "explicit_lifecycle_reactivation",
            "operator_lifecycle_clear",
            "provider_unlock_signal",
        }
    ),
    "stale": frozenset({"quote_age_exceeds_stale_after_minutes"}),
    "missing_unknown": frozenset(
        {
            "provider_market_absent",
            "no_quotes_for_matched_event",
        }
    ),
    "locked": frozenset({"provider_lock_signal"}),
    "cancelled": frozenset({"canonical_bout_cancelled"}),
    "replaced": frozenset({"canonical_bout_replaced"}),
    "review_blocked": frozenset(
        {
            "odds_bout_match_review_reversed",
            "ambiguous_match_blocked",
            "stored_provider_id_unsafe",
            "replacement_match_blocked",
        }
    ),
}

_PROVIDER_IDENTITY_EVIDENCE: Final[frozenset[str]] = frozenset(
    {
        "provider_lock_signal",
        "provider_unlock_signal",
        "provider_market_absent",
    }
)

_CANONICAL_IDENTITY_EVIDENCE: Final[frozenset[str]] = frozenset(
    {
        "canonical_bout_cancelled",
        "canonical_bout_replaced",
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


def alias_effective_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> OddsProviderEventAlias | None:
    """Resolve the alias version effective at as_of (PIT; status-independent)."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    rows = list(
        session.scalars(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.provider == provider,
                OddsProviderEventAlias.external_event_id == external_event_id,
            )
        ).all()
    )
    effective: list[OddsProviderEventAlias] = []
    for row in rows:
        created = _as_utc_sqlite(row.created_at)
        if created > cutoff:
            continue
        if row.superseded_at is not None and _as_utc_sqlite(row.superseded_at) <= cutoff:
            continue
        effective.append(row)
    if not effective:
        return None
    return max(
        effective,
        key=lambda row: (int(row.alias_version), _as_utc_sqlite(row.created_at)),
    )


def alias_quote_lower_bound(alias: OddsProviderEventAlias | None) -> datetime | None:
    """Lower bound for quote visibility under an alias version.

    First alias version for an external ID includes prior quotes for that event.
    Only reused-ID replacement versions (alias_version > 1) establish a cutoff.
    """
    if alias is None:
        return None
    if int(alias.alias_version) <= 1:
        return None
    return _as_utc_sqlite(alias.created_at)


def latest_bout_lifecycle(
    session: Session,
    *,
    bout_id: str,
    as_of: datetime,
    provider: str | None = None,
    external_event_id: str | None = None,
) -> OddsBoutLifecycleObservation | None:
    """Latest explicit lifecycle row at/before as_of (PIT-safe)."""
    cutoff = _require_aware_utc(as_of, field="as_of")
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
    rows = [
        row
        for row in session.scalars(stmt).all()
        if _as_utc_sqlite(row.observed_at) <= cutoff
    ]
    if not rows:
        return None

    def _sort_key(row: OddsBoutLifecycleObservation) -> tuple[datetime, int]:
        return (_as_utc_sqlite(row.observed_at), int(row.id or 0))

    return max(rows, key=_sort_key)


def quotes_visible_under_alias_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> list[OddsQuote]:
    """Quotes visible for the alias effective at as_of (PIT + version cutoff)."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    alias = alias_effective_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=cutoff,
    )
    if alias is None:
        prior = session.scalars(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.provider == provider,
                OddsProviderEventAlias.external_event_id == external_event_id,
            )
        ).all()
        # Between superseded versions (or after cancel of alias) — do not expose
        # prior-version quotes without an effective alias at as_of.
        if any(_as_utc_sqlite(row.created_at) <= cutoff for row in prior):
            return []
        lower = None
    else:
        lower = alias_quote_lower_bound(alias)
    rows = list(
        session.scalars(
            select(OddsQuote).where(
                OddsQuote.provider == provider,
                OddsQuote.external_event_id == external_event_id,
            )
        ).all()
    )
    visible: list[OddsQuote] = []
    for row in rows:
        observed = _as_utc_sqlite(row.observed_at)
        if observed > cutoff:
            continue
        if lower is not None and observed < lower:
            continue
        if row.source_updated_at is not None:
            source = _as_utc_sqlite(row.source_updated_at)
            if source > cutoff:
                continue
        visible.append(row)
    return visible


def quotes_visible_under_active_alias(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime | None = None,
) -> list[OddsQuote]:
    """Compatibility wrapper: default as_of is now (UTC)."""
    stamp = as_of if as_of is not None else datetime.now(UTC)
    return quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=stamp,
    )


def latest_quote_timestamp(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> datetime | None:
    """Latest observed/source timestamp for quotes visible at as_of."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    rows = quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=cutoff,
    )
    stamps: list[datetime] = []
    for row in rows:
        observed = _as_utc_sqlite(row.observed_at)
        stamps.append(observed)
        if row.source_updated_at is not None:
            source = _as_utc_sqlite(row.source_updated_at)
            if source <= cutoff:
                stamps.append(source)
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
    as_of = _require_aware_utc(observed_at, field="observed_at")
    latest = latest_quote_timestamp(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=as_of,
    )
    if latest is None:
        return False
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
    as_of = _require_aware_utc(observed_at, field="observed_at")
    latest = latest_bout_lifecycle(
        session,
        bout_id=bout_id,
        as_of=as_of,
        provider=provider,
        external_event_id=external_event_id,
    )
    if latest is not None and latest.lifecycle in TERMINAL_LIFECYCLES:
        return OddsBoutLifecycleState(latest.lifecycle), False

    visible = quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=as_of,
    )
    if not visible:
        return OddsBoutLifecycleState.MISSING_UNKNOWN, False

    if quote_is_stale(
        session,
        provider=provider,
        external_event_id=external_event_id,
        observed_at=as_of,
        stale_after_minutes=stale_after_minutes,
    ):
        return OddsBoutLifecycleState.STALE, False

    if latest is not None and latest.lifecycle == OddsBoutLifecycleState.STALE.value:
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


def _validate_lifecycle_evidence(
    *,
    lifecycle: OddsBoutLifecycleState,
    evidence_kind: str,
    provider: str | None,
    external_event_id: str | None,
) -> None:
    kind = (evidence_kind or "").strip()
    if not kind:
        raise ValueError("lifecycle evidence_kind is required (no inferred locks)")
    allowed = LIFECYCLE_EVIDENCE_KINDS.get(lifecycle.value)
    if allowed is None or kind not in allowed:
        raise ValueError(
            f"unknown or disallowed evidence_kind {kind!r} for lifecycle "
            f"{lifecycle.value!r}"
        )
    if kind in _PROVIDER_IDENTITY_EVIDENCE and (
        not (provider or "").strip() or not (external_event_id or "").strip()
    ):
        raise ValueError(
            f"evidence_kind {kind!r} requires provider and external_event_id"
        )
    if kind in _CANONICAL_IDENTITY_EVIDENCE:
        # Canonical cancellation/replacement is bout-scoped; provider ids optional.
        pass


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
    _validate_lifecycle_evidence(
        lifecycle=lifecycle,
        evidence_kind=evidence_kind,
        provider=provider,
        external_event_id=external_event_id,
    )
    kind = evidence_kind.strip()
    if price_decimal is not None:
        raise ValueError(
            "refusing lifecycle price_decimal forward-fill; "
            "lifecycle rows never store prices"
        )
    stamp = _require_aware_utc(observed_at, field="observed_at")

    latest = latest_bout_lifecycle(
        session,
        bout_id=bout_id,
        as_of=stamp,
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
