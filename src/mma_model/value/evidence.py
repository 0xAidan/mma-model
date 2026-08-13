"""Neutral immutable evidence DTOs for priced value metrics (DWCS-204).

Booleans alone must not grant EV/CLV/stake. Callers supply typed provenance:
manual observed-price evidence (DWCS-202) or quote + eligibility evidence
(DWCS-203). Selection identity is bout-scoped. Catalog validation uses
DWCS-200 domain contracts. Adapters live in ``mma_model.odds.value_bridge``.
"""

from __future__ import annotations

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
class ManualObservedPriceEvidence:
    """DWCS-202 user-observed available price evidence (no boolean shortcuts).

    Stored rows may be unmatched (``bound_bout_id=None``) but cannot produce
    metrics until explicitly bound to the target canonical bout.
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
    bound_bout_id: str | None = None
    price_role: PriceObservationRole = PriceObservationRole.OPENING

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
        if self.bound_bout_id is not None:
            object.__setattr__(
                self,
                "bound_bout_id",
                _require_nonempty(self.bound_bout_id, field="bound_bout_id"),
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
    """DWCS-203 quote-level eligibility decision bound to a quote_id."""

    quote_id: int
    eligible: bool
    selection_identity: str
    resolved_bout_id: str | None
    reason: str

    def __post_init__(self) -> None:
        if int(self.quote_id) <= 0:
            raise IneligiblePriceError("eligibility quote_id must be positive")
        # selection_identity must be a catalog market id (validated as family:outcome[:line]).
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


@dataclass(frozen=True)
class ClosingPriceEvidence:
    """Closing price with the same provenance gates as opening evidence.

    Provider close requires quote row + eligible decision. Manual close requires
    an available timestamped user observation bound to the target bout.
    """

    manual_evidence: ManualObservedPriceEvidence | None = None
    quote_evidence: ProviderQuoteEvidence | None = None
    eligibility_evidence: QuoteEligibilityEvidence | None = None

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
            if manual.bound_bout_id is None:
                raise IneligiblePriceError(
                    "manual closing evidence must be bound to a canonical bout"
                )
        if quote is not None and quote.price_role is not PriceObservationRole.CLOSING:
            raise IneligiblePriceError(
                "provider closing evidence requires price_role=closing"
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
