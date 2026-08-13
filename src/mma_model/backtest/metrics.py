"""Walk-forward outcome, selection, and priced-only betting metrics (DWCS-306).

ROI / CLV / Kelly / drawdown / losing-run use DWCS-204 value functions and
only valid priced observations. Threshold-only rows stay a separate
denominator. Event-block bootstrap never IID-resamples fights.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Never

import numpy as np

from mma_model.backtest.gates import PricedScopeError
from mma_model.domain.markets import MarketFamily
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import REQUIRED_INTERVAL_LEVELS, EvaluationContract
from mma_model.markets.settlement import SettlementResult
from mma_model.modeling.metrics import (
    binary_brier,
    binary_calibration_report,
    binary_nll,
    joint_terminal_nll,
)
from mma_model.value.ev import expected_value, flat_unit_profit
from mma_model.value.kelly import quarter_kelly_fraction, quarter_kelly_fraction_with_void

DEFAULT_BACKTEST_BOOTSTRAP_SEED: Final = 306001
DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES: Final = 200
DEFAULT_MAX_ATTEMPT_MULTIPLIER: Final = 50
INTERVAL_LEVELS: Final = REQUIRED_INTERVAL_LEVELS
FLAT_STAKE_UNITS: Final = 1.0
INITIAL_KELLY_BANKROLL: Final = 1.0


class MetricScope(StrEnum):
    ALL_ATTEMPTED = "all_attempted"
    ALL_PREDICTIONS = "all_predictions"
    PRICED_ONLY = "priced_only"
    THRESHOLD_ONLY = "threshold_only"
    QUALIFYING_PRICED = "qualifying_priced"


class UniverseKey(StrEnum):
    ALL_DWCS = "all_dwcs"
    STANDARD_ONLY = "standard_only"
    BRAZIL = "brazil"


class MetricsError(ValueError):
    """Walk-forward metrics cannot be computed from the supplied rows."""


@dataclass(frozen=True)
class CountedMetric:
    """Every reported metric carries numerator, denominator, and scope."""

    name: str
    value: float | int | None
    numerator: float | int | None
    denominator: int
    scope: str
    unit: str
    definition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "denominator": self.denominator,
            "name": self.name,
            "numerator": self.numerator,
            "scope": self.scope,
            "unit": self.unit,
            "value": self.value,
        }


@dataclass(frozen=True)
class PricedBet:
    """One settled priced observation used for betting metrics."""

    event_id: str
    bout_id: str
    season: int
    series_variant: str
    market_family: str
    outcome_key: str
    source_kind: str
    provider: str | None
    bookmaker_key: str | None
    model_prob: float
    offered_decimal: float
    settlement: SettlementResult
    is_proxy_timestamp: bool
    is_pre_policy_candidate: bool
    probability_clv: float | None
    closing_ev: float | None
    expected_value: float
    p_void: float | None = None

    @property
    def block_id(self) -> str:
        return self.event_id


@dataclass(frozen=True)
class OutcomeObservation:
    event_id: str
    bout_id: str
    season: int
    series_variant: str
    y: int | None
    p: float | None
    joint: Mapping[str, float] | None
    observed_atom: str | None
    baseline_fifty: float | None
    baseline_rating: float | None
    baseline_no_vig: float | None
    baseline_m1: float | None


@dataclass(frozen=True)
class AttemptRow:
    event_id: str
    bout_id: str
    season: int
    series_variant: str
    status: str
    exclusion_reason: str | None
    predicted: bool
    abstained: bool
    unavailable: bool
    excluded: bool
    locked_not_accessed: bool
    priced: bool
    threshold_only: bool
    pre_policy_candidate: bool
    markets_available: tuple[str, ...]
    markets_unavailable: tuple[str, ...]
    n_priced_selections: int
    n_threshold_selections: int
    priced_market_families: tuple[str, ...]
    threshold_market_families: tuple[str, ...]


@dataclass(frozen=True)
class BettingTotals:
    qualifying_bets: CountedMetric
    turnover: CountedMetric
    flat_1_unit_roi: CountedMetric
    quarter_kelly_roi: CountedMetric
    mean_probability_clv: CountedMetric
    mean_closing_ev: CountedMetric
    maximum_drawdown: CountedMetric
    longest_losing_run: CountedMetric
    n_priced: CountedMetric
    n_threshold_only: CountedMetric
    n_proxy_excluded_from_exact_clv: CountedMetric

    def to_dict(self) -> dict[str, Any]:
        return {
            "flat_1_unit_roi": self.flat_1_unit_roi.to_dict(),
            "longest_losing_run": self.longest_losing_run.to_dict(),
            "maximum_drawdown": self.maximum_drawdown.to_dict(),
            "mean_closing_ev": self.mean_closing_ev.to_dict(),
            "mean_probability_clv": self.mean_probability_clv.to_dict(),
            "n_priced": self.n_priced.to_dict(),
            "n_proxy_excluded_from_exact_clv": self.n_proxy_excluded_from_exact_clv.to_dict(),
            "n_threshold_only": self.n_threshold_only.to_dict(),
            "qualifying_bets": self.qualifying_bets.to_dict(),
            "quarter_kelly_roi": self.quarter_kelly_roi.to_dict(),
            "turnover": self.turnover.to_dict(),
        }


@dataclass(frozen=True)
class IntervalEstimate:
    level: float
    lower: float | None
    upper: float | None
    n_replicates: int
    n_rejected: int
    n_missing: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "lower": self.lower,
            "n_missing": self.n_missing,
            "n_rejected": self.n_rejected,
            "n_replicates": self.n_replicates,
            "upper": self.upper,
        }


@dataclass(frozen=True)
class MarketOutcomeRow:
    """One settled market selection used for per-market log loss / Brier."""

    event_id: str
    bout_id: str
    season: int
    series_variant: str
    market_family: str
    outcome_key: str
    line_point: float | None
    p50: float
    settlement: SettlementResult


def _ratio(numerator: float, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _counted(
    name: str,
    value: float | int | None,
    *,
    numerator: float | int | None,
    denominator: int,
    scope: MetricScope,
    unit: str,
    definition: str,
) -> CountedMetric:
    return CountedMetric(
        name=name,
        value=value,
        numerator=numerator,
        denominator=denominator,
        scope=scope.value,
        unit=unit,
        definition=definition,
    )


def _in_universe(variant: str, universe: UniverseKey) -> bool:
    if universe is UniverseKey.ALL_DWCS:
        return True
    if universe is UniverseKey.STANDARD_ONLY:
        return variant == SeriesVariant.STANDARD.value
    if universe is UniverseKey.BRAZIL:
        return variant == SeriesVariant.BRAZIL.value
    never_universe: Never = universe
    raise MetricsError(f"unhandled universe: {never_universe!r}")


def filter_attempts(
    rows: Sequence[AttemptRow],
    universe: UniverseKey,
) -> tuple[AttemptRow, ...]:
    return tuple(row for row in rows if _in_universe(row.series_variant, universe))


def filter_bets(bets: Sequence[PricedBet], universe: UniverseKey) -> tuple[PricedBet, ...]:
    return tuple(bet for bet in bets if _in_universe(bet.series_variant, universe))


def filter_outcomes(
    rows: Sequence[OutcomeObservation],
    universe: UniverseKey,
) -> tuple[OutcomeObservation, ...]:
    return tuple(row for row in rows if _in_universe(row.series_variant, universe))


def selection_metrics(rows: Sequence[AttemptRow]) -> dict[str, Any]:
    n = len(rows)
    n_pred = sum(1 for row in rows if row.predicted)
    n_abs = sum(1 for row in rows if row.abstained)
    n_unav = sum(1 for row in rows if row.unavailable)
    n_excl = sum(1 for row in rows if row.excluded)
    n_locked = sum(1 for row in rows if row.locked_not_accessed)
    n_priced = sum(1 for row in rows if row.priced)
    n_threshold = sum(1 for row in rows if row.threshold_only)
    n_cand = sum(1 for row in rows if row.pre_policy_candidate)
    reasons = Counter(row.exclusion_reason for row in rows if row.exclusion_reason)
    markets_ok = Counter(
        family for row in rows for family in row.markets_available
    )
    markets_no = Counter(
        family for row in rows for family in row.markets_unavailable
    )
    return {
        "abstained": _counted(
            "abstained",
            n_abs,
            numerator=n_abs,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="Bouts where the model ran and abstained",
        ).to_dict(),
        "attempted": _counted(
            "attempted",
            n,
            numerator=n,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="Every bout in the frozen universe is attempted exactly once",
        ).to_dict(),
        "excluded": _counted(
            "excluded",
            n_excl,
            numerator=n_excl,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="Bouts excluded with a typed reason",
        ).to_dict(),
        "exclusion_reasons": dict(sorted(reasons.items())),
        "locked_not_accessed": _counted(
            "locked_not_accessed",
            n_locked,
            numerator=n_locked,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="2025 holdout bouts counted but not scored without --sealed-holdout",
        ).to_dict(),
        "market_availability": {
            "available": dict(sorted(markets_ok.items())),
            "unavailable": dict(sorted(markets_no.items())),
        },
        "pre_policy_candidates": _counted(
            "pre_policy_candidates",
            n_cand,
            numerator=n_cand,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition=(
                "Preliminary actionable candidates using frozen contract thresholds; "
                "not DWCS-307 recommendations"
            ),
        ).to_dict(),
        "predicted": _counted(
            "predicted",
            n_pred,
            numerator=n_pred,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="Bouts with a coherent calibrated prediction",
        ).to_dict(),
        "priced": _counted(
            "priced",
            n_priced,
            numerator=n_priced,
            denominator=n,
            scope=MetricScope.PRICED_ONLY,
            unit="count",
            definition="Bouts with at least one timestamp-valid offered price",
        ).to_dict(),
        "threshold_only": _counted(
            "threshold_only",
            n_threshold,
            numerator=n_threshold,
            denominator=n,
            scope=MetricScope.THRESHOLD_ONLY,
            unit="count",
            definition="Predicted bouts without a valid priced observation",
        ).to_dict(),
        "unavailable": _counted(
            "unavailable",
            n_unav,
            numerator=n_unav,
            denominator=n,
            scope=MetricScope.ALL_ATTEMPTED,
            unit="count",
            definition="Bouts that could not be scored (features/model missing)",
        ).to_dict(),
    }


def _binary_pairs(
    rows: Sequence[OutcomeObservation],
) -> tuple[list[int], list[float], list[str]]:
    y: list[int] = []
    p: list[float] = []
    events: list[str] = []
    for row in rows:
        if row.y is None or row.p is None:
            continue
        y.append(int(row.y))
        p.append(float(row.p))
        events.append(row.event_id)
    return y, p, events


def _skill_block(
    y: Sequence[int],
    model_p: Sequence[float],
    baseline_p: Sequence[float | None],
    *,
    name: str,
) -> dict[str, Any]:
    paired_y: list[int] = []
    paired_model: list[float] = []
    paired_base: list[float] = []
    n_missing = 0
    for label, mp, bp in zip(y, model_p, baseline_p, strict=True):
        if bp is None:
            n_missing += 1
            continue
        paired_y.append(int(label))
        paired_model.append(float(mp))
        paired_base.append(float(bp))
    denominator = len(paired_y)
    if denominator == 0:
        return {
            "baseline": name,
            "definition": f"Skill vs {name}: baseline_log_loss - model_log_loss",
            "denominator": 0,
            "n_missing": n_missing,
            "numerator": None,
            "scope": MetricScope.ALL_PREDICTIONS.value,
            "unit": "log_loss_difference",
            "value": None,
        }
    model_ll = binary_nll(paired_y, paired_model)
    base_ll = binary_nll(paired_y, paired_base)
    skill = base_ll - model_ll
    return {
        "baseline": name,
        "baseline_log_loss": base_ll,
        "definition": f"Skill vs {name}: baseline_log_loss - model_log_loss (positive=better)",
        "denominator": denominator,
        "model_log_loss": model_ll,
        "n_missing": n_missing,
        "numerator": skill * denominator,
        "scope": MetricScope.ALL_PREDICTIONS.value,
        "unit": "log_loss_difference",
        "value": skill,
    }


def per_market_outcome_metrics(rows: Sequence[MarketOutcomeRow]) -> dict[str, Any]:
    """Log loss / Brier by market on win/loss settlements only (pushes/voids counted out)."""
    by_family: dict[str, list[MarketOutcomeRow]] = {}
    for row in rows:
        by_family.setdefault(row.market_family, []).append(row)
    payload: dict[str, Any] = {}
    families = [item.value for item in MarketFamily]
    for family in families:
        family_rows = by_family.get(family, [])
        scored: list[tuple[int, float]] = []
        n_push = 0
        n_void = 0
        n_unresolved = 0
        for row in family_rows:
            if row.settlement is SettlementResult.WIN:
                scored.append((1, float(row.p50)))
            elif row.settlement is SettlementResult.LOSS:
                scored.append((0, float(row.p50)))
            elif row.settlement is SettlementResult.PUSH:
                n_push += 1
            elif row.settlement is SettlementResult.VOID:
                n_void += 1
            elif row.settlement is SettlementResult.UNRESOLVED:
                n_unresolved += 1
            else:
                never_result: Never = row.settlement
                raise MetricsError(f"unhandled settlement: {never_result!r}")
        n_scored = len(scored)
        if n_scored == 0:
            log_loss = None
            brier = None
            calib = None
        else:
            y = [item[0] for item in scored]
            p = [item[1] for item in scored]
            scored_ids = {
                SettlementResult.WIN,
                SettlementResult.LOSS,
            }
            events = [
                row.event_id for row in family_rows if row.settlement in scored_ids
            ]
            log_loss = binary_nll(y, p)
            brier = binary_brier(y, p)
            calib = binary_calibration_report(y, p, event_ids=events) if n_scored >= 1 else None
        payload[family] = {
            "brier": _counted(
                f"{family}_brier",
                brier,
                numerator=None if brier is None else brier * n_scored,
                denominator=n_scored,
                scope=MetricScope.ALL_PREDICTIONS,
                unit="brier",
                definition=f"Brier on {family} win/loss settlements; pushes/voids excluded",
            ).to_dict(),
            "calibration": None if calib is None else calib.to_dict(),
            "log_loss": _counted(
                f"{family}_log_loss",
                log_loss,
                numerator=None if log_loss is None else log_loss * n_scored,
                denominator=n_scored,
                scope=MetricScope.ALL_PREDICTIONS,
                unit="nll",
                definition=f"Log loss on {family} win/loss settlements; pushes/voids excluded",
            ).to_dict(),
            "n_push": n_push,
            "n_scored_win_loss": n_scored,
            "n_unresolved": n_unresolved,
            "n_void": n_void,
        }
    return payload


def outcome_metrics(
    rows: Sequence[OutcomeObservation],
    *,
    market_rows: Sequence[MarketOutcomeRow] = (),
) -> dict[str, Any]:
    y, p, events = _binary_pairs(rows)
    n_pred = len(rows)
    n_scored = len(y)
    if n_scored == 0:
        calib = None
        log_loss = None
        brier = None
        accuracy = None
    else:
        calib = binary_calibration_report(y, p, event_ids=events)
        log_loss = calib.log_loss
        brier = calib.brier
        hat = [1 if value >= 0.5 else 0 for value in p]
        n_correct = sum(1 for a, b in zip(y, hat, strict=True) if a == b)
        accuracy = _ratio(n_correct, n_scored)

    joint_rows = [
        row for row in rows if row.joint is not None and row.observed_atom is not None
    ]
    if joint_rows:
        joint_ll = joint_terminal_nll(
            [dict(row.joint or {}) for row in joint_rows],
            [str(row.observed_atom) for row in joint_rows],
        )
        joint_den = len(joint_rows)
    else:
        joint_ll = None
        joint_den = 0

    skill: dict[str, Any] = {}
    if n_scored:
        scored = [row for row in rows if row.y is not None and row.p is not None]
        skill["fifty_fifty"] = _skill_block(
            y, p, [row.baseline_fifty for row in scored], name="fifty_fifty"
        )
        skill["sequential_rating"] = _skill_block(
            y, p, [row.baseline_rating for row in scored], name="sequential_rating"
        )
        skill["no_vig_market"] = _skill_block(
            y, p, [row.baseline_no_vig for row in scored], name="no_vig_market"
        )
        skill["m1"] = _skill_block(
            y, p, [row.baseline_m1 for row in scored], name="m1"
        )

    return {
        "accuracy_descriptive_only": _counted(
            "accuracy_descriptive_only",
            accuracy,
            numerator=None if accuracy is None else accuracy * n_scored,
            denominator=n_scored,
            scope=MetricScope.ALL_PREDICTIONS,
            unit="proportion",
            definition="Descriptive accuracy only; not a ranking or go-live gate",
        ).to_dict(),
        "brier": _counted(
            "brier",
            brier,
            numerator=None if brier is None else brier * n_scored,
            denominator=n_scored,
            scope=MetricScope.ALL_PREDICTIONS,
            unit="brier",
            definition="Mean squared error of moneyline P(A wins) on decisive bouts",
        ).to_dict(),
        "calibration": None if calib is None else calib.to_dict(),
        "joint_log_loss": _counted(
            "joint_log_loss",
            joint_ll,
            numerator=None if joint_ll is None else joint_ll * joint_den,
            denominator=joint_den,
            scope=MetricScope.ALL_PREDICTIONS,
            unit="nll",
            definition="Mean terminal-atom NLL when a coherent joint distribution exists",
        ).to_dict(),
        "market_log_loss": _counted(
            "market_log_loss",
            log_loss,
            numerator=None if log_loss is None else log_loss * n_scored,
            denominator=n_scored,
            scope=MetricScope.ALL_PREDICTIONS,
            unit="nll",
            definition=(
                "Binary moneyline log loss on decisive predicted bouts using "
                "pA/(pA+pB) when a joint distribution is present"
            ),
        ).to_dict(),
        "n_predicted": n_pred,
        "n_scored_decisive": n_scored,
        "per_market": per_market_outcome_metrics(market_rows),
        "skill_vs_baselines": skill,
    }


def _settled_profit(bet: PricedBet) -> float | None:
    if bet.settlement is SettlementResult.UNRESOLVED:
        return None
    return flat_unit_profit(
        settlement=bet.settlement,
        offered_decimal=bet.offered_decimal,
    )


def _qualifying(bets: Sequence[PricedBet]) -> tuple[PricedBet, ...]:
    return tuple(bet for bet in bets if bet.is_pre_policy_candidate)


def longest_losing_run(bets: Sequence[PricedBet]) -> int:
    """Consecutive losses on the qualifying path.

    UNRESOLVED rows are omitted (neither extend nor reset). PUSH and VOID
    reset the run. WIN resets. Only LOSS extends the streak.
    """
    run = 0
    longest = 0
    for bet in bets:
        if bet.settlement is SettlementResult.UNRESOLVED:
            continue
        if bet.settlement is SettlementResult.LOSS:
            run += 1
            if run > longest:
                longest = run
            continue
        run = 0
    return longest


def flat_equity_path(bets: Sequence[PricedBet]) -> tuple[tuple[float, ...], float]:
    """Cumulative P&L for flat 1-unit stakes. Returns (equity, max_drawdown_units)."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    path: list[float] = []
    for bet in bets:
        profit = _settled_profit(bet)
        if profit is None:
            path.append(equity)
            continue
        equity += profit
        if equity > peak:
            peak = equity
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        path.append(equity)
    return tuple(path), max_dd


