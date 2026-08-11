"""Strict loader for the frozen DWCS evaluation contract (DWCS-001).

The authoritative contract bytes live in package data
(`mma_model/evaluation/dwcs_v1.json`). The plan-visible path
`config/evaluation/dwcs_v1.json` is a symlink to that same file in the
checkout so there is one source of truth and no silent duplicate drift.

Default loads always verify the canonical SHA-256 digest pinned in
``PINNED_CONTRACT_HASH``. Content changes require updating both
``contract_version`` and ``PINNED_CONTRACT_HASH``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Final, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mma_model.config import get_settings

CONTRACT_FILENAME: Final = "dwcs_v1.json"
CONTRACT_ID: Final = "dwcs_evaluation"
EXPECTED_SCHEMA_VERSION: Final = 1
EXPECTED_CONTRACT_VERSION: Final = "1.0.1"
# Canonical JSON digest: SHA-256 of json.dumps(..., sort_keys=True,
# separators=(",", ":"), ensure_ascii=True) over the authoritative contract
# object (packaged mma_model/evaluation/dwcs_v1.json bytes). Update only
# together with an intentional contract_version bump.
PINNED_CONTRACT_HASH: Final = (
    "af0ad518a6417ac7d67e5f56fe836ab58afe55d8ac70813bf6045307ea6fb2cf"
)

REQUIRED_DEVELOPMENT_SEASONS: Final[tuple[int, ...]] = (
    2017,
    2018,
    2019,
    2020,
    2021,
    2022,
    2023,
)
REQUIRED_VALIDATION_SEASONS: Final[tuple[int, ...]] = (2024,)
REQUIRED_HOLDOUT_SEASONS: Final[tuple[int, ...]] = (2025,)
REQUIRED_INTERVAL_LEVELS: Final[tuple[float, ...]] = (0.9, 0.95)

FAIR_DECIMAL_ODDS_FORMULA: Final = "1 / p50"
ACTIONABLE_DECIMAL_PRICE_FORMULA: Final = "max(1 / p25, 1.05 / p50)"
STRONG_VALUE_DECIMAL_PRICE_FORMULA: Final = "max(1 / p25, 1.10 / p50)"


class EvaluationContractError(Exception):
    """Base error for evaluation-contract failures."""


class ContractValidationError(EvaluationContractError):
    """Contract JSON failed schema or protocol validation."""


class ContractVersionMismatch(EvaluationContractError):
    """Contract version or id did not match the expected frozen identity."""


class ContractSchemaMismatch(EvaluationContractError):
    """Contract schema_version did not match the expected schema."""


class ContractHashMismatch(EvaluationContractError):
    """Contract content hash did not match the pinned digest."""


class SplitGrouping(StrEnum):
    EVENT_CARD = "event_card"


class OuterFold(StrEnum):
    ROLLING_ORIGIN_ONE_CARD = "rolling_origin_one_card_at_a_time"


class ReportUniverse(StrEnum):
    ALL_DWCS = "all_dwcs"
    STANDARD_ONLY = "standard_only"


class TerminalAtom(StrEnum):
    A_KO_TKO = "a_ko_tko"
    A_SUBMISSION = "a_submission"
    A_OTHER_STOPPAGE = "a_other_stoppage"
    A_DECISION = "a_decision"
    B_KO_TKO = "b_ko_tko"
    B_SUBMISSION = "b_submission"
    B_OTHER_STOPPAGE = "b_other_stoppage"
    B_DECISION = "b_decision"
    DRAW = "draw"


class SettlementOnlyLabel(StrEnum):
    NO_CONTEST = "no_contest"
    VOID = "void"


class BaselineM0(StrEnum):
    FIFTY_FIFTY = "fifty_fifty"
    SEQUENTIAL_RATING = "sequential_rating"
    NO_VIG_MARKET = "no_vig_market"


class BaselineM1(StrEnum):
    RIDGE_LOGISTIC = "ridge_logistic_moneyline"


class OutcomeMetric(StrEnum):
    JOINT_LOG_LOSS = "joint_log_loss"
    MARKET_LOG_LOSS = "market_log_loss"
    BRIER = "brier"
    CALIBRATION_INTERCEPT = "calibration_intercept"
    CALIBRATION_SLOPE = "calibration_slope"
    RELIABILITY_BINS = "reliability_bins"
    ECE = "ece"
    ACCURACY_DESCRIPTIVE_ONLY = "accuracy_descriptive_only"
    SKILL_VS_EACH_BASELINE = "skill_vs_each_baseline"


class BettingMetric(StrEnum):
    QUALIFYING_BETS = "qualifying_bets"
    TURNOVER = "turnover"
    FLAT_1_UNIT_ROI = "flat_1_unit_roi"
    QUARTER_KELLY_ROI = "quarter_kelly_roi_capped_at_1_percent_bankroll"
    CLV = "clv"
    MAXIMUM_DRAWDOWN = "maximum_drawdown"
    LONGEST_LOSING_RUN = "longest_losing_run"
    BY_MARKET_SPLITS = "by_market_splits"
    BY_SEASON_SPLITS = "by_season_splits"


class SelectionMetric(StrEnum):
    AVAILABILITY = "availability"
    ABSTENTION_RATE = "abstention_rate"


class PricedRowsRequire(StrEnum):
    TIMESTAMPED_OBSERVED_OR_USER_RECORDED = "timestamped_observed_or_user_recorded_price"


class BootstrapUnit(StrEnum):
    EVENT_BLOCK = "event_block"


class RankConfirmedBy(StrEnum):
    HIGHEST_P25_EV = "highest_p25_ev"


class RecommendationClass(StrEnum):
    CONFIRMED_VALUE = "confirmed_value"
    PRICE_TARGET = "price_target"
    NO_BET = "no_bet"


class FailedFamilyStatus(StrEnum):
    EXPERIMENTAL = "experimental"


class BoundComparison(StrEnum):
    LT = "lt"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeasonBounds(_FrozenModel):
    first: int
    last: int


class UniverseCounts(_FrozenModel):
    cards: int
    bouts: int


class BrazilUniverse(UniverseCounts):
    series_variant: Literal["dwcs_brazil"]
    year: int


class UniverseSpec(_FrozenModel):
    series: Literal["dwcs"]
    seasons: SeasonBounds
    all_dwcs: UniverseCounts
    standard_only: UniverseCounts
    brazil: BrazilUniverse


class SplitWindow(_FrozenModel):
    seasons: tuple[int, ...]
    locked: bool

    @field_validator("seasons", mode="before")
    @classmethod
    def _tupleize_seasons(cls, value: Any) -> tuple[int, ...]:
        if isinstance(value, list):
            return tuple(int(v) for v in value)
        return cast(tuple[int, ...], value)


class SplitsSpec(_FrozenModel):
    grouping: SplitGrouping
    outer_fold: OuterFold
    target_cards: int
    development: SplitWindow
    validation: SplitWindow
    holdout: SplitWindow


class MutableFactRules(_FrozenModel):
    effective_at_strictly_before_cutoff: Literal[True]
    observed_at_at_or_before_cutoff: Literal[True]


class PointInTimeSpec(_FrozenModel):
    prediction_cutoff_minutes_before_scheduled_start: Literal[60]
    identical_cutoff_per_card: Literal[True]
    mutable_fact_rules: MutableFactRules
    forbid_same_card_results: Literal[True]
    forbid_later_corrections_before_adjudication: Literal[True]
    forbid_current_fighter_aggregates: Literal[True]
    forbid_post_cutoff_odds_snapshots: Literal[True]
    proxy_scheduled_start_excludes_exact_closing_line_analysis: Literal[True]


class SensitivitySpec(_FrozenModel):
    report_universes: tuple[ReportUniverse, ...]

    @field_validator("report_universes", mode="before")
    @classmethod
    def _tupleize(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class BaselineSpec(_FrozenModel):
    M0: tuple[BaselineM0, ...]
    M1: BaselineM1

    @field_validator("M0", mode="before")
    @classmethod
    def _tupleize_m0(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class LabelsSpec(_FrozenModel):
    terminal_atoms: tuple[TerminalAtom, ...]
    settlement_only_excluded_from_win_fitting: tuple[SettlementOnlyLabel, ...]
    half_round_intervals_for_three_round_bout: Literal[6]
    baselines: BaselineSpec

    @field_validator("terminal_atoms", "settlement_only_excluded_from_win_fitting", mode="before")
    @classmethod
    def _tupleize(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class MetricsSpec(_FrozenModel):
    outcome: tuple[OutcomeMetric, ...]
    betting_priced_only: tuple[BettingMetric, ...]
    selection: tuple[SelectionMetric, ...]
    priced_rows_require: PricedRowsRequire
    price_target_rows_never_receive_synthetic_betting_performance: Literal[True]

    @field_validator("outcome", "betting_priced_only", "selection", mode="before")
    @classmethod
    def _tupleize(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class IntervalBandSpec(_FrozenModel):
    bootstrap_unit: BootstrapUnit
    interval_levels: tuple[float, ...]
    note: str | None = None

    @field_validator("interval_levels", mode="before")
    @classmethod
    def _tupleize_levels(cls, value: Any) -> tuple[float, ...]:
        return tuple(float(v) for v in value)


class ConfidenceIntervalsSpec(_FrozenModel):
    bootstrap_refits: Literal[200]
    bootstrap_unit: BootstrapUnit
    probability_and_ev: IntervalBandSpec
    betting_metrics: IntervalBandSpec


class RecommendationSpec(_FrozenModel):
    max_confirmed_value_markets_per_matchup: Literal[1]
    rank_confirmed_by: RankConfirmedBy
    emit_no_bet_when_gates_fail: Literal[True]
    fair_decimal_odds: Literal["1 / p50"]
    actionable_ev_target: Literal[0.05]
    strong_value_ev_target: Literal[0.1]
    actionable_decimal_price: Literal["max(1 / p25, 1.05 / p50)"]
    strong_value_decimal_price: Literal["max(1 / p25, 1.10 / p50)"]
    confirmed_value_min_prob_ev_positive: Literal[0.7]
    exact_round_actionable_ev_target: Literal[0.1]
    exact_round_min_prob_ev_positive: Literal[0.75]
    classifications: tuple[RecommendationClass, ...]
    unpriced_target_is_not_best_available_market: Literal[True]
    american_odds_renderer_expresses_or_better: Literal[True]

    @field_validator("classifications", mode="before")
    @classmethod
    def _tupleize_classifications(cls, value: Any) -> tuple[Any, ...]:
        return tuple(value)


class PricePolicySpec(_FrozenModel):
    bookmaker_odds_optional_enrichment: Literal[True]
    missing_bet365_does_not_block_core_guidance: Literal[True]
    sportsbook_agnostic_fair_actionable_strong_value_required: Literal[True]
    exact_ev_roi_clv_require_timestamped_price: Literal[True]
    price_target_only_rows_never_receive_synthetic_betting_performance: Literal[True]


class DataGateSpec(_FrozenModel):
    target_cards: Literal[89]
    target_bouts: Literal[440]
    min_cross_source_reconciliation: float
    min_result_agreement: float
    max_unresolved_identity_conflicts: Literal[0]
    max_leakage_invariant_failures: Literal[0]


class StrictUpperBound(_FrozenModel):
    """Numeric gate with an unambiguous comparison operator."""

    strict_upper_bound: float
    comparison: BoundComparison
    meaning: str

    def passes(self, value: float) -> bool:
        if self.comparison is BoundComparison.LT:
            return value < self.strict_upper_bound
        raise EvaluationContractError(f"Unsupported comparison: {self.comparison!r}")


class MoneylineGateSpec(_FrozenModel):
    max_unexplained_feature_exclusion_rate_decisive: float
    holdout_2025_delta_log_loss_vs_m1_must_be_non_positive: Literal[True]
    holdout_2025_event_block_90pct_ucb_delta_log_loss: StrictUpperBound
    odds_backed_2020_plus_max_worse_than_no_vig_log_loss: float
    odds_backed_2020_plus_positive_skill_vs_data_only_baseline: Literal[True]
    calibration_slope_min: float
    calibration_slope_max: float
    calibration_intercept_min: float
    calibration_intercept_max: float
    ece_max: float
    min_qualifying_historical_priced_bets: int
    positive_mean_same_book_clv: Literal[True]
    clv_90pct_lower_bound_at_or_above_zero: Literal[True]
    positive_flat_stake_roi: Literal[True]
    max_simulated_bankroll_drawdown_quarter_kelly: float
    consecutive_live_paper_cards: Literal[3]


class MarketFamilyGateSpec(_FrozenModel):
    totals_inside_distance_min_priced_oos: int
    method_min_priced_oos: int
    exact_round_min_priced_oos: int
    exact_round_min_realized_per_active_bucket: int
    failed_family_status: FailedFamilyStatus
    failed_family_may_not_emit_confirmed_value_or_price_target: Literal[True]


class GoLiveGatesSpec(_FrozenModel):
    data: DataGateSpec
    moneyline: MoneylineGateSpec
    market_families: MarketFamilyGateSpec


class EvaluationContract(_FrozenModel):
    """Immutable machine-readable DWCS evaluation contract."""

    schema_version: int
    contract_id: str
    contract_version: str
    description: str
    universe: UniverseSpec
    splits: SplitsSpec
    point_in_time: PointInTimeSpec
    sensitivity: SensitivitySpec
    labels: LabelsSpec
    metrics: MetricsSpec
    confidence_intervals: ConfidenceIntervalsSpec
    recommendation: RecommendationSpec
    price_policy: PricePolicySpec
    go_live_gates: GoLiveGatesSpec
    content_hash: str = Field(description="SHA-256 of canonical contract JSON bytes")

    @model_validator(mode="after")
    def _validate_protocol_invariants(self) -> EvaluationContract:
        if self.splits.development.seasons != REQUIRED_DEVELOPMENT_SEASONS:
            raise ValueError(
                f"development seasons must be {REQUIRED_DEVELOPMENT_SEASONS}, "
                f"got {self.splits.development.seasons}"
            )
        if self.splits.development.locked:
            raise ValueError("development split must not be locked")
        if self.splits.validation.seasons != REQUIRED_VALIDATION_SEASONS:
            raise ValueError(
                f"validation seasons must be {REQUIRED_VALIDATION_SEASONS}, "
                f"got {self.splits.validation.seasons}"
            )
        if self.splits.validation.locked:
            raise ValueError("validation split must not be locked")
        if self.splits.holdout.seasons != REQUIRED_HOLDOUT_SEASONS:
            raise ValueError(
                f"holdout seasons must be {REQUIRED_HOLDOUT_SEASONS}, "
                f"got {self.splits.holdout.seasons}"
            )
        if not self.splits.holdout.locked:
            raise ValueError("holdout 2025 must be locked")
        if self.splits.grouping is not SplitGrouping.EVENT_CARD:
            raise ValueError("splits.grouping must be event_card")
        if self.splits.outer_fold is not OuterFold.ROLLING_ORIGIN_ONE_CARD:
            raise ValueError("splits.outer_fold must be rolling_origin_one_card_at_a_time")
        if self.splits.target_cards != 89:
            raise ValueError("splits.target_cards must be 89")

        if set(self.sensitivity.report_universes) != {
            ReportUniverse.ALL_DWCS,
            ReportUniverse.STANDARD_ONLY,
        }:
            raise ValueError("sensitivity.report_universes must be all_dwcs and standard_only")

        if set(self.metrics.outcome) != set(OutcomeMetric):
            raise ValueError("metrics.outcome must match the frozen outcome metric set")
        if set(self.metrics.betting_priced_only) != set(BettingMetric):
            raise ValueError("metrics.betting_priced_only must match the frozen betting metric set")
        if set(self.metrics.selection) != set(SelectionMetric):
            raise ValueError("metrics.selection must match the frozen selection metric set")
        if (
            self.metrics.priced_rows_require
            is not PricedRowsRequire.TIMESTAMPED_OBSERVED_OR_USER_RECORDED
        ):
            raise ValueError("priced rows must require timestamped observed/user-recorded prices")

        ci = self.confidence_intervals
        if ci.bootstrap_unit is not BootstrapUnit.EVENT_BLOCK:
            raise ValueError("confidence_intervals.bootstrap_unit must be event_block")
        for band_name, band in (
            ("probability_and_ev", ci.probability_and_ev),
            ("betting_metrics", ci.betting_metrics),
        ):
            if band.bootstrap_unit is not BootstrapUnit.EVENT_BLOCK:
                raise ValueError(f"{band_name}.bootstrap_unit must be event_block")
            if band.interval_levels != REQUIRED_INTERVAL_LEVELS:
                raise ValueError(
                    f"{band_name}.interval_levels must be {REQUIRED_INTERVAL_LEVELS}, "
                    f"got {band.interval_levels}"
                )

        rec = self.recommendation
        if set(rec.classifications) != set(RecommendationClass):
            raise ValueError("recommendation.classifications must be the full frozen set")
        if rec.exact_round_actionable_ev_target != rec.strong_value_ev_target:
            raise ValueError("exact-round actionable EV target must equal strong_value_ev_target")
        if abs((1.0 + rec.actionable_ev_target) - 1.05) > 1e-12:
            raise ValueError("actionable_ev_target must be consistent with 1.05 / p50")
        if abs((1.0 + rec.strong_value_ev_target) - 1.10) > 1e-12:
            raise ValueError("strong_value_ev_target must be consistent with 1.10 / p50")

        bound = self.go_live_gates.moneyline.holdout_2025_event_block_90pct_ucb_delta_log_loss
        if bound.comparison is not BoundComparison.LT:
            raise ValueError("holdout UCB comparison must be strict lt")
        if bound.strict_upper_bound != 0.02:
            raise ValueError("holdout UCB strict_upper_bound must be 0.02")
        if self.go_live_gates.moneyline.ece_max != 0.08:
            raise ValueError("moneyline.ece_max must be 0.08")

        return self


def package_contract_resource_path() -> Path:
    """Filesystem path to the packaged authoritative contract resource."""
    root = resources.files("mma_model.evaluation")
    resource = root.joinpath(CONTRACT_FILENAME)
    with resources.as_file(resource) as path:
        return Path(path)


def visible_contract_path(*, root: Path | None = None) -> Path:
    """Plan-visible checkout path (`config/evaluation/dwcs_v1.json`)."""
    base = root if root is not None else get_settings().project_root
    return base / "config" / "evaluation" / CONTRACT_FILENAME


def _is_valid_contract_json_file(path: Path) -> bool:
    """True when path contains a JSON object with contract identity keys."""
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and "schema_version" in payload
        and "contract_id" in payload
        and "contract_version" in payload
    )


def _resolve_symlink_pointer_text(visible: Path) -> Path | None:
    """Resolve a Git ``core.symlinks=false`` plain-text symlink pointer.

    When Git cannot create real symlinks, the plan-visible path may be a text
    file whose contents are the relative link target rather than JSON.
    """
    try:
        text = visible.read_text(encoding="utf-8")
    except OSError:
        return None
    stripped = text.strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None
    # Reject obvious JSON so we do not treat objects/arrays as path text.
    if stripped[0] in "{[":
        return None
    candidate = (visible.parent / stripped).resolve()
    if candidate.is_file() and _is_valid_contract_json_file(candidate):
        return candidate
    return None


def contract_path(*, root: Path | None = None) -> Path:
    """Resolve the contract path for tooling.

    Prefer the plan-visible checkout path when it is valid contract JSON (real
    file or working symlink). If that path exists but is only a plain-text
    symlink pointer (common on Windows / ``core.symlinks=false``), resolve the
    pointer when possible; otherwise use the packaged authoritative resource.
    Never trust ``exists()`` alone.
    """
    visible = visible_contract_path(root=root)
    if visible.is_file() and _is_valid_contract_json_file(visible):
        return visible
    if visible.is_file():
        pointed = _resolve_symlink_pointer_text(visible)
        if pointed is not None:
            return pointed
    return package_contract_resource_path()


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def compute_contract_hash(payload: Mapping[str, Any]) -> str:
    """Return SHA-256 hex digest of canonical JSON (sorted keys, compact)."""
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationContractError(f"Unable to read evaluation contract at {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractValidationError(f"Evaluation contract is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError("Evaluation contract root must be a JSON object")
    return payload


def _read_package_payload() -> dict[str, Any]:
    root = resources.files("mma_model.evaluation")
    resource = root.joinpath(CONTRACT_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, AttributeError) as exc:
        raise EvaluationContractError(
            f"Unable to read packaged evaluation contract resource {CONTRACT_FILENAME}"
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractValidationError("Packaged evaluation contract is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError("Evaluation contract root must be a JSON object")
    return payload


def load_evaluation_contract(
    *,
    path: Path | None = None,
    expected_schema_version: int = EXPECTED_SCHEMA_VERSION,
    expected_contract_version: str = EXPECTED_CONTRACT_VERSION,
    expected_contract_id: str = CONTRACT_ID,
    expected_hash: str | None = None,
    enforce_pinned_digest: bool = True,
    root: Path | None = None,
) -> EvaluationContract:
    """Load and validate the frozen evaluation contract.

    Default / canonical loads always verify ``PINNED_CONTRACT_HASH``.
    ``enforce_pinned_digest=False`` is for deliberate negative tests only.
    """
    if path is not None:
        payload = _read_payload(path)
    elif root is not None:
        payload = _read_payload(contract_path(root=root))
    else:
        # Canonical default: packaged resource (works for wheel and editable installs).
        payload = _read_package_payload()

    content_hash = compute_contract_hash(payload)

    schema_version = payload.get("schema_version")
    if schema_version != expected_schema_version:
        raise ContractSchemaMismatch(
            f"schema_version mismatch: got {schema_version!r}, expected {expected_schema_version!r}"
        )

    contract_version = payload.get("contract_version")
    if contract_version != expected_contract_version:
        raise ContractVersionMismatch(
            "contract_version mismatch: "
            f"got {contract_version!r}, expected {expected_contract_version!r}"
        )

    contract_id = payload.get("contract_id")
    if contract_id != expected_contract_id:
        raise ContractVersionMismatch(
            f"contract_id mismatch: got {contract_id!r}, expected {expected_contract_id!r}"
        )

    if enforce_pinned_digest and content_hash != PINNED_CONTRACT_HASH:
        raise ContractHashMismatch(
            f"content hash mismatch versus pinned digest: got {content_hash}, "
            f"expected {PINNED_CONTRACT_HASH}"
        )

    if expected_hash is not None and content_hash != expected_hash:
        raise ContractHashMismatch(
            f"content hash mismatch: got {content_hash}, expected {expected_hash}"
        )

    try:
        return EvaluationContract.model_validate({**payload, "content_hash": content_hash})
    except ValidationError as exc:
        raise ContractValidationError(str(exc)) from exc


def fair_decimal_odds(p50: float) -> float:
    """Break-even fair decimal odds from median calibrated probability."""
    if p50 <= 0.0 or p50 > 1.0:
        raise ValueError("p50 must be in (0, 1]")
    return 1.0 / p50


def actionable_decimal_price(p50: float, p25: float, *, ev_target: float = 0.05) -> float:
    """Worse (higher) of conservative p25 break-even and target-EV price."""
    if p50 <= 0.0 or p50 > 1.0 or p25 <= 0.0 or p25 > 1.0:
        raise ValueError("p50 and p25 must be in (0, 1]")
    if ev_target < 0.0:
        raise ValueError("ev_target must be non-negative")
    return max(1.0 / p25, (1.0 + ev_target) / p50)


def strong_value_decimal_price(p50: float, p25: float, *, ev_target: float = 0.10) -> float:
    """Worse (higher) of conservative p25 break-even and strong-value EV price."""
    return actionable_decimal_price(p50, p25, ev_target=ev_target)


def mutable_fact_allowed_at_cutoff(
    *,
    effective_at: datetime,
    observed_at: datetime,
    cutoff: datetime,
    require_effective_strictly_before: bool = True,
    require_observed_at_or_before: bool = True,
) -> bool:
    """Point-in-time gate for mutable facts relative to a card cutoff."""
    effective_ok = (not require_effective_strictly_before) or (effective_at < cutoff)
    observed_ok = (not require_observed_at_or_before) or (observed_at <= cutoff)
    return effective_ok and observed_ok


def holdout_ucb_delta_log_loss_passes(ucb: float, contract: EvaluationContract) -> bool:
    """Apply the strict holdout UCB gate (ucb < +0.02)."""
    gate = contract.go_live_gates.moneyline.holdout_2025_event_block_90pct_ucb_delta_log_loss
    return gate.passes(ucb)
