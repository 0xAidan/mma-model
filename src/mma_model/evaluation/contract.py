"""Strict loader for the frozen DWCS evaluation contract (DWCS-001).

Normal model runs must load this contract read-only. Version, schema, and hash
mismatches hard-fail so later evaluators cannot silently drift.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mma_model.config import get_settings

CONTRACT_FILENAME = "dwcs_v1.json"
CONTRACT_ID = "dwcs_evaluation"
EXPECTED_SCHEMA_VERSION = 1
EXPECTED_CONTRACT_VERSION = "1.0.0"


class EvaluationContractError(Exception):
    """Base error for evaluation-contract failures."""


class ContractValidationError(EvaluationContractError):
    """Contract JSON failed schema validation."""


class ContractVersionMismatch(EvaluationContractError):
    """Contract version did not match the expected frozen version."""


class ContractSchemaMismatch(EvaluationContractError):
    """Contract schema_version did not match the expected schema."""


class ContractHashMismatch(EvaluationContractError):
    """Contract content hash did not match the expected digest."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeasonBounds(_FrozenModel):
    first: int
    last: int


class UniverseCounts(_FrozenModel):
    cards: int
    bouts: int


class BrazilUniverse(UniverseCounts):
    series_variant: str
    year: int


class UniverseSpec(_FrozenModel):
    series: str
    seasons: SeasonBounds
    all_dwcs: UniverseCounts
    standard_only: UniverseCounts
    brazil: BrazilUniverse


class SplitWindow(_FrozenModel):
    seasons: list[int]
    locked: bool


class SplitsSpec(_FrozenModel):
    grouping: str
    outer_fold: str
    target_cards: int
    development: SplitWindow
    validation: SplitWindow
    holdout: SplitWindow


class MutableFactRules(_FrozenModel):
    effective_at_strictly_before_cutoff: bool
    observed_at_at_or_before_cutoff: bool


class PointInTimeSpec(_FrozenModel):
    prediction_cutoff_minutes_before_scheduled_start: int
    identical_cutoff_per_card: bool
    mutable_fact_rules: MutableFactRules
    forbid_same_card_results: bool
    forbid_later_corrections_before_adjudication: bool
    forbid_current_fighter_aggregates: bool
    forbid_post_cutoff_odds_snapshots: bool
    proxy_scheduled_start_excludes_exact_closing_line_analysis: bool


class SensitivitySpec(_FrozenModel):
    report_universes: list[str]


class BaselineSpec(_FrozenModel):
    M0: list[str]
    M1: str


class LabelsSpec(_FrozenModel):
    terminal_atoms: list[str]
    settlement_only_excluded_from_win_fitting: list[str]
    half_round_intervals_for_three_round_bout: int
    baselines: BaselineSpec


class MetricsSpec(_FrozenModel):
    outcome: list[str]
    betting_priced_only: list[str]
    selection: list[str]
    priced_rows_require: str
    price_target_rows_never_receive_synthetic_betting_performance: bool


class ConfidenceIntervalsSpec(_FrozenModel):
    bootstrap_refits: int
    bootstrap_unit: str
    levels: list[float]


class RecommendationSpec(_FrozenModel):
    max_confirmed_value_markets_per_matchup: int
    rank_confirmed_by: str
    emit_no_bet_when_gates_fail: bool
    fair_decimal_odds: str
    actionable_ev_target: float
    strong_value_ev_target: float
    actionable_decimal_price: str
    strong_value_decimal_price: str
    confirmed_value_min_prob_ev_positive: float
    exact_round_actionable_ev_target: float
    exact_round_min_prob_ev_positive: float
    classifications: list[str]
    unpriced_target_is_not_best_available_market: bool
    american_odds_renderer_expresses_or_better: bool


class PricePolicySpec(_FrozenModel):
    bookmaker_odds_optional_enrichment: bool
    missing_bet365_does_not_block_core_guidance: bool
    sportsbook_agnostic_fair_actionable_strong_value_required: bool
    exact_ev_roi_clv_require_timestamped_price: bool
    price_target_only_rows_never_receive_synthetic_betting_performance: bool


class DataGateSpec(_FrozenModel):
    target_cards: int
    target_bouts: int
    min_cross_source_reconciliation: float
    min_result_agreement: float
    max_unresolved_identity_conflicts: int
    max_leakage_invariant_failures: int


class MoneylineGateSpec(_FrozenModel):
    max_unexplained_feature_exclusion_rate_decisive: float
    holdout_2025_delta_log_loss_vs_m1_non_positive: bool
    holdout_2025_event_block_90pct_ucb_delta_log_loss_max: float
    odds_backed_2020_plus_max_worse_than_no_vig_log_loss: float
    odds_backed_2020_plus_positive_skill_vs_data_only_baseline: bool
    calibration_slope_min: float
    calibration_slope_max: float
    calibration_intercept_min: float
    calibration_intercept_max: float
    ece_max: float
    min_qualifying_historical_priced_bets: int
    positive_mean_same_book_clv: bool
    clv_90pct_lower_bound_at_or_above_zero: bool
    positive_flat_stake_roi: bool
    max_simulated_bankroll_drawdown_quarter_kelly: float
    consecutive_live_paper_cards: int


class MarketFamilyGateSpec(_FrozenModel):
    totals_inside_distance_min_priced_oos: int
    method_min_priced_oos: int
    exact_round_min_priced_oos: int
    exact_round_min_realized_per_active_bucket: int
    failed_family_status: str
    failed_family_may_not_emit_confirmed_value_or_price_target: bool


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


def contract_path(*, root: Path | None = None) -> Path:
    base = root if root is not None else get_settings().project_root
    return base / "config" / "evaluation" / CONTRACT_FILENAME


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


def load_evaluation_contract(
    *,
    path: Path | None = None,
    expected_schema_version: int = EXPECTED_SCHEMA_VERSION,
    expected_contract_version: str = EXPECTED_CONTRACT_VERSION,
    expected_contract_id: str = CONTRACT_ID,
    expected_hash: str | None = None,
    root: Path | None = None,
) -> EvaluationContract:
    """Load and validate the frozen evaluation contract.

    Hard-fails on schema validation errors and on schema/version/id/hash mismatch.
    Returns a frozen pydantic model; callers cannot mutate fields in place.
    """
    resolved = path if path is not None else contract_path(root=root)
    payload = _read_payload(resolved)
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
    if require_effective_strictly_before and not (effective_at < cutoff):
        return False
    if require_observed_at_or_before and not (observed_at <= cutoff):
        return False
    return True
