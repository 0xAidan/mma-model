"""Real DWCS-306 walk-forward evidence into DWCS-307 replay."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mma_model.backtest.engine import (
    PrecomputedScorer,
    _eligibility,
    _selection_id,
    run_walk_forward,
)
from mma_model.backtest.report import attach_content_hash
from mma_model.domain.markets import RecommendationState
from mma_model.modeling.uncertainty import EventBlock, event_block_refit_bootstrap
from mma_model.recommend.policy import PRODUCTION_BOOTSTRAP_REFITS, NoBetReason
from mma_model.recommend.replay import execute_recommend_replay
from tests.backtest.helpers import (
    CONTRACT,
    decisive_facts,
    later_dev_card,
    make_prediction,
    make_quote,
    make_score,
    two_bout_dev_card,
)
from tests.recommend.helpers import HASH_A, HASH_B, HASH_C, HASH_D, POLICY

START = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)
CUTOFF = START - timedelta(minutes=60)
OBSERVED = START - timedelta(minutes=90)


def _with_eligibility(quote, *, eligible: bool = True):
    identity = _selection_id(quote.market_family, quote.outcome_key, quote.line_point)
    return replace(
        quote,
        eligible=eligible,
        eligibility_evidence=_eligibility(
            quote,
            selection_identity=identity,
            evaluated_at=CUTOFF,
            eligible=eligible,
            reason="none" if eligible else "blocked",
        ),
    )


def _production_prediction(bout_id: str, event_id: str, *, p_a: float = 0.50, p25: float = 0.40):
    prediction = make_prediction(
        bout_id,
        event_id,
        p_a=p_a,
        p25=p25,
        estimator_hash=HASH_A,
    )
    markets = tuple(
        replace(
            market,
            uncertainty_successful_refits=PRODUCTION_BOOTSTRAP_REFITS,
            uncertainty_seed=307001,
            production_qualified=True,
            prob_ev_positive=0.80,
            estimator_hash=HASH_A,
            calibrator_hash=HASH_B,
            data_hash=HASH_C,
            config_hash=HASH_D,
        )
        if market.available and market.outcome_key
        else market
        for market in prediction.markets
    )
    return replace(
        prediction,
        markets=markets,
        calibrator_hash=HASH_B,
        feature_quality="healthy",
        identity_resolved=True,
        canonical_match=True,
    )


def _run_card(*, quotes, predictions, facts=None):
    scorer = PrecomputedScorer(
        {
            "dev-2017": make_score(
                "dev-2017",
                predictions,
                estimator_hash=HASH_A,
            )
        }
    )
    return run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=scorer,
        quotes=quotes,
        settlement_facts=facts
        or {
            "2017-a": decisive_facts("a"),
            "2017-b": decisive_facts("b"),
        },
        require_target_cards=False,
        bootstrap_replicates=4,
    )


def test_real_walk_forward_replay_confirms_eligible_priced_quote(tmp_path: Path) -> None:
    quotes = (
        _with_eligibility(
            make_quote("2017-a", observed_at=OBSERVED, price=2.60, quote_id=1)
        ),
        _with_eligibility(
            make_quote(
                "2017-b",
                observed_at=OBSERVED,
                price=2.10,
                quote_id=2,
                outcome="fighter_a",
            )
        ),
    )
    payload = _run_card(
        quotes=quotes,
        predictions=(
            _production_prediction("2017-a", "dev-2017"),
            _production_prediction("2017-b", "dev-2017"),
        ),
    )
    attempt = next(item for item in payload["attempts"] if item["bout_id"] == "2017-a")
    priced = attempt["priced_rows"]
    assert priced
    row = next(item for item in priced if item["outcome_key"] == "fighter_a")
    assert row["observed_at"]
    assert row["eligibility_decision_identity"]
    assert row["eligibility_decision_version"]
    assert row["eligibility_evaluated_at"]
    assert row["selection_identity"] == "moneyline:fighter_a"
    assert row["quote_id"] == 1
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    confirmed = [item for item in report["confirmed_value"] if item["bout_id"] == "2017-a"]
    below = [item for item in report["no_bet"] if item["bout_id"] == "2017-b"]
    assert confirmed
    assert confirmed[0]["classification"] == RecommendationState.CONFIRMED_VALUE
    assert below
    assert below[0]["classification"] == RecommendationState.NO_BET
    assert NoBetReason.BELOW_ACTIONABLE.value in below[0]["reasons"]
    assert report["counts"]["confirmed_value"] >= 1
    assert report["counts"]["no_bet"] >= 1
    assert report["priced_policy"]["confirmed_count"] >= 1


def test_stale_and_missing_eligibility_stay_quoted_no_bet(tmp_path: Path) -> None:
    stale = make_quote(
        "2017-a",
        observed_at=OBSERVED,
        price=2.80,
        quote_id=3,
        lifecycle="stale",
        eligible=False,
    )
    missing = make_quote("2017-b", observed_at=OBSERVED, price=2.80, quote_id=4)
    payload = _run_card(
        quotes=(stale, missing),
        predictions=(
            _production_prediction("2017-a", "dev-2017"),
            _production_prediction("2017-b", "dev-2017"),
        ),
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    def _fighter_a(rows: list[dict], bout_id: str) -> dict:
        return next(
            item
            for item in rows
            if item["bout_id"] == bout_id and item["outcome"] == "fighter_a"
        )

    stale_row = _fighter_a(report["no_bet"], "2017-a")
    missing_row = _fighter_a(report["no_bet"], "2017-b")
    assert stale_row["classification"] == RecommendationState.NO_BET
    assert NoBetReason.STALE_LINE.value in stale_row["reasons"]
    assert missing_row["classification"] == RecommendationState.NO_BET
    assert NoBetReason.MISSING_ELIGIBILITY_DECISION.value in missing_row["reasons"]
    assert missing_row["offered_decimal"] == 2.80
    watch_a = [
        item
        for item in report["price_target_watchlist"]
        if item["bout_id"] == "2017-a" and item["outcome"] == "fighter_a"
    ]
    watch_b = [
        item
        for item in report["price_target_watchlist"]
        if item["bout_id"] == "2017-b" and item["outcome"] == "fighter_a"
    ]
    assert not watch_a
    assert not watch_b


def test_metric_bootstrap_cannot_stand_in_for_probability_refits(tmp_path: Path) -> None:
    payload = attach_content_hash(
        {
            "schema_version": "dwcs_backtest_evidence_v1.1",
            "attempts": [
                {
                    "event_id": "dev-2017",
                    "bout_id": "2017-a",
                    "cutoff": CUTOFF.isoformat(),
                    "source_quality": {
                        "feature_quality": "healthy",
                        "identity_resolved": True,
                        "canonical_match": True,
                    },
                    "prediction": {
                        "estimator_hash": HASH_A,
                        "calibrator_hash": HASH_B,
                        "feature_quality": "healthy",
                        "identity_resolved": True,
                        "canonical_match": True,
                        "markets": [
                            {
                                "family": "moneyline",
                                "outcome_key": "fighter_a",
                                "line_point": None,
                                "p50": 0.50,
                                "p25": 0.40,
                                "available": True,
                                "uncertainty_successful_refits": 3,
                                "uncertainty_seed": 307001,
                                "production_qualified": False,
                                "prob_ev_positive": 0.99,
                                "estimator_hash": HASH_A,
                                "calibrator_hash": HASH_B,
                                "data_hash": HASH_C,
                                "config_hash": HASH_D,
                            }
                        ],
                    },
                    "priced_rows": [],
                    "threshold_only_rows": [],
                }
            ],
            "bootstrap": {"n_replicates": 200},
            "hashes": {
                "contract": POLICY.evaluation_contract_hash,
                "data": HASH_C,
                "config": HASH_D,
            },
        }
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    assert not report["confirmed_value"]
    assert not report["price_target_watchlist"]
    assert report["no_bet"]
    assert NoBetReason.NONPRODUCTION_UNCERTAINTY.value in report["no_bet"][0]["reasons"]


def test_injected_200_refits_can_confirm(tmp_path: Path) -> None:
    summary = event_block_refit_bootstrap(
        (
            EventBlock(event_id="e1", samples=(1, 2)),
            EventBlock(event_id="e2", samples=(3,)),
        ),
        refit=lambda bag: 1,
        predict=lambda _fitted: {"t": 0.55},
        n_replicates=PRODUCTION_BOOTSTRAP_REFITS,
        seed=307001,
        estimator_hash=HASH_A,
        config_hash=HASH_D,
        data_hash=HASH_C,
        contract_hash=POLICY.evaluation_contract_hash,
    )
    assert summary.n_successful == PRODUCTION_BOOTSTRAP_REFITS
    assert summary.production_qualified is True
    quotes = (
        _with_eligibility(
            make_quote("2017-a", observed_at=OBSERVED, price=2.60, quote_id=11)
        ),
    )
    prediction = _production_prediction("2017-a", "dev-2017")
    prediction = replace(
        prediction,
        markets=tuple(
            replace(
                market,
                uncertainty_successful_refits=summary.n_successful,
                uncertainty_seed=summary.seed,
                production_qualified=summary.production_qualified,
            )
            if market.available and market.outcome_key
            else market
            for market in prediction.markets
        ),
    )
    payload = _run_card(
        quotes=quotes,
        predictions=(
            prediction,
            _production_prediction("2017-b", "dev-2017"),
        ),
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    assert any(
        item["bout_id"] == "2017-a"
        and item["classification"] == RecommendationState.CONFIRMED_VALUE
        for item in report["confirmed_value"]
    )


def test_user_observed_missing_manual_binding_stays_no_bet(tmp_path: Path) -> None:
    quotes = (
        replace(
            make_quote(
                "2017-a",
                observed_at=OBSERVED,
                price=2.60,
                quote_id=21,
                source_kind="user_observed",
            ),
            fixture_provenance=False,
        ),
    )
    payload = _run_card(
        quotes=quotes,
        predictions=(
            _production_prediction("2017-a", "dev-2017"),
            _production_prediction("2017-b", "dev-2017"),
        ),
    )
    path = tmp_path / "backtest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = execute_recommend_replay(backtest_json=path, policy=POLICY)
    fighter_a = next(
        item
        for item in report["no_bet"]
        if item["bout_id"] == "2017-a" and item["outcome"] == "fighter_a"
    )
    assert fighter_a["classification"] == RecommendationState.NO_BET
    assert NoBetReason.INELIGIBLE_QUOTE.value in fighter_a["reasons"]
    assert not any(
        item["bout_id"] == "2017-a" and item["outcome"] == "fighter_a"
        for item in report["price_target_watchlist"]
    )
