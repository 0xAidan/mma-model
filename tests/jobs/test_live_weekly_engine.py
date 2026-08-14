"""Live weekly engine: real upcoming-card work vs seam stubs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import CanonicalBout, CanonicalEvent, CanonicalFighter
from mma_model.domain.markets import RecommendationState
from mma_model.dwcs.ids import upcoming_bout_id, upcoming_event_id, upcoming_fighter_id
from mma_model.jobs.discover_live import DiscoverEventPage, persist_from_listing
from mma_model.jobs.handlers import (
    handle_discover,
    handle_identity,
    handle_ingest_history,
    handle_publish,
    handle_score,
)
from mma_model.jobs.types import DueJob, EventContext, JobStatus, JobType
from mma_model.odds.events_for_schedule import load_upcoming_dwcs_events_from_db
from mma_model.publish.builder import build_matchups_document
from mma_model.publish.constants import CURRENT_EVENT_JSON, MATCHUPS_JSON
from mma_model.publish.schema import RecommendationStateView
from mma_model.ufcstats.parsers import EventRow
from tests.publish.helpers import seed_publication

AS_OF = datetime(2026, 8, 14, 15, 0, 0, tzinfo=UTC)
EVENT_START = datetime(2026, 8, 25, 23, 0, 0, tzinfo=UTC)
UFC_EVENT = "ufcstats-dwcs-2026-1"
UFC_FIGHT = "ufcstats-fight-001"
UFC_FA = "ufcstats-fighter-a"
UFC_FB = "ufcstats-fighter-b"


def _open(tmp_path: Path) -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, future=True)(), engine


def _discover_job() -> DueJob:
    return DueJob(
        job_type=JobType.DISCOVER,
        idempotency_key="discover:dwcs:2026-08-14",
        dependencies=(),
        scope="series",
        series="dwcs",
        window_slot="2026-08-14",
    )


def _event_job(job_type: JobType, *, event_id: str, slot: str = "preview") -> DueJob:
    return DueJob(
        job_type=job_type,
        idempotency_key=f"{job_type.value}:{event_id}:{slot}",
        dependencies=(),
        event_id=event_id,
        scope="event",
        series="dwcs",
        window_slot=slot,
    )


def _listing() -> list[EventRow]:
    return [
        EventRow(
            ufcstats_id=UFC_EVENT,
            name="Dana White's Contender Series Week 3",
            date=EVENT_START.replace(tzinfo=None),
            location="Las Vegas, Nevada, USA",
            url=f"http://www.ufcstats.com/event-details/{UFC_EVENT}",
        ),
        EventRow(
            ufcstats_id="ufc-fight-night-other",
            name="UFC Fight Night: Someone vs Other",
            date=EVENT_START.replace(tzinfo=None),
            location="Austin, Texas, USA",
            url="http://www.ufcstats.com/event-details/ufc-fight-night-other",
        ),
    ]


def _pages() -> dict[str, DiscoverEventPage]:
    return {
        UFC_EVENT: DiscoverEventPage(
            event_name="Dana White's Contender Series Week 3",
            date_text="August 25, 2026",
            event_date=EVENT_START,
            location="Las Vegas, Nevada, USA",
            fights=(
                {
                    "external_fight_id": UFC_FIGHT,
                    "fighter_a": {"id": UFC_FA, "name": "Alex Contender"},
                    "fighter_b": {"id": UFC_FB, "name": "Jordan Prospect"},
                },
            ),
        )
    }


def test_run_job_tick_enables_live_preview_publish() -> None:
    text = Path("deploy/run-job.sh").read_text(encoding="utf-8")
    assert "--live" in text
    assert "--publish-root /public" in text


def test_stub_discover_is_not_a_real_card(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        result = handle_discover(
            session,
            job=_discover_job(),
            as_of=AS_OF,
            events=(),
            context={},
        )
        assert result.status is JobStatus.SUCCESS
        assert result.detail.startswith("discover seam")
        assert session.scalar(select(CanonicalEvent)) is None
        assert "Alex Contender" not in (result.detail or "")
    finally:
        session.close()
        engine.dispose()


def test_fixture_discover_publishes_paper_card(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        root = tmp_path / "public"
        result = handle_discover(
            session,
            job=_discover_job(),
            as_of=AS_OF,
            events=(),
            context={
                "discover_listing": _listing(),
                "discover_event_pages": _pages(),
                "publish_root": str(root),
            },
        )
        session.commit()
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("events_written") == 1
        assert result.counts.get("bouts_written") == 1
        event_id = upcoming_event_id(UFC_EVENT)
        event = session.get(CanonicalEvent, event_id)
        assert event is not None
        assert event.name.startswith("Dana White")
        assert event.series == "dwcs"
        assert event.status == "scheduled"
        bout = session.get(CanonicalBout, upcoming_bout_id(UFC_FIGHT))
        assert bout is not None
        fa = session.get(CanonicalFighter, upcoming_fighter_id(UFC_FA))
        fb = session.get(CanonicalFighter, upcoming_fighter_id(UFC_FB))
        assert fa is not None and fa.display_name == "Alex Contender"
        assert fb is not None and fb.display_name == "Jordan Prospect"

        current = json.loads((root / "live" / CURRENT_EVENT_JSON).read_text(encoding="utf-8"))
        matchups = json.loads((root / "live" / MATCHUPS_JSON).read_text(encoding="utf-8"))
        assert current["title"]["presence"] == "known"
        assert "Contender Series" in (current["title"]["value"] or "")
        assert current["event_id"]["value"] == event_id
        assert len(matchups["matchups"]) == 1
        row = matchups["matchups"][0]
        assert row["primary_state"] == RecommendationStateView.NO_BET.value
        assert row["performance_lane"] == "paper"
        names = {fighter["display_name"]["value"] for fighter in row["fighters"]}
        assert names == {"Alex Contender", "Jordan Prospect"}
        assert matchups["no_bet_ids"] == [row["bout_id"]]
        assert matchups["confirmed_value_ranked"] == []
    finally:
        session.close()
        engine.dispose()


def test_upcoming_loader_includes_bout_ids(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        rows = load_upcoming_dwcs_events_from_db(
            session, as_of=AS_OF, horizon=timedelta(days=30)
        )
        assert len(rows) == 1
        assert rows[0]["bout_ids"] == (upcoming_bout_id(UFC_FIGHT),)
        assert rows[0]["name"].startswith("Dana White")
    finally:
        session.close()
        engine.dispose()


def test_ingest_live_empty_db_does_not_use_sample_roster(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        result = handle_ingest_history(
            session,
            job=_discover_job(),
            as_of=AS_OF,
            events=(),
            context={"live": True},
        )
        assert result.status is JobStatus.SUCCESS
        assert result.counts.get("profiles") == 0
        assert "Alex Sample" not in result.detail
        assert "no upcoming" in result.detail
    finally:
        session.close()
        engine.dispose()


def test_identity_blocks_bout_without_source_ids(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        session.add(CanonicalFighter(id="fa", display_name="No Source A"))
        session.add(CanonicalFighter(id="fb", display_name="No Source B"))
        session.add(
            CanonicalEvent(
                id="evt-live",
                name="Dana White's Contender Series",
                series="dwcs",
                status="scheduled",
                scheduled_start_at=EVENT_START,
            )
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="bout-live",
                event_id="evt-live",
                fighter_a_id="fa",
                fighter_b_id="fb",
                status="scheduled",
            )
        )
        session.commit()
        result = handle_identity(
            session,
            job=_event_job(JobType.IDENTITY, event_id="evt-live"),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id="evt-live",
                    event_start=EVENT_START,
                    bout_ids=("bout-live",),
                ),
            ),
            context={"live": True},
        )
        assert result.status is JobStatus.FAILED
        assert result.error_class is not None
        assert result.error_class.value == "identity_unresolved"
        assert result.blocks_downstream is True
        assert result.blocked_bout_ids == ("bout-live",)
    finally:
        session.close()
        engine.dispose()


def test_identity_resolves_persisted_ufcstats_fighters(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        event_id = upcoming_event_id(UFC_EVENT)
        bout_id = upcoming_bout_id(UFC_FIGHT)
        result = handle_identity(
            session,
            job=_event_job(JobType.IDENTITY, event_id=event_id),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id=event_id,
                    event_start=EVENT_START,
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


def test_live_score_fails_closed_without_champion(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        result = handle_score(
            session,
            job=_event_job(JobType.SCORE, event_id="evt-live"),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id="evt-live",
                    event_start=EVENT_START,
                    bout_ids=("bout-live",),
                ),
            ),
            context={"live": True, "require_champion": True},
        )
        assert result.status is JobStatus.FAILED
        assert result.blocks_downstream is True
        assert result.artifact_digest != "incumbent-artifact-v1"
        assert "champion" in result.detail
    finally:
        session.close()
        engine.dispose()


def test_official_publish_still_refuses_empty_ledger(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        event_id = upcoming_event_id(UFC_EVENT)
        bout_id = upcoming_bout_id(UFC_FIGHT)
        result = handle_publish(
            session,
            job=_event_job(JobType.PUBLISH, event_id=event_id, slot="t60"),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id=event_id,
                    event_start=EVENT_START,
                    bout_ids=(bout_id,),
                ),
            ),
            context={"publish_root": str(tmp_path / "official")},
        )
        assert result.status is JobStatus.FAILED
        assert "zero official publications" in result.detail
        assert not (tmp_path / "official" / "live").exists()
    finally:
        session.close()
        engine.dispose()


def test_preview_publish_allowed_without_official_rows(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        event_id = upcoming_event_id(UFC_EVENT)
        bout_id = upcoming_bout_id(UFC_FIGHT)
        root = tmp_path / "preview"
        result = handle_publish(
            session,
            job=_event_job(JobType.PUBLISH, event_id=event_id, slot="preview"),
            as_of=AS_OF,
            events=(
                EventContext(
                    event_id=event_id,
                    event_start=EVENT_START,
                    bout_ids=(bout_id,),
                ),
            ),
            context={"publish_root": str(root), "preview": True},
        )
        assert result.status is JobStatus.SUCCESS
        matchups = json.loads((root / "live" / MATCHUPS_JSON).read_text(encoding="utf-8"))
        assert matchups["matchups"][0]["primary_state"] == "no_bet"
        assert matchups["matchups"][0]["performance_lane"] == "paper"
    finally:
        session.close()
        engine.dispose()


def test_matchups_include_unpublished_scheduled_bouts(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        event_id = upcoming_event_id(UFC_EVENT)
        doc = build_matchups_document(session, event_id=event_id, as_of=AS_OF)
        assert len(doc.matchups) == 1
        assert doc.matchups[0].primary_state is RecommendationStateView.NO_BET
        assert doc.matchups[0].performance_lane.value == "paper"
        assert "Alex Contender" in {
            f.display_name.value for f in doc.matchups[0].fighters
        }
    finally:
        session.close()
        engine.dispose()


def test_golden_publications_unchanged_when_no_canonical_bouts(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-pt",
            state=RecommendationState.PRICE_TARGET,
            with_observed=False,
            performance_lane="paper",
        )
        doc = build_matchups_document(session, event_id="evt-1", as_of=AS_OF)
        assert [row.bout_id for row in doc.matchups] == ["bout-pt"]
        assert doc.matchups[0].primary_state is RecommendationStateView.PRICE_TARGET
    finally:
        session.close()
        engine.dispose()
