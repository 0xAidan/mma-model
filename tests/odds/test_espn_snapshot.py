"""ESPN public odds snapshot: empty items are unknown, never Bet365."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.odds import OddsAvailabilityObservation, OddsQuote
from mma_model.dwcs.ids import canonical_event_id
from mma_model.jobs.discover_espn import persist_from_espn_events
from mma_model.jobs.handlers import handle_snapshot_odds
from mma_model.jobs.types import DueJob, JobStatus, JobType
from mma_model.odds.bookmaker_keys import is_bet365_bookmaker_key
from mma_model.sources.espn_public.odds import parse_espn_odds
from mma_model.sources.espn_public.parser import parse_espn_scoreboard
from tests.jobs.test_live_weekly_engine import AS_OF

ROOT = Path(__file__).resolve().parents[2]
SCOREBOARD = ROOT / "tests/fixtures/espn/scoreboard_upcoming_v1.json"
EMPTY_ODDS = ROOT / "tests/fixtures/espn/odds_empty_v1.json"
MONEYLINE_ODDS = ROOT / "tests/fixtures/espn/odds_moneyline_v1.json"
COMP_ID = "401903489"


def _open(tmp_path: Path) -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / 'odds.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, future=True)(), engine


def _job() -> DueJob:
    return DueJob(
        job_type=JobType.SNAPSHOT_ODDS,
        idempotency_key="snapshot-odds:dwcs:2026-08-14",
        dependencies=(),
        scope="series",
        series="dwcs",
    )


def test_empty_items_are_unknown_not_a_crash() -> None:
    parsed = parse_espn_odds(json.loads(EMPTY_ODDS.read_text(encoding="utf-8")))
    assert parsed.empty is True
    assert parsed.quotes == ()


def test_moneyline_items_are_not_labeled_bet365() -> None:
    parsed = parse_espn_odds(json.loads(MONEYLINE_ODDS.read_text(encoding="utf-8")))
    assert parsed.empty is False
    assert len(parsed.quotes) == 1
    assert not is_bet365_bookmaker_key(parsed.quotes[0].bookmaker_key)
    assert "bet365" not in parsed.quotes[0].bookmaker_title.lower()


def test_bet365_provider_is_skipped() -> None:
    parsed = parse_espn_odds(
        {
            "items": [
                {
                    "provider": {"id": "bet365", "name": "Bet365"},
                    "homeTeamOdds": {"moneyLine": -120, "athlete": {"id": "1"}},
                    "awayTeamOdds": {"moneyLine": 100, "athlete": {"id": "2"}},
                }
            ]
        }
    )
    assert parsed.empty is True
    assert parsed.skipped_bet365 == 1


def test_live_snapshot_records_unknown_for_empty_items(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_espn_events(
            session,
            events=parse_espn_scoreboard(
                json.loads(SCOREBOARD.read_text(encoding="utf-8"))
            ),
        )
        session.commit()
        result = handle_snapshot_odds(
            session,
            job=_job(),
            as_of=AS_OF,
            events=(),
            context={
                "live": True,
                "espn_odds": {
                    COMP_ID: json.loads(EMPTY_ODDS.read_text(encoding="utf-8"))
                },
            },
        )
        session.commit()
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("unknown") == 1
        assert result.counts.get("quotes") == 0
        row = session.scalar(select(OddsAvailabilityObservation))
        assert row is not None
        assert row.availability == "unknown"
        assert row.provider == "espn_public"
        assert session.scalar(select(OddsQuote)) is None
        assert canonical_event_id("600060733")
    finally:
        session.close()
        engine.dispose()


def test_live_snapshot_stores_public_moneylines(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_espn_events(
            session,
            events=parse_espn_scoreboard(
                json.loads(SCOREBOARD.read_text(encoding="utf-8"))
            ),
        )
        session.commit()
        result = handle_snapshot_odds(
            session,
            job=_job(),
            as_of=AS_OF,
            events=(),
            context={
                "live": True,
                "espn_odds": {
                    COMP_ID: json.loads(MONEYLINE_ODDS.read_text(encoding="utf-8"))
                },
            },
        )
        session.commit()
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("quotes") == 2
        quotes = session.scalars(select(OddsQuote)).all()
        assert len(quotes) == 2
        assert all(row.provider == "espn_public" for row in quotes)
        assert all(not is_bet365_bookmaker_key(row.bookmaker_key) for row in quotes)
    finally:
        session.close()
        engine.dispose()


def test_seam_without_live_stays_offline() -> None:
    # No database: handler must not open ESPN.
    from sqlalchemy.orm import Session as SASession

    class _Boom(SASession):
        def get(self, *args, **kwargs):  # noqa: ANN002
            raise AssertionError("seam must not touch the session")

    result = handle_snapshot_odds(
        _Boom(),  # type: ignore[arg-type]
        job=_job(),
        as_of=AS_OF,
        events=(),
        context={},
    )
    assert result.status is JobStatus.SUCCESS
    assert result.counts.get("snapshot") == "seam"


def test_parse_requires_items() -> None:
    with pytest.raises(Exception, match="items"):
        parse_espn_odds({"count": 0})