def _kelly_fraction_for_bet(bet: PricedBet) -> float:
    if bet.p_void is None or bet.p_void == 0.0:
        return quarter_kelly_fraction(bet.model_prob, bet.offered_decimal)
    return quarter_kelly_fraction_with_void(
        bet.model_prob, bet.offered_decimal, p_void=bet.p_void
    )


def kelly_bankroll_path(bets: Sequence[PricedBet]) -> tuple[tuple[float, ...], float]:
    """Capped quarter-Kelly starting at 1.0.

    All bets on one card are staked from that card's starting bankroll, then
    the aggregate card P&L is applied. Fight 2 does not compound fight 1 on
    the same card. Drawdown is recorded at event boundaries. UNRESOLVED rows
    post no stake. PUSH/VOID return the stake (P&L 0) and still turn over.
    """
    bankroll = INITIAL_KELLY_BANKROLL
    peak = INITIAL_KELLY_BANKROLL
    max_dd = 0.0
    path: list[float] = [bankroll]
    by_event: dict[str, list[PricedBet]] = {}
    order: list[str] = []
    for bet in bets:
        if bet.event_id not in by_event:
            order.append(bet.event_id)
            by_event[bet.event_id] = []
        by_event[bet.event_id].append(bet)
    for event_id in order:
        card_start = bankroll
        card_pnl = 0.0
        for bet in by_event[event_id]:
            if bet.settlement is SettlementResult.UNRESOLVED:
                continue
            fraction = _kelly_fraction_for_bet(bet)
            stake = fraction * card_start
            if bet.settlement is SettlementResult.WIN:
                card_pnl += stake * (bet.offered_decimal - 1.0)
            elif bet.settlement is SettlementResult.LOSS:
                card_pnl -= stake
            elif bet.settlement is SettlementResult.PUSH or bet.settlement is SettlementResult.VOID:
                pass
            else:
                never_result: Never = bet.settlement
                raise MetricsError(f"unhandled settlement: {never_result!r}")
        bankroll = card_start + card_pnl
        path.append(bankroll)
        if bankroll > peak:
            peak = bankroll
        if peak > 0.0:
            drawdown = (peak - bankroll) / peak
            if drawdown > max_dd:
                max_dd = drawdown
    return tuple(path), max_dd


