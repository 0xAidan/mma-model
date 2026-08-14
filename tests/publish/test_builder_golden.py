"""Golden fixtures and builder projection coverage (DWCS-500)."""

from __future__ import annotations

import json
from pathlib import Path

from mma_model.domain.markets import RecommendationState
from mma_model.publish.builder import build_matchups_document, build_release_files
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES
from mma_model.publish.schema import (
    PriceAvailability,
    RecommendationStateView,
    validate_document,
)
from tests.publish.helpers import (
    FIXED_PUBLISHED,
    open_publish_session,
    seed_publication,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_golden_states_and_lanes(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-cv",
            state=RecommendationState.CONFIRMED_VALUE,
            with_observed=True,
            observed_decimal=2.4,
            performance_lane="qualified",
        )
        seed_publication(
            session,
            bout_id="bout-pt",
            state=RecommendationState.PRICE_TARGET,
            with_observed=False,
            performance_lane="paper",
        )
        seed_publication(
            session,
            bout_id="bout-nb",
            state=RecommendationState.NO_BET,
            with_observed=False,
            performance_lane="experimental",
        )
        seed_publication(
            session,
            bout_id="bout-stale",
            state=RecommendationState.PRICE_TARGET,
            with_observed=False,
            add_stale_line=True,
            performance_lane="paper",
        )
        seed_publication(
            session,
            bout_id="bout-repl",
            state=RecommendationState.NO_BET,
            with_observed=False,
            add_replacement_warning=True,
            performance_lane="paper",
        )
        seed_publication(
            session,
            bout_id="bout-unavail",
            state=RecommendationState.PRICE_TARGET,
            with_observed=False,
            performance_lane="paper",
        )

        matchups = build_matchups_document(
            session, event_id="evt-1", as_of=FIXED_PUBLISHED
        )
        by_id = {row.bout_id: row for row in matchups.matchups}

        cv = by_id["bout-cv"]
        assert cv.primary_state is RecommendationStateView.CONFIRMED_VALUE
        assert cv.prices.observed is not None
        assert cv.prices.exact_ev is not None
        assert cv.performance_lane.value == "qualified"
        assert "bout-cv" in matchups.confirmed_value_ranked

        pt = by_id["bout-pt"]
        assert pt.primary_state is RecommendationStateView.PRICE_TARGET
        assert pt.prices.exact_ev is None
        assert pt.prices.observed is None
        assert "bout-pt" in matchups.price_target_watchlist

        nb = by_id["bout-nb"]
        assert nb.primary_state is RecommendationStateView.NO_BET
        assert "bout-nb" in matchups.no_bet_ids

        stale = by_id["bout-stale"]
        assert stale.prices.price_availability is PriceAvailability.STALE
        assert stale.prices.line_freshness.value == "stale"

        repl = by_id["bout-repl"]
        assert repl.card_change_warnings
        assert repl.card_change_warnings[0].event_type == "replacement"

        unavail = by_id["bout-unavail"]
        assert unavail.prices.price_availability in {
            PriceAvailability.AVAILABLE,
            PriceAvailability.UNAVAILABLE,
        }
        # No observed price → no exact EV.
        assert unavail.prices.exact_ev is None

        files = build_release_files(
            session,
            release_id="golden-v1",
            event_id="evt-1",
            window_slot="t60",
            as_of=FIXED_PUBLISHED,
        )
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name in DASHBOARD_RELEASE_FILES:
            payload = json.loads(files[name])
            validate_document(name, payload)
            (FIXTURES / name).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            # v1 fixture still validates (backward-compat check).
            validate_document(name, json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    finally:
        session.close()
        engine.dispose()


def test_confirmed_value_demoted_without_observed(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-cv-no-obs",
            state=RecommendationState.CONFIRMED_VALUE,
            with_observed=False,
        )
        matchups = build_matchups_document(session, event_id="evt-1")
        row = matchups.matchups[0]
        assert row.primary_state is RecommendationStateView.PRICE_TARGET
        assert row.prices.exact_ev is None
        assert matchups.confirmed_value_ranked == ()
    finally:
        session.close()
        engine.dispose()
