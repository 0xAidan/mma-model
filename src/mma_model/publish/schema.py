"""Pydantic v2 dashboard publish contracts (DWCS-500).

Python models are the single source of truth for JSON Schema and TypeScript.
``extra=forbid`` rejects unknown fields. Exact EV requires an observed price.
ROI/CLV exist only on confirmed-price performance buckets.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from mma_model.publish.constants import (
    DASHBOARD_CONTRACT_ID,
    DASHBOARD_CONTRACT_VERSION,
    DASHBOARD_SCHEMA_VERSION,
    DASHBOARD_TICKET,
    REQUIRED_DASHBOARD_HEALTH,
)


class StrictModel(BaseModel):
    """Base for dashboard documents: forbid extras; freeze instances."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RecommendationStateView(StrEnum):
    CONFIRMED_VALUE = "confirmed_value"
    PRICE_TARGET = "price_target"
    NO_BET = "no_bet"


class PerformanceLaneView(StrEnum):
    QUALIFIED = "qualified"
    PAPER = "paper"
    EXPERIMENTAL = "experimental"


class QuoteSourceTypeView(StrEnum):
    AUTOMATIC = "automatic"
    USER_OBSERVED = "user_observed"


class HealthStatusView(StrEnum):
    HEALTHY = "healthy"
    MISSING = "missing"
    STALE = "stale"
    BLOCKED = "blocked"
    FAILED = "failed"


class FieldPresence(StrEnum):
    """Explicit presence for sparse event metadata (never invent title/date)."""

    KNOWN = "known"
    MISSING = "missing"
    UNKNOWN = "unknown"


class PriceAvailability(StrEnum):
    AVAILABLE = "available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class LineFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class ContractEnvelope(StrictModel):
    """Version markers required on every published dashboard JSON file."""

    schema_version: Literal[1] = DASHBOARD_SCHEMA_VERSION
    contract_id: Literal["dwcs_dashboard"] = DASHBOARD_CONTRACT_ID
    contract_version: str = DASHBOARD_CONTRACT_VERSION
    ticket: Literal["DWCS-500"] = DASHBOARD_TICKET

    @field_validator("contract_version")
    @classmethod
    def _nonempty_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("contract_version must be non-empty")
        return value


class OptionalStringField(StrictModel):
    presence: FieldPresence
    value: str | None = None

    @model_validator(mode="after")
    def _presence_matches_value(self) -> OptionalStringField:
        if self.presence is FieldPresence.KNOWN and (
            self.value is None or not str(self.value).strip()
        ):
            raise ValueError("known fields require a non-empty value")
        if self.presence is not FieldPresence.KNOWN and self.value is not None:
            raise ValueError("missing/unknown fields must not invent a value")
        return self


class ArtifactHashes(StrictModel):
    model_hash: str | None = None
    feature_hash: str | None = None
    data_hash: str | None = None
    config_hash: str | None = None
    artifact_hash: str | None = None
    policy_hash: str | None = None
    thresholds_hash: str | None = None


class ObservedPriceView(StrictModel):
    decimal_odds: Annotated[float, Field(gt=1.0)]
    american_odds: float
    sportsbook: Annotated[str, Field(min_length=1)]
    source_type: QuoteSourceTypeView
    source_label: Annotated[str, Field(min_length=1)]
    timestamp: Annotated[str, Field(min_length=1)]


class MatchupPrices(StrictModel):
    """Sportsbook-agnostic thresholds plus optional observed enrichment."""

    model_fair_probability: Annotated[float, Field(gt=0.0, lt=1.0)] | None = None
    fair_decimal: Annotated[float, Field(gt=1.0)] | None = None
    fair_american: float | None = None
    fair_or_better: str | None = None
    actionable_decimal: Annotated[float, Field(gt=1.0)] | None = None
    actionable_american: float | None = None
    actionable_or_better: str | None = None
    strong_value_decimal: Annotated[float, Field(gt=1.0)] | None = None
    strong_value_american: float | None = None
    strong_value_or_better: str | None = None
    observed: ObservedPriceView | None = None
    exact_ev: float | None = None
    line_movement: float | None = None
    price_availability: PriceAvailability = PriceAvailability.UNAVAILABLE
    line_freshness: LineFreshness = LineFreshness.UNKNOWN

    @model_validator(mode="after")
    def _ev_requires_observed(self) -> MatchupPrices:
        if self.exact_ev is not None and self.observed is None:
            raise ValueError("exact_ev is allowed only when an observed price exists")
        return self


class ReasonBlocker(StrictModel):
    code: Annotated[str, Field(min_length=1)]
    message: Annotated[str, Field(min_length=1)]


class FighterSummary(StrictModel):
    fighter_id: OptionalStringField
    display_name: OptionalStringField
    corner: Literal["a", "b", "unknown"] = "unknown"


class MatchupCardChangeWarning(StrictModel):
    code: Annotated[str, Field(min_length=1)]
    message: Annotated[str, Field(min_length=1)]
    event_type: Annotated[str, Field(min_length=1)]
    observed_at: Annotated[str, Field(min_length=1)]


