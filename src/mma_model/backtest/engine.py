"""Event-grouped multi-market walk-forward engine (DWCS-306).

One outer test unit is a complete card. All bouts share one cutoff. Training
uses only earlier cards; 2025 never enters a refit. Missing source data yields
typed exclusions, never fabricated production scores.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Never, Protocol

from mma_model.backtest.contract import (
    compute_data_hash,
    compute_splits_config_hash,
    current_feature_spec_hash,
)
from mma_model.backtest.gates import (
    HOLDOUT_YEAR,
    PRICED_SCOPE,
    THRESHOLD_SCOPE,
    DatabaseMutationError,
    assert_contract_frozen,
    assert_cutoff_before_results,
    assert_evaluator_hashes,
    assert_holdout_not_in_train,
    assert_readonly_database_url,
    assert_same_card_not_in_train,
    assert_threshold_only_clean,
)
from mma_model.backtest.metrics import (
    DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    AttemptRow,
    OutcomeObservation,
    PricedBet,
    UniverseKey,
    assert_breakdowns_reconcile,
    betting_metrics,
    bootstrap_betting_intervals,
    bootstrap_outcome_intervals,
    breakdowns,
    filter_attempts,
    filter_bets,
    filter_outcomes,
    outcome_metrics,
    selection_metrics,
)
from mma_model.backtest.report import (
    EVIDENCE_SCHEMA_VERSION,
    attach_content_hash,
    isoformat_utc,
    metric_definitions,
    write_evidence_files,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.domain.quote_eligibility import QUOTE_ELIGIBILITY_DECISION_VERSION
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import EvaluationContract, load_evaluation_contract
from mma_model.features.as_of import cutoff_for_event, implied_event_start
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotBout,
    snapshot_from_session,
    to_label_version,
)
from mma_model.features.spec import spec_hash
from mma_model.labels.outcomes import (
    MethodLabel,
    ResultClass,
    VersionKind,
    WinnerSide,
    settlement_label,
    terminal_atom,
    training_label,
)
from mma_model.markets.derive import derive_markets
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementResult,
    settle,
)
from mma_model.modeling.artifacts import (
    PINNED_RIDGE_SPEC_HASH,
    compute_code_hash,
    load_ridge_spec,
    resolve_code_commit,
)
from mma_model.modeling.baselines import (
    LABEL_LAG,
    LabeledSample,
    MissingNoVig,
    PreCutoffMoneyline,
    TrainError,
    coin_flip_win_prob,
    fit_ridge,
    no_vig_win_prob,
    predict_ridge_raw,
    predict_ridge_win_prob,
    protocol_training_universe,
    sequential_rating_win_prob,
)
from mma_model.modeling.calibration import SigmoidCalibrator
from mma_model.modeling.metrics import MetricsError, fit_logistic_recalibration, stable_logit
from mma_model.modeling.splits import (
    EventGroup,
    FoldKind,
    FoldMetadata,
    FoldRole,
    SplitCard,
    cards_from_manifest,
    cards_from_session,
    group_cards,
    outer_folds,
    protocol_fixture_cards,
)
from mma_model.quality.readonly import (
    CoverageDatabaseError,
    open_readonly_sqlite_engine,
    readonly_session_factory,
)
from mma_model.quality.schema import sha256_canonical
from mma_model.value.evidence import (
    ClosingPriceEvidence,
    ManualBindingSource,
    ManualBoutBindingAssertion,
    ManualObservedPriceEvidence,
    PriceObservationRole,
    PriceProvenanceKind,
    ProviderQuoteEvidence,
    QuoteEligibilityEvidence,
    ValueSelectionContext,
    compute_eligibility_decision_identity,
)
from mma_model.value.priced import PricedValueRequest, compute_priced_value_metrics
from mma_model.value.thresholds import compute_value_price_thresholds

CUTOFF_POLICY: Final = "scheduled_minus_60m"
LEGACY_BACKTEST_METHOD: Final = "disabled_unsafe_fight_by_fight"
UNSAFE_EVALUATOR_NOTE: Final = (
    "The fight-by-fight walk_forward_backtest is disabled and is not betting "
    "evidence. Use mma-model backtest run for event-grouped replay."
)
M1_UNSUPPORTED_MARKETS: Final = (
    MarketFamily.TOTALS,
    MarketFamily.GOES_DISTANCE,
    MarketFamily.METHOD,
    MarketFamily.FIGHTER_BY_METHOD,
    MarketFamily.EXACT_ROUND,
)
BLOCKING_QUOTE_LIFECYCLES: Final = frozenset(
    {"stale", "replaced", "cancelled", "locked", "review_blocked", "missing_unknown"}
)


class AttemptStatus(StrEnum):
    PREDICTED = "predicted"
    ABSTAINED = "abstained"
    UNAVAILABLE = "unavailable"
    EXCLUDED = "excluded"


class ExclusionReason(StrEnum):
    LOCKED_NOT_ACCESSED = "locked_not_accessed"
    MISSING_DATABASE = "missing_database"
    MISSING_FEATURES = "missing_features"
    MISSING_MODEL = "missing_model"
    MISSING_SETTLEMENT_FACTS = "missing_settlement_facts"
    INSUFFICIENT_TRAIN = "insufficient_train"
    POST_CUTOFF_ODDS = "post_cutoff_odds"
    STALE_ODDS = "stale_odds"
    REPLACED_ODDS = "replaced_odds"
    AMBIGUOUS_ODDS = "ambiguous_odds"
    INELIGIBLE_QUOTE = "ineligible_quote"
    SUSPENDED_ODDS = "suspended_odds"
    PROXY_TIMESTAMP_EXCLUDES_EXACT_CLV = "proxy_timestamp_excludes_exact_clv"
    UNSUPPORTED_MARKET = "unsupported_market"
    M1_MONEYLINE_FALLBACK = "m1_moneyline_fallback"
    THRESHOLD_ONLY = "threshold_only"
    FEATURE_QUALITY = "feature_quality"
    NO_COHERENT_DISTRIBUTION = "no_coherent_distribution"
    HOLDOUT_IN_TRAIN = "holdout_in_train"


class BacktestError(ValueError):
    """Walk-forward engine cannot proceed."""


class WalkForwardDeprecatedError(BacktestError):
    """Legacy fight-by-fight evaluator is disabled."""


@dataclass(frozen=True)
class QuoteCandidate:
    """Timestamped offered (and optional closing) quote for one selection."""

    bout_id: str
    market_family: str
    outcome_key: str
    line_point: float | None
    price_decimal: float
    observed_at: datetime
    bookmaker_key: str
    provider: str
    region: str
    quote_id: int
    availability: str = "available"
    lifecycle: str = "active"
    eligible: bool = True
    eligibility_reason: str = "none"
    is_proxy_timestamp: bool = False
    is_replacement: bool = False
    is_ambiguous: bool = False
    source_kind: str = "provider_quote"
    closing_price_decimal: float | None = None
    closing_observed_at: datetime | None = None
    closing_bookmaker_key: str | None = None
    closing_quote_id: int | None = None
    closing_lifecycle: str = "active"


@dataclass(frozen=True)
class MarketPrediction:
    family: str
    outcome_key: str
    line_point: float | None
    p50: float
    p25: float | None
    available: bool
    availability_reason: str | None
    draw_probability: float | None = None


@dataclass(frozen=True)
class BoutPrediction:
    """One coherent prediction for a bout (M2 atoms or M1 moneyline fallback)."""

    bout_id: str
    event_id: str
    model_id: str
    p_fighter_a: float
    p_fighter_b: float
    p_draw: float
    p50: float
    p25: float | None
    joint_atoms: Mapping[str, float] | None
    markets: tuple[MarketPrediction, ...]
    estimator_hash: str
    calibrator_hash: str | None
    train_event_ids: tuple[str, ...]
    max_train_timestamp: datetime | None
    baseline_fifty: float
    baseline_rating: float | None
    baseline_no_vig: float | None
    baseline_m1: float | None
    p25_unavailable_reason: str | None = None


@dataclass(frozen=True)
class CardScore:
    """All bout scores for one card from a single pre-card fitted state."""

    event_id: str
    estimator_hash: str
    train_event_ids: tuple[str, ...]
    max_train_timestamp: datetime | None
    holdout_in_train: bool
    predictions: tuple[BoutPrediction, ...]
    unavailable: tuple[tuple[str, ExclusionReason], ...] = ()
    abstained: tuple[tuple[str, ExclusionReason], ...] = ()


@dataclass(frozen=True)
class QuoteJoinResult:
    quote: QuoteCandidate | None
    priced: bool
    reason: ExclusionReason | None
    detail: str


@dataclass(frozen=True)
class BoutAttempt:
    event_id: str
    bout_id: str
    season: int
    series_variant: str
    cutoff: datetime
    cutoff_kind: str
    status: AttemptStatus
    exclusion_reason: ExclusionReason | None
    exclusion_detail: str
    prediction: BoutPrediction | None
    card_estimator_hash: str | None
    priced_rows: tuple[dict[str, Any], ...]
    threshold_only_rows: tuple[dict[str, Any], ...]
    pre_policy_candidates: tuple[dict[str, Any], ...]
    settlement_facts_present: bool
    source_quality: Mapping[str, Any]

    def card_output_payload(self) -> dict[str, Any]:
        return {
            "bout_id": self.bout_id,
            "card_estimator_hash": self.card_estimator_hash,
            "cutoff": self.cutoff.isoformat(),
            "event_id": self.event_id,
            "exclusion_detail": self.exclusion_detail,
            "exclusion_reason": (
                None if self.exclusion_reason is None else self.exclusion_reason.value
            ),
            "pre_policy_candidates": list(self.pre_policy_candidates),
            "prediction": None if self.prediction is None else _prediction_payload(self.prediction),
            "priced_rows": list(self.priced_rows),
            "season": self.season,
            "series_variant": self.series_variant,
            "settlement_facts_present": self.settlement_facts_present,
            "source_quality": dict(self.source_quality),
            "status": self.status.value,
            "threshold_only_rows": list(self.threshold_only_rows),
        }


class CardScorer(Protocol):
    def score_card(
        self,
        group: EventGroup,
        fold: FoldMetadata,
    ) -> CardScore:
        """Score every bout on ``group`` from one frozen pre-card state."""


@dataclass
class PrecomputedScorer:
    """Test/helper scorer: one CardScore per event_id, frozen before the card."""

    by_event: dict[str, CardScore]

    def score_card(self, group: EventGroup, fold: FoldMetadata) -> CardScore:
        score = self.by_event.get(group.event_id)
        if score is None:
            missing = tuple(
                (bout_id, ExclusionReason.MISSING_MODEL) for bout_id in group.bout_ids
            )
            return CardScore(
                event_id=group.event_id,
                estimator_hash="missing",
                train_event_ids=fold.train_event_ids,
                max_train_timestamp=fold.max_train_timestamp,
                holdout_in_train=False,
                predictions=(),
                unavailable=missing,
            )
        if score.holdout_in_train:
            raise BacktestError("precomputed score claimed holdout_in_train=true")
        bout_ids = {item.bout_id for item in score.predictions}
        if any(item.event_id != group.event_id for item in score.predictions):
            raise BacktestError("precomputed predictions leak another event_id")
        extra_unavailable = tuple(
            (bout_id, ExclusionReason.MISSING_MODEL)
            for bout_id in group.bout_ids
            if bout_id not in bout_ids
            and bout_id not in {item[0] for item in score.unavailable}
            and bout_id not in {item[0] for item in score.abstained}
        )
        return CardScore(
            event_id=score.event_id,
            estimator_hash=score.estimator_hash,
            train_event_ids=fold.train_event_ids,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=score.predictions,
            unavailable=score.unavailable + extra_unavailable,
            abstained=score.abstained,
        )


@dataclass
class ManifestExclusionScorer:
    """Attempt the frozen universe with explicit exclusions when sources are absent."""

    reason: ExclusionReason = ExclusionReason.MISSING_DATABASE
    detail: str = "no database/features/models/odds; exclusions only"

    def score_card(self, group: EventGroup, fold: FoldMetadata) -> CardScore:
        return CardScore(
            event_id=group.event_id,
            estimator_hash="none",
            train_event_ids=fold.train_event_ids,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=(),
            unavailable=tuple((bout_id, self.reason) for bout_id in group.bout_ids),
        )


def _selection_id(family: str, outcome: str, line_point: float | None) -> str:
    if line_point is None:
        return f"{family}:{outcome}"
    return f"{family}:{outcome}:{float(line_point)}"


def _prediction_payload(prediction: BoutPrediction) -> dict[str, Any]:
    return {
        "baseline_fifty": prediction.baseline_fifty,
        "baseline_m1": prediction.baseline_m1,
        "baseline_no_vig": prediction.baseline_no_vig,
        "baseline_rating": prediction.baseline_rating,
        "bout_id": prediction.bout_id,
        "calibrator_hash": prediction.calibrator_hash,
        "estimator_hash": prediction.estimator_hash,
        "event_id": prediction.event_id,
        "joint_atoms": None if prediction.joint_atoms is None else dict(prediction.joint_atoms),
        "markets": [
            {
                "availability_reason": item.availability_reason,
                "available": item.available,
                "draw_probability": item.draw_probability,
                "family": item.family,
                "line_point": item.line_point,
                "outcome_key": item.outcome_key,
                "p25": item.p25,
                "p50": item.p50,
            }
            for item in prediction.markets
        ],
        "max_train_timestamp": (
            None
            if prediction.max_train_timestamp is None
            else prediction.max_train_timestamp.isoformat()
        ),
        "model_id": prediction.model_id,
        "p25": prediction.p25,
        "p25_unavailable_reason": prediction.p25_unavailable_reason,
        "p50": prediction.p50,
        "p_draw": prediction.p_draw,
        "p_fighter_a": prediction.p_fighter_a,
        "p_fighter_b": prediction.p_fighter_b,
        "train_event_ids": list(prediction.train_event_ids),
    }


def moneyline_markets(
    *,
    p_a: float,
    p_b: float,
    p_draw: float,
    p25: float | None,
    fallback_reason: str | None,
) -> tuple[MarketPrediction, ...]:
    rows = [
        MarketPrediction(
            family=MarketFamily.MONEYLINE.value,
            outcome_key=OutcomeKey.FIGHTER_A.value,
            line_point=None,
            p50=p_a,
            p25=p25,
            available=True,
            availability_reason=None,
            draw_probability=p_draw,
        ),
        MarketPrediction(
            family=MarketFamily.MONEYLINE.value,
            outcome_key=OutcomeKey.FIGHTER_B.value,
            line_point=None,
            p50=p_b,
            p25=None if p25 is None else (1.0 - p25 if p25 <= 1.0 else None),
            available=True,
            availability_reason=None,
            draw_probability=p_draw,
        ),
    ]
    extra: list[MarketPrediction] = []
    for family in M1_UNSUPPORTED_MARKETS:
        extra.append(
            MarketPrediction(
                family=family.value,
                outcome_key="",
                line_point=None,
                p50=0.0,
                p25=None,
                available=False,
                availability_reason=fallback_reason or ExclusionReason.UNSUPPORTED_MARKET.value,
            )
        )
    return tuple(rows + extra)


def markets_from_joint(
    atoms: Mapping[str, float],
    *,
    scheduled_rounds: int,
    p25: float | None,
) -> tuple[MarketPrediction, ...]:
    derived = derive_markets(atoms, scheduled_rounds=scheduled_rounds)
    rows: list[MarketPrediction] = []
    for family, mapping in derived.as_family_map().items():
        for outcome, prob in mapping.items():
            rows.append(
                MarketPrediction(
                    family=family.value,
                    outcome_key=outcome.value,
                    line_point=None,
                    p50=float(prob),
                    p25=p25 if family is MarketFamily.MONEYLINE else None,
                    available=True,
                    availability_reason=None,
                    draw_probability=derived.draw if family is MarketFamily.MONEYLINE else None,
                )
            )
    for line_point, mapping in derived.totals.items():
        for outcome, prob in mapping.items():
            rows.append(
                MarketPrediction(
                    family=MarketFamily.TOTALS.value,
                    outcome_key=outcome.value,
                    line_point=float(line_point),
                    p50=float(prob),
                    p25=None,
                    available=True,
                    availability_reason=None,
                )
            )
    return tuple(rows)


def join_quote(
    candidates: Sequence[QuoteCandidate],
    *,
    bout_id: str,
    family: str,
    outcome_key: str,
    line_point: float | None,
    cutoff: datetime,
) -> QuoteJoinResult:
    matching = [
        item
        for item in candidates
        if item.bout_id == bout_id
        and item.market_family == family
        and item.outcome_key == outcome_key
        and item.line_point == line_point
    ]
    if not matching:
        return QuoteJoinResult(None, False, ExclusionReason.THRESHOLD_ONLY, "no quote")
    if any(item.is_ambiguous for item in matching):
        return QuoteJoinResult(None, False, ExclusionReason.AMBIGUOUS_ODDS, "ambiguous")
    prices = {(item.observed_at, item.price_decimal) for item in matching}
    latest_time = max(item.observed_at for item in matching)
    at_latest = [item for item in matching if item.observed_at == latest_time]
    if len({item.price_decimal for item in at_latest}) > 1:
        return QuoteJoinResult(
            None, False, ExclusionReason.AMBIGUOUS_ODDS, "conflicting same-timestamp prices"
        )
    chosen = max(matching, key=lambda item: (item.observed_at, item.quote_id))
    if chosen.observed_at > cutoff:
        return QuoteJoinResult(chosen, False, ExclusionReason.POST_CUTOFF_ODDS, "post-cutoff")
    if chosen.is_replacement or chosen.lifecycle == "replaced":
        return QuoteJoinResult(chosen, False, ExclusionReason.REPLACED_ODDS, "replaced")
    if chosen.lifecycle == "stale":
        return QuoteJoinResult(chosen, False, ExclusionReason.STALE_ODDS, "stale")
    if chosen.availability != "available" or chosen.lifecycle in BLOCKING_QUOTE_LIFECYCLES:
        return QuoteJoinResult(chosen, False, ExclusionReason.SUSPENDED_ODDS, chosen.lifecycle)
    if not chosen.eligible:
        return QuoteJoinResult(
            chosen, False, ExclusionReason.INELIGIBLE_QUOTE, chosen.eligibility_reason
        )
    _ = prices
    return QuoteJoinResult(chosen, True, None, "eligible")


def _method_for_facts(method: MethodLabel | None) -> str | None:
    if method is None:
        return None
    if method is MethodLabel.KO_TKO:
        return "ko_tko"
    if method is MethodLabel.SUBMISSION:
        return "submission"
    if method is MethodLabel.DECISION:
        return "decision"
    if method is MethodLabel.OTHER_STOPPAGE:
        return "other_stoppage"
    if method is MethodLabel.TECHNICAL_DECISION:
        return "technical_decision"
    if method is MethodLabel.TECHNICAL_DRAW:
        return "technical_draw"
    never_method: Never = method
    raise BacktestError(f"unhandled method label: {never_method!r}")


def facts_from_snapshot(snapshot: FeatureSnapshot, bout_id: str) -> BoutSettlementFacts | None:
    bout = snapshot.bout_by_id(bout_id)
    if bout is None:
        return None
    night = [
        row
        for row in snapshot.result_versions
        if row.bout_id == bout_id and row.version_kind == VersionKind.EVENT_NIGHT.value
    ]
    if not night:
        return None
    chosen = max(night, key=lambda row: int(row.revision))
    label = settlement_label(to_label_version(chosen))
    result_class = label.result_class.value
    if result_class not in {"decisive", "draw", "no_contest"}:
        return BoutSettlementFacts(
            scheduled_rounds=int(bout.scheduled_rounds or 3),
            pending=True,
        )
    winner_side = None if label.winner_side is None else label.winner_side.value
    return BoutSettlementFacts(
        scheduled_rounds=int(bout.scheduled_rounds or 3),
        result_class=result_class,  # type: ignore[arg-type]
        winner_side=winner_side,  # type: ignore[arg-type]
        method=_method_for_facts(label.method),  # type: ignore[arg-type]
        ending_round=chosen.ending_round,
    )


def _is_pre_policy_candidate(
    *,
    family: MarketFamily,
    p50: float,
    p25: float | None,
    offered: float,
    contract: EvaluationContract,
) -> bool:
    conservative = p25 if p25 is not None else p50
    if conservative > p50:
        conservative = p50
    thresholds = compute_value_price_thresholds(p50, conservative, family=family)
    return offered + 1e-12 >= thresholds.actionable_decimal


def _eligibility(
    quote: QuoteCandidate,
    *,
    selection_identity: str,
    evaluated_at: datetime,
    eligible: bool,
    reason: str,
) -> QuoteEligibilityEvidence:
    identity = compute_eligibility_decision_identity(
        quote_id=quote.quote_id,
        evaluated_at=evaluated_at,
        eligible=eligible,
        reason=reason,
        selection_identity=selection_identity,
        resolved_bout_id=quote.bout_id,
        quote_availability_at_decision=quote.availability,
        quote_freshness_at=quote.observed_at,
        lifecycle_state_at_decision=quote.lifecycle,
        decision_version=QUOTE_ELIGIBILITY_DECISION_VERSION,
    )
    return QuoteEligibilityEvidence(
        quote_id=quote.quote_id,
        eligible=eligible,
        selection_identity=selection_identity,
        resolved_bout_id=quote.bout_id,
        reason=reason,
        evaluated_at=evaluated_at,
        quote_availability_at_decision=quote.availability,
        decision_identity=identity,
        quote_freshness_at=quote.observed_at,
        lifecycle_state_at_decision=quote.lifecycle,
        decision_version=QUOTE_ELIGIBILITY_DECISION_VERSION,
    )


def _priced_metrics_for_quote(
    *,
    quote: QuoteCandidate,
    p50: float,
    family: MarketFamily,
    outcome: OutcomeKey,
    line_point: float | None,
    cutoff: datetime,
    settlement: SettlementResult | None,
    event_start: datetime,
) -> dict[str, Any]:
    selection_identity = _selection_id(family.value, outcome.value, line_point)
    context = ValueSelectionContext(
        bout_id=quote.bout_id,
        market_family=family.value,
        outcome_key=outcome.value,
        line_point=line_point,
    )
    closing = None
    if (
        quote.closing_price_decimal is not None
        and quote.closing_observed_at is not None
        and quote.closing_quote_id is not None
        and quote.closing_observed_at > quote.observed_at
        and quote.closing_observed_at <= event_start
        and not quote.is_proxy_timestamp
    ):
        close_book = quote.closing_bookmaker_key or quote.bookmaker_key
        close_quote = QuoteCandidate(
            bout_id=quote.bout_id,
            market_family=quote.market_family,
            outcome_key=quote.outcome_key,
            line_point=quote.line_point,
            price_decimal=quote.closing_price_decimal,
            observed_at=quote.closing_observed_at,
            bookmaker_key=close_book,
            provider=quote.provider,
            region=quote.region,
            quote_id=quote.closing_quote_id,
            availability="available",
            lifecycle=quote.closing_lifecycle,
            source_kind=quote.source_kind,
        )
        if quote.source_kind == "user_observed":
            close_manual = _manual_evidence(close_quote, role=PriceObservationRole.CLOSING)
            closing = ClosingPriceEvidence(
                manual_evidence=close_manual,
                closing_cutoff=quote.closing_observed_at,
            )
        else:
            close_provider = _provider_evidence(close_quote, role=PriceObservationRole.CLOSING)
            close_elig = _eligibility(
                close_quote,
                selection_identity=selection_identity,
                evaluated_at=quote.closing_observed_at,
                eligible=True,
                reason="none",
            )
            closing = ClosingPriceEvidence(
                quote_evidence=close_provider,
                eligibility_evidence=close_elig,
            )
    if quote.source_kind == "user_observed":
        request = PricedValueRequest(
            model_prob=p50,
            target_context=context,
            valuation_cutoff=cutoff,
            product_eligible=True,
            manual_evidence=_manual_evidence(quote, role=PriceObservationRole.OPENING),
            closing_evidence=closing,
            settlement=settlement,
        )
    else:
        request = PricedValueRequest(
            model_prob=p50,
            target_context=context,
            valuation_cutoff=cutoff,
            product_eligible=True,
            quote_evidence=_provider_evidence(quote, role=PriceObservationRole.OPENING),
            eligibility_evidence=_eligibility(
                quote,
                selection_identity=selection_identity,
                evaluated_at=cutoff,
                eligible=True,
                reason="none",
            ),
            closing_evidence=closing,
            settlement=settlement,
        )
    metrics = compute_priced_value_metrics(request)
    payload = {
        "available": metrics.available,
        "bookmaker_key": quote.bookmaker_key,
        "bout_id": quote.bout_id,
        "closing_ev": metrics.closing_ev,
        "expected_value": metrics.expected_value,
        "flat_unit_profit": metrics.flat_unit_profit,
        "line_point": line_point,
        "market_family": family.value,
        "model_prob": p50,
        "offered_decimal": quote.price_decimal,
        "outcome_key": outcome.value,
        "probability_clv": metrics.probability_clv,
        "provider": quote.provider,
        "quarter_kelly_fraction": metrics.quarter_kelly_fraction,
        "reason": metrics.reason.value,
        "scope": PRICED_SCOPE,
        "settlement": None if settlement is None else settlement.value,
        "source_kind": quote.source_kind,
        "stake_fraction": metrics.stake_fraction,
        "is_proxy_timestamp": quote.is_proxy_timestamp,
    }
    if quote.is_proxy_timestamp:
        payload["probability_clv"] = None
        payload["closing_ev"] = None
        payload["clv_exclusion"] = ExclusionReason.PROXY_TIMESTAMP_EXCLUDES_EXACT_CLV.value
    return payload


def _manual_evidence(
    quote: QuoteCandidate,
    *,
    role: PriceObservationRole,
) -> ManualObservedPriceEvidence:
    return ManualObservedPriceEvidence(
        provenance=PriceProvenanceKind.USER_OBSERVED,
        automated=False,
        market_family=quote.market_family,
        outcome_key=quote.outcome_key,
        line_point=quote.line_point,
        selection_identity=_selection_id(
            quote.market_family, quote.outcome_key, quote.line_point
        ),
        price_decimal=quote.price_decimal,
        lifecycle="available",
        observed_at=quote.observed_at,
        bookmaker_key=quote.bookmaker_key,
        region=quote.region,
        bout_binding=ManualBoutBindingAssertion(
            bout_id=quote.bout_id,
            asserted_at=quote.observed_at,
            asserted_by="backtest-engine",
            source=ManualBindingSource.OPERATOR_ASSERTION,
            note="dwcs-306 walk-forward",
        ),
        price_role=role,
    )


def _provider_evidence(
    quote: QuoteCandidate,
    *,
    role: PriceObservationRole,
) -> ProviderQuoteEvidence:
    return ProviderQuoteEvidence(
        quote_id=quote.quote_id,
        provider=quote.provider,
        bookmaker_key=quote.bookmaker_key,
        region=quote.region,
        market_family=quote.market_family,
        outcome_key=quote.outcome_key,
        line_point=quote.line_point,
        selection_identity=_selection_id(
            quote.market_family, quote.outcome_key, quote.line_point
        ),
        price_decimal=quote.price_decimal,
        availability="available",
        observed_at=quote.observed_at,
        bout_id=quote.bout_id,
        price_role=role,
    )


def _settle_market(
    facts: BoutSettlementFacts | None,
    family: MarketFamily,
    outcome: OutcomeKey,
    line_point: float | None,
) -> SettlementResult | None:
    if facts is None:
        return None
    decision = settle(
        MarketSelection(family=family, outcome=outcome, line_point=line_point),
        facts,
    )
    return decision.result


def _threshold_row(market: MarketPrediction) -> dict[str, Any]:
    return {
        "availability_reason": market.availability_reason,
        "available": market.available,
        "expected_value": None,
        "family": market.family,
        "flat_unit_profit": None,
        "line_point": market.line_point,
        "outcome_key": market.outcome_key,
        "p25": market.p25,
        "p50": market.p50,
        "probability_clv": None,
        "quarter_kelly_fraction": None,
        "realized_roi": None,
        "scope": THRESHOLD_SCOPE,
        "stake_fraction": None,
        "turnover": None,
    }


@dataclass
class SnapshotWalkForwardScorer:
    """Fit M1 on earlier snapshot cards only; score the whole test card together."""

    snapshot: FeatureSnapshot
    eval_event_ids: frozenset[str]
    contract: EvaluationContract
    _prior_oof_y: list[int] = field(default_factory=list)
    _prior_oof_logit: list[float] = field(default_factory=list)
    last_fit_hashes: dict[str, str] = field(default_factory=dict)

    def score_card(self, group: EventGroup, fold: FoldMetadata) -> CardScore:
        assert_same_card_not_in_train(
            test_event_id=group.event_id, train_event_ids=fold.train_event_ids
        )
        assert_cutoff_before_results(
            max_train_timestamp=fold.max_train_timestamp,
            cutoff=group.cutoff.cutoff,
            event_id=group.event_id,
        )
        assert_holdout_not_in_train(fold.train_event_ids)
        train_samples = self._train_samples(group)
        train_events = tuple(sorted({sample.event_id for sample in train_samples}))
        assert_holdout_not_in_train(train_events)
        if any(sample.event_id == group.event_id for sample in train_samples):
            raise BacktestError(f"same-card sample leaked into train for {group.event_id}")
        if not train_samples:
            return CardScore(
                event_id=group.event_id,
                estimator_hash="none",
                train_event_ids=train_events,
                max_train_timestamp=fold.max_train_timestamp,
                holdout_in_train=False,
                predictions=(),
                unavailable=tuple(
                    (bout_id, ExclusionReason.INSUFFICIENT_TRAIN) for bout_id in group.bout_ids
                ),
            )
        try:
            spec = load_ridge_spec()
            model = fit_ridge(train_samples, spec)
        except TrainError:
            return CardScore(
                event_id=group.event_id,
                estimator_hash="none",
                train_event_ids=train_events,
                max_train_timestamp=fold.max_train_timestamp,
                holdout_in_train=False,
                predictions=(),
                unavailable=tuple(
                    (bout_id, ExclusionReason.INSUFFICIENT_TRAIN) for bout_id in group.bout_ids
                ),
            )
        calibrator = self._calibrator()
        estimator_hash = model.predictor.identity_hash()
        calibrator_hash = (
            None
            if calibrator is None
            else sha256_canonical({"a": calibrator.a, "b": calibrator.b})
        )
        self.last_fit_hashes[group.event_id] = estimator_hash
        predictions: list[BoutPrediction] = []
        unavailable: list[tuple[str, ExclusionReason]] = []
        builder = FeatureBuilder(self.snapshot)
        for bout_id in group.bout_ids:
            bout = self.snapshot.bout_by_id(bout_id)
            if bout is None:
                unavailable.append((bout_id, ExclusionReason.MISSING_FEATURES))
                continue
            row = builder.build(
                bout.fighter_a_id,
                bout.fighter_b_id,
                group.cutoff,
                bout_id=bout_id,
            )
            raw_p = predict_ridge_raw(model, row.values)
            if calibrator is not None:
                p_a = calibrator.apply_probability(raw_p)
            else:
                p_a = predict_ridge_win_prob(model, row.values)
            p_a = min(max(float(p_a), 1e-15), 1.0 - 1e-15)
            p_b = 1.0 - p_a
            rating = sequential_rating_win_prob(row.values)
            moneyline = self._quote_moneyline(bout_id, group.cutoff.cutoff)
            no_vig = no_vig_win_prob(moneyline, cutoff=group.cutoff.cutoff)
            no_vig_p = None if isinstance(no_vig, MissingNoVig) else float(no_vig)
            prediction = BoutPrediction(
                bout_id=bout_id,
                event_id=group.event_id,
                model_id="M1",
                p_fighter_a=p_a,
                p_fighter_b=p_b,
                p_draw=0.0,
                p50=p_a,
                p25=None,
                joint_atoms=None,
                markets=moneyline_markets(
                    p_a=p_a,
                    p_b=p_b,
                    p_draw=0.0,
                    p25=None,
                    fallback_reason=ExclusionReason.M1_MONEYLINE_FALLBACK.value,
                ),
                estimator_hash=estimator_hash,
                calibrator_hash=calibrator_hash,
                train_event_ids=train_events,
                max_train_timestamp=fold.max_train_timestamp,
                baseline_fifty=coin_flip_win_prob(row.values),
                baseline_rating=rating,
                baseline_no_vig=no_vig_p,
                baseline_m1=raw_p,
                p25_unavailable_reason="walk_forward_p25_requires_dwcs_305_bootstrap",
            )
            predictions.append(prediction)
            y = self._binary_y(bout)
            if y is not None:
                self._prior_oof_y.append(y)
                self._prior_oof_logit.append(float(stable_logit(raw_p)))
        return CardScore(
            event_id=group.event_id,
            estimator_hash=estimator_hash,
            train_event_ids=train_events,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=tuple(predictions),
            unavailable=tuple(unavailable),
        )

    def _calibrator(self) -> SigmoidCalibrator | None:
        if len(self._prior_oof_y) < 8:
            return None
        if len(set(self._prior_oof_y)) < 2:
            return None
        try:
            slope, intercept = fit_logistic_recalibration(
                self._prior_oof_logit, self._prior_oof_y
            )
        except MetricsError:
            return None
        return SigmoidCalibrator(a=slope, b=intercept)

    def _binary_y(self, bout: SnapshotBout) -> int | None:
        versions = [
            to_label_version(row)
            for row in self.snapshot.result_versions
            if row.bout_id == bout.bout_id
        ]
        if not versions:
            return None
        # Labels for calibration OOF are the event-night outcome of already
        # scored *prior* cards. The current card is appended after predict.
        night = [row for row in versions if row.version_kind is VersionKind.EVENT_NIGHT]
        if not night:
            return None
        label = settlement_label(max(night, key=lambda item: item.revision))
        if label.binary_winner is WinnerSide.A:
            return 1
        if label.binary_winner is WinnerSide.B:
            return 0
        return None

    def _quote_moneyline(self, bout_id: str, cutoff: datetime) -> PreCutoffMoneyline | None:
        return None

    def _train_samples(self, group: EventGroup) -> tuple[LabeledSample, ...]:
        builder = FeatureBuilder(self.snapshot)
        samples: list[LabeledSample] = []
        bouts_by_event: dict[str, list[SnapshotBout]] = {}
        events = {event.event_id: event for event in self.snapshot.events}
        for bout in self.snapshot.bouts:
            bouts_by_event.setdefault(bout.event_id, []).append(bout)
        for event_id, bouts in bouts_by_event.items():
            event = events.get(event_id)
            if event is None or event.scheduled_start_at is None:
                continue
            start = event.scheduled_start_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if start.year == HOLDOUT_YEAR:
                continue
            if event_id == group.event_id:
                continue
            if start >= group.cutoff.cutoff:
                continue
            card = SplitCard(
                event_id=event_id,
                scheduled_start_at=start,
                event_date=event.event_date,
                series_variant=SeriesVariant.STANDARD,
                bout_ids=tuple(item.bout_id for item in bouts),
            )
            cutoff = cutoff_for_event(card)
            label_at = implied_event_start(cutoff) + LABEL_LAG
            for bout in bouts:
                versions = [
                    to_label_version(row)
                    for row in self.snapshot.result_versions
                    if row.bout_id == bout.bout_id
                ]
                label = training_label(versions, label_at)
                if label.binary_winner is None:
                    continue
                row = builder.build(
                    bout.fighter_a_id,
                    bout.fighter_b_id,
                    cutoff,
                    bout_id=bout.bout_id,
                )
                samples.append(
                    LabeledSample(
                        sample_id=bout.bout_id,
                        event_id=event_id,
                        fighter_a_id=bout.fighter_a_id,
                        fighter_b_id=bout.fighter_b_id,
                        cutoff=cutoff.cutoff,
                        values=row.values,
                        names=row.names,
                        binary_winner=label.binary_winner,
                    )
                )
        return tuple(samples)


class ProtocolWalkForwardScorer(SnapshotWalkForwardScorer):
    """Protocol fixture: real FeatureBuilder + M1 fit/calibration callbacks."""

    def __init__(self, contract: EvaluationContract) -> None:
        cards, snapshot, odds = protocol_training_universe()
        super().__init__(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in cards),
            contract=contract,
        )
        self._odds = odds

    def _quote_moneyline(self, bout_id: str, cutoff: datetime) -> PreCutoffMoneyline | None:
        quote = self._odds.get(bout_id)
        if quote is None:
            return None
        if quote.observed_at > cutoff:
            return None
        return quote


def protocol_quotes_from_universe() -> tuple[QuoteCandidate, ...]:
    _cards, _snapshot, odds = protocol_training_universe()
    rows: list[QuoteCandidate] = []
    quote_id = 1
    for bout_id, moneyline in odds.items():
        for outcome, price in moneyline.decimal_odds.items():
            rows.append(
                QuoteCandidate(
                    bout_id=bout_id,
                    market_family=MarketFamily.MONEYLINE.value,
                    outcome_key=outcome,
                    line_point=None,
                    price_decimal=float(price),
                    observed_at=moneyline.observed_at,
                    bookmaker_key="protocol_book",
                    provider="protocol",
                    region="us",
                    quote_id=quote_id,
                    source_kind="provider_quote",
                    closing_price_decimal=(
                        float(price) - 0.05 if float(price) > 1.1 else float(price)
                    ),
                    closing_observed_at=moneyline.observed_at.replace(
                        minute=min(moneyline.observed_at.minute + 10, 59)
                    ),
                    closing_bookmaker_key="protocol_book",
                    closing_quote_id=quote_id + 1000,
                )
            )
            quote_id += 1
    return tuple(rows)


def protocol_settlement_facts() -> dict[str, BoutSettlementFacts]:
    _cards, snapshot, _odds = protocol_training_universe()
    facts: dict[str, BoutSettlementFacts] = {}
    for bout in snapshot.bouts:
        got = facts_from_snapshot(snapshot, bout.bout_id)
        if got is not None:
            facts[bout.bout_id] = got
    return facts


def _attempt_from_locked(group: EventGroup, bout_id: str) -> BoutAttempt:
    return BoutAttempt(
        event_id=group.event_id,
        bout_id=bout_id,
        season=group.season,
        series_variant=group.series_variant.value,
        cutoff=group.cutoff.cutoff,
        cutoff_kind=group.cutoff.cutoff_kind.value,
        status=AttemptStatus.EXCLUDED,
        exclusion_reason=ExclusionReason.LOCKED_NOT_ACCESSED,
        exclusion_detail="2025 holdout locked; pass --sealed-holdout after freeze",
        prediction=None,
        card_estimator_hash=None,
        priced_rows=(),
        threshold_only_rows=(),
        pre_policy_candidates=(),
        settlement_facts_present=False,
        source_quality={"holdout": True, "accessed": False},
    )


def _grade_bout(
    *,
    group: EventGroup,
    bout_id: str,
    score: CardScore,
    quotes: Sequence[QuoteCandidate],
    facts_by_bout: Mapping[str, BoutSettlementFacts],
    contract: EvaluationContract,
) -> BoutAttempt:
    prediction = next((item for item in score.predictions if item.bout_id == bout_id), None)
    unavailable = dict(score.unavailable)
    abstained = dict(score.abstained)
    if bout_id in unavailable:
        return BoutAttempt(
            event_id=group.event_id,
            bout_id=bout_id,
            season=group.season,
            series_variant=group.series_variant.value,
            cutoff=group.cutoff.cutoff,
            cutoff_kind=group.cutoff.cutoff_kind.value,
            status=AttemptStatus.UNAVAILABLE,
            exclusion_reason=unavailable[bout_id],
            exclusion_detail=unavailable[bout_id].value,
            prediction=None,
            card_estimator_hash=score.estimator_hash,
            priced_rows=(),
            threshold_only_rows=(),
            pre_policy_candidates=(),
            settlement_facts_present=bout_id in facts_by_bout,
            source_quality={"reason": unavailable[bout_id].value},
        )
    if bout_id in abstained:
        return BoutAttempt(
            event_id=group.event_id,
            bout_id=bout_id,
            season=group.season,
            series_variant=group.series_variant.value,
            cutoff=group.cutoff.cutoff,
            cutoff_kind=group.cutoff.cutoff_kind.value,
            status=AttemptStatus.ABSTAINED,
            exclusion_reason=abstained[bout_id],
            exclusion_detail=abstained[bout_id].value,
            prediction=None,
            card_estimator_hash=score.estimator_hash,
            priced_rows=(),
            threshold_only_rows=(),
            pre_policy_candidates=(),
            settlement_facts_present=bout_id in facts_by_bout,
            source_quality={"reason": abstained[bout_id].value},
        )
    if prediction is None:
        return BoutAttempt(
            event_id=group.event_id,
            bout_id=bout_id,
            season=group.season,
            series_variant=group.series_variant.value,
            cutoff=group.cutoff.cutoff,
            cutoff_kind=group.cutoff.cutoff_kind.value,
            status=AttemptStatus.UNAVAILABLE,
            exclusion_reason=ExclusionReason.MISSING_MODEL,
            exclusion_detail="scorer omitted bout",
            prediction=None,
            card_estimator_hash=score.estimator_hash,
            priced_rows=(),
            threshold_only_rows=(),
            pre_policy_candidates=(),
            settlement_facts_present=bout_id in facts_by_bout,
            source_quality={},
        )
    facts = facts_by_bout.get(bout_id)
    priced_rows: list[dict[str, Any]] = []
    threshold_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    event_start = implied_event_start(group.cutoff)
    for market in prediction.markets:
        if not market.available or not market.outcome_key:
            threshold_rows.append(_threshold_row(market))
            continue
        family = MarketFamily(market.family)
        outcome = OutcomeKey(market.outcome_key)
        joined = join_quote(
            quotes,
            bout_id=bout_id,
            family=market.family,
            outcome_key=market.outcome_key,
            line_point=market.line_point,
            cutoff=group.cutoff.cutoff,
        )
        if not joined.priced or joined.quote is None:
            row = _threshold_row(market)
            if joined.reason is not None and joined.reason is not ExclusionReason.THRESHOLD_ONLY:
                row["quote_exclusion"] = joined.reason.value
                row["quote_exclusion_detail"] = joined.detail
            threshold_rows.append(row)
            continue
        settlement = _settle_market(facts, family, outcome, market.line_point)
        priced = _priced_metrics_for_quote(
            quote=joined.quote,
            p50=market.p50,
            family=family,
            outcome=outcome,
            line_point=market.line_point,
            cutoff=group.cutoff.cutoff,
            settlement=settlement,
            event_start=event_start,
        )
        is_candidate = _is_pre_policy_candidate(
            family=family,
            p50=market.p50,
            p25=market.p25,
            offered=joined.quote.price_decimal,
            contract=contract,
        )
        priced["pre_policy_candidate"] = is_candidate
        priced["label"] = "pre_policy_candidate" if is_candidate else "priced_observation"
        priced_rows.append(priced)
        if is_candidate:
            candidates.append(
                {
                    "bout_id": bout_id,
                    "label": "pre_policy_candidate",
                    "market_family": market.family,
                    "outcome_key": market.outcome_key,
                    "offered_decimal": joined.quote.price_decimal,
                    "p50": market.p50,
                    "recommendation": False,
                }
            )
    assert_threshold_only_clean(threshold_rows)
    return BoutAttempt(
        event_id=group.event_id,
        bout_id=bout_id,
        season=group.season,
        series_variant=group.series_variant.value,
        cutoff=group.cutoff.cutoff,
        cutoff_kind=group.cutoff.cutoff_kind.value,
        status=AttemptStatus.PREDICTED,
        exclusion_reason=None,
        exclusion_detail="",
        prediction=prediction,
        card_estimator_hash=score.estimator_hash,
        priced_rows=tuple(priced_rows),
        threshold_only_rows=tuple(threshold_rows),
        pre_policy_candidates=tuple(candidates),
        settlement_facts_present=facts is not None,
        source_quality={"cutoff_kind": group.cutoff.cutoff_kind.value},
    )


def _y_from_facts(facts: BoutSettlementFacts | None) -> int | None:
    if facts is None or facts.result_class != "decisive" or facts.winner_side is None:
        return None
    if facts.winner_side == "a":
        return 1
    if facts.winner_side == "b":
        return 0
    return None


def _observed_atom(facts: BoutSettlementFacts | None) -> str | None:
    if facts is None or facts.winner_side is None or facts.method is None:
        if facts is not None and facts.result_class == "draw":
            return "draw"
        return None
    try:
        method = MethodLabel(facts.method)
    except ValueError:
        return None
    side = WinnerSide.A if facts.winner_side == "a" else WinnerSide.B
    result = ResultClass.DECISIVE if facts.result_class == "decisive" else ResultClass.DRAW
    atom = terminal_atom(result_class=result, method=method, winner_side=side)
    return None if atom is None else atom.value


def _attempts_to_rows(
    attempts: Sequence[BoutAttempt],
    facts_by_bout: Mapping[str, BoutSettlementFacts],
) -> tuple[tuple[AttemptRow, ...], tuple[OutcomeObservation, ...], tuple[PricedBet, ...], int]:
    attempt_rows: list[AttemptRow] = []
    outcomes: list[OutcomeObservation] = []
    bets: list[PricedBet] = []
    for attempt in attempts:
        pred = attempt.prediction
        available = tuple(
            sorted(
                {
                    market.family
                    for market in (pred.markets if pred is not None else ())
                    if market.available
                }
            )
        )
        unavailable = tuple(
            sorted(
                {
                    market.family
                    for market in (pred.markets if pred is not None else ())
                    if not market.available
                }
            )
        )
        attempt_rows.append(
            AttemptRow(
                event_id=attempt.event_id,
                bout_id=attempt.bout_id,
                season=attempt.season,
                series_variant=attempt.series_variant,
                status=attempt.status.value,
                exclusion_reason=(
                    None if attempt.exclusion_reason is None else attempt.exclusion_reason.value
                ),
                predicted=attempt.status is AttemptStatus.PREDICTED,
                abstained=attempt.status is AttemptStatus.ABSTAINED,
                unavailable=attempt.status is AttemptStatus.UNAVAILABLE,
                excluded=attempt.status is AttemptStatus.EXCLUDED,
                locked_not_accessed=(
                    attempt.exclusion_reason is ExclusionReason.LOCKED_NOT_ACCESSED
                ),
                priced=bool(attempt.priced_rows),
                threshold_only=bool(attempt.threshold_only_rows) and not attempt.priced_rows,
                pre_policy_candidate=bool(attempt.pre_policy_candidates),
                markets_available=available,
                markets_unavailable=unavailable,
            )
        )
        if pred is not None:
            facts = facts_by_bout.get(attempt.bout_id)
            outcomes.append(
                OutcomeObservation(
                    event_id=attempt.event_id,
                    bout_id=attempt.bout_id,
                    season=attempt.season,
                    series_variant=attempt.series_variant,
                    y=_y_from_facts(facts),
                    p=pred.p_fighter_a,
                    joint=pred.joint_atoms,
                    observed_atom=_observed_atom(facts),
                    baseline_fifty=pred.baseline_fifty,
                    baseline_rating=pred.baseline_rating,
                    baseline_no_vig=pred.baseline_no_vig,
                    baseline_m1=pred.baseline_m1,
                )
            )
        for row in attempt.priced_rows:
            result = row.get("settlement")
            if result is None:
                settle_enum = SettlementResult.UNRESOLVED
            else:
                settle_enum = SettlementResult(str(result))
            bets.append(
                PricedBet(
                    event_id=attempt.event_id,
                    bout_id=attempt.bout_id,
                    season=attempt.season,
                    series_variant=attempt.series_variant,
                    market_family=str(row["market_family"]),
                    outcome_key=str(row["outcome_key"]),
                    source_kind=str(row.get("source_kind") or "provider_quote"),
                    provider=row.get("provider"),
                    bookmaker_key=row.get("bookmaker_key"),
                    model_prob=float(row.get("model_prob") or 0.5),
                    offered_decimal=float(row["offered_decimal"]),
                    settlement=settle_enum,
                    is_proxy_timestamp=bool(row.get("is_proxy_timestamp")),
                    is_pre_policy_candidate=bool(row.get("pre_policy_candidate")),
                    probability_clv=(
                        None
                        if row.get("probability_clv") is None
                        else float(row["probability_clv"])
                    ),
                    closing_ev=(
                        None if row.get("closing_ev") is None else float(row["closing_ev"])
                    ),
                    expected_value=float(row.get("expected_value") or 0.0),
                )
            )
    n_threshold_bouts = sum(1 for row in attempt_rows if row.threshold_only)
    return tuple(attempt_rows), tuple(outcomes), tuple(bets), n_threshold_bouts


def run_walk_forward(
    *,
    contract: EvaluationContract,
    cards: Sequence[SplitCard],
    scorer: CardScorer,
    quotes: Sequence[QuoteCandidate] = (),
    settlement_facts: Mapping[str, BoutSettlementFacts] | None = None,
    sealed_holdout: bool = False,
    bootstrap_seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    bootstrap_replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    require_target_cards: bool = True,
    generated_at: datetime | None = None,
    extra_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Chronological card walk-forward. 2025 is never used to refit."""
    assert_contract_frozen(contract)
    groups = group_cards(cards, contract)
    if require_target_cards and len(groups) != contract.splits.target_cards:
        raise BacktestError(
            f"universe has {len(groups)} cards, expected {contract.splits.target_cards}"
        )
    data_hash = compute_data_hash(
        [
            {
                "event_id": group.event_id,
                "bout_ids": list(group.bout_ids),
                "cutoff": group.cutoff.cutoff.isoformat(),
            }
            for group in groups
        ]
    )
    config_hash = compute_splits_config_hash(contract)
    feature_hash = current_feature_spec_hash()
    assert_evaluator_hashes(
        contract_hash=contract.content_hash,
        feature_spec_hash=feature_hash,
        data_hash=data_hash,
        config_hash=config_hash,
        expected_data_hash=data_hash,
        expected_config_hash=config_hash,
    )
    plan = outer_folds(
        cards,
        allow_holdout=False,
        contract=contract,
        require_target_cards=require_target_cards,
    )
    folds_by_event = {fold.test_event_id: fold for fold in plan.folds}
    facts = dict(settlement_facts or {})
    attempts: list[BoutAttempt] = []
    holdout_accessed = False
    holdout_accessed_at: str | None = None
    for group in groups:
        fold = folds_by_event.get(group.event_id)
        if group.role is FoldRole.HOLDOUT and not sealed_holdout:
            for bout_id in group.bout_ids:
                attempts.append(_attempt_from_locked(group, bout_id))
            continue
        if group.role is FoldRole.HOLDOUT and sealed_holdout:
            holdout_accessed = True
            holdout_accessed_at = isoformat_utc(datetime.now(UTC))
            if fold is None:
                fold = FoldMetadata(
                    fold_id=f"outer:holdout:{group.event_id}",
                    kind=FoldKind.OUTER,
                    role=FoldRole.HOLDOUT,
                    test_event_id=group.event_id,
                    test_event_ids=(group.event_id,),
                    test_bout_ids=group.bout_ids,
                    cutoff=group.cutoff.cutoff,
                    max_train_timestamp=max(
                        (
                            item.event_start
                            for item in groups
                            if item.event_start < group.cutoff.cutoff
                            and item.role is not FoldRole.HOLDOUT
                        ),
                        default=None,
                    ),
                    train_event_ids=tuple(
                        item.event_id
                        for item in groups
                        if item.event_start < group.cutoff.cutoff
                        and item.role is not FoldRole.HOLDOUT
                    ),
                    series_variant=group.series_variant,
                    in_all_dwcs=group.in_all_dwcs,
                    in_standard_only=group.in_standard_only,
                    locked=True,
                    contract_hash=contract.content_hash,
                    feature_spec_hash=feature_hash,
                    data_hash=data_hash,
                    config_hash=config_hash,
                )
        if fold is None:
            raise BacktestError(f"missing fold metadata for {group.event_id}")
        assert_holdout_not_in_train(fold.train_event_ids)
        score = scorer.score_card(group, fold)
        if score.holdout_in_train:
            raise BacktestError("scorer used 2025 in training")
        assert_holdout_not_in_train(score.train_event_ids)
        hashes = {item.estimator_hash for item in score.predictions}
        if len(hashes) > 1:
            raise BacktestError(
                f"same-card predictions used multiple estimator hashes on {group.event_id}"
            )
        for bout_id in group.bout_ids:
            attempts.append(
                _grade_bout(
                    group=group,
                    bout_id=bout_id,
                    score=score,
                    quotes=quotes,
                    facts_by_bout=facts,
                    contract=contract,
                )
            )
    n_cards = len(groups)
    n_bouts = sum(len(group.bout_ids) for group in groups)
    if len(attempts) != n_bouts:
        raise BacktestError(f"attempt count {len(attempts)} != bout count {n_bouts}")
    attempt_rows, outcomes, bets, n_threshold = _attempts_to_rows(attempts, facts)
    assert_breakdowns_reconcile(attempts=attempt_rows, bets=bets)
    all_metrics = {
        universe.value: {
            "betting": betting_metrics(
                filter_bets(bets, universe),
                n_threshold_only=sum(
                    1
                    for row in filter_attempts(attempt_rows, universe)
                    if row.threshold_only
                ),
            ).to_dict(),
            "outcome": outcome_metrics(filter_outcomes(outcomes, universe)),
            "selection": selection_metrics(filter_attempts(attempt_rows, universe)),
        }
        for universe in UniverseKey
    }
    n_std_cards = sum(1 for group in groups if group.series_variant is SeriesVariant.STANDARD)
    n_br_cards = sum(1 for group in groups if group.series_variant is SeriesVariant.BRAZIL)
    n_std_bouts = sum(
        len(group.bout_ids)
        for group in groups
        if group.series_variant is SeriesVariant.STANDARD
    )
    n_br_bouts = sum(
        len(group.bout_ids)
        for group in groups
        if group.series_variant is SeriesVariant.BRAZIL
    )
    commit, commit_reason = resolve_code_commit()
    code_hash = compute_code_hash(
        extra_paths=[
            Path(__file__),
            Path(__file__).with_name("metrics.py"),
            Path(__file__).with_name("report.py"),
            Path(__file__).with_name("gates.py"),
        ]
    )
    hashes = {
        "calibration": extra_hashes.get("calibration") if extra_hashes else None,
        "code": code_hash,
        "config": config_hash,
        "contract": contract.content_hash,
        "data": data_hash,
        "feature_spec": feature_hash,
        "model": extra_hashes.get("model") if extra_hashes else PINNED_RIDGE_SPEC_HASH,
        "odds": sha256_canonical(
            {
                "quotes": [
                    {
                        "bout_id": item.bout_id,
                        "observed_at": item.observed_at.isoformat(),
                        "quote_id": item.quote_id,
                    }
                    for item in sorted(quotes, key=lambda row: (row.bout_id, row.quote_id))
                ]
            }
        ),
        "policy": "dwcs-306-pre-policy-candidate-not-recommendation",
        "settlement": sha256_canonical(
            {"bout_ids": sorted(facts), "n": len(facts)}
        ),
        "spec": spec_hash(),
    }
    bootstrap = bootstrap_betting_intervals(
        bets,
        seed=bootstrap_seed,
        replicates=bootstrap_replicates,
        n_threshold_only=n_threshold,
    )
    bootstrap["outcome"] = bootstrap_outcome_intervals(
        outcomes, seed=bootstrap_seed, replicates=bootstrap_replicates
    )
    payload: dict[str, Any] = {
        "bootstrap": bootstrap,
        "breakdowns": breakdowns(
            attempts=attempt_rows,
            outcomes=outcomes,
            bets=bets,
            n_threshold_only=n_threshold,
        ),
        "cutoff_policy": CUTOFF_POLICY,
        "evidence": True,
        "git_commit": commit,
        "git_commit_reason": commit_reason,
        "hashes": hashes,
        "holdout": {
            "holdout_accessed": holdout_accessed,
            "holdout_accessed_at": holdout_accessed_at if holdout_accessed else None,
            "sealed_holdout": sealed_holdout,
            "train_includes_2025": False,
        },
        "metric_definitions": metric_definitions(),
        "metrics": all_metrics,
        "n_attempts": len(attempts),
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "ticket": "DWCS-306",
        "universe": {
            "bouts": n_bouts,
            "brazil_bouts": n_br_bouts,
            "brazil_cards": n_br_cards,
            "cards": n_cards,
            "standard_bouts": n_std_bouts,
            "standard_cards": n_std_cards,
        },
        "attempts": [item.card_output_payload() for item in attempts],
        "card_output_hashes": {
            group.event_id: sha256_canonical(
                {
                    "attempts": [
                        item.card_output_payload()
                        for item in attempts
                        if item.event_id == group.event_id
                    ]
                }
            )
            for group in groups
        },
    }
    if generated_at is not None:
        payload["generated_at"] = isoformat_utc(generated_at)
    return attach_content_hash(payload)


