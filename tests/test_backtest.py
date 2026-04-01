"""Walk-forward backtest smoke tests."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.models import Base
from mma_model.predict.backtest import walk_forward_backtest


def test_walk_forward_raises_without_data():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        try:
            walk_forward_backtest(session, min_train_fights=5, min_prior_fights=0)
        except ValueError as e:
            assert "eligible fights" in str(e).lower() or "need" in str(e).lower()
        else:
            raise AssertionError("expected ValueError")
    finally:
        session.close()
