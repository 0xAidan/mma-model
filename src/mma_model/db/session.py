"""Database engine and session factory."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.config import get_settings
from mma_model.db.models import Base


def get_engine():
    settings = get_settings()
    url = settings.mma_database_url
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        parent = __import__("pathlib").Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, echo=False, future=True)


engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
