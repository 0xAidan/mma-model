"""Neutral immutable evidence DTOs for priced value metrics (DWCS-204).

Booleans alone must not grant EV/CLV/stake. Callers supply typed provenance:
manual observed-price evidence (DWCS-202) or quote + eligibility evidence
(DWCS-203). Selection identity is bout-scoped. Catalog validation uses
DWCS-200 domain contracts. Adapters live in ``mma_model.odds.value_bridge``.

Eligibility evidence is time-bound: ``evaluated_at`` / decision identity must
match the valuation cutoff in use; stale/replayed decisions are rejected.
Manual bout binding is an auditable assertion (actor/time/source), not a
casual unbound id field.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.value.errors import (
    IneligiblePriceError,
    InvalidOddsError,
    SelectionMismatchError,
)
from mma_model.value.odds import validate_decimal_odds


class PriceProvenanceKind(StrEnum):
    USER_OBSERVED = "user_observed"
    PROVIDER_QUOTE = "provider_quote"


class PriceObservationRole(StrEnum):
    OPENING = "opening"
    CLOSING = "closing"


class ManualBindingSource(StrEnum):
    """Named auditable sources for manual bout binding assertions."""

    USER_ASSERTION = "user_assertion"
    OPERATOR_ASSERTION = "operator_assertion"


# Closing CLV default policy: same book/region as opening unless explicitly allowed.
CROSS_BOOK_CLOSING_POLICY_DEFAULT: bool = False


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise InvalidOddsError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _require_nonempty(value: str, *, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise IneligiblePriceError(f"{field} must be non-empty")
    return text


def validate_catalog_selection(
    market_family: str,
    outcome_key: str,
    line_point: float | None,
) -> tuple[MarketFamily, OutcomeKey, str]:
    """Validate family/outcome/line via DWCS-200 catalog; return canonical market id."""
    family_text = _require_nonempty(market_family, field="market_family")
    outcome_text = _require_nonempty(outcome_key, field="outcome_key")
    try:
        family = MarketFamily(family_text)
    except ValueError as exc:
        raise IneligiblePriceError(f"unknown market_family: {family_text!r}") from exc
    try:
        outcome = OutcomeKey(outcome_text)
    except ValueError as exc:
        raise IneligiblePriceError(f"unknown outcome_key: {outcome_text!r}") from exc
    try:
        assert_known_outcome(family, outcome)
        catalog = catalog_for_family(family)
        if not catalog.is_valid_line_point(line_point):
            raise ValueError(
                f"invalid line_point {line_point!r} for family {family.value!r}"
            )
    except ValueError as exc:
        raise IneligiblePriceError(str(exc)) from exc
    if line_point is None:
        market_id = f"{family.value}:{outcome.value}"
    else:
        market_id = f"{family.value}:{outcome.value}:{float(line_point)}"
    return family, outcome, market_id


def value_selection_identity(bout_id: str, market_selection_identity: str) -> str:
    """Fight-unique value selection key: ``bout|family:outcome[:line]``."""
    bout = _require_nonempty(bout_id, field="bout_id")
    market = _require_nonempty(market_selection_identity, field="market_selection_identity")
    return f"{bout}|{market}"


def compute_eligibility_decision_identity(
    *,
    quote_id: int,
    evaluated_at: datetime,
    eligible: bool,
    reason: str,
    selection_identity: str,
    resolved_bout_id: str | None,
    quote_availability_at_decision: str,
    quote_freshness_at: datetime | None,
    lifecycle_state_at_decision: str | None,
) -> str:
    """Content identity for eligibility evidence (replay/staleness binding)."""
    as_of = _require_aware_utc(evaluated_at, field="evaluated_at").isoformat()
    freshness = (
        None
        if quote_freshness_at is None
        else _require_aware_utc(quote_freshness_at, field="quote_freshness_at").isoformat()
    )
    payload = "|".join(
        [
            str(int(quote_id)),
            as_of,
            "1" if eligible else "0",
            str(reason),
            str(selection_identity),
            "" if resolved_bout_id is None else str(resolved_bout_id),
            str(quote_availability_at_decision),
            "" if freshness is None else freshness,
            "" if lifecycle_state_at_decision is None else str(lifecycle_state_at_decision),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"elig_v1:{digest}"


@dataclass(frozen=True)
class ValueSelectionContext:
    """Canonical target for priced metrics: bout + DWCS-200 market selection."""

    bout_id: str
    market_family: str
    outcome_key: str
    line_point: float | None = None
    event_id: str | None = None
    market_selection_identity: str = ""
    value_selection_identity: str = ""

    def __post_init__(self) -> None:
        bout = _require_nonempty(self.bout_id, field="bout_id")
        _family, _outcome, market_id = validate_catalog_selection(
            self.market_family, self.outcome_key, self.line_point
        )
        value_id = value_selection_identity(bout, market_id)
        object.__setattr__(self, "bout_id", bout)
        object.__setattr__(self, "market_family", _family.value)
        object.__setattr__(self, "outcome_key", _outcome.value)
        object.__setattr__(
            self,
            "line_point",
            None if self.line_point is None else float(self.line_point),
        )
        object.__setattr__(self, "market_selection_identity", market_id)
        object.__setattr__(self, "value_selection_identity", value_id)
        if self.event_id is not None:
            object.__setattr__(
                self, "event_id", _require_nonempty(self.event_id, field="event_id")
            )


@dataclass(frozen=True)
class ManualBoutBindingAssertion:
    """Auditable user/operator assertion binding a manual price to a canonical bout.

    This is an explicit named action/DTO — not a casual unbound id parameter.
    """

    bout_id: str
    asserted_at: datetime
    asserted_by: str
    source: ManualBindingSource
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "bout_id", _require_nonempty(self.bout_id, field="bout_id")
        )
        object.__setattr__(
            self,
            "asserted_at",
            _require_aware_utc(self.asserted_at, field="asserted_at"),
        )
        object.__setattr__(
            self,
            "asserted_by",
            _require_nonempty(self.asserted_by, field="asserted_by"),
        )
        if not isinstance(self.source, ManualBindingSource):
            raise IneligiblePriceError(
                "manual bout binding source must be ManualBindingSource "
                f"(user_assertion|operator_assertion); got {self.source!r}"
            )
        if self.note is not None:
            object.__setattr__(self, "note", str(self.note))


@dataclass(frozen=True)
class ManualObservedPriceEvidence:
    """DWCS-202 user-observed available price evidence (no boolean shortcuts).

    Stored rows may be unmatched (``bout_binding=None``) but cannot produce
    metrics until an explicit ``ManualBoutBindingAssertion`` binds them to the
    target canonical bout.
    """

    provenance: PriceProvenanceKind
    automated: bool
    market_family: str
    outcome_key: str
    line_point: float | None
    selection_identity: str
    price_decimal: float
    lifecycle: str
    observed_at: datetime
    bookmaker_key: str
    region: str
    bout_binding: ManualBoutBindingAssertion | None = None
    price_role: PriceObservationRole = PriceObservationRole.OPENING

    @property
    def bound_bout_id(self) -> str | None:
        """Bound bout from auditable assertion, if present."""
        if self.bout_binding is None:
            return None
        return self.bout_binding.bout_id

    def __post_init__(self) -> None:
        if self.provenance is not PriceProvenanceKind.USER_OBSERVED:
            raise IneligiblePriceError("manual evidence requires user_observed provenance")
        if self.automated:
            raise IneligiblePriceError("user_observed evidence must set automated=False")
        if self.lifecycle != "available":
            raise IneligiblePriceError(
                "manual priced metrics require lifecycle=available"
            )
        _family, _outcome, market_id = validate_catalog_selection(
            self.market_family, self.outcome_key, self.line_point
        )
        object.__setattr__(self, "market_family", _family.value)
        object.__setattr__(self, "outcome_key", _outcome.value)
        object.__setattr__(
            self,
            "line_point",
            None if self.line_point is None else float(self.line_point),
        )
        object.__setattr__(
            self,
            "price_decimal",
            validate_decimal_odds(self.price_decimal, field="price_decimal"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_aware_utc(self.observed_at, field="observed_at"),
        )
        object.__setattr__(
            self, "bookmaker_key", _require_nonempty(self.bookmaker_key, field="bookmaker_key")
        )
        object.__setattr__(self, "region", _require_nonempty(self.region, field="region"))
        if self.selection_identity != market_id:
            raise IneligiblePriceError(
                "selection_identity mismatch versus catalog family/outcome/line: "
                f"got {self.selection_identity!r}, expected {market_id!r}"
            )
        if self.bout_binding is not None and self.bout_binding.asserted_at < self.observed_at:
            raise IneligiblePriceError(
                "manual bout binding asserted_at must be >= price observed_at"
            )


@dataclass(frozen=True)
class ProviderQuoteEvidence:
    """Persisted quote fields required for provider priced metrics."""

    quote_id: int
    provider: str
    bookmaker_key: str
    region: str
    market_family: str
    outcome_key: str
    line_point: float | None
    selection_identity: str
    price_decimal: float
    availability: str
    observed_at: datetime
    bout_id: str | None
    dedupe_key: str | None = None
    external_event_id: str | None = None
    price_role: PriceObservationRole = PriceObservationRole.OPENING

    def __post_init__(self) -> None:
        if int(self.quote_id) <= 0:
            raise IneligiblePriceError("quote_id must be a positive integer")
        if self.availability != "available":
            raise IneligiblePriceError(
                "provider priced metrics require quote availability=available"
            )
        _family, _outcome, market_id = validate_catalog_selection(
            self.market_family, self.outcome_key, self.line_point
        )
        object.__setattr__(self, "market_family", _family.value)
        object.__setattr__(self, "outcome_key", _outcome.value)
        object.__setattr__(
            self,
            "line_point",
            None if self.line_point is None else float(self.line_point),
        )
        object.__setattr__(self, "provider", _require_nonempty(self.provider, field="provider"))
        object.__setattr__(
            self, "bookmaker_key", _require_nonempty(self.bookmaker_key, field="bookmaker_key")
        )
        object.__setattr__(self, "region", _require_nonempty(self.region, field="region"))
        object.__setattr__(
            self,
            "price_decimal",
            validate_decimal_odds(self.price_decimal, field="price_decimal"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_aware_utc(self.observed_at, field="observed_at"),
        )
        if self.selection_identity != market_id:
            raise IneligiblePriceError(
                "quote selection_identity mismatch versus catalog family/outcome/line: "
                f"got {self.selection_identity!r}, expected {market_id!r}"
            )
        if self.bout_id is not None:
            object.__setattr__(
                self, "bout_id", _require_nonempty(self.bout_id, field="bout_id")
            )


@dataclass(frozen=True)
class QuoteEligibilityEvidence:
    """DWCS-203 quote-level eligibility decision bound to a quote + cutoff.

    Timeless eligible flags are rejected: ``evaluated_at`` (as_of), availability
    at decision, and ``decision_identity`` bind the decision to the quote and
    valuation cutoff in use.
    """

    quote_id: int
    eligible: bool
    selection_identity: str
    resolved_bout_id: str | None
    reason: str
    evaluated_at: datetime
    quote_availability_at_decision: str
    decision_identity: str = ""
    quote_freshness_at: datetime | None = None
    lifecycle_state_at_decision: str | None = None
    decision_version: str | None = None

    def __post_init__(self) -> None:
        if int(self.quote_id) <= 0:
            raise IneligiblePriceError("eligibility quote_id must be positive")
        evaluated = _require_aware_utc(self.evaluated_at, field="evaluated_at")
        object.__setattr__(self, "evaluated_at", evaluated)
        availability = _require_nonempty(
            self.quote_availability_at_decision,
            field="quote_availability_at_decision",
        )
        object.__setattr__(self, "quote_availability_at_decision", availability)
        freshness = (
            None
            if self.quote_freshness_at is None
            else _require_aware_utc(
                self.quote_freshness_at, field="quote_freshness_at"
            )
        )
        object.__setattr__(self, "quote_freshness_at", freshness)
        if self.lifecycle_state_at_decision is not None:
            object.__setattr__(
                self,
                "lifecycle_state_at_decision",
                _require_nonempty(
                    self.lifecycle_state_at_decision,
                    field="lifecycle_state_at_decision",
                ),
            )
        if self.decision_version is not None:
            object.__setattr__(
                self,
                "decision_version",
                _require_nonempty(self.decision_version, field="decision_version"),
            )

        parts = self.selection_identity.split(":")
        if len(parts) < 2:
            raise IneligiblePriceError(
                f"eligibility selection_identity is not catalog-shaped: "
                f"{self.selection_identity!r}"
            )
        family_text, outcome_text = parts[0], parts[1]
        line = float(parts[2]) if len(parts) >= 3 else None
        _family, _outcome, market_id = validate_catalog_selection(
            family_text, outcome_text, line
        )
        if self.selection_identity != market_id:
            raise IneligiblePriceError(
                "eligibility selection_identity mismatch versus catalog: "
                f"got {self.selection_identity!r}, expected {market_id!r}"
            )
        if self.eligible:
            if not self.resolved_bout_id or not str(self.resolved_bout_id).strip():
                raise IneligiblePriceError(
                    "eligible QuoteEligibilityEvidence requires nonempty resolved_bout_id"
                )
            if self.reason != "none":
                raise IneligiblePriceError(
                    "eligible QuoteEligibilityEvidence requires reason='none' "
                    f"(got {self.reason!r})"
                )
            if availability != "available":
                raise IneligiblePriceError(
                    "eligible QuoteEligibilityEvidence requires "
                    "quote_availability_at_decision='available'"
                )
            object.__setattr__(
                self,
                "resolved_bout_id",
                _require_nonempty(self.resolved_bout_id, field="resolved_bout_id"),
            )
        else:
            if self.reason == "none":
                raise IneligiblePriceError(
                    "ineligible QuoteEligibilityEvidence must not use reason='none'"
                )

        expected_identity = compute_eligibility_decision_identity(
            quote_id=int(self.quote_id),
            evaluated_at=evaluated,
            eligible=bool(self.eligible),
            reason=str(self.reason),
            selection_identity=str(self.selection_identity),
            resolved_bout_id=self.resolved_bout_id,
            quote_availability_at_decision=availability,
            quote_freshness_at=freshness,
            lifecycle_state_at_decision=self.lifecycle_state_at_decision,
        )
        if self.decision_identity:
            supplied = _require_nonempty(
                self.decision_identity, field="decision_identity"
            )
            if supplied != expected_identity:
                raise IneligiblePriceError(
                    "eligibility decision_identity does not match content identity "
                    "(stale/replayed or tampered evidence)"
                )
            object.__setattr__(self, "decision_identity", supplied)
        else:
            object.__setattr__(self, "decision_identity", expected_identity)


@dataclass(frozen=True)
class ClosingPriceEvidence:
    """Closing price with the same provenance gates as opening evidence.

    Provider close requires quote row + eligible decision evaluated at
    ``closing_cutoff``. Manual close requires an available timestamped user
    observation with auditable bout binding. Same-book close is required unless
    ``allow_cross_book=True`` (explicit policy exception; labeled in provenance).
    """

    manual_evidence: ManualObservedPriceEvidence | None = None
    quote_evidence: ProviderQuoteEvidence | None = None
    eligibility_evidence: QuoteEligibilityEvidence | None = None
    closing_cutoff: datetime | None = None
    allow_cross_book: bool = CROSS_BOOK_CLOSING_POLICY_DEFAULT

    def __post_init__(self) -> None:
        manual = self.manual_evidence
        quote = self.quote_evidence
        elig = self.eligibility_evidence
        if manual is None and quote is None:
            raise IneligiblePriceError("closing evidence requires manual or provider quote")
        if manual is not None and (quote is not None or elig is not None):
            raise IneligiblePriceError(
                "closing evidence must be manual XOR provider quote+eligibility"
            )
        if quote is not None and elig is None:
            raise IneligiblePriceError(
                "provider closing evidence requires QuoteEligibilityEvidence"
            )
        if elig is not None and quote is None:
            raise IneligiblePriceError(
                "closing eligibility requires matching ProviderQuoteEvidence"
            )
        if manual is not None:
            if manual.price_role is not PriceObservationRole.CLOSING:
                raise IneligiblePriceError("manual closing evidence requires price_role=closing")
            if manual.bout_binding is None:
                raise IneligiblePriceError(
                    "manual closing evidence requires ManualBoutBindingAssertion"
                )
            cutoff = (
                manual.observed_at
                if self.closing_cutoff is None
                else _require_aware_utc(self.closing_cutoff, field="closing_cutoff")
            )
            object.__setattr__(self, "closing_cutoff", cutoff)
            if cutoff != manual.observed_at:
                raise IneligiblePriceError(
                    "manual closing_cutoff must equal closing observation observed_at"
                )
        if quote is not None:
            if quote.price_role is not PriceObservationRole.CLOSING:
                raise IneligiblePriceError(
                    "provider closing evidence requires price_role=closing"
                )
            if self.closing_cutoff is None:
                raise IneligiblePriceError(
                    "provider closing evidence requires closing_cutoff"
                )
            cutoff = _require_aware_utc(self.closing_cutoff, field="closing_cutoff")
            object.__setattr__(self, "closing_cutoff", cutoff)
            assert elig is not None
            if elig.evaluated_at != cutoff:
                raise IneligiblePriceError(
                    "closing eligibility evaluated_at must equal closing_cutoff"
                )
            if quote.observed_at > cutoff:
                raise IneligiblePriceError(
                    "closing quote observed_at must be <= closing_cutoff"
                )


@dataclass(frozen=True)
class SelectionPriceObservation:
    """Resolved timestamped available price for one bout-scoped selection."""

    provenance: PriceProvenanceKind
    bout_id: str
    market_family: str
    outcome_key: str
    line_point: float | None
    market_selection_identity: str
    value_selection_identity: str
    price_decimal: float
    observed_at: datetime
    lifecycle_or_availability: str
    price_role: PriceObservationRole
    provider: str | None = None
    bookmaker_key: str | None = None
    region: str | None = None
    quote_id: int | None = None

    def __post_init__(self) -> None:
        if self.lifecycle_or_availability != "available":
            raise IneligiblePriceError(
                "selection price observation requires available lifecycle/availability"
            )
        bout = _require_nonempty(self.bout_id, field="bout_id")
        _family, _outcome, market_id = validate_catalog_selection(
            self.market_family, self.outcome_key, self.line_point
        )
        value_id = value_selection_identity(bout, market_id)
        object.__setattr__(self, "bout_id", bout)
        object.__setattr__(self, "market_family", _family.value)
        object.__setattr__(self, "outcome_key", _outcome.value)
        object.__setattr__(
            self,
            "line_point",
            None if self.line_point is None else float(self.line_point),
        )
        object.__setattr__(self, "market_selection_identity", market_id)
        object.__setattr__(self, "value_selection_identity", value_id)
        object.__setattr__(
            self,
            "price_decimal",
            validate_decimal_odds(self.price_decimal, field="price_decimal"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _require_aware_utc(self.observed_at, field="observed_at"),
        )


def assert_matches_context(
    observation: SelectionPriceObservation,
    context: ValueSelectionContext,
) -> None:
    """Require observation value selection identity equals the target context."""
    if observation.value_selection_identity != context.value_selection_identity:
        raise SelectionMismatchError(
            "observation/context value selection mismatch: "
            f"{observation.value_selection_identity!r} vs "
            f"{context.value_selection_identity!r}"
        )


def assert_same_selection(
    opening: SelectionPriceObservation,
    closing: SelectionPriceObservation,
) -> None:
    """Require identical bout-scoped selection and closing time strictly after bet."""
    if opening.value_selection_identity != closing.value_selection_identity:
        raise SelectionMismatchError(
            "opening/closing value selection mismatch: "
            f"{opening.value_selection_identity!r} vs "
            f"{closing.value_selection_identity!r}"
        )
    if closing.observed_at <= opening.observed_at:
        raise SelectionMismatchError(
            "closing observed_at must be strictly greater than opening/bet observed_at"
        )
