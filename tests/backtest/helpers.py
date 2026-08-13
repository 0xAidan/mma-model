"""Shared builders for DWCS-306 walk-forward tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from mma_model.backtest.engine import (
    BoutPrediction,
    CardScore,
    MarketPrediction,
    QuoteCandidate,
    moneyline_markets,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import load_evaluation_contract
from mma_model.markets.settlement import BoutSettlementFacts
from mma_model.modeling.splits import SplitCard

CONTRACT = load_evaluation_contract()


def make_card(
    event_id: str,
    start: datetime,
    bout_ids: tuple[str, ...],
    *,
    variant: SeriesVariant = SeriesVariant.STANDARD,
) -> SplitCard:
    return SplitCard(
        event_id=event_id,
        scheduled_start_at=start,
        event_date=start.date(),
        series_variant=variant,
        bout_ids=bout_ids,
    )


def two_bout_dev_card() -> SplitCard:
    return make_card(
        "dev-2017",
        datetime(2017, 7, 11, 19, 0, tzinfo=UTC),
        ("2017-a", "2017-b"),
    )


def later_dev_card() -> SplitCard:
    return make_card(
        "dev-2023",
        datetime(2023, 8, 22, 2, 0, tzinfo=UTC),
        ("2023-a",),
    )


def val_card() -> SplitCard:
    return make_card(
        "val-2024",
        datetime(2024, 8, 13, 2, 0, tzinfo=UTC),
        ("2024-a",),
    )


def hold_card() -> SplitCard:
    return make_card(
        "hold-2025",
        datetime(2025, 8, 12, 2, 0, tzinfo=UTC),
        ("2025-a",),
    )


def brazil_card() -> SplitCard:
    return make_card(
        "brazil-2018",
        datetime(2018, 8, 11, 1, 0, tzinfo=UTC),
        ("br-a",),
        variant=SeriesVariant.BRAZIL,
    )


def small_universe() -> tuple[SplitCard, ...]:
    return (two_bout_dev_card(), brazil_card(), later_dev_card(), val_card(), hold_card())


def make_prediction(
    bout_id: str,
    event_id: str,
    *,
    p_a: float = 0.62,
    p25: float | None = 0.55,
    estimator_hash: str = "est-1",
    train_event_ids: tuple[str, ...] = (),
    max_train_timestamp: datetime | None = None,
    joint_atoms: dict[str, float] | None = None,
    markets: tuple[MarketPrediction, ...] | None = None,
) -> BoutPrediction:
    p_a = min(max(p_a, 1e-15), 1.0 - 1e-15)
    p_b = 1.0 - p_a
    if markets is None:
        markets = moneyline_markets(
            p_a=p_a,
            p_b=p_b,
            p_draw=0.0,
            p25=p25,
            p75=None if p25 is None else min(0.99, float(p25) + 0.08),
            fallback_reason="m1_moneyline_fallback",
        )
    return BoutPrediction(
        bout_id=bout_id,
        event_id=event_id,
        model_id="M1",
        p_fighter_a=p_a,
        p_fighter_b=p_b,
        p_draw=0.0,
        p50=p_a,
        p25=p25,
        joint_atoms=joint_atoms,
        markets=markets,
        estimator_hash=estimator_hash,
        calibrator_hash=None,
        train_event_ids=train_event_ids,
        max_train_timestamp=max_train_timestamp,
        baseline_fifty=0.5,
        baseline_rating=0.5,
        baseline_no_vig=None,
        baseline_m1=p_a,
    )


def make_score(
    event_id: str,
    predictions: tuple[BoutPrediction, ...],
    *,
    estimator_hash: str = "est-1",
    train_event_ids: tuple[str, ...] = (),
    max_train_timestamp: datetime | None = None,
) -> CardScore:
    return CardScore(
        event_id=event_id,
        estimator_hash=estimator_hash,
        train_event_ids=train_event_ids,
        max_train_timestamp=max_train_timestamp,
        holdout_in_train=False,
        predictions=predictions,
    )


def make_quote(
    bout_id: str,
    *,
    price: float = 2.10,
    observed_at: datetime,
    quote_id: int = 1,
    outcome: str = OutcomeKey.FIGHTER_A.value,
    family: str = MarketFamily.MONEYLINE.value,
    line_point: float | None = None,
    lifecycle: str = "active",
    eligible: bool = True,
    availability: str = "available",
    is_proxy: bool = False,
    is_replacement: bool = False,
    is_ambiguous: bool = False,
    close_price: float | None = 1.95,
    close_at: datetime | None = None,
    bookmaker_key: str = "ref_book",
    provider: str = "the_odds_api",
    source_kind: str = "provider_quote",
) -> QuoteCandidate:
    closing_at = close_at
    if closing_at is None and close_price is not None:
        closing_at = observed_at + timedelta(minutes=20)
    return QuoteCandidate(
        bout_id=bout_id,
        market_family=family,
        outcome_key=outcome,
        line_point=line_point,
        price_decimal=price,
        observed_at=observed_at,
        bookmaker_key=bookmaker_key,
        provider=provider,
        region="us",
        quote_id=quote_id,
        availability=availability,
        lifecycle=lifecycle,
        eligible=eligible,
        eligibility_reason="none" if eligible else "blocked",
        is_proxy_timestamp=is_proxy,
        is_replacement=is_replacement,
        is_ambiguous=is_ambiguous,
        source_kind=source_kind,
        closing_price_decimal=close_price,
        closing_observed_at=closing_at,
        closing_bookmaker_key=bookmaker_key,
        closing_quote_id=None if close_price is None else quote_id + 500,
        fixture_provenance=True,
        historical_evidence=False,
    )


def decisive_facts(winner: str = "a") -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="decisive",
        winner_side=winner,  # type: ignore[arg-type]
        method="decision",
        ending_round=3,
        elapsed_seconds_in_round=300,
    )


def draw_facts() -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="draw",
        ending_round=3,
        elapsed_seconds_in_round=300,
    )


def nc_facts() -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=3,
        result_class="no_contest",
    )


def cancelled_facts() -> BoutSettlementFacts:
    return BoutSettlementFacts(
        scheduled_rounds=3,
        cancelled=True,
    )


def scores_for_small_universe(
    *,
    include_holdout: bool = False,
) -> dict[str, CardScore]:
    payload: dict[str, CardScore] = {
        "dev-2017": make_score(
            "dev-2017",
            (
                make_prediction("2017-a", "dev-2017", p_a=0.61, estimator_hash="e2017"),
                make_prediction("2017-b", "dev-2017", p_a=0.58, estimator_hash="e2017"),
            ),
            estimator_hash="e2017",
        ),
        "brazil-2018": make_score(
            "brazil-2018",
            (make_prediction("br-a", "brazil-2018", p_a=0.57, estimator_hash="e2018"),),
            estimator_hash="e2018",
        ),
        "dev-2023": make_score(
            "dev-2023",
            (make_prediction("2023-a", "dev-2023", p_a=0.66, estimator_hash="e2023"),),
            estimator_hash="e2023",
        ),
        "val-2024": make_score(
            "val-2024",
            (make_prediction("2024-a", "val-2024", p_a=0.54, estimator_hash="e2024"),),
            estimator_hash="e2024",
        ),
    }
    if include_holdout:
        payload["hold-2025"] = make_score(
            "hold-2025",
            (make_prediction("2025-a", "hold-2025", p_a=0.52, estimator_hash="e2025"),),
            estimator_hash="e2025",
        )
    return payload


def quote_before_cutoff(bout_id: str, start: datetime, **kwargs: Any) -> QuoteCandidate:
    cutoff = start - timedelta(minutes=60)
    observed = cutoff - timedelta(minutes=15)
    return make_quote(bout_id, observed_at=observed, **kwargs)
