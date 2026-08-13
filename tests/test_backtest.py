"""Legacy walk-forward wrapper is callable and is not betting evidence."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.models import Base
from mma_model.predict.backtest import walk_forward_backtest


def test_walk_forward_is_callable_without_running_unsafe_evaluator() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        out = walk_forward_backtest(session, min_train_fights=5, min_prior_fights=0)
        assert out["evidence"] is False
        assert out["unsafe_evaluator_ran"] is False
        assert out["same_card_leakage"] is False
        assert out["legacy_args"]["session_provided"] is True
        assert out["method"] == "disabled_unsafe_fight_by_fight"
    finally:
        session.close()
