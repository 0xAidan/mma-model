"""DWCS-203 bout/line lifecycle states, precedence, and value eligibility."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import (
    OddsAvailabilityObservation,
    OddsBoutLifecycleObservation,
    OddsMatchObservation,
    OddsProviderEventAlias,
    OddsQuote,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.odds.manual_price import canonical_selection_identity

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

# Explicit transition matrix: terminal → ACTIVE only via listed evidence.
# provider_unlock exits LOCKED only; review approval exits REVIEW_BLOCKED;
# cancelled/replaced require canonical correction (else remain terminal).
_TERMINAL_TO_ACTIVE_EVIDENCE: Final[dict[str, frozenset[str]]] = {
    "locked": frozenset({"provider_unlock_signal"}),
    "review_blocked": frozenset(
        {
            "odds_bout_match_review_approved",
            "operator_lifecycle_clear",
            # A newer matched rematch supersedes prior ambiguity blocks.
            "match_provider_id",
            "match_participant_pair",
            "match_manual_review",
        }
    ),
    "cancelled": frozenset({"canonical_bout_correction_reactivate"}),
    "replaced": frozenset({"canonical_bout_correction_reactivate"}),
}

# Fail-closed allowlists: unknown evidence kinds are rejected.
LIFECYCLE_EVIDENCE_KINDS: Final[dict[str, frozenset[str]]] = {
    "active": frozenset(
        {
            "match_provider_id",
            "match_participant_pair",
            "match_manual_review",
            "odds_bout_match_review_approved",
            "fresh_quote_clears_observational_block",
            "provider_unlock_signal",
            "operator_lifecycle_clear",
            "canonical_bout_correction_reactivate",
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
        "canonical_bout_correction_reactivate",
    }
)

_OBSERVATIONAL_CLEARABLE: Final[frozenset[str]] = frozenset(
    {
        "stale",
        "missing_unknown",
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


class QuoteBlockReason(StrEnum):
    NONE = "none"
    UNMATCHED = "unmatched"
    LATEST_MATCH_NOT_MATCHED = "latest_match_not_matched"
    ALIAS_BOUT_MISMATCH = "alias_bout_mismatch"
    CALLER_MATCH_RESTRICT = "caller_match_restrict"
    BOUT_TERMINAL = "bout_terminal"
    SELECTION_LOCKED = "selection_locked"
    MARKET_UNKNOWN = "market_unknown"
    QUOTE_UNAVAILABLE = "quote_unavailable"
    STALE = "stale"
    NOT_VISIBLE = "not_visible"


_MATCHED_STATUS: Final[str] = "matched"


class AvailabilityNote(StrEnum):
    """Non-blocking availability history note for operators."""

    NONE = "none"
    UNKNOWN_BLOCKING = "unknown_blocking"
    RECOVERED_BY_NEWER_QUOTE = "recovered_by_newer_quote"


@dataclass(frozen=True)
class QuoteEligibilityDecision:
    """Per-quote value gate (book/region/market/outcome/line)."""

    quote_id: int
    eligible: bool
    status: QuoteValueEligibility
    reason: QuoteBlockReason
    detail: str
    selection_identity: str
    bookmaker_key: str
    region: str
    market_family: str
    outcome_key: str
    line_point: float | None
    freshness_at: datetime | None
    resolved_bout_id: str | None = None
    availability_note: AvailabilityNote = AvailabilityNote.NONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote_id": self.quote_id,
            "eligible": self.eligible,
            "status": self.status.value,
            "reason": self.reason.value,
            "detail": self.detail,
            "selection_identity": self.selection_identity,
            "bookmaker_key": self.bookmaker_key,
            "region": self.region,
            "market_family": self.market_family,
            "outcome_key": self.outcome_key,
            "line_point": self.line_point,
            "freshness_at": (
                self.freshness_at.isoformat() if self.freshness_at is not None else None
            ),
            "resolved_bout_id": self.resolved_bout_id,
            "availability_note": self.availability_note.value,
        }


def classify_quote_value_eligibility(
    *,
    match_status: str,
    lifecycle: OddsBoutLifecycleState | str,
) -> QuoteValueEligibility:
    """Bout/event match gate only — does not imply every quote is eligible.

    Passes when matched and bout lifecycle is not terminal. Stale/missing markets
    and selection locks are enforced by ``resolve_quote_value_eligibility``.
    """
    life = (
        lifecycle.value
        if isinstance(lifecycle, OddsBoutLifecycleState)
        else str(lifecycle)
    )
    if match_status != "matched":
        return QuoteValueEligibility.BLOCKED
    if life in TERMINAL_LIFECYCLES:
        return QuoteValueEligibility.BLOCKED
    return QuoteValueEligibility.ELIGIBLE


def _lifecycle_row_is_bout_scoped(row: OddsBoutLifecycleObservation) -> bool:
    return (
        row.bookmaker_key is None
        and row.region is None
        and row.market_family is None
        and row.outcome_key is None
        and row.line_point is None
        and row.quote_id is None
    )


def _selection_scope_applies(
    row: OddsBoutLifecycleObservation,
    *,
    quote: OddsQuote,
) -> bool:
    """True when every non-null scope field on ``row`` matches ``quote``."""
    if _lifecycle_row_is_bout_scoped(row):
        return False
    if row.quote_id is not None and int(row.quote_id) != int(quote.id):
        return False
    if row.bookmaker_key is not None and row.bookmaker_key != quote.bookmaker_key:
        return False
    if row.region is not None and row.region != quote.region:
        return False
    if row.market_family is not None and row.market_family != quote.market_family:
        return False
    if row.outcome_key is not None and row.outcome_key != quote.outcome_key:
        return False
    return not (
        row.line_point is not None
        and (
            quote.line_point is None
            or float(row.line_point) != float(quote.line_point)
        )
    )


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


def latest_match_observation_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> OddsMatchObservation | None:
    """Latest persisted match decision for provider/external event at cutoff."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    rows = list(
        session.scalars(
            select(OddsMatchObservation).where(
                OddsMatchObservation.provider == provider,
                OddsMatchObservation.external_event_id == external_event_id,
            )
        ).all()
    )
    visible = [
        row for row in rows if _as_utc_sqlite(row.observed_at) <= cutoff
    ]
    if not visible:
        return None
    return max(
        visible,
        key=lambda row: (
            _as_utc_sqlite(row.observed_at),
            int(row.id or 0),
        ),
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
    """Latest bout/provider-event scoped lifecycle at/before as_of (PIT-safe).

    Selection-scoped rows (book/market/outcome/line/quote) are ignored here.
    """
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
        and _lifecycle_row_is_bout_scoped(row)
    ]
    if not rows:
        return None

    def _sort_key(row: OddsBoutLifecycleObservation) -> tuple[datetime, int]:
        return (_as_utc_sqlite(row.observed_at), int(row.id or 0))

    return max(rows, key=_sort_key)


