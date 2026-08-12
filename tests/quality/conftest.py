"""Shared DWCS-106 fixtures. Disposable temp DBs only; never the live user DB."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.cli import main
from mma_model.db.session import _attach_sqlite_listeners


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    root = tmp_path_factory.mktemp("dwcs106-pop")
    db_path = root / "phase1.db"
    raw = root / "raw"
    url = f"sqlite:///{db_path}"
    code = main(
        [
            "dwcs",
            "sync-history",
            "--through",
            "2025",
            "--database-url",
            url,
            "--raw-store",
            str(raw),
            "--json",
        ]
    )
    assert code == 0
    engine = create_engine(url, future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"db_url": url, "engine": engine, "Session": Session, "db_path": db_path}
    engine.dispose()
