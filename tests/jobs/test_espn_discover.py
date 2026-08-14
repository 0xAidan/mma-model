"""ESPN discover fallback, identity, and ingest fail-soft."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    EventSourceId,
)
from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id, canonical_fighter_id
from mma_model.jobs.discover_espn import persist_from_espn_events
from mma_model.jobs.handlers import handle_discover, handle_identity, handle_ingest_history
from mma_model.jobs.types import DueJob, EventContext, JobStatus, JobType
from mma_model.sources.espn_public.parser import ESPN_IDENTITY_SOURCE, parse_espn_scoreboard
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.publish.constants import CURRENT_EVENT_JSON, MATCHUPS_JSON
from tests.jobs.test_live_weekly_engine import AS_OF, _discover_job, _event_job

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/espn/scoreboard_upcoming_v1.json"
WEEK2_EVENT = "600060733"
WEEK2_BOUT = "401903489"
WEEK2_FA = "5307810"
WEEK2_FB = "5307811"


def _open(tmp_path: Path) -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / 'espn.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, future=True)(), engine


def _scoreboard() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_persist_uses_canonical_espn_ids(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        written = persist_from_espn_events(
            session, events=parse_espn_scoreboard(_scoreboard())
        )
        session.commit()
        assert written.events_written == 1
        assert written.bouts_written == 1
        event_id = canonical_event_id(WEEK2_EVENT)
        bout_id = canonical_bout_id(WEEK2_BOUT)
        assert written.event_ids == [event_id]
        event = session.get(CanonicalEvent, event_id)
        assert event is not None
        assert event.status == "scheduled"
        assert "Week 2" in event.name
        assert session.get(CanonicalBout, bout_id) is not None
        assert session.get(CanonicalFighter, canonical_fighter_id(WEEK2_FA)) is not None
        assert session.scalar(
            select(EventSourceId).where(
                EventSourceId.source == ESPN_IDENTITY_SOURCE,
                EventSourceId.external_id == WEEK2_EVENT,
            )
        )
        assert session.scalar(
            select(BoutSourceId).where(
                BoutSourceId.source == ESPN_IDENTITY_SOURCE,
                BoutSourceId.external_id == WEEK2_BOUT,
            )
        )
        names = {
            session.get(CanonicalFighter, canonical_fighter_id(WEEK2_FA)).display_name,
            session.get(CanonicalFighter, canonical_fighter_id(WEEK2_FB)).display_name,
        }
        assert names == {"Alex Contender", "Jordan Prospect"}
        assert session.scalar(select(CanonicalEvent).where(CanonicalEvent.name.contains("Week 1"))) is None
        assert session.scalar(
            select(CanonicalEvent).where(CanonicalEvent.name.contains("Fight Night"))
        ) is None
    finally:
        session.close()
        engine.dispose()


def test_ufcstats_blocked_uses_espn_fixture(tmp_path: Path, monkeypatch) -> None:
    def _boom(*, cache_dir, robots_disallow=False):
        raise SourceBlockedError(
            "cloudflare_challenge", host="ufcstats.com", status_code=200
        )

    monkeypatch.setattr(
        "mma_model.jobs.live_engine.fetch_live_listing_and_pages", _boom
    )
    session, engine = _open(tmp_path)
    try:
        root = tmp_path / "public"
        result = handle_discover(
            session,
            job=_discover_job(),
            as_of=AS_OF,
            events=(),
            context={
                "live": True,
                "espn_scoreboard": _scoreboard(),
                "publish_root": str(root),
            },
        )
        session.commit()
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("events_written") == 1
        event_id = canonical_event_id(WEEK2_EVENT)
        matchups = json.loads((root / "live" / MATCHUPS_JSON).read_text(encoding="utf-8"))
        assert matchups["matchups"][0]["primary_state"] == "no_bet"
        assert matchups["matchups"][0]["performance_lane"] == "paper"
        names = {
            fighter["display_name"]["value"]
            for fighter in matchups["matchups"][0]["fighters"]
        }
        assert names == {"Alex Contender", "Jordan Prospect"}
        current = json.loads((root / "live" / CURRENT_EVENT_JSON).read_text(encoding="utf-8"))
        assert current["event_id"]["value"] == event_id
    finally:
        session.close()
        engine.dispose()


def test_both_sources_blocked_fails_closed(tmp_path: Path, monkeypatch) -> None:
    def _ufc_boom(*, cache_dir, robots_disallow=False):
        raise SourceBlockedError("cloudflare_challenge", host="ufcstats.com", status_code=200)

    def _espn_boom(context, *, cache_dir, as_of):
        raise SourceBlockedError("http_403", host="site.api.espn.com", status_code=403)

    monkeypatch.setattr(
        "mma_model.jobs.live_engine.fetch_live_listing_and_pages", _ufc_boom
    )
    monkeypatch.setattr(
        "mma_model.jobs.live_engine.upcoming_from_espn_context", _espn_boom
    )
    session, engine = _open(tmp_path)
    try:
        result = handle_discover(
            session,
            job=_discover_job(),
            as_of=AS_OF,
            events=(),
            context={"live": True},
        )
        assert result.status is JobStatus.FAILED
        assert result.blocks_downstream is True
        assert session.scalar(select(CanonicalEvent)) is None
    finally:
        session.close()
        engine.dispose()


def test_identity_resolves_espn_fighters(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_espn_events(session, events=parse_espn_scoreboard(_scoreboard()))
        session.commit()
        event_id = canonical_event_id(WEEK2_EVENT)
        bout_id = canonical_bout_id(WEEK2_BOUT)
        result = handle_identity(
            session,
            job=_event_job(JobType.IDENTITY, event_id=event_id),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id=event_id,
                    event_start=datetime(2026, 8, 18, 23, 0, tzinfo=UTC),
                    bout_ids=(bout_id,),
                ),
            ),
            context={"live": True},
        )
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("resolved") == 1
        assert result.blocked_bout_ids == ()
    finally:
        session.close()
        engine.dispose()


def test_ingest_history_block_is_fail_soft(tmp_path: Path, monkeypatch) -> None:
    persist_session, engine = _open(tmp_path)
    try:
        persist_from_espn_events(
            persist_session, events=parse_espn_scoreboard(_scoreboard())
        )
        persist_session.commit()

        def _boom(**kwargs):
            raise SourceBlockedError("http_403", host="tapology.com", status_code=403)

        monkeypatch.setattr("mma_model.jobs.live_engine.sync_regional_history", _boom)
        result = handle_ingest_history(
            persist_session,
            job=DueJob(
                job_type=JobType.INGEST_HISTORY,
                idempotency_key="ingest:dwcs:2026-08-14",
                dependencies=(),
                scope="series",
                series="dwcs",
            ),
            as_of=AS_OF,
            events=(),
            context={
                "live": True,
                "allow_live_http": True,
                "history_clients": {"tapology_public": object()},
            },
        )
        assert result.status is JobStatus.SUCCESS
        assert result.blocks_downstream is False
        assert result.counts.get("histories") == 0
        assert "card still valid" in result.detail
    finally:
        persist_session.close()
        engine.dispose()
