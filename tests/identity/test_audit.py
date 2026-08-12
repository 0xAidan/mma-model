"""Deterministic identity audit report tests (DWCS-104)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.audit import build_identity_audit
from mma_model.identity.resolver import resolve_fighter

UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 21, 0, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def test_audit_deterministic_fixed_db_config_hash(env) -> None:
    Session = env["Session"]
    with Session() as session:
        fid = canonical_fighter_id("88001")
        session.add(CanonicalFighter(id=fid, display_name="Audit Fighter"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="88001"))
        session.commit()
        resolve_fighter(
            session,
            source="tapology_public",
            external_id="aud-1",
            display_name="Audit Fighter",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        a = build_identity_audit(session, series="dwcs")
        b = build_identity_audit(session, series="dwcs")
    assert a.report_hash == b.report_hash
    assert a.to_dict() == b.to_dict()
    assert a.canonical_fighter_count == 1
    assert a.exact_espn_mappings == 1
    assert a.unresolved_conflicts >= 1
