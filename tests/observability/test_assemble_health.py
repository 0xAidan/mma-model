"""Production health assembly: honest probes, never a hardcoded green report."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from mma_model.cli import main
from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.jobs.discover_live import persist_from_listing
from mma_model.jobs.handlers import handle_discover
from mma_model.observability.assemble import assemble_health
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    HealthSeverity,
    HealthStatus,
    validate_health_json,
)
from mma_model.odds.types import REQUESTS_LAST_SOURCE_PROVIDER
from mma_model.publish.constants import HEALTH_JSON
from tests.jobs.test_live_weekly_engine import (
    AS_OF,
    _discover_job,
    _listing,
    _pages,
)

NOT_PROBED = "not yet probed"


def _open(tmp_path: Path) -> tuple[Session, object]:
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, future=True)(), engine


def _by_name(report):
    return {item.name: item for item in report.components}


def test_empty_database_is_not_all_green(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        report = assemble_health(session, as_of=AS_OF, publish_root=tmp_path / "public")
        validate_health_json(report.to_dict())
        names = [item.name for item in report.components]
        assert names == list(HEALTH_COMPONENT_NAMES)
        assert report.rollup != HealthSeverity.GREEN
        assert report.ok is False
        by_name = _by_name(report)
        assert by_name["sources"].status is HealthStatus.MISSING
        assert "not yet probed" not in by_name["sources"].detail
        assert by_name["model"].status is HealthStatus.BLOCKED
        assert by_name["backup"].status is HealthStatus.MISSING
        assert by_name["quota"].status is HealthStatus.MISSING
        assert by_name["publish"].status is HealthStatus.MISSING
        assert all(item.status is not HealthStatus.HEALTHY for item in report.components)
    finally:
        session.close()
        engine.dispose()


def test_fixture_discover_changes_sources_off_default_missing(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        persist_from_listing(session, listing=_listing(), pages=_pages())
        session.commit()
        report = assemble_health(session, as_of=AS_OF)
        by_name = _by_name(report)
        assert by_name["sources"].status is HealthStatus.HEALTHY
        assert by_name["sources"].counts.get("upcoming_events") == 1
        assert by_name["sources"].counts.get("bouts") == 1
        assert NOT_PROBED not in by_name["sources"].detail
        assert by_name["identity"].status is HealthStatus.HEALTHY
        assert NOT_PROBED not in by_name["identity"].detail
        assert by_name["model"].status is HealthStatus.BLOCKED
        assert by_name["grade"].status is HealthStatus.MISSING
        assert report.rollup is HealthSeverity.RED
    finally:
        session.close()
        engine.dispose()


def test_demo_live_json_is_not_healthy_publish(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        root = tmp_path / "public"
        live = root / "live"
        live.mkdir(parents=True)
        (live / "current-event.json").write_text(
            json.dumps(
                {
                    "event_id": {"value": "evt-1"},
                    "title": {"value": "fixture confirmed_value"},
                }
            ),
            encoding="utf-8",
        )
        report = assemble_health(session, as_of=AS_OF, publish_root=root)
        publish = _by_name(report)["publish"]
        assert publish.status is HealthStatus.STALE
        assert "evt-1" in publish.detail or "demo" in publish.detail
        assert publish.status is not HealthStatus.HEALTHY
    finally:
        session.close()
        engine.dispose()


def test_backup_and_quota_stay_missing_without_evidence(tmp_path: Path) -> None:
    session, engine = _open(tmp_path)
    try:
        report = assemble_health(
            session,
            as_of=AS_OF,
            data_dir=tmp_path / "data",
        )
        by_name = _by_name(report)
        assert by_name["backup"].status is HealthStatus.MISSING
        assert by_name["quota"].status is HealthStatus.MISSING
        assert by_name["odds"].status is HealthStatus.MISSING

        stamp = tmp_path / "data" / "backup.last_ok"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("2026-08-14T12:00:00Z\n", encoding="utf-8")
        session.add(
            OddsQuotaObservation(
                provider="the_odds_api",
                endpoint="current_odds",
                observed_at=AS_OF - timedelta(hours=1),
                requests_remaining=400,
                requests_used=100,
                requests_last=1,
                requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
                empty_response=0,
            )
        )
        session.commit()
        probed = assemble_health(
            session,
            as_of=AS_OF,
            data_dir=tmp_path / "data",
        )
        probed_by_name = _by_name(probed)
        assert probed_by_name["backup"].status is HealthStatus.HEALTHY
        assert probed_by_name["quota"].status is HealthStatus.HEALTHY
        assert probed_by_name["odds"].status is HealthStatus.HEALTHY
    finally:
        session.close()
        engine.dispose()


def test_preview_publish_writes_assembled_health_not_default_missing(
    tmp_path: Path,
) -> None:
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
        assert result.status.value == "success"
        payload = json.loads((root / "live" / HEALTH_JSON).read_text(encoding="utf-8"))
        details = [str(item.get("detail") or "") for item in payload["components"]]
        assert not any(NOT_PROBED in detail for detail in details)
        sources = next(item for item in payload["components"] if item["name"] == "data")
        assert sources["status"] == HealthStatus.HEALTHY.value
        model = next(item for item in payload["components"] if item["name"] == "model")
        assert model["status"] == HealthStatus.BLOCKED.value
    finally:
        session.close()
        engine.dispose()


def test_health_cli_database_url_is_not_default_missing(
    tmp_path: Path, capsys
) -> None:
    db_path = tmp_path / "cli-health.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    engine.dispose()

    code = main(
        [
            "health",
            "--json",
            "--database-url",
            f"sqlite:///{db_path}",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    validate_health_json(payload)
    assert payload["rollup"] != "green"
    sources = next(item for item in payload["components"] if item["name"] == "sources")
    assert NOT_PROBED not in sources["detail"]
    assert sources["status"] == HealthStatus.MISSING.value
    assert not all(item["status"] == "healthy" for item in payload["components"])