class MatchupMarket(StrictModel):
    """One market projection under a bout (primary or additional prediction)."""

    market_family: str | None = None
    outcome_key: str | None = None
    line_point: float | None = None
    selection_id: str | None = None
    prices: MatchupPrices
    maturity: PerformanceLaneView
    is_primary: bool = False
    reasons: tuple[ReasonBlocker, ...] = ()
    reason_plain: str = ""


class MatchupRow(StrictModel):
    bout_id: Annotated[str, Field(min_length=1)]
    event_id: Annotated[str, Field(min_length=1)]
    publication_id: str | None = None
    primary_state: RecommendationStateView
    performance_lane: PerformanceLaneView
    maturity: PerformanceLaneView
    market_family: str | None = None
    outcome_key: str | None = None
    line_point: float | None = None
    selection_id: str | None = None
    fighters: tuple[FighterSummary, ...] = ()
    prices: MatchupPrices
    markets: tuple[MatchupMarket, ...] = ()
    primary_reason: str | None = None
    reason_plain: str = ""
    reasons: tuple[ReasonBlocker, ...] = ()
    blockers: tuple[ReasonBlocker, ...] = ()
    card_change_warnings: tuple[MatchupCardChangeWarning, ...] = ()
    hashes: ArtifactHashes = Field(default_factory=ArtifactHashes)
    detail: str = ""

    @model_validator(mode="after")
    def _primary_market_present(self) -> MatchupRow:
        if not self.markets:
            raise ValueError("matchup markets must include at least the primary market")
        primaries = [m for m in self.markets if m.is_primary]
        if len(primaries) != 1:
            raise ValueError("matchup markets must mark exactly one primary market")
        return self


class CountdownFields(StrictModel):
    event_start_at: OptionalStringField
    seconds_until_start: int | None = None
    is_past: bool | None = None


class CurrentEventDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    event_id: OptionalStringField
    title: OptionalStringField
    event_date: OptionalStringField
    countdown: CountdownFields
    last_successful_update_at: OptionalStringField
    as_of: Annotated[str, Field(min_length=1)]


class MatchupsDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    event_id: OptionalStringField
    as_of: Annotated[str, Field(min_length=1)]
    matchups: tuple[MatchupRow, ...]
    confirmed_value_ranked: tuple[str, ...] = ()
    price_target_watchlist: tuple[str, ...] = ()
    no_bet_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _one_primary_state_lists(self) -> MatchupsDocument:
        ids = [row.bout_id for row in self.matchups]
        if len(ids) != len(set(ids)):
            raise ValueError("matchups must have unique bout_id values")
        by_id = {row.bout_id: row for row in self.matchups}
        for bout_id in self.confirmed_value_ranked:
            row = by_id.get(bout_id)
            if row is None or row.primary_state is not RecommendationStateView.CONFIRMED_VALUE:
                raise ValueError(
                    "confirmed_value_ranked entries must reference confirmed_value matchups"
                )
            if row.prices.observed is None or row.prices.exact_ev is None:
                raise ValueError(
                    "confirmed_value rows must include observed price and exact_ev"
                )
        for bout_id in self.price_target_watchlist:
            row = by_id.get(bout_id)
            if row is None or row.primary_state is not RecommendationStateView.PRICE_TARGET:
                raise ValueError(
                    "price_target_watchlist entries must reference price_target matchups"
                )
            if row.prices.exact_ev is not None:
                raise ValueError("price_target rows must not include exact_ev")
        for bout_id in self.no_bet_ids:
            row = by_id.get(bout_id)
            if row is None or row.primary_state is not RecommendationStateView.NO_BET:
                raise ValueError("no_bet_ids entries must reference no_bet matchups")
        return self


class DashboardHealthComponent(StrictModel):
    name: Literal[
        "pipeline",
        "data",
        "identity",
        "odds",
        "model",
        "grading",
        "backup",
        "quota",
        "freshness",
    ]
    status: HealthStatusView
    detail: str = ""
    as_of: Annotated[str, Field(min_length=1)]


class DashboardHealthDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    as_of: Annotated[str, Field(min_length=1)]
    components: tuple[DashboardHealthComponent, ...]

    @model_validator(mode="after")
    def _required_named_components(self) -> DashboardHealthDocument:
        names = [c.name for c in self.components]
        if len(names) != len(set(names)):
            raise ValueError("dashboard health components must be unique by name")
        missing = sorted(REQUIRED_DASHBOARD_HEALTH - set(names))
        if missing:
            raise ValueError(
                f"dashboard health missing required components: {','.join(missing)}"
            )
        return self


class PerformanceFilters(StrictModel):
    season: str | None = None
    market: str | None = None
    model: str | None = None
    source: str | None = None
    data_quality: str | None = None


class PredictiveMetrics(StrictModel):
    """All model outputs — proper scoring only (no ROI/CLV)."""

    sample_count: Annotated[int, Field(ge=0)] = 0
    log_loss: float | None = None
    brier: float | None = None
    calibration_slope: float | None = None
    calibration_intercept: float | None = None