def latest_selection_lifecycle(
    session: Session,
    *,
    bout_id: str,
    quote: OddsQuote,
    as_of: datetime,
    provider: str | None = None,
    external_event_id: str | None = None,
) -> OddsBoutLifecycleObservation | None:
    """Latest selection-scoped lifecycle applying to ``quote`` at as_of."""
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
        and _selection_scope_applies(row, quote=quote)
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


def quote_authoritative_freshness(
    row: OddsQuote,
    *,
    as_of: datetime,
) -> datetime | None:
    """Authoritative freshness clock for one quote at as_of.

    Prefer ``source_updated_at`` when present (book/provider line age). Fall back
    to ``observed_at`` only when source update is absent. Future source clocks
    relative to as_of are rejected (None).
    """
    cutoff = _require_aware_utc(as_of, field="as_of")
    observed = _as_utc_sqlite(row.observed_at)
    if observed > cutoff:
        return None
    if row.source_updated_at is not None:
        source = _as_utc_sqlite(row.source_updated_at)
        if source > cutoff:
            return None
        return source
    return observed


def latest_quote_timestamp(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> datetime | None:
    """Latest authoritative freshness among quotes visible at as_of."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    rows = quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=cutoff,
    )
    stamps: list[datetime] = []
    for row in rows:
        freshness = quote_authoritative_freshness(row, as_of=cutoff)
        if freshness is not None:
            stamps.append(freshness)
    if not stamps:
        return None
    return max(stamps)


def quote_row_is_stale(
    row: OddsQuote,
    *,
    as_of: datetime,
    stale_after_minutes: int,
) -> bool:
    """True when this quote's own authoritative freshness exceeds stale_after."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    freshness = quote_authoritative_freshness(row, as_of=cutoff)
    if freshness is None:
        return True
    return cutoff - freshness > timedelta(minutes=stale_after_minutes)


def quote_is_stale(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    observed_at: datetime,
    stale_after_minutes: int,
) -> bool:
    """Deprecated event-wide helper: true only when every visible quote is stale.

    Prefer ``quote_row_is_stale`` / ``resolve_quote_value_eligibility``.
    """
    as_of = _require_aware_utc(observed_at, field="observed_at")
    rows = quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=as_of,
    )
    if not rows:
        return False
    return all(
        quote_row_is_stale(row, as_of=as_of, stale_after_minutes=stale_after_minutes)
        for row in rows
    )