def betting_metrics(bets: Sequence[PricedBet], *, n_threshold_only: int) -> BettingTotals:
    if n_threshold_only < 0:
        raise PricedScopeError("threshold-only denominator cannot be negative")
    qualifying = _qualifying(bets)
    n_qual = len(qualifying)
    profits: list[float] = []
    for bet in qualifying:
        profit = _settled_profit(bet)
        if profit is None:
            continue
        profits.append(profit)
    n_settled = len(profits)
    n_unresolved = sum(1 for bet in qualifying if bet.settlement is SettlementResult.UNRESOLVED)
    n_push_void = sum(
        1
        for bet in qualifying
        if bet.settlement is SettlementResult.PUSH or bet.settlement is SettlementResult.VOID
    )
    turnover = float(n_settled) * FLAT_STAKE_UNITS
    flat_roi = _ratio(sum(profits), n_settled) if n_settled else None
    _path, flat_dd = flat_equity_path(qualifying)
    kelly_path, kelly_dd = kelly_bankroll_path(qualifying)
    kelly_final = kelly_path[-1] if kelly_path else INITIAL_KELLY_BANKROLL
    kelly_roi = kelly_final - INITIAL_KELLY_BANKROLL
    clv_vals = [
        bet.probability_clv
        for bet in qualifying
        if bet.probability_clv is not None and not bet.is_proxy_timestamp
    ]
    close_ev_vals = [
        bet.closing_ev
        for bet in qualifying
        if bet.closing_ev is not None and not bet.is_proxy_timestamp
    ]
    n_proxy = sum(1 for bet in qualifying if bet.is_proxy_timestamp)
    losing = longest_losing_run(qualifying)
    n_priced_sel = len(bets)
    n_threshold_sel = n_threshold_only
    selection_den = n_priced_sel + n_threshold_sel
    return BettingTotals(
        qualifying_bets=_counted(
            "qualifying_bets",
            n_qual,
            numerator=n_qual,
            denominator=len(bets),
            scope=MetricScope.QUALIFYING_PRICED,
            unit="count",
            definition="Priced pre_policy_candidate rows using frozen contract thresholds",
        ),
        turnover=_counted(
            "turnover",
            turnover,
            numerator=turnover,
            denominator=n_settled,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="flat_stake_units",
            definition=(
                "Sum of flat 1-unit stakes on settled qualifying bets. PUSH/VOID "
                f"count in turnover with profit 0 (n_push_void={n_push_void}). "
                f"UNRESOLVED rows are excluded (n_unresolved={n_unresolved})."
            ),
        ),
        flat_1_unit_roi=_counted(
            "flat_1_unit_roi",
            flat_roi,
            numerator=sum(profits) if profits else None,
            denominator=n_settled,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="unit_profit_per_unit_stake",
            definition=(
                "Mean flat_unit_profit over settled qualifying bets (push/void=0). "
                "Unresolved rows are excluded from the denominator."
            ),
        ),
        quarter_kelly_roi=_counted(
            "quarter_kelly_roi_capped_at_1_percent_bankroll",
            kelly_roi if n_settled else None,
            numerator=kelly_roi if n_settled else None,
            denominator=1,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="bankroll_return",
            definition=(
                "Card-level capped quarter-Kelly bankroll return (final-1.0) from "
                "starting bankroll 1.0. Same-card stakes share the card-start "
                "bankroll. Unresolved rows post no stake and are excluded."
            ),
        ),
        mean_probability_clv=_counted(
            "clv",
            _ratio(sum(clv_vals), len(clv_vals)) if clv_vals else None,
            numerator=sum(clv_vals) if clv_vals else None,
            denominator=len(clv_vals),
            scope=MetricScope.QUALIFYING_PRICED,
            unit="probability_points",
            definition="Mean same-selection probability CLV; proxy timestamps excluded",
        ),
        mean_closing_ev=_counted(
            "closing_ev",
            _ratio(sum(close_ev_vals), len(close_ev_vals)) if close_ev_vals else None,
            numerator=sum(close_ev_vals) if close_ev_vals else None,
            denominator=len(close_ev_vals),
            scope=MetricScope.QUALIFYING_PRICED,
            unit="ev_per_unit_stake",
            definition="Mean same-selection closing EV; proxy timestamps excluded",
        ),
        maximum_drawdown=_counted(
            "maximum_drawdown",
            kelly_dd if n_qual else None,
            numerator=kelly_dd if n_qual else None,
            denominator=n_qual,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="peak_bankroll_fraction",
            definition="Max peak-to-trough fraction on the capped quarter-Kelly bankroll path",
        ),
        longest_losing_run=_counted(
            "longest_losing_run",
            losing,
            numerator=losing,
            denominator=n_qual,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="consecutive_losses",
            definition=(
                "Longest consecutive LOSS streak. PUSH/VOID reset the run. "
                "UNRESOLVED rows are omitted (neither extend nor reset)."
            ),
        ),
        n_priced=_counted(
            "n_priced",
            n_priced_sel,
            numerator=n_priced_sel,
            denominator=selection_den,
            scope=MetricScope.PRICED_ONLY,
            unit="count",
            definition=(
                "Priced selection rows in a shared selection denominator with "
                "threshold-only selection rows (bout-level counts stay in selection metrics)"
            ),
        ),
        n_threshold_only=_counted(
            "n_threshold_only",
            n_threshold_sel,
            numerator=n_threshold_sel,
            denominator=selection_den,
            scope=MetricScope.THRESHOLD_ONLY,
            unit="count",
            definition="Threshold-only selection rows; no synthetic EV/ROI/CLV/profit/stake",
        ),
        n_proxy_excluded_from_exact_clv=_counted(
            "n_proxy_excluded_from_exact_clv",
            n_proxy,
            numerator=n_proxy,
            denominator=n_qual,
            scope=MetricScope.QUALIFYING_PRICED,
            unit="count",
            definition="Proxy scheduled-start timestamps excluded from exact CLV",
        ),
    )


