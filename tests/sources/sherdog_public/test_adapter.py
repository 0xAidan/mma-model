"""Sherdog public adapter tests (DWCS-105)."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from mma_model.sources.sherdog_public.adapter import SherdogPublicAdapter

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/sherdog"
UTC = timezone.utc


def test_adapter_maps_fixture_without_network(tmp_path: Path) -> None:
    root = tmp_path / "sherdog"
    fighters = root / "fighters"
    fighters.mkdir(parents=True)
    shutil.copy(FIXTURES / "fighter_public_sample.html", fighters / "sh-100.html")
    adapter = SherdogPublicAdapter.for_fixtures(fixture_root=root)
    rows = list(
        adapter.iter_fighter_observations(
            fighter_external_id="sh-100",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    bouts = [row for row in rows if row.entity_kind == "regional_bout"]
    assert bouts
    assert all(row.source == "sherdog_public" for row in rows)
    assert all(row.attributes.get("source_class") == "public extraction" for row in bouts)
