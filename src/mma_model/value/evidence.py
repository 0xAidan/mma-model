"""Neutral immutable evidence DTOs for priced value metrics (DWCS-204).

Booleans alone must not grant EV/CLV/stake. Callers supply typed provenance:
manual observed-price evidence (DWCS-202) or quote + eligibility evidence
(DWCS-203). This module stays free of odds.manual_price / odds.lifecycle imports
to avoid cycles; adapters live in ``mma_model.odds.value_bridge``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from mma_model.value.errors import (
    IneligiblePriceError,
    InvalidOddsError,
    SelectionMismatchError,
)
from mma_model.value.odds import validate_decimal_odds


class PriceProvenanceKind(StrEnum):
    USER_OBSERVED = "user_observed"
    PROVIDER_QUOTE = "provider_quote"


def _require_aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise InvalidOddsError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ManualObservedPriceEvidence:
    """DWCS-202 user-observed available price evidence (no boolean shortcuts)."""

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

    def __post_init__(self) -> None:
        if self.provenance is not PriceProvenanceKind.USER_OBSERVED:
            raise IneligiblePriceError("manual evidence requires user_observed provenance")
        if self.automated:
            raise IneligiblePriceError("user_observed evidence must set automated=False")
        if self.lifecycle != "available":
            raise IneligiblePriceError(
                "manual priced metrics require lifecycle=available"
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
        if not self.selection_identity.strip():
            raise IneligiblePriceError("selection_identity must be non-empty")
        expected = _selection_identity(
            self.market_family, self.outcome_key, self.line_point
        )
        if self.selection_identity != expected:
            raise IneligiblePriceError(
                "selection_identity mismatch versus family/outcome/line: "
                f"got {self.selection_identity!r}, expected {expected!r}"
            )


@dataclass(frozen=True)
class ProviderQuoteEvidence:
    """Persisted quote fields required for provider priced metrics."""

    quote_id: int
    market_family: str
    outcome_key: str
    line_point: float | None
    selection_identity: str
    price_decimal: float
    availability: str
    observed_at: datetime
    bout_id: str | None
    bookmaker_key: str
    region: str

    def __post_init__(self) -> None:
        if int(self.quote_id) <= 0:
            raise IneligiblePriceError("quote_id must be a positive integer")
        if self.availability != "available":
            raise IneligiblePriceError(
                "provider priced metrics require quote availability=available"
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
        expected = _selection_identity(
            self.market_family, self.outcome_key, self.line_point
        )
        if self.selection_identity != expected:
            raise IneligiblePriceError(
                "quote selection_identity mismatch versus family/outcome/line: "
                f"got {self.selection_identity!r}, expected {expected!r}"
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
        if not self.selection_identity.strip():
            raise IneligiblePriceError("eligibility selection_identity must be non-empty")


@dataclass(frozen=True)
class SelectionPriceObservation:
    """Timestamped available price for one canonical selection (CLV path)."""

    provenance: PriceProvenanceKind
    market_family: str
    outcome_key: str
    line_point: float | None
    selection_identity: str
    price_decimal: float
    observed_at: datetime
    lifecycle_or_availability: str

    def __post_init__(self) -> None:
        if self.lifecycle_or_availability != "available":
            raise IneligiblePriceError(
                "selection price observation requires available lifecycle/availability"
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
        expected = _selection_identity(
            self.market_family, self.outcome_key, self.line_point
        )
        if self.selection_identity != expected:
            raise SelectionMismatchError(
                "selection_identity mismatch versus family/outcome/line: "
                f"got {self.selection_identity!r}, expected {expected!r}"
            )


def _selection_identity(
    market_family: str,
    outcome_key: str,
    line_point: float | None,
) -> str:
    family = str(market_family).strip()
    outcome = str(outcome_key).strip()
    if not family or not outcome:
        raise IneligiblePriceError("market_family and outcome_key must be non-empty")
    if line_point is None:
        return f"{family}:{outcome}"
    return f"{family}:{outcome}:{float(line_point)}"


def assert_same_selection(
    opening: SelectionPriceObservation,
    closing: SelectionPriceObservation,
) -> None:
    """Require identical selection identity and closing time after bet time."""
    if opening.selection_identity != closing.selection_identity:
        raise SelectionMismatchError(
            "opening/closing selection_identity mismatch: "
            f"{opening.selection_identity!r} vs {closing.selection_identity!r}"
        )
    if (
        opening.market_family != closing.market_family
        or opening.outcome_key != closing.outcome_key
        or opening.line_point != closing.line_point
    ):
        raise SelectionMismatchError(
            "opening/closing market/outcome/line identity mismatch"
        )
    if closing.observed_at < opening.observed_at:
        raise SelectionMismatchError(
            "closing observed_at must be >= opening/bet observed_at"
        )