def availability_observations_visible_under_alias_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    as_of: datetime,
) -> list[OddsAvailabilityObservation]:
    """Availability rows visible under the alias effective at as_of (PIT)."""
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
        if any(_as_utc_sqlite(row.created_at) <= cutoff for row in prior):
            return []
        lower = None
    else:
        lower = alias_quote_lower_bound(alias)
    rows = list(
        session.scalars(
            select(OddsAvailabilityObservation).where(
                OddsAvailabilityObservation.provider == provider,
                OddsAvailabilityObservation.external_event_id == external_event_id,
            )
        ).all()
    )
    visible: list[OddsAvailabilityObservation] = []
    for row in rows:
        observed = _as_utc_sqlite(row.observed_at)
        if observed > cutoff:
            continue
        if lower is not None and observed < lower:
            continue
        if row.snapshot_at is not None and _as_utc_sqlite(row.snapshot_at) > cutoff:
            continue
        visible.append(row)
    return visible


def market_availability_unknown_at(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    bookmaker_key: str,
    region: str,
    market_family: str,
    provider_market_key: str | None = None,
    as_of: datetime,
    quote_evidence_at: datetime | None = None,
) -> OddsAvailabilityObservation | None:
    """Latest blocking UNKNOWN for book/region/market at as_of, else None.

    Never infers SUSPENDED/LOCKED from absence — only explicit UNKNOWN rows.
    When ``quote_evidence_at`` is set (available quote observe/source clock), an
    UNKNOWN *strictly older* than that evidence is cleared. Equal timestamps
    remain fail-closed (UNKNOWN still blocks).
    """
    cutoff = _require_aware_utc(as_of, field="as_of")
    candidates = [
        row
        for row in availability_observations_visible_under_alias_at(
            session,
            provider=provider,
            external_event_id=external_event_id,
            as_of=cutoff,
        )
        if row.availability == "unknown"
        and (row.bookmaker_key or "") == bookmaker_key
        and row.region == region
        and (
            row.market_family == market_family
            or (
                provider_market_key is not None
                and row.provider_market_key == provider_market_key
            )
        )
    ]
    if not candidates:
        return None

    def _sort_key(row: OddsAvailabilityObservation) -> tuple[datetime, int]:
        return (_as_utc_sqlite(row.observed_at), int(row.id or 0))

    latest = max(candidates, key=_sort_key)
    if quote_evidence_at is not None:
        evidence = _require_aware_utc(quote_evidence_at, field="quote_evidence_at")
        # Fail-closed equality: only strictly newer quote evidence clears.
        if _as_utc_sqlite(latest.observed_at) < evidence:
            return None
    return latest