def legacy_deprecation_record() -> dict[str, Any]:
    return {
        "deprecated": True,
        "evidence": False,
        "method": LEGACY_BACKTEST_METHOD,
        "note": UNSAFE_EVALUATOR_NOTE,
        "replacement": "mma-model backtest run --contract config/evaluation/dwcs_v1.json "
        "--output output/backtests",
        "ticket": "DWCS-306",
    }


def execute_backtest_run(
    *,
    contract_path: Path,
    output_dir: Path,
    fixture: str | None = None,
    from_manifest: bool = False,
    database_url: str | None = None,
    sealed_holdout: bool = False,
    bootstrap_replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    bootstrap_seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    generated_at: datetime | None = None,
    default_database_url: str | None = None,
    scorer: CardScorer | None = None,
    quotes: Sequence[QuoteCandidate] | None = None,
    settlement_facts: Mapping[str, BoutSettlementFacts] | None = None,
) -> dict[str, Any]:
    """CLI/runtime entry: load universe, run engine, write evidence."""
    contract = load_evaluation_contract(path=contract_path)
    if database_url and fixture == "protocol":
        raise BacktestError("pass --fixture protocol or --database-url, not both")
    engine = None
    try:
        if database_url is not None:
            url = assert_readonly_database_url(
                database_url, default_url=default_database_url
            )
            engine = open_readonly_sqlite_engine(url)
            factory = readonly_session_factory(engine)
            with factory() as session:
                cards = cards_from_session(session)
                snapshot = snapshot_from_session(session)
            require_target = True
            active_scorer: CardScorer = scorer or SnapshotWalkForwardScorer(
                snapshot=snapshot,
                eval_event_ids=frozenset(card.event_id for card in cards),
                contract=contract,
            )
            active_quotes = quotes or ()
            active_facts = settlement_facts or {}
        elif fixture == "protocol":
            cards = protocol_fixture_cards()
            require_target = False
            active_scorer = scorer or ProtocolWalkForwardScorer(contract)
            active_quotes = quotes if quotes is not None else protocol_quotes_from_universe()
            active_facts = (
                dict(settlement_facts)
                if settlement_facts is not None
                else protocol_settlement_facts()
            )
        else:
            cards = cards_from_manifest()
            require_target = True
            active_scorer = scorer or ManifestExclusionScorer()
            active_quotes = quotes or ()
            active_facts = settlement_facts or {}
            _ = from_manifest
        payload = run_walk_forward(
            contract=contract,
            cards=cards,
            scorer=active_scorer,
            quotes=active_quotes,
            settlement_facts=active_facts,
            sealed_holdout=sealed_holdout,
            bootstrap_seed=bootstrap_seed,
            bootstrap_replicates=bootstrap_replicates,
            require_target_cards=require_target,
            generated_at=generated_at,
        )
    except CoverageDatabaseError as exc:
        raise DatabaseMutationError(str(exc)) from exc
    finally:
        if engine is not None:
            engine.dispose()
    paths = write_evidence_files(output_dir, payload)
    return {**payload, "output": paths}
