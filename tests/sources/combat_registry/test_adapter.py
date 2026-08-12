"""Combat Registry public adapter tests (DWCS-105)."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.combat_registry.adapter import CombatRegistryPublicAdapter
from mma_model.sources.http.block_signals import SourceBlockedError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/combat_registry"
UTC = timezone.utc


def test_adapter_maps_public_results(tmp_path: Path) -> None:
    root = tmp_path / "combat"
    results = root / "results"
    results.mkdir(parents=True)
    shutil.copy(FIXTURES / "results_sample.html", results / "cr-100.html")
    adapter = CombatRegistryPublicAdapter.for_fixtures(fixture_root=root)
    rows = list(
        adapter.iter_fighter_observations(
            fighter_external_id="cr-100",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    bouts = [row for row in rows if row.entity_kind == "regional_bout"]
    assert bouts
    assert all(row.source == "combat_registry" for row in rows)
    assert all(row.attributes.get("source_class") == "official record" for row in bouts)


def test_login_portal_kills_source(tmp_path: Path) -> None:
    root = tmp_path / "combat"
    results = root / "results"
    results.mkdir(parents=True)
    shutil.copy(FIXTURES / "results_login.html", results / "cr-login.html")
    adapter = CombatRegistryPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(SourceBlockedError) as exc:
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="cr-login",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
    assert exc.value.reason == "login_wall"
