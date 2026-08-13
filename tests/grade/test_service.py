"""Idempotency, reconstruction, settlement, and audit coverage (DWCS-400)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.recommendations import (
    ObservedPrice,
    OfficialPublication,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.grade.service import (
    StateEventType,
    append_state_event,
    audit_series,
    grade_predictions,
    publish_official_t60,
    quote_content_hash,
    reconstruct_model_identity,
    reconstruct_quote,
    reconstruct_thresholds,
    record_observed_price,
    settle_recommendations,
    thresholds_content_hash,
)
from mma_model.markets.rules import get_rule_set
from mma_model.markets.settlement import BoutSettlementFacts, SettlementResult
from mma_model.recommend.policy import QuoteSourceKind
from tests.grade.helpers import (
    FIXED_CUTOFF,
    FIXED_PUBLISHED,
    REPO_ROOT,
    alembic_config,
    decisive_a_ko,
    open_test_session,
    sample_thresholds,
    seed_model_and_prediction,
    seed_official,
)


def test_official_t60_republish_idempotent_and_line_change_appends(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        first, created = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
        )
        assert created is True
        thresholds_id = first.price_target_id
        assert thresholds_id is not None
        target_before = session.get(PriceTarget, thresholds_id)
        assert target_before is not None
        fair_before = target_before.fair_decimal

        second, created_again = publish_official_t60(
            session,
            event_id="evt-1",
            bout_id="bout-1",
            selection_id="evt-1:bout-1:moneyline:fighter_a",
            state=RecommendationState.PRICE_TARGET,  # would-be overwrite ignored
            cutoff_at=FIXED_CUTOFF,
            published_at=FIXED_PUBLISHED,
            market_family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            prediction_id=prediction.id,
            thresholds=sample_thresholds(),
            model_run_id=run.id,
            performance_lane="paper",
        )
        assert created_again is False
        assert second.id == first.id
        assert second.state == RecommendationState.CONFIRMED_VALUE.value
        assert second.price_target_id == thresholds_id
        target_after = session.get(PriceTarget, thresholds_id)
        assert target_after is not None
        assert target_after.fair_decimal == fair_before

        event, event_created = append_state_event(
            session,
            official_publication_id=first.id,
            event_type=StateEventType.LINE_CHANGE,
            observed_at=FIXED_PUBLISHED + timedelta(minutes=30),
            reason_code="line_moved",
            detail="book shortened",
            payload={"new_decimal": 1.9},
        )
        assert event_created is True
        session.commit()

        assert session.scalar(select(func.count()).select_from(OfficialPublication)) == 1
        assert session.scalar(select(func.count()).select_from(RecommendationStateEvent)) == 1
        refreshed = session.get(OfficialPublication, first.id)
        assert refreshed is not None
        assert refreshed.state == RecommendationState.CONFIRMED_VALUE.value
        assert event.official_publication_id == first.id
    finally:
        session.close()
        engine.dispose()


def test_reconstruction_from_stored_hashes(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
        )
        quote, _ = record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="pinnacle",
            decimal_odds=2.15,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
            region="us",
        )
        session.commit()

        identity = reconstruct_model_identity(session, prediction_id=prediction.id)
        assert identity.artifact_digest == run.artifact_digest
        assert identity.model_hash == run.model_hash
        assert identity.feature_hash == run.feature_hash
        assert identity.config_hash == run.config_hash
        assert identity.data_hash == run.data_hash
        assert identity.cutoff_at == prediction.cutoff_at
        assert identity.published_at == prediction.published_at

        assert pub.price_target_id is not None
        thresholds = reconstruct_thresholds(session, price_target_id=pub.price_target_id)
        expected_hash = thresholds_content_hash(sample_thresholds())
        assert thresholds.thresholds_hash == expected_hash
        assert thresholds.fair_decimal == 2.0
        assert thresholds.actionable_decimal == 2.1

        reconstructed = reconstruct_quote(session, observed_price_id=quote.id)
        assert reconstructed.sportsbook == "pinnacle"
        assert reconstructed.decimal_odds == 2.15
        assert reconstructed.source_type == QuoteSourceKind.AUTOMATIC.value
        assert reconstructed.quote_hash == quote_content_hash(
            sportsbook="pinnacle",
            decimal_odds=2.15,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
            region="us",
        )
    finally:
        session.close()
        engine.dispose()


def test_price_target_only_grades_without_pnl_or_manufactured_price(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.PRICE_TARGET,
            prediction_id=prediction.id,
            model_run_id=run.id,
            performance_lane="experimental",
        )
        grades = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        grade, created = grades[0]
        assert created is True
        assert grade.sporting_result == SettlementResult.WIN.value

        settlements = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        assert settlements == []
        assert session.scalar(select(func.count()).select_from(ObservedPrice)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RecommendationSettlement)) == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_unpriced_confirmed_and_no_bet_have_no_settlement_pnl(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        confirmed, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
            selection_id="evt-1:bout-1:moneyline:fighter_a:confirmed",
        )
        no_bet, _ = publish_official_t60(
            session,
            event_id="evt-1",
            bout_id="bout-1",
            selection_id="evt-1:bout-1:no_bet",
            state=RecommendationState.NO_BET,
            cutoff_at=FIXED_CUTOFF,
            published_at=FIXED_PUBLISHED,
            reasons=("missing_prob_ev_positive",),
            primary_reason="missing_prob_ev_positive",
            model_run_id=run.id,
            performance_lane="paper",
        )
        assert no_bet.price_target_id is None
        out = settle_recommendations(
            session,
            official_publication_ids=[confirmed.id, no_bet.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        assert out == []
        assert session.scalar(select(func.count()).select_from(ObservedPrice)) == 0
        assert (
            session.scalar(select(func.count()).select_from(RecommendationSettlement)) == 0
        )
    finally:
        session.close()
        engine.dispose()


def test_priced_confirmed_value_settles_with_rule_version(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
            performance_lane="qualified",
        )
        quote, _ = record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="bet365",
            decimal_odds=2.5,
            source_type=QuoteSourceKind.USER_OBSERVED,
            source_timestamp=FIXED_PUBLISHED,
        )
        settlements = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
            closing_decimal_by_publication={pub.id: 2.2},
        )
        row, created = settlements[0]
        assert created is True
        assert row.observed_price_id == quote.id
        assert row.settlement_result == SettlementResult.WIN.value
        assert row.rule_set_id
        assert row.rule_set_version
        assert row.rule_content_hash
        assert row.profit == pytest.approx(1.5)
        assert row.roi == pytest.approx(1.5)
        assert row.clv is not None
        assert quote.sportsbook == "bet365"
        assert quote.source_type == QuoteSourceKind.USER_OBSERVED.value
        assert quote.source_timestamp == FIXED_PUBLISHED
    finally:
        session.close()
        engine.dispose()


def test_void_push_draw_nc_cancel_replacement_reason_codes(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        cases = [
            (
                "void-draw",
                BoutSettlementFacts(scheduled_rounds=3, result_class="draw", ending_round=3),
                SettlementResult.VOID,
            ),
            (
                "nc",
                BoutSettlementFacts(scheduled_rounds=3, result_class="no_contest"),
                SettlementResult.VOID,
            ),
            (
                "cancel",
                BoutSettlementFacts(scheduled_rounds=3, cancelled=True),
                SettlementResult.VOID,
            ),
        ]
        for suffix, facts, expected in cases:
            pub, _ = publish_official_t60(
                session,
                event_id="evt-1",
                bout_id="bout-1",
                selection_id=f"evt-1:bout-1:moneyline:fighter_a:{suffix}",
                state=RecommendationState.CONFIRMED_VALUE,
                cutoff_at=FIXED_CUTOFF,
                published_at=FIXED_PUBLISHED,
                market_family=MarketFamily.MONEYLINE,
                outcome_key=OutcomeKey.FIGHTER_A,
                prediction_id=prediction.id,
                thresholds=sample_thresholds(),
                model_run_id=run.id,
            )
            record_observed_price(
                session,
                official_publication_id=pub.id,
                sportsbook="book",
                decimal_odds=2.0,
                source_type=QuoteSourceKind.AUTOMATIC,
                source_timestamp=FIXED_PUBLISHED,
                idempotency_key=f"quote:{suffix}",
            )
            settlements = settle_recommendations(
                session,
                official_publication_ids=[pub.id],
                facts_by_bout={"bout-1": facts},
            )
            row, _ = settlements[0]
            assert row.settlement_result == expected.value
            assert row.reason_code
            assert row.profit == 0.0

        # Push via totals exact-half rule set override (reuse settlement engine).
        base = get_rule_set()
        push_rules = base.model_copy(
            update={
                "totals": base.totals.model_copy(update={"exact_half_result": "push"}),
                "rule_set_id": "test_push_half",
                "version": "test",
            }
        )
        totals_pub, _ = publish_official_t60(
            session,
            event_id="evt-1",
            bout_id="bout-1",
            selection_id="evt-1:bout-1:totals:over:2.5",
            state=RecommendationState.CONFIRMED_VALUE,
            cutoff_at=FIXED_CUTOFF,
            published_at=FIXED_PUBLISHED,
            market_family=MarketFamily.TOTALS,
            outcome_key=OutcomeKey.OVER,
            line_point=2.5,
            prediction_id=prediction.id,
            thresholds=sample_thresholds(),
            model_run_id=run.id,
        )
        record_observed_price(
            session,
            official_publication_id=totals_pub.id,
            sportsbook="book",
            decimal_odds=1.9,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
            idempotency_key="quote:push",
        )
        push_facts = BoutSettlementFacts(
            scheduled_rounds=3,
            result_class="decisive",
            winner_side="a",
            method="ko_tko",
            ending_round=3,
            elapsed_seconds_in_round=150,  # exactly 2.5 rounds at 300s/round
        )
        push_rows = settle_recommendations(
            session,
            official_publication_ids=[totals_pub.id],
            facts_by_bout={"bout-1": push_facts},
            rule_set=push_rules,
        )
        push_row, _ = push_rows[0]
        assert push_row.settlement_result == SettlementResult.PUSH.value
        assert "exact_half_result=push" in push_row.reason_code
        assert push_row.profit == 0.0

        # Replacement invalidates via state event; official row is not deleted.
        old_pub, _ = publish_official_t60(
            session,
            event_id="evt-1",
            bout_id="bout-old",
            selection_id="evt-1:bout-old:moneyline:fighter_a",
            state=RecommendationState.CONFIRMED_VALUE,
            cutoff_at=FIXED_CUTOFF,
            published_at=FIXED_PUBLISHED,
            market_family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            thresholds=sample_thresholds(),
            model_run_id=run.id,
        )
        event, created = append_state_event(
            session,
            official_publication_id=old_pub.id,
            event_type=StateEventType.REPLACEMENT_INVALIDATED,
            observed_at=FIXED_PUBLISHED + timedelta(hours=1),
            reason_code="replacement",
            detail="fighter replaced; new bout identity required",
            payload={"new_bout_id": "bout-new"},
        )
        assert created is True
        assert event.reason_code == "replacement"
        assert session.get(OfficialPublication, old_pub.id) is not None
        assert session.scalar(
            select(func.count())
            .select_from(RecommendationStateEvent)
            .where(
                RecommendationStateEvent.event_type
                == StateEventType.REPLACEMENT_INVALIDATED.value
            )
        ) == 1
    finally:
        session.close()
        engine.dispose()


def test_event_night_settlement_survives_current_correction(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
        )
        record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="bet365",
            decimal_odds=2.0,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
        )
        night = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
            result_version_kind="event_night",
            revision=1,
        )
        night_row, _ = night[0]
        night_profit = night_row.profit
        night_id = night_row.id
        assert night_profit == 1.0

        # Later overturn to no-contest (current revision).
        overturned = BoutSettlementFacts(scheduled_rounds=3, result_class="no_contest")
        current = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": overturned},
            result_version_kind="current",
            revision=1,
        )
        current_row, created = current[0]
        assert created is True
        assert current_row.id != night_id
        assert current_row.settlement_result == SettlementResult.VOID.value
        assert current_row.profit == 0.0

        frozen = session.get(RecommendationSettlement, night_id)
        assert frozen is not None
        assert frozen.profit == night_profit
        assert frozen.result_version_kind == "event_night"
        assert session.scalar(select(func.count()).select_from(RecommendationSettlement)) == 2

        # Same for prediction grades.
        g1 = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
            result_version_kind="event_night",
            revision=1,
        )
        g2 = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": overturned},
            result_version_kind="current",
            revision=1,
        )
        assert g1[0][0].sporting_result == SettlementResult.WIN.value
        assert g2[0][0].sporting_result == SettlementResult.VOID.value
        assert g1[0][0].id != g2[0][0].id
    finally:
        session.close()
        engine.dispose()


def test_repeated_grade_and_settle_are_noop(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
        )
        record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="bet365",
            decimal_odds=2.0,
            source_type=QuoteSourceKind.AUTOMATIC,
            source_timestamp=FIXED_PUBLISHED,
        )
        first_grades = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        first_settles = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        g1, c1 = first_grades[0]
        s1, sc1 = first_settles[0]
        assert c1 is True and sc1 is True

        second_grades = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        second_settles = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        g2, c2 = second_grades[0]
        s2, sc2 = second_settles[0]
        assert c2 is False and sc2 is False
        assert g2.id == g1.id
        assert s2.id == s1.id
        assert session.scalar(select(func.count()).select_from(PredictionGrade)) == 1
        assert (
            session.scalar(select(func.count()).select_from(RecommendationSettlement)) == 1
        )
    finally:
        session.close()
        engine.dispose()


def test_audit_series_and_cli_deterministic(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    command.upgrade(alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    try:
        with Session() as session:
            run, prediction = seed_model_and_prediction(session)
            pub, _ = seed_official(
                session,
                state=RecommendationState.CONFIRMED_VALUE,
                prediction_id=prediction.id,
                model_run_id=run.id,
                performance_lane="qualified",
            )
            record_observed_price(
                session,
                official_publication_id=pub.id,
                sportsbook="bet365",
                decimal_odds=2.0,
                source_type=QuoteSourceKind.AUTOMATIC,
                source_timestamp=FIXED_PUBLISHED,
            )
            grade_predictions(
                session,
                prediction_ids=[prediction.id],
                facts_by_bout={"bout-1": decisive_a_ko()},
            )
            settle_recommendations(
                session,
                official_publication_ids=[pub.id],
                facts_by_bout={"bout-1": decisive_a_ko()},
            )
            # price-target-only should not affect profit totals
            publish_official_t60(
                session,
                event_id="evt-1",
                bout_id="bout-2",
                selection_id="evt-1:bout-2:moneyline:fighter_b",
                state=RecommendationState.PRICE_TARGET,
                cutoff_at=FIXED_CUTOFF,
                published_at=FIXED_PUBLISHED,
                market_family=MarketFamily.MONEYLINE,
                outcome_key=OutcomeKey.FIGHTER_B,
                thresholds=sample_thresholds(),
                model_run_id=run.id,
                performance_lane="experimental",
            )
            session.commit()
            audit = audit_series(session, series="dwcs")
            payload = audit.as_dict()
            assert payload["series"] == "dwcs"
            assert payload["counts"]["official_publications"] == 2
            assert payload["counts"]["state_confirmed_value"] == 1
            assert payload["counts"]["state_price_target"] == 1
            assert "qualified" in payload["performance"]
            assert "paper" in payload["performance"]
            assert "experimental" in payload["performance"]
            assert "model_version" in payload["performance"]
            assert payload["performance"]["qualified"]["profit_sum"] == 1.0
            # experimental price-target has no settlement profit
            assert payload["performance"]["experimental"]["profit_sum"] is None
            first = json.dumps(payload, sort_keys=True)
            second = json.dumps(audit_series(session, series="dwcs").as_dict(), sort_keys=True)
            assert first == second
    finally:
        engine.dispose()

    python = Path(sys.executable)
    cmd = [
        str(python),
        "-m",
        "mma_model.cli",
        "grade",
        "audit",
        "--series",
        "dwcs",
        "--database-url",
        f"sqlite:///{db_path}",
        "--json",
    ]
    env = {**os.environ, "PYTHONPATH": "src"}
    first_cli = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    second_cli = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert first_cli.returncode == 0
    assert first_cli.stdout == second_cli.stdout
    parsed = json.loads(first_cli.stdout)
    assert parsed["series"] == "dwcs"
    assert "performance" in parsed