def _percentile_interval(
    samples: Sequence[float],
    level: float,
) -> tuple[float | None, float | None]:
    if not samples:
        return None, None
    arr = np.asarray(list(samples), dtype=np.float64)
    alpha = (1.0 - level) / 2.0
    lower = float(np.quantile(arr, alpha, method="linear"))
    upper = float(np.quantile(arr, 1.0 - alpha, method="linear"))
    return lower, upper


def event_blocks(bets: Sequence[PricedBet]) -> dict[str, tuple[PricedBet, ...]]:
    grouped: dict[str, list[PricedBet]] = {}
    for bet in bets:
        grouped.setdefault(bet.event_id, []).append(bet)
    return {key: tuple(value) for key, value in grouped.items()}


def resample_event_blocks(
    blocks: Mapping[str, Sequence[PricedBet]],
    *,
    rng: np.random.Generator,
) -> tuple[PricedBet, ...]:
    """Draw complete event blocks with replacement; fights stay together."""
    event_ids = tuple(sorted(blocks))
    if not event_ids:
        return ()
    drawn = rng.choice(np.asarray(event_ids), size=len(event_ids), replace=True)
    out: list[PricedBet] = []
    for event_id in drawn:
        out.extend(blocks[str(event_id)])
    return tuple(out)


def _validated_interval_levels(contract: EvaluationContract | None) -> tuple[float, ...]:
    levels = (
        INTERVAL_LEVELS if contract is None else contract_interval_levels(contract)
    )
    normalized = tuple(float(level) for level in levels)
    expected = tuple(float(level) for level in REQUIRED_INTERVAL_LEVELS)
    if normalized != expected:
        raise MetricsError(
            f"interval_levels must be {expected}, got {normalized}"
        )
    return normalized