def quote_availability_evidence_at(
    quote: OddsQuote,
    *,
    as_of: datetime,
) -> datetime | None:
    """Clock proving the quote's market was available (for UNKNOWN recovery).

    Uses observed_at (when the available quote was acquired). Future observations
    relative to as_of are rejected.
    """
    cutoff = _require_aware_utc(as_of, field="as_of")
    observed = _as_utc_sqlite(quote.observed_at)
    if observed > cutoff:
        return None
    return observed


def prior_unknown_cleared_by_quote(
    session: Session,
    *,
    quote: OddsQuote,
    as_of: datetime,
) -> bool:
    """True when a prior UNKNOWN exists but is older than this available quote."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    evidence = quote_availability_evidence_at(quote, as_of=cutoff)
    if evidence is None or quote.availability != "available":
        return False
    candidates = [
        row
        for row in availability_observations_visible_under_alias_at(
            session,
            provider=quote.provider,
            external_event_id=quote.external_event_id,
            as_of=cutoff,
        )
        if row.availability == "unknown"
        and (row.bookmaker_key or "") == quote.bookmaker_key
        and row.region == quote.region
        and (
            row.market_family == quote.market_family
            or row.provider_market_key == quote.provider_market_key
        )
    ]
    if not candidates:
        return False
    latest = max(candidates, key=lambda row: _as_utc_sqlite(row.observed_at))
    return _as_utc_sqlite(latest.observed_at) < evidence


def resolve_match_lifecycle(
    session: Session,
    *,
    bout_id: str,
    provider: str,
    external_event_id: str,
    observed_at: datetime,
    stale_after_minutes: int,
) -> tuple[OddsBoutLifecycleState, bool]:
    """Resolve bout-level match lifecycle and identity value gate.

    ``eligible`` means matched identity may proceed to quote-level checks — it
    does not mean every quote under the event is fresh/available. Staleness and
    missing markets are enforced per quote, not by max() across books/markets.
    """
    del stale_after_minutes  # quote-level only; retained for call-site compat
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
        # Identity gate still open; no quotes to evaluate.
        return OddsBoutLifecycleState.MISSING_UNKNOWN, True

    # Bout observational ACTIVE when any quote is visible; per-quote stale/missing
    # never flips the whole event.
    return OddsBoutLifecycleState.ACTIVE, True


def clears_observational_block(
    *,
    previous: OddsBoutLifecycleObservation | None,
    resolved: OddsBoutLifecycleState,
) -> bool:
    """True when a fresh-quote ACTIVE should persist clear-of-STALE/MISSING evidence."""
    if resolved is not OddsBoutLifecycleState.ACTIVE or previous is None:
        return False
    return previous.lifecycle in _OBSERVATIONAL_CLEARABLE


def _quote_selection_identity(quote: OddsQuote) -> str:
    family = MarketFamily(quote.market_family)
    outcome = OutcomeKey(quote.outcome_key)
    return canonical_selection_identity(family, outcome, quote.line_point)


def resolve_quote_value_eligibility(
    session: Session,
    *,
    quote: OddsQuote,
    bout_id: str | None,
    match_status: str,
    as_of: datetime,
    stale_after_minutes: int,
) -> QuoteEligibilityDecision:
    """Combine latest match decision + alias-effective bout with quote gates.

    Authority for value is the latest persisted match observation at ``as_of``
    (must be MATCHED to the same bout as the effective alias). An old alias that
    remains after a newer ambiguous/unmatched decision is never value authority.
    Caller ``bout_id`` / ``match_status`` may only further restrict, never grant.
    """
    cutoff = _require_aware_utc(as_of, field="as_of")
    caller_status = (match_status or "").strip()
    selection = _quote_selection_identity(quote)
    base = dict(
        quote_id=int(quote.id),
        selection_identity=selection,
        bookmaker_key=quote.bookmaker_key,
        region=quote.region,
        market_family=quote.market_family,
        outcome_key=quote.outcome_key,
        line_point=quote.line_point,
        freshness_at=quote_authoritative_freshness(quote, as_of=cutoff),
        availability_note=AvailabilityNote.NONE,
        resolved_bout_id=None,
    )

    visible = quotes_visible_under_alias_at(
        session,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
        as_of=cutoff,
    )
    if not any(int(row.id) == int(quote.id) for row in visible):
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.NOT_VISIBLE,
            detail="quote not visible under alias at as_of",
            **base,
        )

    alias = alias_effective_at(
        session,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
        as_of=cutoff,
    )
    if alias is None:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.UNMATCHED,
            detail="no effective alias at as_of for quote value eligibility",
            **base,
        )

    latest_match = latest_match_observation_at(
        session,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
        as_of=cutoff,
    )
    if latest_match is None:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.UNMATCHED,
            detail="no persisted match decision at as_of",
            resolved_bout_id=alias.bout_id,
            availability_note=AvailabilityNote.NONE,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )
    if latest_match.match_status != _MATCHED_STATUS:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.LATEST_MATCH_NOT_MATCHED,
            detail=(
                f"latest match_status={latest_match.match_status!r} "
                f"(reason={latest_match.reason!r}) blocks quote value"
            ),
            resolved_bout_id=alias.bout_id,
            availability_note=AvailabilityNote.NONE,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )
    if latest_match.bout_id != alias.bout_id:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.ALIAS_BOUT_MISMATCH,
            detail=(
                f"latest matched bout_id={latest_match.bout_id!r} != "
                f"alias.bout_id={alias.bout_id!r}"
            ),
            resolved_bout_id=alias.bout_id,
            availability_note=AvailabilityNote.NONE,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )

    # Caller inputs may only further restrict; never grant over persisted state.
    if caller_status and caller_status != _MATCHED_STATUS:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.CALLER_MATCH_RESTRICT,
            detail=(
                f"caller match_status={caller_status!r} further restricts "
                "despite persisted matched decision"
            ),
            resolved_bout_id=alias.bout_id,
            availability_note=AvailabilityNote.NONE,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )
    if bout_id is not None and bout_id != alias.bout_id:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.ALIAS_BOUT_MISMATCH,
            detail=(
                f"caller bout_id={bout_id!r} != alias.bout_id={alias.bout_id!r} "
                f"(alias_version={alias.alias_version})"
            ),
            resolved_bout_id=alias.bout_id,
            availability_note=AvailabilityNote.NONE,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )

    resolved_bout_id = alias.bout_id
    base["resolved_bout_id"] = resolved_bout_id

    bout_life = latest_bout_lifecycle(
        session,
        bout_id=resolved_bout_id,
        as_of=cutoff,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
    )
    if bout_life is not None and bout_life.lifecycle in TERMINAL_LIFECYCLES:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.BOUT_TERMINAL,
            detail=f"bout lifecycle={bout_life.lifecycle}",
            **base,
        )

    selection_life = latest_selection_lifecycle(
        session,
        bout_id=resolved_bout_id,
        quote=quote,
        as_of=cutoff,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
    )
    if (
        selection_life is not None
        and selection_life.lifecycle == OddsBoutLifecycleState.LOCKED.value
    ):
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.SELECTION_LOCKED,
            detail=f"selection locked ({selection_life.evidence_kind})",
            **base,
        )

    quote_evidence = quote_availability_evidence_at(quote, as_of=cutoff)
    unknown = market_availability_unknown_at(
        session,
        provider=quote.provider,
        external_event_id=quote.external_event_id,
        bookmaker_key=quote.bookmaker_key,
        region=quote.region,
        market_family=quote.market_family,
        provider_market_key=quote.provider_market_key,
        as_of=cutoff,
        quote_evidence_at=quote_evidence,
    )
    if unknown is not None:
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.MARKET_UNKNOWN,
            detail=(
                f"availability unknown for {quote.bookmaker_key}/"
                f"{quote.provider_market_key} (preserved UNKNOWN)"
            ),
            availability_note=AvailabilityNote.UNKNOWN_BLOCKING,
            resolved_bout_id=resolved_bout_id,
            quote_id=int(quote.id),
            selection_identity=selection,
            bookmaker_key=quote.bookmaker_key,
            region=quote.region,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            freshness_at=base["freshness_at"],
        )

    availability_note = AvailabilityNote.NONE
    if prior_unknown_cleared_by_quote(session, quote=quote, as_of=cutoff):
        availability_note = AvailabilityNote.RECOVERED_BY_NEWER_QUOTE
    base["availability_note"] = availability_note

    if quote.availability != "available":
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.QUOTE_UNAVAILABLE,
            detail=f"quote availability={quote.availability}",
            **base,
        )

    if quote_row_is_stale(
        quote, as_of=cutoff, stale_after_minutes=stale_after_minutes
    ):
        return QuoteEligibilityDecision(
            eligible=False,
            status=QuoteValueEligibility.BLOCKED,
            reason=QuoteBlockReason.STALE,
            detail="quote authoritative freshness exceeds stale_after_minutes",
            **base,
        )

    return QuoteEligibilityDecision(
        eligible=True,
        status=QuoteValueEligibility.ELIGIBLE,
        reason=QuoteBlockReason.NONE,
        detail=(
            "alias-bound bout + available + fresh + not locked"
            + (
                "; prior UNKNOWN cleared by newer quote"
                if availability_note is AvailabilityNote.RECOVERED_BY_NEWER_QUOTE
                else ""
            )
        ),
        **base,
    )


def resolve_visible_quotes_value_eligibility(
    session: Session,
    *,
    provider: str,
    external_event_id: str,
    bout_id: str | None,
    match_status: str,
    as_of: datetime,
    stale_after_minutes: int,
) -> list[QuoteEligibilityDecision]:
    """Evaluate every alias-visible quote at as_of."""
    cutoff = _require_aware_utc(as_of, field="as_of")
    rows = quotes_visible_under_alias_at(
        session,
        provider=provider,
        external_event_id=external_event_id,
        as_of=cutoff,
    )
    return [
        resolve_quote_value_eligibility(
            session,
            quote=row,
            bout_id=bout_id,
            match_status=match_status,
            as_of=cutoff,
            stale_after_minutes=stale_after_minutes,
        )
        for row in sorted(rows, key=lambda q: int(q.id or 0))
    ]


def summarize_quote_eligibility(
    decisions: list[QuoteEligibilityDecision],
) -> dict[str, Any]:
    """Aggregate quote-level eligibility counts/reasons for reconcile reports."""
    counts: dict[str, int] = {
        "visible": len(decisions),
        "eligible": 0,
        "blocked": 0,
        "availability_recovered": 0,
        "availability_unknown_blocking": 0,
    }
    by_reason: dict[str, int] = {}
    for row in decisions:
        if row.eligible:
            counts["eligible"] += 1
        else:
            counts["blocked"] += 1
            by_reason[row.reason.value] = by_reason.get(row.reason.value, 0) + 1
        if row.availability_note is AvailabilityNote.RECOVERED_BY_NEWER_QUOTE:
            counts["availability_recovered"] += 1
        elif row.availability_note is AvailabilityNote.UNKNOWN_BLOCKING:
            counts["availability_unknown_blocking"] += 1
    return {
        **counts,
        "blocked_by_reason": dict(sorted(by_reason.items())),
        "quotes": [row.as_dict() for row in decisions],
    }


def _bind_lifecycle_quote_scope(
    session: Session,
    *,
    provider: str | None,
    external_event_id: str | None,
    bookmaker_key: str | None,
    region: str | None,
    market_family: str | None,
    outcome_key: str | None,
    line_point: float | None,
    quote_id: int | None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    float | None,
    int | None,
]:
    """Validate quote_id exists/belongs and scope fields agree; derive blanks."""
    if quote_id is None:
        return (
            bookmaker_key,
            region,
            market_family,
            outcome_key,
            line_point,
            None,
        )
    row = session.get(OddsQuote, int(quote_id))
    if row is None:
        raise ValueError(f"quote_id {quote_id} not found")
    if provider is not None and row.provider != provider:
        raise ValueError(
            f"quote_id {quote_id} provider mismatch: "
            f"got {row.provider!r}, expected {provider!r}"
        )
    if (
        external_event_id is not None
        and row.external_event_id != external_event_id
    ):
        raise ValueError(
            f"quote_id {quote_id} external_event_id mismatch: "
            f"got {row.external_event_id!r}, expected {external_event_id!r}"
        )

    def _agree(name: str, supplied: str | None, actual: str) -> str:
        if supplied is None:
            return actual
        if supplied != actual:
            raise ValueError(
                f"scope {name} mismatches quote_id {quote_id}: "
                f"got {supplied!r}, quote has {actual!r}"
            )
        return supplied

    bound_book = _agree("bookmaker_key", bookmaker_key, row.bookmaker_key)
    bound_region = _agree("region", region, row.region)
    bound_family = _agree("market_family", market_family, row.market_family)
    bound_outcome = _agree("outcome_key", outcome_key, row.outcome_key)
    if line_point is not None:
        if row.line_point is None or float(line_point) != float(row.line_point):
            raise ValueError(
                f"scope line_point mismatches quote_id {quote_id}: "
                f"got {line_point!r}, quote has {row.line_point!r}"
            )
        bound_line: float | None = float(line_point)
    else:
        bound_line = (
            float(row.line_point) if row.line_point is not None else None
        )
    return (
        bound_book,
        bound_region,
        bound_family,
        bound_outcome,
        bound_line,
        int(quote_id),
    )


def _lifecycle_dedupe_key(
    *,
    bout_id: str,
    lifecycle: str,
    evidence_kind: str,
    observed_at: datetime,
    provider: str | None,
    external_event_id: str | None,
    bookmaker_key: str | None,
    region: str | None,
    market_family: str | None,
    outcome_key: str | None,
    line_point: float | None,
    quote_id: int | None,
) -> str:
    point = "" if line_point is None else f"{float(line_point):.4f}"
    material = "|".join(
        [
            bout_id,
            lifecycle,
            evidence_kind,
            provider or "",
            external_event_id or "",
            bookmaker_key or "",
            region or "",
            market_family or "",
            outcome_key or "",
            point,
            "" if quote_id is None else str(int(quote_id)),
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
    bookmaker_key: str | None,
    region: str | None,
    market_family: str | None,
    outcome_key: str | None,
    line_point: float | None,
    quote_id: int | None,
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
    scoped = any(
        value is not None
        for value in (
            bookmaker_key,
            region,
            market_family,
            outcome_key,
            line_point,
            quote_id,
        )
    )
    if scoped and lifecycle in {
        OddsBoutLifecycleState.CANCELLED,
        OddsBoutLifecycleState.REPLACED,
        OddsBoutLifecycleState.REVIEW_BLOCKED,
    }:
        raise ValueError(
            f"lifecycle {lifecycle.value!r} is bout-scoped only "
            "(selection scope refused)"
        )
    if scoped and lifecycle is OddsBoutLifecycleState.LOCKED and not (
        (bookmaker_key or "").strip() and (market_family or quote_id is not None)
    ):
        raise ValueError(
            "selection-scoped lock requires bookmaker_key and "
            "market_family (or quote_id)"
        )
    if kind in _CANONICAL_IDENTITY_EVIDENCE:
        # Canonical cancellation/replacement is bout-scoped; provider ids optional.
        pass


def _terminal_active_transition_allowed(
    *,
    from_lifecycle: str,
    evidence_kind: str,
) -> bool:
    allowed = _TERMINAL_TO_ACTIVE_EVIDENCE.get(from_lifecycle, frozenset())
    return evidence_kind in allowed


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
    bookmaker_key: str | None = None,
    region: str | None = None,
    market_family: str | None = None,
    outcome_key: str | None = None,
    line_point: float | None = None,
    quote_id: int | None = None,
) -> OddsBoutLifecycleObservation | None:
    """Append an explicit lifecycle observation; never forward-fill prices.

    Selection scope (book/region/market/outcome/line/quote_id) models line-level
    lock/removal without inferring or forward-filling. Null scope = bout/event.

    Returns ``None`` when an ACTIVE transition is refused by the terminal
    transition matrix (provider unlock exits LOCKED only; cancelled/replaced
    require canonical correction evidence; review approval exits REVIEW_BLOCKED).
    """
    (
        bookmaker_key,
        region,
        market_family,
        outcome_key,
        line_point,
        quote_id,
    ) = _bind_lifecycle_quote_scope(
        session,
        provider=provider,
        external_event_id=external_event_id,
        bookmaker_key=bookmaker_key,
        region=region,
        market_family=market_family,
        outcome_key=outcome_key,
        line_point=line_point,
        quote_id=quote_id,
    )
    _validate_lifecycle_evidence(
        lifecycle=lifecycle,
        evidence_kind=evidence_kind,
        provider=provider,
        external_event_id=external_event_id,
        bookmaker_key=bookmaker_key,
        region=region,
        market_family=market_family,
        outcome_key=outcome_key,
        line_point=line_point,
        quote_id=quote_id,
    )
    kind = evidence_kind.strip()
    if price_decimal is not None:
        raise ValueError(
            "refusing lifecycle price_decimal forward-fill; "
            "lifecycle rows never store prices"
        )
    stamp = _require_aware_utc(observed_at, field="observed_at")
    scoped = any(
        value is not None
        for value in (
            bookmaker_key,
            region,
            market_family,
            outcome_key,
            line_point,
            quote_id,
        )
    )

    if scoped:
        # Selection ACTIVE/unlock only contends with prior selection-scoped terminals.
        stub = OddsQuote(
            id=int(quote_id or 0),
            dedupe_key="selection-scope-probe",
            dedupe_version=2,
            provider=provider or "",
            bookmaker_key=bookmaker_key or "",
            bookmaker_title="",
            region=region or "",
            event_id="00000000-0000-0000-0000-000000000000",
            external_event_id=external_event_id or "",
            market_family=market_family or "",
            provider_market_key="",
            outcome_key=outcome_key or "",
            outcome_label="",
            line_point=line_point,
            price_decimal=1.01,
            availability="available",
            observed_at=stamp,
            source_updated_at=None,
            commence_time=stamp,
            snapshot_at=None,
            raw_ref="selection-scope-probe",
        )
        latest = latest_selection_lifecycle(
            session,
            bout_id=bout_id,
            quote=stub,
            as_of=stamp,
            provider=provider,
            external_event_id=external_event_id,
        )
    else:
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
        and not _terminal_active_transition_allowed(
            from_lifecycle=latest.lifecycle,
            evidence_kind=kind,
        )
    ):
        return None

    dedupe = _lifecycle_dedupe_key(
        bout_id=bout_id,
        lifecycle=lifecycle.value,
        evidence_kind=kind,
        observed_at=stamp,
        provider=provider,
        external_event_id=external_event_id,
        bookmaker_key=bookmaker_key,
        region=region,
        market_family=market_family,
        outcome_key=outcome_key,
        line_point=line_point,
        quote_id=quote_id,
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
        bookmaker_key=bookmaker_key,
        region=region,
        market_family=market_family,
        outcome_key=outcome_key,
        line_point=line_point,
        quote_id=quote_id,
        lifecycle=lifecycle.value,
        evidence_kind=kind,
        detail=detail,
        price_decimal=None,
        observed_at=stamp,
    )
    session.add(row)
    session.flush()
    return row
