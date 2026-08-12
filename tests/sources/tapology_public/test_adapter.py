"""Tapology public adapter tests (DWCS-105)."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.tapology_public.adapter import TapologyPublicAdapter
from mma_model.sources.tapology_public.errors import ParserSchemaDriftError

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/tapology"
UTC = timezone.utc


def _stage(tmp_path: Path, name: str, dest_id: str) -> Path:
    root = tmp_path / "tapology"
    fighters = root / "fighters"
    fighters.mkdir(parents=True)
    shutil.copy(FIXTURES / name, fighters / f"{dest_id}.html")
    return root


def test_adapter_maps_fixture_without_network(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_public_sample.html", "tap-100")
    shutil.copy(FIXTURES / "fighter_tap-100-p2.html", root / "fighters" / "tap-100-p2.html")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    rows = list(
        adapter.iter_fighter_observations(
            fighter_external_id="tap-100",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    bouts = [row for row in rows if row.entity_kind == "regional_bout"]
    assert bouts
    assert all(row.source == "tapology_public" for row in rows)
    assert all("quality_tier" not in row.attributes for row in rows)
    assert all(row.attributes.get("source_class") == "public extraction" for row in bouts)
    assert any(row.attributes.get("result") == "draw" for row in bouts)
    assert any(row.attributes.get("result") == "nc" for row in bouts)
    invalid = [
        row for row in bouts if row.attributes.get("external_bout_id") == "tb-invalid-time"
    ]
    assert invalid
    assert invalid[0].attributes["elapsed_seconds"] is None
    assert invalid[0].attributes["duration_status"] == "invalid"


def test_login_wall_kills_source(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_login.html", "tap-login")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(SourceBlockedError) as exc:
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="tap-login",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
    assert exc.value.reason == "login_wall"


def test_captcha_kills_source(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_captcha.html", "tap-cap")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(SourceBlockedError) as exc:
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="tap-cap",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
    assert exc.value.reason == "captcha_interstitial"


def test_cloudflare_kills_source(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_cloudflare.html", "tap-cf")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(SourceBlockedError) as exc:
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="tap-cf",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
    assert exc.value.reason == "cloudflare_challenge"


def test_paywall_kills_source(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_paywall.html", "tap-pay")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(SourceBlockedError) as exc:
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="tap-pay",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
    assert exc.value.reason in {"paywall", "login_wall"}


def test_schema_drift_hard_fails(tmp_path: Path) -> None:
    root = _stage(tmp_path, "fighter_schema_drift.html", "tap-drift")
    adapter = TapologyPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(ParserSchemaDriftError):
        list(
            adapter.iter_fighter_observations(
                fighter_external_id="tap-drift",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
