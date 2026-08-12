"""CLI identity subcommands (DWCS-104). Temp DB only; no network."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.cli import main
from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.resolver import resolve_fighter

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
FIXED_NOW = datetime(2026, 8, 12, 19, 0, 0, tzinfo=UTC)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture
def db_env(tmp_path: Path):
    db_path = tmp_path / "cli_identity.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with Session() as session:
        fid = canonical_fighter_id("55001")
        session.add(CanonicalFighter(id=fid, display_name="CLI Fighter"))
        session.add(FighterSourceId(fighter_id=fid, source="espn", external_id="55001"))
        session.commit()
        queued = resolve_fighter(
            session,
            source="tapology_public",
            external_id="cli-1",
            display_name="CLI Fighter",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        review_id = queued.review_id
    yield {
        "db_path": db_path,
        "db_url": f"sqlite:///{db_path}",
        "engine": engine,
        "Session": Session,
        "review_id": review_id,
        "canonical_id": canonical_fighter_id("55001"),
    }
    engine.dispose()


def test_identity_audit_list_json_readonly(db_env, capsys) -> None:
    code = main(
        [
            "identity",
            "audit",
            "--database-url",
            db_env["db_url"],
            "--series",
            "dwcs",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert "report_hash" in payload
    assert "pending_reviews" in payload
    assert "canonical_fighter_count" in payload

    code = main(
        [
            "identity",
            "list",
            "--database-url",
            db_env["db_url"],
            "--status",
            "pending",
            "--json",
        ]
    )
    listed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert any(row["id"] == db_env["review_id"] for row in listed["reviews"])


def test_approve_rejects_live_db_without_override(capsys) -> None:
    code = main(
        [
            "identity",
            "approve",
            "--database-url",
            "sqlite:///data/mma.db",
            "--review-id",
            "x",
            "--canonical-id",
            "y",
            "--actor",
            "tester",
        ]
    )
    err = capsys.readouterr().out + capsys.readouterr().err
    assert code == 2
    assert "refusing" in err.lower() or "allow-user-db" in err.lower()


def test_approve_reject_on_temp_db(db_env, capsys) -> None:
    code = main(
        [
            "identity",
            "approve",
            "--database-url",
            db_env["db_url"],
            "--review-id",
            db_env["review_id"],
            "--canonical-id",
            db_env["canonical_id"],
            "--actor",
            "cli-tester",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "approved"

    # Seed another pending for reject.
    with db_env["Session"]() as session:
        queued = resolve_fighter(
            session,
            source="sherdog_public",
            external_id="cli-2",
            display_name="CLI Fighter",
            actor="system",
            now=FIXED_NOW,
        )
        session.commit()
        rid = queued.review_id
    code = main(
        [
            "identity",
            "reject",
            "--database-url",
            db_env["db_url"],
            "--review-id",
            rid,
            "--actor",
            "cli-tester",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "rejected"


def test_cli_identity_subprocess_no_network(db_env) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from mma_model.cli import main; "
                f"raise SystemExit(main(['identity','audit','--database-url',"
                f"'{db_env['db_url']}','--json']))"
            ),
        ],
        cwd=str(REPO_ROOT),
        env={
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PATH": "",
            "HOME": str(db_env["db_path"].parent),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "report_hash" in proc.stdout
