"""User-observed and reference price observations (DWCS-202).

Manual prices are labeled ``user_observed`` and are never automated.
Exact EV requires an observed available price; locks/removals/entitlement
failures are explicit and never forward-filled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final

from mma_model.domain.markets import (
    MarketFamily,
    OutcomeKey,
    assert_known_outcome,
    catalog_for_family,
)
from mma_model.odds.normalize import ensure_utc, parse_utc_datetime
from mma_model.odds.types import PROVIDER_THE_ODDS_API

MANUAL_SOURCE_LABEL: Final[str] = "user_observed"


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


# States that may carry a numeric offered price.
_PRICED_STATES: Final[frozenset[LineLifecycleState]] = frozenset(
    {LineLifecycleState.AVAILABLE}
)


class EntitlementFailure(ValueError):
    """Raised when constructing an entitlement failure without using the recorder."""


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
    settlement_identity: str | None
    detail: str | None = None
    prior_price_decimal: float | None = None

    def __post_init__(self) -> None:
        assert_known_outcome(self.market_family, self.outcome_key)
        catalog = catalog_for_family(self.market_family)
        if not catalog.is_valid_line_point(self.line_point):
            raise ValueError(
                f"invalid line_point {self.line_point!r} for {self.market_family}"
            )
        if self.source_kind is PriceSourceKind.USER_OBSERVED and self.automated:
            raise ValueError("user_observed prices must set automated=False")
        if self.source_kind is PriceSourceKind.USER_OBSERVED and self.provider is not None:
            raise ValueError("user_observed prices must not claim a provider id")
        if self.lifecycle in _PRICED_STATES:
            if self.price_decimal is None or self.price_decimal <= 1.0:
                raise ValueError(
                    "available observations require price_decimal > 1.0"
                )
        else:
            if self.price_decimal is not None:
                raise ValueError(
                    f"{self.lifecycle.value} observations must not carry price_decimal "
                    "(no forward-fill)"
                )
        ensure_utc(self.observed_at, field="observed_at")
        if self.source_updated_at is not None:
            ensure_utc(self.source_updated_at, field="source_updated_at")

    @property
    def dedupe_key(self) -> str:
        payload = "|".join(
            [
                self.source_kind.value,
                self.provider or "",
                self.bookmaker_key,
                self.region,
                self.event_external_id or "",
                self.market_family.value,
                self.outcome_key.value,
                "" if self.line_point is None else f"{self.line_point:.4f}",
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
            "settlement_identity": self.settlement_identity,
            "price_decimal": self.price_decimal,
            "detail": self.detail,
        }

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
        settlement_identity: str | None = None,
    ) -> ObservedPrice:
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
            observed_at=ensure_utc(observed_at, field="observed_at"),
            source_updated_at=None,
            event_external_id=event_external_id,
            settlement_identity=settlement_identity,
            detail=f"provider={provider}: {detail}",
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
        settlement_identity: str | None = None,
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
            observed_at=ensure_utc(observed_at, field="observed_at"),
            source_updated_at=(
                None
                if source_updated_at is None
                else ensure_utc(source_updated_at, field="source_updated_at")
            ),
            event_external_id=event_external_id,
            settlement_identity=settlement_identity,
            detail="reference odds; never Bet365",
        )


def compute_exact_ev(model_prob: float, offered_decimal: float) -> float:
    """Exact EV per 1 unit staked from model prob and decimal offer.

    ``EV = model_prob * offered_decimal - 1``. Requires a real offered price.
    """
    if not 0.0 < model_prob < 1.0:
        raise ValueError("model_prob must be in (0, 1)")
    if offered_decimal <= 1.0:
        raise ValueError("offered_decimal must be > 1")
    return model_prob * offered_decimal - 1.0


def parse_manual_price_observation(payload: Mapping[str, Any]) -> ObservedPrice:
    """Parse a user-entered numeric price / book / time observation."""
    bookmaker_key = str(payload.get("bookmaker_key") or "").strip()
    if not bookmaker_key:
        raise ValueError("manual observation requires bookmaker_key")
    region = str(payload.get("region") or "").strip()
    if not region:
        raise ValueError("manual observation requires region")

    family = MarketFamily(str(payload["market_family"]))
    outcome = OutcomeKey(str(payload["outcome_key"]))
    assert_known_outcome(family, outcome)

    raw_lifecycle = payload.get("lifecycle") or LineLifecycleState.AVAILABLE.value
    lifecycle = LineLifecycleState(str(raw_lifecycle))

    observed_at = parse_utc_datetime(payload.get("observed_at"), field="observed_at")
    if observed_at is None:
        raise ValueError("manual observation requires observed_at")
    source_updated_at = parse_utc_datetime(
        payload.get("source_updated_at"), field="source_updated_at"
    )

    line_point_raw = payload.get("line_point")
    line_point = None if line_point_raw in (None, "") else float(line_point_raw)

    prior_raw = payload.get("prior_price_decimal")
    prior_price = None if prior_raw in (None, "") else float(prior_raw)

    price_raw = payload.get("price_decimal")
    if lifecycle is LineLifecycleState.AVAILABLE:
        if price_raw in (None, ""):
            raise ValueError("available manual observation requires price_decimal")
        price_decimal = float(price_raw)
    else:
        # Explicit non-priced states: ignore any accidental price field.
        price_decimal = None

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
        settlement_identity=_optional_str(payload.get("settlement_identity")),
        detail=_optional_str(payload.get("detail")),
        prior_price_decimal=prior_price,
    )


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