class ConfirmedPriceMetrics(StrictModel):
    """Betting results only when observed prices exist."""

    pick_count: Annotated[int, Field(ge=0)] = 0
    hit_rate: float | None = None
    flat_unit_roi: float | None = None
    clv: float | None = None
    drawdown: float | None = None


class PriceTargetOnlyMetrics(StrictModel):
    """Price-target outputs — ROI/CLV/exact EV are schema-illegal here."""

    pick_count: Annotated[int, Field(ge=0)] = 0
    sporting_grade_count: Annotated[int, Field(ge=0)] = 0


class LaneMetricsBucket(StrictModel):
    lane: PerformanceLaneView
    predictive: PredictiveMetrics = Field(default_factory=PredictiveMetrics)
    confirmed_price: ConfirmedPriceMetrics = Field(default_factory=ConfirmedPriceMetrics)
    price_target_only: PriceTargetOnlyMetrics = Field(default_factory=PriceTargetOnlyMetrics)


class PerformanceDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    as_of: Annotated[str, Field(min_length=1)]
    filters: PerformanceFilters = Field(default_factory=PerformanceFilters)
    predictive: PredictiveMetrics = Field(default_factory=PredictiveMetrics)
    confirmed_price: ConfirmedPriceMetrics = Field(default_factory=ConfirmedPriceMetrics)
    price_target_only: PriceTargetOnlyMetrics = Field(default_factory=PriceTargetOnlyMetrics)
    by_lane: tuple[LaneMetricsBucket, ...] = ()


class HistoryPoint(StrictModel):
    at: Annotated[str, Field(min_length=1)]
    label: Annotated[str, Field(min_length=1)]
    bucket: Literal["predictive", "confirmed_price", "price_target_only"]
    lane: PerformanceLaneView | None = None
    value: float | None = None
    # Betting metrics only legal on confirmed_price bucket.
    flat_unit_roi: float | None = None
    clv: float | None = None

    @model_validator(mode="after")
    def _roi_clv_only_confirmed(self) -> HistoryPoint:
        if self.bucket != "confirmed_price" and (
            self.flat_unit_roi is not None or self.clv is not None
        ):
            raise ValueError(
                "roi/clv are illegal on predictive and price_target_only history points"
            )
        return self


class HistoryDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    as_of: Annotated[str, Field(min_length=1)]
    filters: PerformanceFilters = Field(default_factory=PerformanceFilters)
    points: tuple[HistoryPoint, ...] = ()


class ReleaseFileEntry(StrictModel):
    name: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(min_length=64, max_length=64)]


class ReleaseDocument(ContractEnvelope):
    series: Literal["dwcs"] = "dwcs"
    release_id: Annotated[str, Field(min_length=1)]
    event_id: str | None = None
    window_slot: str | None = None
    publications: Annotated[int, Field(ge=0)] = 0
    as_of: Annotated[str, Field(min_length=1)]
    files: tuple[ReleaseFileEntry, ...]
    hashes: ArtifactHashes = Field(default_factory=ArtifactHashes)


class ManifestDocument(ContractEnvelope):
    release_id: Annotated[str, Field(min_length=1)]
    files: tuple[str, ...]
    descriptions: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _nonempty_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("manifest files must be non-empty")
        return value


DOCUMENT_MODELS: dict[str, type[ContractEnvelope]] = {
    "release.json": ReleaseDocument,
    "manifest.json": ManifestDocument,
    "current-event.json": CurrentEventDocument,
    "matchups.json": MatchupsDocument,
    "performance.json": PerformanceDocument,
    "history.json": HistoryDocument,
    "health.json": DashboardHealthDocument,
}


def validate_document(name: str, payload: object) -> ContractEnvelope:
    model = DOCUMENT_MODELS.get(name)
    if model is None:
        raise ValueError(f"unknown dashboard document: {name!r}")
    if not isinstance(payload, dict):
        raise ValueError(f"{name}: payload must be an object")
    return model.model_validate(payload)


__all__ = [
    "ArtifactHashes",
    "ConfirmedPriceMetrics",
    "ContractEnvelope",
    "CountdownFields",
    "CurrentEventDocument",
    "DOCUMENT_MODELS",
    "DashboardHealthComponent",
    "DashboardHealthDocument",
    "FieldPresence",
    "FighterSummary",
    "HealthStatusView",
    "HistoryDocument",
    "HistoryPoint",
    "LaneMetricsBucket",
    "LineFreshness",
    "ManifestDocument",
    "MatchupCardChangeWarning",
    "MatchupMarket",
    "MatchupPrices",
    "MatchupRow",
    "MatchupsDocument",
    "ObservedPriceView",
    "OptionalStringField",
    "PerformanceDocument",
    "PerformanceFilters",
    "PerformanceLaneView",
    "PriceAvailability",
    "PriceTargetOnlyMetrics",
    "PredictiveMetrics",
    "QuoteSourceTypeView",
    "ReasonBlocker",
    "RecommendationStateView",
    "ReleaseDocument",
    "ReleaseFileEntry",
    "StrictModel",
    "validate_document",
]
