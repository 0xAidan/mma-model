"""Dataset builder smoke tests."""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.models import Base
from mma_model.predict.dataset import build_training_arrays


def test_build_training_empty_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        X, y, ids = build_training_arrays(session)
        assert X.shape[0] == 0
        assert y.shape[0] == 0
        assert ids == []
    finally:
        session.close()
