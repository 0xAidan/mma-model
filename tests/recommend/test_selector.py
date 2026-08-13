"""Bout selection: one confirmed pick, one primary target, deterministic ranks."""

from __future__ import annotations

import random

from mma_model.domain.markets import OutcomeKey, RecommendationState
from mma_model.recommend.policy import NoBetReason
from mma_model.recommend.selector import select_recommendations
from tests.recommend.helpers import POLICY, eligible_quote, make_candidate


def test_at_most_one_confirmed_under_ties_and_multiple_markets() -> None:
    a = make_candidate(bout_id="bout-tie", outcome=OutcomeKey.FIGHTER_A, quote=eligible_quote(2.60))
    b = make_candidate(bout_id="bout-tie", outcome=OutcomeKey.FIGHTER_B, quote=eligible_quote(2.60))
    report = select_recommendations([b, a], POLICY)
    assert len(report.confirmed_value) == 1
    winner = report.confirmed_value[0]
    assert winner.outcome is OutcomeKey.FIGHTER_A
    demoted = [row for row in report.no_bet if row.bout_id == "bout-tie"]
    assert demoted
    assert all(
        NoBetReason.LOWER_RANKED_ELIGIBLE_SELECTION in row.reasons for row in demoted
    )


def test_exactly_one_primary_unpriced_target() -> None:
    first = make_candidate(
        bout_id="unpriced",
        outcome=OutcomeKey.FIGHTER_A,
        quote=None,
        p50=0.60,
        p25=0.52,
        prob_ev_positive=None,
    )
    second = make_candidate(
        bout_id="unpriced",
        outcome=OutcomeKey.FIGHTER_B,
        quote=None,
        p50=0.40,
        p25=0.35,
        prob_ev_positive=None,
    )
    report = select_recommendations([second, first], POLICY)
    assert len(report.price_target_watchlist) == 2
    primaries = [row for row in report.price_target_watchlist if row.primary_price_target]
    assert len(primaries) == 1
    assert primaries[0].outcome is OutcomeKey.FIGHTER_A
    assert primaries[0].thresholds is not None
    secondary = [row for row in report.price_target_watchlist if not row.primary_price_target]
    assert len(secondary) == 1
    assert NoBetReason.SECONDARY_PRICE_TARGET in secondary[0].reasons
    for row in report.price_target_watchlist:
        payload = row.as_dict()
        assert payload["median_ev"] is None
        assert payload["roi"] is None
        assert payload["clv"] is None
        assert payload["profit"] is None
        assert payload["is_best_available_market"] is False


def test_ranking_is_deterministic_under_shuffled_input() -> None:
    rows = [
        make_candidate(
            event_id="e-a",
            bout_id="b-a",
            outcome=OutcomeKey.FIGHTER_A,
            quote=eligible_quote(2.70),
            p50=0.52,
            p25=0.41,
        ),
        make_candidate(
            event_id="e-a",
            bout_id="b-a",
            outcome=OutcomeKey.FIGHTER_B,
            quote=eligible_quote(2.70),
            p50=0.48,
            p25=0.39,
        ),
        make_candidate(
            event_id="e-b",
            bout_id="b-b",
            quote=None,
            p50=0.55,
            p25=0.50,
            prob_ev_positive=None,
        ),
        make_candidate(
            event_id="e-c",
            bout_id="b-c",
            identity_resolved=False,
            quote=eligible_quote(9.0),
        ),
    ]
    reports = []
    for seed in (1, 2, 3, 4, 5):
        shuffled = list(rows)
        random.Random(seed).shuffle(shuffled)
        reports.append(select_recommendations(shuffled, POLICY))
    assert len({item.content_hash for item in reports}) == 1
    ids = [
        (
            tuple(row.selection_id for row in item.confirmed_value),
            tuple(row.selection_id for row in item.price_target_watchlist),
            tuple(row.selection_id for row in item.no_bet),
        )
        for item in reports
    ]
    assert len(set(ids)) == 1
    report = reports[0]
    assert report.confirmed_value
    assert report.price_target_watchlist
    assert report.no_bet
    assert report.priced_policy["roi"] is None
    assert report.unpriced_target_coverage["unpriced_target_is_not_best_available_market"] is True


def test_quoted_failure_is_no_bet_not_price_target() -> None:
    report = select_recommendations(
        [make_candidate(quote=eligible_quote(2.10), p50=0.50, p25=0.40)],
        POLICY,
    )
    assert not report.confirmed_value
    assert not report.price_target_watchlist
    assert report.no_bet[0].classification is RecommendationState.NO_BET
    assert report.no_bet[0].offered_decimal == 2.10
