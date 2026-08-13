"""Append-only enforcement for DWCS-400 ledger tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError, OperationalError

from mma_model.db.grade_guards import LEDGER_TABLES
from mma_model.db.tables.recommendations import (
    ModelRun,
    ObservedPrice,
    OfficialPublication,
    Prediction,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)
from mma_model.domain.markets import RecommendationState
from mma_model.grade.service import (
    append_state_event,
    grade_predictions,
    record_observed_price,
    settle_recommendations,
)
from mma_model.recommend.policy import QuoteSourceKind
from tests.grade.helpers import (
    FIXED_PUBLISHED,
    alembic_config,
    decisive_a_ko,
    open_test_session,
    seed_model_and_prediction,
    seed_official,
)

_ORM_MODELS = (
    ModelRun,
    Prediction,
    PriceTarget,
    OfficialPublication,
    RecommendationStateEvent,
    ObservedPrice,
    PredictionGrade,
    RecommendationSettlement,
)


def test_orm_update_and_delete_rejected_on_every_ledger_table(tmp_path: Path) -> None:
    session, engine = open_test_session(tmp_path)
    try:
        run, prediction = seed_model_and_prediction(session)
        pub, _ = seed_official(
            session,
            state=RecommendationState.CONFIRMED_VALUE,
            prediction_id=prediction.id,
            model_run_id=run.id,
        )
        event, _ = append_state_event(
            session,
            official_publication_id=pub.id,
            event_type="line_change",
            observed_at=FIXED_PUBLISHED,
            reason_code="line_moved",
            detail="later move",
        )
        quote, _ = record_observed_price(
            session,
            official_publication_id=pub.id,
            sportsbook="bet365",
            decimal_odds=2.2,
            source_type=QuoteSourceKind.USER_OBSERVED,
            source_timestamp=FIXED_PUBLISHED,
        )
        grades = grade_predictions(
            session,
            prediction_ids=[prediction.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        grade, _ = grades[0]
        settlements = settle_recommendations(
            session,
            official_publication_ids=[pub.id],
            facts_by_bout={"bout-1": decisive_a_ko()},
        )
        settlement, _ = settlements[0]
        target = session.get(PriceTarget, pub.price_target_id)
        assert target is not None
        session.commit()

        samples = {
            ModelRun: run,
            Prediction: prediction,
            PriceTarget: target,
            OfficialPublication: pub,
            RecommendationStateEvent: event,
            ObservedPrice: quote,
            PredictionGrade: grade,
            RecommendationSettlement: settlement,
        }
        assert set(samples) == set(_ORM_MODELS)

        for model, row in samples.items():
            refreshed = session.get(model, row.id)
            assert refreshed is not None
            before = {
                col.name: getattr(refreshed, col.name) for col in model.__table__.columns
            }
            # Attempt a trivial UPDATE via ORM.
            first_col = next(
                c.name
                for c in model.__table__.columns
                if c.name not in {"id"} and c.type.python_type is str
            )
            setattr(refreshed, first_col, getattr(refreshed, first_col) + "_mut")
            with pytest.raises(
                (OperationalError, IntegrityError, Exception), match="append-only"
            ):
                session.commit()
            session.rollback()
            after = session.get(model, row.id)
            assert after is not None
            for col_name, value in before.items():
                assert getattr(after, col_name) == value

            victim = session.get(model, row.id)
            assert victim is not None
            session.delete(victim)
            with pytest.raises(
                (OperationalError, IntegrityError, Exception), match="append-only"
            ):
                session.commit()
            session.rollback()
            assert session.get(model, row.id) is not None
    finally:
        session.close()
        engine.dispose()


def test_raw_sql_update_delete_rejected_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "grade_mig.db"
    command.upgrade(alembic_config(db_path), "head")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = FIXED_PUBLISHED.isoformat()
        digest = "a" * 64
        conn.execute(
            "INSERT INTO model_runs("
            "id, idempotency_key, series, spec_id, artifact_digest, model_hash, "
            "feature_hash, config_hash, data_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run1", "k1", "dwcs", "ridge", digest, digest, digest, digest, digest, now),
        )
        conn.commit()
        for table in LEDGER_TABLES:
            # Ensure each table exists after migration.
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute("UPDATE model_runs SET spec_id='x' WHERE id='run1'")
            conn.commit()
        conn.rollback()
        with pytest.raises(sqlite3.Error, match="append-only"):
            conn.execute("DELETE FROM model_runs WHERE id='run1'")
            conn.commit()
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0] == 1
        assert (
            conn.execute("SELECT spec_id FROM model_runs WHERE id='run1'").fetchone()[0]
            == "ridge"
        )
    finally:
        conn.close()


def test_migration_installs_ledger_triggers(tmp_path: Path) -> None:
    db_path = tmp_path / "triggers.db"
    command.upgrade(alembic_config(db_path), "head")
    conn = sqlite3.connect(db_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        for table in LEDGER_TABLES:
            assert f"{table}_no_update" in names
            assert f"{table}_no_delete" in names
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(LEDGER_TABLES).issubset(tables)
    finally:
        conn.close()
