"""Migration 0016 unique decision-evidence index: clean path, fail-closed, downgrade."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from mma_model.db.tables.identity import (
    IDENTITY_DECISION_EVIDENCE_INDEX_NAME,
    IDENTITY_DECISION_EVIDENCE_WHERE,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
NOW = datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC).isoformat()
PREV_REVISION = "0015_quote_eligibility_scope"
THIS_REVISION = "0016_identity_decision_evidence_unique"


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _index_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (IDENTITY_DECISION_EVIDENCE_INDEX_NAME,),
    ).fetchone()
    return None if row is None else row[0]


def _insert_review(conn: sqlite3.Connection, review_id: str, *, status: str = "approved") -> None:
    conn.execute(
        "INSERT INTO identity_review_queue("
        "id, status, version, source, external_id, display_name, normalized_name, "
        "candidate_canonical_ids_json, evidence_json, rule_id, resolver_version, "
        "created_at, updated_at, reversible) "
        "VALUES (?, ?, 2, 'tapology_public', ?, 'Fighter', 'fighter', '[]', '{}', "
        "'manual_approve', '1', ?, ?, 1)",
        (review_id, status, f"ext-{review_id}", NOW, NOW),
    )


def _insert_evidence(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    review_id: str,
    action: str,
) -> None:
    conn.execute(
        "INSERT INTO identity_match_evidence("
        "id, created_at, resolver_version, rule_id, action, source, external_id, "
        "display_name, normalized_name, actor, review_id, evidence_json, "
        "reversible, status) "
        "VALUES (?, ?, '1', 'manual_approve', ?, 'tapology_public', ?, "
        "'Fighter', 'fighter', 'tester', ?, '{}', 1, 'active')",
        (evidence_id, NOW, action, f"ext-{review_id}", review_id),
    )


def test_model_and_migration_predicates_match() -> None:
    mig_path = (
        REPO_ROOT
        / "migrations"
        / "versions"
        / "0016_identity_decision_evidence_unique.py"
    )
    text_src = mig_path.read_text(encoding="utf-8")
    assert "IDENTITY_DECISION_EVIDENCE_WHERE" in text_src
    assert "IDENTITY_DECISION_EVIDENCE_INDEX_NAME" in text_src
    assert IDENTITY_DECISION_EVIDENCE_WHERE == (
        "action IN ('approved', 'rejected') AND review_id IS NOT NULL"
    )
    assert IDENTITY_DECISION_EVIDENCE_INDEX_NAME == "uq_identity_evidence_review_decision"


def test_migration_0016_clean_path_creates_index(tmp_path: Path) -> None:
    db_path = tmp_path / "clean.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PREV_REVISION)

    conn = sqlite3.connect(db_path)
    try:
        _insert_review(conn, "rev-clean")
        _insert_evidence(
            conn, evidence_id="ev-1", review_id="rev-clean", action="approved"
        )
        conn.commit()
        assert _index_sql(conn) is None
    finally:
        conn.close()

    command.upgrade(cfg, THIS_REVISION)

    conn = sqlite3.connect(db_path)
    try:
        sql = _index_sql(conn)
        assert sql is not None
        assert IDENTITY_DECISION_EVIDENCE_INDEX_NAME in sql
        assert "UNIQUE" in sql.upper()
        assert "approved" in sql
        assert "rejected" in sql
        assert conn.execute(
            "SELECT COUNT(*) FROM identity_match_evidence WHERE action='approved'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_migration_0016_fails_closed_on_duplicate_decision_evidence(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dup.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, PREV_REVISION)

    conn = sqlite3.connect(db_path)
    try:
        _insert_review(conn, "rev-dup-a")
        _insert_review(conn, "rev-dup-b")
        _insert_evidence(
            conn, evidence_id="ev-a1", review_id="rev-dup-a", action="approved"
        )
        _insert_evidence(
            conn, evidence_id="ev-a2", review_id="rev-dup-a", action="approved"
        )
        _insert_evidence(
            conn, evidence_id="ev-b1", review_id="rev-dup-b", action="rejected"
        )
        _insert_evidence(
            conn, evidence_id="ev-b2", review_id="rev-dup-b", action="rejected"
        )
        _insert_evidence(
            conn, evidence_id="ev-b3", review_id="rev-dup-b", action="rejected"
        )
        # Non-terminal actions must not trip the guard.
        _insert_evidence(
            conn, evidence_id="ev-q", review_id="rev-dup-a", action="queued"
        )
        conn.commit()
        before = conn.execute(
            "SELECT COUNT(*) FROM identity_match_evidence"
        ).fetchone()[0]
    finally:
        conn.close()

    with pytest.raises(Exception) as excinfo:
        command.upgrade(cfg, THIS_REVISION)

    # Alembic may wrap; walk causes for our actionable error text.
    causes: list[BaseException] = [excinfo.value]
    cur: BaseException | None = excinfo.value
    while cur is not None and getattr(cur, "__cause__", None) is not None:
        cur = cur.__cause__
        assert cur is not None
        causes.append(cur)
    joined = " | ".join(str(c) for c in causes)
    assert "Cannot create uq_identity_evidence_review_decision" in joined
    assert "rev-dup-a" in joined
    assert "rev-dup-b" in joined
    assert "count=2" in joined
    assert "count=3" in joined
    assert "not deleted" in joined.lower() or "not rewritten" in joined.lower()

    conn = sqlite3.connect(db_path)
    try:
        assert _index_sql(conn) is None
        after = conn.execute(
            "SELECT COUNT(*) FROM identity_match_evidence"
        ).fetchone()[0]
        assert after == before
        assert after == 6
    finally:
        conn.close()


def test_migration_0016_downgrade_drops_index_and_reupgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "roundtrip.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, THIS_REVISION)

    conn = sqlite3.connect(db_path)
    try:
        assert _index_sql(conn) is not None
    finally:
        conn.close()

    command.downgrade(cfg, PREV_REVISION)
    conn = sqlite3.connect(db_path)
    try:
        assert _index_sql(conn) is None
        _insert_review(conn, "rev-rt")
        _insert_evidence(
            conn, evidence_id="ev-rt", review_id="rev-rt", action="approved"
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, THIS_REVISION)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    try:
        names = {
            idx["name"]
            for idx in inspect(engine).get_indexes("identity_match_evidence")
        }
        assert IDENTITY_DECISION_EVIDENCE_INDEX_NAME in names
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM identity_match_evidence "
                    "WHERE action='approved'"
                )
            ).scalar()
        assert count == 1
    finally:
        engine.dispose()