def bootstrap_betting_intervals(
    bets: Sequence[PricedBet],
    *,
    seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    n_threshold_only: int = 0,
    contract: EvaluationContract | None = None,
) -> dict[str, Any]:
    """Event-block bootstrap of priced betting metrics at contract interval levels.

    Missing CLV / closing EV is not coerced to 0. Each metric keeps its own
    n_replicates / n_missing. Empty-bet ROI replicates are rejected and counted.
    """
    levels = _validated_interval_levels(contract)
    blocks = event_blocks(bets)
    event_ids = tuple(sorted(blocks))
    n_events = len(event_ids)
    rng = np.random.default_rng(int(seed))
    max_attempts = max(int(replicates) * DEFAULT_MAX_ATTEMPT_MULTIPLIER, int(replicates))
    roi_samples: list[float] = []
    clv_samples: list[float] = []
    close_ev_samples: list[float] = []
    dd_samples: list[float] = []
    lose_samples: list[float] = []
    rejected_empty = 0
    n_clv_missing = 0
    n_close_ev_missing = 0
    attempts = 0
    while attempts < max_attempts and len(roi_samples) < int(replicates):
        attempts += 1
        drawn = resample_event_blocks(blocks, rng=rng)
        if not drawn:
            rejected_empty += 1
            continue
        totals = betting_metrics(drawn, n_threshold_only=n_threshold_only)
        if totals.flat_1_unit_roi.value is None:
            rejected_empty += 1
            continue
        roi_samples.append(float(totals.flat_1_unit_roi.value))
        if totals.mean_probability_clv.value is None:
            n_clv_missing += 1
        else:
            clv_samples.append(float(totals.mean_probability_clv.value))
        if totals.mean_closing_ev.value is None:
            n_close_ev_missing += 1
        else:
            close_ev_samples.append(float(totals.mean_closing_ev.value))
        dd_samples.append(
            float(totals.maximum_drawdown.value)
            if totals.maximum_drawdown.value is not None
            else 0.0
        )
        lose_samples.append(float(totals.longest_losing_run.value or 0))
    intervals: dict[str, dict[str, Any]] = {}
    for metric_name, samples, n_missing, n_rejected in (
        ("flat_1_unit_roi", roi_samples, 0, rejected_empty),
        ("clv", clv_samples, n_clv_missing, 0),
        ("closing_ev", close_ev_samples, n_close_ev_missing, 0),
        ("maximum_drawdown", dd_samples, 0, rejected_empty),
        ("longest_losing_run", lose_samples, 0, rejected_empty),
    ):
        metric_intervals = []
        for level in levels:
            lower, upper = _percentile_interval(samples, level)
            metric_intervals.append(
                IntervalEstimate(
                    level=level,
                    lower=lower,
                    upper=upper,
                    n_replicates=len(samples),
                    n_rejected=n_rejected,
                    n_missing=n_missing,
                ).to_dict()
            )
        intervals[metric_name] = metric_intervals
    return {
        "bootstrap_unit": "event_block",
        "event_count": n_events,
        "event_ids": list(event_ids),
        "intervals": intervals,
        "n_rejected_empty_bets": rejected_empty,
        "n_rejected": rejected_empty,
        "n_replicates": len(roi_samples),
        "requested_replicates": int(replicates),
        "seed": int(seed),
        "clv_n_replicates": len(clv_samples),
        "clv_n_missing": n_clv_missing,
        "closing_ev_n_replicates": len(close_ev_samples),
        "closing_ev_n_missing": n_close_ev_missing,
    }


