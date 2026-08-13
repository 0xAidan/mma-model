"""Shared builders for DWCS-307 recommendation tests."""

from __future__ import annotations

from datetime import UTC, datetime

from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey
from mma_model.domain.quote_eligibility import QUOTE_ELIGIBILITY_DECISION_VERSION
from mma_model.recommend.policy import (
    PRODUCTION_BOOTSTRAP_REFITS,
    ProbabilitySemantics,
    QuoteEvidence,
    QuoteSourceKind,
    RecommendationPolicy,
    SelectionCandidate,
    canonical_selection_id,
    load_recommendation_policy,
)

POLICY = load_recommendation_policy()
CUTOFF = datetime(2024, 8, 13, 1, 0, tzinfo=UTC)
ON_TIME = datetime(2024, 8, 13, 0, 30, tzinfo=UTC)
HASH_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HASH_C = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
HASH_D = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"


def eligible_quote(
    offered: float = 2.60,
    *,
    observed_at: datetime = ON_TIME,
    cutoff: datetime = CUTOFF,
    stale: bool = False,
    suspended: bool = False,
    locked: bool = False,
    replaced: bool = False,
    ambiguous: bool = False,
    eligible: bool = True,
    lifecycle: str = "active",
    availability: str = "available",
    include_decision: bool = True,
    source_kind: QuoteSourceKind = QuoteSourceKind.AUTOMATIC,
) -> QuoteEvidence:
    identity = None
    version = None
    evaluated = None
    if include_decision:
        identity = "qe_v1:" + HASH_A
        version = QUOTE_ELIGIBILITY_DECISION_VERSION
        evaluated = cutoff
    return QuoteEvidence(
        offered_decimal=offered,
        source_kind=source_kind,
        observed_at=observed_at,
        cutoff=cutoff,
        bookmaker_key="test_book",
        region="us",
        eligibility_decision_identity=identity,
        eligibility_decision_version=version,
        eligibility_evaluated_at=evaluated,
        eligible=eligible,
        availability=availability,
        lifecycle=lifecycle,
        freshness_at=observed_at,
        stale=stale,
        suspended=suspended,
        locked=locked,
        replaced=replaced,
        ambiguous=ambiguous,
    )


def make_candidate(
    *,
    event_id: str = "event-1",
    bout_id: str = "bout-1",
    family: MarketFamily = MarketFamily.MONEYLINE,
    outcome: OutcomeKey = OutcomeKey.FIGHTER_A,
    p50: float = 0.50,
    p25: float | None = 0.40,
    quote: QuoteEvidence | None | str = "default",
    line_point: float | None = None,
    bootstrap_successful_count: int | None = PRODUCTION_BOOTSTRAP_REFITS,
    prob_ev_positive: float | None = 0.80,
    identity_resolved: bool = True,
    canonical_match: bool = True,
    ambiguous: bool = False,
    replacement: bool = False,
    data_quality_pass: bool = True,
    model_qualified: bool = True,
    calibrated: bool = True,
    market_maturity: MarketMaturity | None = None,
    probability_semantics: ProbabilitySemantics = ProbabilitySemantics.EXHAUSTIVE,
    p_win_unconditional: float | None = None,
    p_void: float | None = None,
    estimator_hash: str = HASH_A,
    policy: RecommendationPolicy = POLICY,
) -> SelectionCandidate:
    resolved_quote = eligible_quote() if quote == "default" else quote
    maturity = policy.maturity_for(family) if market_maturity is None else market_maturity
    return SelectionCandidate(
        event_id=event_id,
        bout_id=bout_id,
        selection_id=canonical_selection_id(
            event_id=event_id,
            bout_id=bout_id,
            family=family,
            outcome=outcome,
            line_point=line_point,
        ),
        family=family,
        outcome=outcome,
        line_point=line_point,
        p50=p50,
        p25=p25,
        probability_semantics=probability_semantics,
        bootstrap_successful_count=bootstrap_successful_count,
        bootstrap_seed=307001,
        estimator_hash=estimator_hash,
        calibration_hash=HASH_B,
        data_hash=HASH_C,
        config_hash=HASH_D,
        identity_resolved=identity_resolved,
        canonical_match=canonical_match,
        ambiguous=ambiguous,
        replacement=replacement,
        data_quality_pass=data_quality_pass,
        model_qualified=model_qualified,
        calibrated=calibrated,
        market_maturity=maturity,
        p_win_unconditional=p_win_unconditional,
        p_void=p_void,
        evaluation_contract_hash=policy.evaluation_contract_hash,
        quote=resolved_quote,
        prob_ev_positive=prob_ev_positive,
        production_uncertainty=bootstrap_successful_count == PRODUCTION_BOOTSTRAP_REFITS,
    )
