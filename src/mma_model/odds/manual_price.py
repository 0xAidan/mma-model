"""User-observed and reference price observations (DWCS-202).

Manual prices are labeled ``user_observed`` and are never automated.
Exact EV requires an observed available price; locks/removals/entitlement
failures are explicit and never forward-filled.

``selection_identity`` is the canonical DWCS-200 family/outcome/line key.
It is not the settlement rule-set content hash (see ``mma_model.markets.rules``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.odds.bookmaker_keys import is_bet365_bookmaker_key
from mma_model.odds.normalize import ensure_utc, parse_utc_datetime
from mma_model.odds.provider_decision import licensed_bookmaker_adapter_authorized
from mma_model.odds.types import PROVIDER_THE_ODDS_API
from mma_model.value.errors import InvalidOddsError, InvalidProbabilityError
from mma_model.value.ev import compute_exact_ev as value_compute_exact_ev

MANUAL_SOURCE_LABEL: Final[str] = "user_observed"

# Caller-supplied keys that would silently relabel provenance if accepted.
_RESERVED_MANUAL_PROVENANCE_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_kind", "automated"}
)


class PriceSourceKind(StrEnum):
    USER_OBSERVED = "user_observed"
    REFERENCE_PROVIDER = "reference_provider"


class LineLifecycleState(StrEnum):
    """Explicit line lifecycle — never invent availability or forward-fill."""

    AVAILABLE = "available"
    UNKNOWN = "unknown"
    SUSPENDED = "suspended"
    LOCKED = "locked"
    REMOVED = "removed"
    ENTITLEMENT_FAILED = "entitlement_failed"


_PRICED_STATES: Final[frozenset[LineLifecycleState]] = frozenset(
    {LineLifecycleState.AVAILABLE}
)


class EntitlementFailure(ValueError):
    """Raised when constructing an entitlement failure without using the recorder."""


def validate_market_selection(
    family: MarketFamily,
    outcome_key: OutcomeKey,
    line_point: float | None,
) -> None:
    """Reject family/outcome/line combinations outside the DWCS-200 catalog."""
    assert_known_outcome(family, outcome_key)
    catalog = catalog_for_family(family)
    if not catalog.is_valid_line_point(line_point):
        if catalog.requires_line_point():
            raise ValueError(
                f"market family {family.value!r} requires a canonical line_point "
                f"in {catalog.line_points!r} (got {line_point!r})"
            )
        raise ValueError(
            f"market family {family.value!r} rejects line_point "
            f"(got {line_point!r}; expected null)"
        )


def canonical_selection_identity(
    family: MarketFamily,
    outcome_key: OutcomeKey,
    line_point: float | None,
) -> str:
    """Deterministic DWCS-200 selection identity (family:outcome[:line]).

    Distinct from settlement rule-set identity (contract content hash).
    """
    validate_market_selection(family, outcome_key, line_point)
    if line_point is None:
        return f"{family.value}:{outcome_key.value}"
    return f"{family.value}:{outcome_key.value}:{float(line_point)}"


def _normalize_utc(value: datetime, *, field: str) -> datetime:
    """Require aware datetime and return UTC (for frozen dataclass assignment)."""
    return ensure_utc(value, field=field).astimezone(UTC)


def _resolve_selection_identity(
    *,
    family: MarketFamily,
    outcome_key: OutcomeKey,
    line_point: float | None,
    supplied: str | None,
) -> str:
    canonical = canonical_selection_identity(family, outcome_key, line_point)
    if supplied is None:
        return canonical
    if supplied != canonical:
        raise ValueError(
            "selection_identity mismatch versus family/outcome/line: "
            f"got {supplied!r}, expected {canonical!r}"
        )
    return canonical


@dataclass(frozen=True)
class ObservedPrice:
    """Canonical observation identity for an offered (or missing) price."""

    source_kind: PriceSourceKind
    automated: bool
    provider: str | None
    bookmaker_key: str
    bookmaker_title: str | None
    region: str
    market_family: MarketFamily
    outcome_key: OutcomeKey
    line_point: float | None
    price_decimal: float | None
    lifecycle: LineLifecycleState
    observed_at: datetime
    source_updated_at: datetime | None
    event_external_id: str | None
    selection_identity: str | None = None
    detail: str | None = None
    attempted_provider: str | None = None

    def __post_init__(self) -> None:
        if not str(self.bookmaker_key).strip():
            raise ValueError("bookmaker_key must be non-empty")
        if not str(self.region).strip():
            raise ValueError("region must be non-empty")
        validate_market_selection(
            self.market_family, self.outcome_key, self.line_point
        )
        if self.source_kind is PriceSourceKind.USER_OBSERVED and self.automated:
            raise ValueError("user_observed prices must set automated=False")
        if self.source_kind is PriceSourceKind.USER_OBSERVED and self.provider is not None:
            raise ValueError("user_observed prices must not claim a provider id")
        if (
            (
                self.automated
                or self.source_kind is PriceSourceKind.REFERENCE_PROVIDER
            )
            and is_bet365_bookmaker_key(self.bookmaker_key)
            and not licensed_bookmaker_adapter_authorized()
        ):
            raise ValueError(
                "automated/reference observations may not use Bet365 aliases "
                f"({self.bookmaker_key!r}) while Phase 0 fallback is active; "
                "record Bet365 only via non-automated user_observed prices"
            )
        if self.lifecycle in _PRICED_STATES:
            if self.price_decimal is None or self.price_decimal <= 1.0:
                raise ValueError(
                    "available observations require price_decimal > 1.0"
                )
        elif self.price_decimal is not None:
            raise ValueError(
                f"{self.lifecycle.value} observations must not carry price_decimal "
                "(no forward-fill)"
            )

        if self.lifecycle is LineLifecycleState.ENTITLEMENT_FAILED:
            if not (self.attempted_provider and str(self.attempted_provider).strip()):
                raise ValueError(
                    "entitlement_failed requires non-empty attempted_provider"
                )
        elif self.attempted_provider is not None:
            raise ValueError(
                "attempted_provider is only valid for entitlement_failed lifecycle"
            )

        object.__setattr__(
            self,
            "observed_at",
            _normalize_utc(self.observed_at, field="observed_at"),
        )
        if self.source_updated_at is not None:
            object.__setattr__(
                self,
                "source_updated_at",
                _normalize_utc(self.source_updated_at, field="source_updated_at"),
            )
            if self.source_updated_at > self.observed_at:
                raise ValueError(
                    "source_updated_at must be <= observed_at "
                    f"(got source_updated_at={self.source_updated_at.isoformat()}, "
                    f"observed_at={self.observed_at.isoformat()})"
                )

        object.__setattr__(
            self,
            "selection_identity",
            _resolve_selection_identity(
                family=self.market_family,
                outcome_key=self.outcome_key,
                line_point=self.line_point,
                supplied=_optional_str(self.selection_identity),
            ),
        )

    @property
    def dedupe_key(self) -> str:
        payload = "|".join(
            [
                self.source_kind.value,
                self.provider or "",
                self.attempted_provider or "",
                self.bookmaker_key,
                self.region,
                self.event_external_id or "",
                self.market_family.value,
                self.outcome_key.value,
                "" if self.line_point is None else f"{float(self.line_point):.4f}",
                self.lifecycle.value,
                self.observed_at.isoformat(),
                "" if self.price_decimal is None else f"{self.price_decimal:.6f}",
            ]
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def as_identity_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind.value,
            "automated": self.automated,
            "provider": self.provider,
            "attempted_provider": self.attempted_provider,
            "bookmaker_key": self.bookmaker_key,
            "bookmaker_title": self.bookmaker_title,
            "region": self.region,
            "market_family": self.market_family.value,
            "outcome_key": self.outcome_key.value,
            "line_point": self.line_point,
            "lifecycle": self.lifecycle.value,
            "observed_at": self.observed_at.isoformat(),
            "source_updated_at": (
                None
                if self.source_updated_at is None
                else self.source_updated_at.isoformat()
            ),
            "event_external_id": self.event_external_id,
            "selection_identity": self.selection_identity,
            "price_decimal": self.price_decimal,
            "detail": self.detail,
        }

    def matches_selection(
        self,
        *,
        family: MarketFamily,
        outcome_key: OutcomeKey,
        line_point: float | None,
    ) -> bool:
        if self.market_family is not family:
            return False
        if self.outcome_key is not outcome_key:
            return False
        if self.line_point is None and line_point is None:
            return True
        if self.line_point is None or line_point is None:
            return False
        return float(self.line_point) == float(line_point)

    @classmethod
    def entitlement_failed(cls, **_kwargs: Any) -> ObservedPrice:
        """Refuse to construct entitlement rows via this constructor."""
        raise EntitlementFailure(
            "use ObservedPrice.record_entitlement_failure(...) so entitlement "
            "failures stay explicit and never invent a price"
        )

    @classmethod
    def record_entitlement_failure(
        cls,
        *,
        provider: str,
        bookmaker_key: str,
        region: str,
        market_family: MarketFamily,
        outcome_key: OutcomeKey,
        observed_at: datetime,
        detail: str,
        bookmaker_title: str | None = None,
        line_point: float | None = None,
        event_external_id: str | None = None,
        selection_identity: str | None = None,
        source_updated_at: datetime | None = None,
    ) -> ObservedPrice:
        attempted = str(provider).strip()
        if not attempted:
            raise ValueError("entitlement failure requires non-empty provider")
        return cls(
            source_kind=PriceSourceKind.USER_OBSERVED,
            automated=False,
            provider=None,
            bookmaker_key=bookmaker_key,
            bookmaker_title=bookmaker_title,
            region=region,
            market_family=market_family,
            outcome_key=outcome_key,
            line_point=line_point,
            price_decimal=None,
            lifecycle=LineLifecycleState.ENTITLEMENT_FAILED,
            observed_at=observed_at,
            source_updated_at=source_updated_at,
            event_external_id=event_external_id,
            selection_identity=selection_identity,
            detail=detail,
            attempted_provider=attempted,
        )

    @classmethod
    def from_reference_quote(
        cls,
        *,
        provider: str,
        bookmaker_key: str,
        bookmaker_title: str | None,
        region: str,
        market_family: MarketFamily,
        outcome_key: OutcomeKey,
        price_decimal: float,
        observed_at: datetime,
        event_external_id: str | None = None,
        line_point: float | None = None,
        source_updated_at: datetime | None = None,
        selection_identity: str | None = None,
    ) -> ObservedPrice:
        if provider != PROVIDER_THE_ODDS_API:
            raise ValueError(
                f"reference quotes must use provider={PROVIDER_THE_ODDS_API!r} "
                f"(got {provider!r}); licensed adapters are unauthorized"
            )
        return cls(
            source_kind=PriceSourceKind.REFERENCE_PROVIDER,
            automated=True,
            provider=provider,
            bookmaker_key=bookmaker_key,
            bookmaker_title=bookmaker_title,
            region=region,
            market_family=market_family,
            outcome_key=outcome_key,
            line_point=line_point,
            price_decimal=price_decimal,
            lifecycle=LineLifecycleState.AVAILABLE,
            observed_at=observed_at,
            source_updated_at=source_updated_at,
            event_external_id=event_external_id,
            selection_identity=selection_identity,
            detail="reference odds; never Bet365",
            attempted_provider=None,
        )


def compute_exact_ev(model_prob: float, offered_decimal: float) -> float:
    """Exact EV per 1 unit staked from model prob and decimal offer.

    ``EV = model_prob * offered_decimal - 1``. Requires a real offered price.
    Delegates to the DWCS-204 value math implementation.
    """
    try:
        return value_compute_exact_ev(model_prob, offered_decimal)
    except (InvalidOddsError, InvalidProbabilityError) as exc:
        raise ValueError(str(exc)) from exc


def parse_manual_price_observation(payload: Mapping[str, Any]) -> ObservedPrice:
    """Parse a user-entered numeric price / book / time observation.

    Always labels the row ``user_observed`` / non-automated. Reserved provenance
    fields (``source_kind``, ``automated``) are rejected when present so callers
    cannot silently claim reference/automated provenance. ``provider`` is only
    accepted as an alias for ``attempted_provider`` on ``entitlement_failed``.
    """
    if "prior_price_decimal" in payload and payload.get("prior_price_decimal") not in (
        None,
        "",
    ):
        raise ValueError(
            "prior_price_decimal is not accepted (no forward-fill / silent drop); "
            "record a prior AVAILABLE observation separately if needed"
        )

    for key in _RESERVED_MANUAL_PROVENANCE_FIELDS:
        if key in payload and payload.get(key) not in (None, ""):
            raise ValueError(
                f"manual observation rejects reserved field {key!r}; "
                "parser always sets source_kind=user_observed and automated=false"
            )

    if "settlement_identity" in payload and payload.get("settlement_identity") not in (
        None,
        "",
    ):
        raise ValueError(
            "settlement_identity is not accepted; use selection_identity "
            "(canonical DWCS-200 family:outcome[:line], not settlement rule-set id)"
        )

    bookmaker_key = str(payload.get("bookmaker_key") or "").strip()
    if not bookmaker_key:
        raise ValueError("manual observation requires bookmaker_key")
    region = str(payload.get("region") or "").strip()
    if not region:
        raise ValueError("manual observation requires region")

    family = MarketFamily(str(payload["market_family"]))
    outcome = OutcomeKey(str(payload["outcome_key"]))
    line_point_raw = payload.get("line_point")
    line_point = None if line_point_raw in (None, "") else float(line_point_raw)
    validate_market_selection(family, outcome, line_point)

    raw_lifecycle = payload.get("lifecycle") or LineLifecycleState.AVAILABLE.value
    lifecycle = LineLifecycleState(str(raw_lifecycle))

    observed_at = parse_utc_datetime(payload.get("observed_at"), field="observed_at")
    if observed_at is None:
        raise ValueError("manual observation requires observed_at")
    source_updated_at = parse_utc_datetime(
        payload.get("source_updated_at"), field="source_updated_at"
    )

    price_raw = payload.get("price_decimal")
    price_present = price_raw not in (None, "")
    if lifecycle is LineLifecycleState.AVAILABLE:
        if not price_present:
            raise ValueError("available manual observation requires price_decimal")
        price_decimal = float(price_raw)
    else:
        if price_present:
            raise ValueError(
                f"{lifecycle.value} observations must not include price_decimal "
                "(reject forward-fill; omit the field)"
            )
        price_decimal = None

    attempted_provider = _optional_str(payload.get("attempted_provider"))
    provider_alias = _optional_str(payload.get("provider"))
    if lifecycle is LineLifecycleState.ENTITLEMENT_FAILED:
        if attempted_provider and provider_alias and attempted_provider != provider_alias:
            raise ValueError(
                "conflicting provider and attempted_provider for entitlement_failed "
                f"(provider={provider_alias!r}, attempted_provider={attempted_provider!r})"
            )
        if not attempted_provider:
            attempted_provider = provider_alias
        if not attempted_provider:
            raise ValueError(
                "entitlement_failed requires attempted_provider "
                "(or provider alias in JSON)"
            )
    else:
        if provider_alias is not None:
            raise ValueError(
                "provider is only valid as attempted_provider alias when "
                "lifecycle=entitlement_failed"
            )
        if attempted_provider is not None:
            raise ValueError(
                "attempted_provider is only valid when lifecycle=entitlement_failed"
            )

    title = payload.get("bookmaker_title")
    return ObservedPrice(
        source_kind=PriceSourceKind.USER_OBSERVED,
        automated=False,
        provider=None,
        bookmaker_key=bookmaker_key,
        bookmaker_title=None if title in (None, "") else str(title),
        region=region,
        market_family=family,
        outcome_key=outcome,
        line_point=line_point,
        price_decimal=price_decimal,
        lifecycle=lifecycle,
        observed_at=observed_at,
        source_updated_at=source_updated_at,
        event_external_id=_optional_str(payload.get("event_external_id")),
        selection_identity=_optional_str(payload.get("selection_identity")),
        detail=_optional_str(payload.get("detail")),
        attempted_provider=attempted_provider,
    )


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return text or None