def bootstrap_outcome_intervals(
    rows: Sequence[OutcomeObservation],
    *,
    seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    contract: EvaluationContract | None = None,
    market_rows: Sequence[MarketOutcomeRow] = (),
) -> dict[str, Any]:
    levels = _validated_interval_levels(contract)
    grouped: dict[str, list[OutcomeObservation]] = {}
    for row in rows:
        grouped.setdefault(row.event_id, []).append(row)
    market_grouped: dict[str, list[MarketOutcomeRow]] = {}
    for row in market_rows:
        market_grouped.setdefault(row.event_id, []).append(row)
    event_ids = tuple(sorted(set(grouped) | set(market_grouped)))
    rng = np.random.default_rng(int(seed))
    max_attempts = max(int(replicates) * DEFAULT_MAX_ATTEMPT_MULTIPLIER, int(replicates))
    ll_samples: list[float] = []
    brier_samples: list[float] = []
    joint_samples: list[float] = []
    rejected = 0
    n_joint_missing = 0
    attempts = 0
    while len(ll_samples) < int(replicates) and attempts < max_attempts:
        attempts += 1
        if not event_ids:
            rejected += 1
            break
        drawn_ids = rng.choice(np.asarray(event_ids), size=len(event_ids), replace=True)
        drawn: list[OutcomeObservation] = []
        drawn_markets: list[MarketOutcomeRow] = []
        for event_id in drawn_ids:
            drawn.extend(grouped.get(str(event_id), ()))
            drawn_markets.extend(market_grouped.get(str(event_id), ()))
        y, p, _events = _binary_pairs(drawn)
        if not y:
            rejected += 1
            continue
        ll_samples.append(binary_nll(y, p))
        brier_samples.append(binary_brier(y, p))
        joint_rows = [
            row for row in drawn if row.joint is not None and row.observed_atom is not None
        ]
        if joint_rows:
            joint_samples.append(
                joint_terminal_nll(
                    [dict(row.joint or {}) for row in joint_rows],
                    [str(row.observed_atom) for row in joint_rows],
                )
            )
        else:
            n_joint_missing += 1
    n_kept = len(ll_samples)
    interval_map: dict[str, list[dict[str, Any]]] = {}
    for name, samples, n_missing in (
        ("market_log_loss", ll_samples, 0),
        ("brier", brier_samples, 0),
        ("joint_log_loss", joint_samples, n_joint_missing),
    ):
        metric_intervals = []
        for level in levels:
            lower, upper = _percentile_interval(samples, level)
            metric_intervals.append(
                IntervalEstimate(
                    level=level,
                    lower=lower,
                    upper=upper,
                    n_replicates=len(samples),
                    n_rejected=rejected,
                    n_missing=n_missing,
                ).to_dict()
            )
        interval_map[name] = metric_intervals
    per_market: dict[str, Any] = {}
    for family in (item.value for item in MarketFamily):
        fam_samples: list[float] = []
        fam_missing = 0
        fam_attempts = 0
        rng_m = np.random.default_rng(int(seed) + 17)
        while len(fam_samples) < int(replicates) and fam_attempts < max_attempts:
            fam_attempts += 1
            if not event_ids:
                break
            drawn_ids = rng_m.choice(np.asarray(event_ids), size=len(event_ids), replace=True)
            drawn_m: list[MarketOutcomeRow] = []
            for event_id in drawn_ids:
                drawn_m.extend(market_grouped.get(str(event_id), ()))
            scored = [
                row
                for row in drawn_m
                if row.market_family == family
                and row.settlement in {SettlementResult.WIN, SettlementResult.LOSS}
            ]
            if not scored:
                fam_missing += 1
                continue
            y_m = [1 if row.settlement is SettlementResult.WIN else 0 for row in scored]
            p_m = [float(row.p50) for row in scored]
            fam_samples.append(binary_nll(y_m, p_m))
        fam_intervals = []
        for level in levels:
            lower, upper = _percentile_interval(fam_samples, level)
            fam_intervals.append(
                IntervalEstimate(
                    level=level,
                    lower=lower,
                    upper=upper,
                    n_replicates=len(fam_samples),
                    n_rejected=0,
                    n_missing=fam_missing,
                ).to_dict()
            )
        per_market[family] = {"log_loss": fam_intervals}
    return {
        "bootstrap_unit": "event_block",
        "event_count": len(event_ids),
        "intervals": interval_map,
        "n_rejected": rejected,
        "n_replicates": n_kept,
        "per_market": per_market,
        "requested_replicates": int(replicates),
        "seed": int(seed),
    }


