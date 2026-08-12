"""UFCStats public adapter tests (DWCS-102 Task 5)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/ufcstats"
UTC = timezone.utc


def _stage_fixtures(tmp_path: Path) -> Path:
    root = tmp_path / "ufcstats_fixtures"
    events = root / "events"
    fights = root / "fights"
    events.mkdir(parents=True)
    fights.mkdir(parents=True)
    shutil.copy(
        FIXTURES / "event_details_sample.html",
        events / "evt1.html",
    )
    shutil.copy(
        FIXTURES / "fight_details_sample.html",
        fights / "fight001abc.html",
    )
    return root


def test_adapter_maps_fixture_without_network(tmp_path: Path) -> None:
    root = _stage_fixtures(tmp_path)
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=root)
    rows = list(
        adapter.iter_observations(
            event_external_ids=["evt1"],
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    assert rows
    assert all(r.source == "ufcstats_public" for r in rows)
    assert all(r.observed_at.year == 2026 for r in rows)
    assert all(r.quality_tier in {"gold", "silver", "bronze"} for r in rows)
    assert all(r.timestamp_quality_source is not None for r in rows)
    assert all("quality_tier" not in r.attributes for r in rows)


def test_audit_manifest_scope_classifies_every_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_path = tmp_path / "events.jsonl"
    bouts_path = tmp_path / "bouts.jsonl"
    events = [
        {
            "event_id": "dwcs:event:1",
            "calendar_year": 2019,
            "ufcstats_event_id": None,
            "source_ids": {"ufcstats_event_id": None},
        },
        {
            "event_id": "dwcs:event:2",
            "calendar_year": 2019,
            "ufcstats_event_id": "evt1",
            "source_ids": {"ufcstats_event_id": "evt1"},
        },
    ]
    bouts = [
        {
            "bout_id": "dwcs:bout:1",
            "event_id": "dwcs:event:1",
            "ufcstats_bout_id": None,
            "source_ids": {"ufcstats_bout_id": None},
        },
        {
            "bout_id": "dwcs:bout:2",
            "event_id": "dwcs:event:2",
            "ufcstats_bout_id": "fight001abc",
            "source_ids": {"ufcstats_bout_id": "fight001abc"},
        },
    ]
    events_path.write_text(
        "\n".join(json.dumps(row) for row in events) + "\n", encoding="utf-8"
    )
    bouts_path.write_text(
        "\n".join(json.dumps(row) for row in bouts) + "\n", encoding="utf-8"
    )
    root = _stage_fixtures(tmp_path)
    adapter = UfcstatsPublicAdapter.for_fixtures(
        fixture_root=root,
    )
    adapter.events_manifest = events_path
    adapter.bouts_manifest = bouts_path
    report = adapter.audit_manifest_scope(years=range(2019, 2020))
    assert report["events_total"] == 2
    assert report["bouts_total"] == 2
    statuses = {row["entity_id"]: row["status"] for row in report["event_classifications"]}
    assert statuses["dwcs:event:1"] == "unresolved"
    assert statuses["dwcs:event:2"] == "present"
    bout_statuses = {
        row["entity_id"]: row["status"] for row in report["bout_classifications"]
    }
    assert bout_statuses["dwcs:bout:1"] == "unresolved"
    assert bout_statuses["dwcs:bout:2"] == "present"


def test_audit_deterministic_output_order(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    bouts_path = tmp_path / "bouts.jsonl"
    events_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event_id": eid,
                    "calendar_year": 2018,
                    "ufcstats_event_id": None,
                    "source_ids": {},
                }
            )
            for eid in ("dwcs:event:b", "dwcs:event:a")
        )
        + "\n",
        encoding="utf-8",
    )
    bouts_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "bout_id": bid,
                    "event_id": "dwcs:event:a",
                    "ufcstats_bout_id": None,
                    "source_ids": {},
                }
            )
            for bid in ("dwcs:bout:b", "dwcs:bout:a")
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=tmp_path)
    adapter.events_manifest = events_path
    adapter.bouts_manifest = bouts_path
    report = adapter.audit_manifest_scope(years=range(2018, 2019))
    event_ids = [r["entity_id"] for r in report["event_classifications"]]
    bout_ids = [r["entity_id"] for r in report["bout_classifications"]]
    assert event_ids == sorted(event_ids)
    assert bout_ids == sorted(bout_ids)
