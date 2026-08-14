"""Golden fixtures and builder projection coverage (DWCS-500)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mma_model.domain.markets import RecommendationState
from mma_model.grade.service import StateEventType, append_state_event
from mma_model.observability.publish_guard import PublishValidationError
from mma_model.publish.builder import (
    _line_movement_and_warnings,
    _movement_from_payload,
    build_matchups_document,
    build_release_files,
)
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
        assert cv.prices.price_availability is PriceAvailability.AVAILABLE
        assert cv.maturity.value == "qualified"
        assert cv.reason_plain
        assert cv.prices.fair_or_better
        assert cv.prices.actionable_or_better
        assert cv.prices.strong_value_or_better
        assert len(cv.markets) >= 1
        assert sum(1 for m in cv.markets if m.is_primary) == 1
        assert "bout-cv" in matchups.confirmed_value_ranked

        pt = by_id["bout-pt"]
        assert pt.primary_state is RecommendationStateView.PRICE_TARGET
        assert pt.prices.exact_ev is None
        assert pt.prices.observed is None
        assert pt.prices.price_availability is PriceAvailability.UNAVAILABLE
        assert "bout-pt" in matchups.price_target_watchlist

        nb = by_id["bout-nb"]
        assert nb.primary_state is RecommendationStateView.NO_BET
        assert "bout-nb" in matchups.no_bet_ids

        stale = by_id["bout-stale"]
        assert stale.prices.price_availability is PriceAvailability.STALE
        assert stale.prices.line_freshness.value == "stale"

        repl = by_id["bout-repl"]
        assert repl.card_change_warnings
        assert "replacement" in repl.card_change_warnings[0].event_type

        unavail = by_id["bout-unavail"]
        assert unavail.prices.price_availability is PriceAvailability.UNAVAILABLE
        assert unavail.prices.exact_ev is None

        # Watchlist is deterministic by bout_id / selection_id.
        assert matchups.price_target_watchlist == tuple(
            sorted(matchups.price_target_watchlist)
        )

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
            validate_document(name, json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    finally:
        session.close()
        engine.dispose()


def test_confirmed_value_without_observed_fails_closed(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-cv-no-obs",
            state=RecommendationState.CONFIRMED_VALUE,
            with_observed=False,
        )
        with pytest.raises(PublishValidationError, match="observed price"):
            build_matchups_document(session, event_id="evt-1", as_of=FIXED_PUBLISHED)
        with pytest.raises(PublishValidationError, match="observed price"):
            build_release_files(
                session,
                release_id="should-fail",
                event_id="evt-1",
                as_of=FIXED_PUBLISHED,
            )
    finally:
        session.close()
        engine.dispose()


def test_replacement_invalidated_warning(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        pub = seed_publication(
            session,
            bout_id="bout-repl-invalidated",
            state=RecommendationState.NO_BET,
            with_observed=False,
        )
        append_state_event(
            session,
            official_publication_id=pub.id,
            event_type=StateEventType.REPLACEMENT_INVALIDATED,
            observed_at=FIXED_PUBLISHED,
            reason_code="replacement_invalidated",
            detail="bout replaced before T-60",
            payload={"bout_id": "bout-repl-invalidated"},
            idempotency_key=f"state:{pub.id}:replacement_invalidated",
        )
        session.commit()
        matchups = build_matchups_document(
            session, event_id="evt-1", as_of=FIXED_PUBLISHED
        )
        row = matchups.matchups[0]
        assert row.card_change_warnings
        assert row.card_change_warnings[0].event_type == "replacement_invalidated"
    finally:
        session.close()
        engine.dispose()


def test_line_movement_is_delta_not_absolute() -> None:
    assert _movement_from_payload({"delta": 0.15}) == pytest.approx(0.15)
    assert _movement_from_payload({"new_decimal": 2.6, "old_decimal": 2.4}) == pytest.approx(
        0.2
    )
    assert _movement_from_payload({"new_decimal": 2.6}) is None
    assert _movement_from_payload({"decimal_odds": 2.6}) is None


def test_line_movement_from_state_events(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        pub = seed_publication(
            session,
            bout_id="bout-move",
            state=RecommendationState.PRICE_TARGET,
        )
        append_state_event(
            session,
            official_publication_id=pub.id,
            event_type="line_change",
            observed_at=FIXED_PUBLISHED,
            reason_code="line_moved",
            detail="post T-60 drift",
            payload={"new_decimal": 2.6, "old_decimal": 2.4},
            idempotency_key=f"state:{pub.id}:line_change",
        )
        session.commit()
        movement, _freshness, _warnings = _line_movement_and_warnings(session, pub.id)
        assert movement == pytest.approx(0.2)
        matchups = build_matchups_document(
            session, event_id="evt-1", as_of=FIXED_PUBLISHED
        )
        assert matchups.matchups[0].prices.line_movement == pytest.approx(0.2)
    finally:
        session.close()
        engine.dispose()