def _selection_threshold_count(rows: Sequence[AttemptRow]) -> int:
    return sum(row.n_threshold_selections for row in rows)


def _selection_priced_count(rows: Sequence[AttemptRow]) -> int:
    return sum(row.n_priced_selections for row in rows)


def breakdowns(
    *,
    attempts: Sequence[AttemptRow],
    outcomes: Sequence[OutcomeObservation],
    bets: Sequence[PricedBet],
    n_threshold_only: int,
    market_rows: Sequence[MarketOutcomeRow] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {"universes": {}, "years": {}, "markets": {}, "sources": {}}
    for universe in UniverseKey:
        u_attempts = filter_attempts(attempts, universe)
        u_outcomes = filter_outcomes(outcomes, universe)
        u_bets = filter_bets(bets, universe)
        u_threshold = _selection_threshold_count(u_attempts)
        u_markets = tuple(
            row for row in market_rows if _in_universe(row.series_variant, universe)
        )
        payload["universes"][universe.value] = {
            "betting": betting_metrics(u_bets, n_threshold_only=u_threshold).to_dict(),
            "n_attempts": len(u_attempts),
            "n_priced_selections": _selection_priced_count(u_attempts),
            "n_threshold_selections": u_threshold,
            "outcome": outcome_metrics(u_outcomes, market_rows=u_markets),
            "selection": selection_metrics(u_attempts),
        }
    years = sorted({row.season for row in attempts})
    for year in years:
        y_attempts = tuple(row for row in attempts if row.season == year)
        y_outcomes = tuple(row for row in outcomes if row.season == year)
        y_bets = tuple(bet for bet in bets if bet.season == year)
        y_threshold = _selection_threshold_count(y_attempts)
        y_markets = tuple(row for row in market_rows if row.season == year)
        payload["years"][str(year)] = {
            "betting": betting_metrics(y_bets, n_threshold_only=y_threshold).to_dict(),
            "n_attempts": len(y_attempts),
            "n_priced_selections": _selection_priced_count(y_attempts),
            "n_threshold_selections": y_threshold,
            "outcome": outcome_metrics(y_outcomes, market_rows=y_markets),
            "selection": selection_metrics(y_attempts),
        }
    families = sorted({item.value for item in MarketFamily} | {bet.market_family for bet in bets})
    for market in families:
        m_bets = tuple(bet for bet in bets if bet.market_family == market)
        m_threshold = sum(
            1
            for row in attempts
            for family in row.threshold_market_families
            if family == market
        )
        m_outcomes = tuple(row for row in market_rows if row.market_family == market)
        payload["markets"][market] = {
            "betting": betting_metrics(m_bets, n_threshold_only=m_threshold).to_dict(),
            "n_threshold_selections": m_threshold,
            "outcome": per_market_outcome_metrics(m_outcomes).get(market, {}),
        }
    sources = sorted(
        {
            f"{bet.source_kind}:{bet.provider or ''}:{bet.bookmaker_key or ''}"
            for bet in bets
        }
    )
    for source in sources:
        s_bets = tuple(
            bet
            for bet in bets
            if f"{bet.source_kind}:{bet.provider or ''}:{bet.bookmaker_key or ''}"
            == source
        )
        payload["sources"][source] = {
            "betting": betting_metrics(s_bets, n_threshold_only=0).to_dict(),
            "n_priced_selections": len(s_bets),
        }
    payload["sources"]["threshold_only"] = {
        "n_threshold_selections": n_threshold_only,
        "betting": betting_metrics((), n_threshold_only=n_threshold_only).to_dict(),
    }
    payload["n_threshold_only_top"] = n_threshold_only
    payload["n_priced_selections_top"] = len(bets)
    return payload


def assert_breakdowns_reconcile(
    *,
    attempts: Sequence[AttemptRow],
    bets: Sequence[PricedBet],
    market_rows: Sequence[MarketOutcomeRow] = (),
) -> None:
    """Year / Brazil / standard / market / source slices must reconcile."""
    n_all = len(attempts)
    n_std = sum(1 for row in attempts if row.series_variant == SeriesVariant.STANDARD.value)
    n_br = sum(1 for row in attempts if row.series_variant == SeriesVariant.BRAZIL.value)
    if n_std + n_br != n_all:
        raise MetricsError(
            f"standard ({n_std}) + brazil ({n_br}) != all_dwcs ({n_all})"
        )
    year_sum = sum(
        1
        for _year in {row.season for row in attempts}
        for row in attempts
        if row.season == _year
    )
    if year_sum != n_all:
        raise MetricsError(f"year slices {year_sum} != attempted {n_all}")
    n_bets = len(bets)
    n_std_bets = sum(1 for bet in bets if bet.series_variant == SeriesVariant.STANDARD.value)
    n_br_bets = sum(1 for bet in bets if bet.series_variant == SeriesVariant.BRAZIL.value)
    if n_std_bets + n_br_bets != n_bets:
        raise MetricsError(
            f"standard bets ({n_std_bets}) + brazil bets ({n_br_bets}) != {n_bets}"
        )
    market_sum = sum(
        len(tuple(bet for bet in bets if bet.market_family == market))
        for market in {item.market_family for item in bets}
    )
    if market_sum != n_bets:
        raise MetricsError(f"market slices {market_sum} != priced bets {n_bets}")
    n_threshold = _selection_threshold_count(attempts)
    threshold_market_sum = sum(
        sum(1 for family in row.threshold_market_families if family == market)
        for market in {item.value for item in MarketFamily}
        for row in attempts
    )
    if threshold_market_sum != n_threshold:
        raise MetricsError(
            f"threshold market slices {threshold_market_sum} != {n_threshold}"
        )
    n_priced_sel = _selection_priced_count(attempts)
    if n_priced_sel != n_bets:
        raise MetricsError(
            f"priced selection rows {n_priced_sel} != priced bets {n_bets}"
        )
    n_outcome = len(market_rows)
    outcome_family_sum = sum(
        1
        for family in {item.value for item in MarketFamily}
        for row in market_rows
        if row.market_family == family
    )
    if outcome_family_sum != n_outcome:
        raise MetricsError(
            f"outcome market slices {outcome_family_sum} != {n_outcome}"
        )


def expected_value_for_row(model_prob: float, offered_decimal: float) -> float:
    """Delegate to DWCS-204; never duplicate the EV formula here."""
    return expected_value(model_prob, offered_decimal)


def contract_interval_levels(contract: EvaluationContract) -> tuple[float, ...]:
    levels = contract.confidence_intervals.betting_metrics.interval_levels
    return tuple(float(level) for level in levels)
