"""HTTP politeness contract tests (DWCS-102 Task 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLITE_PATH = ROOT / "config" / "sources" / "http_politeness_v1.json"

REQUIRED_HOSTS = (
    "ufcstats.com",
    "tapology.com",
    "sherdog.com",
    "combatreg.com",
    "bestfightodds.com",
)


def test_load_http_politeness_requires_contact_and_stop_codes() -> None:
    from mma_model.sources.http_politeness import load_http_politeness

    cfg = load_http_politeness(POLITE_PATH)
    assert cfg.hosts["ufcstats.com"].min_delay_sec >= 0.75
    assert 403 in cfg.hosts["ufcstats.com"].stop_status_codes
    assert cfg.user_agent
    assert cfg.contact


def test_http_politeness_hosts_concurrency_and_stop_codes() -> None:
    from mma_model.sources.http_politeness import load_http_politeness

    cfg = load_http_politeness(POLITE_PATH)
    for host in REQUIRED_HOSTS:
        host_cfg = cfg.hosts[host]
        assert host_cfg.max_concurrency == 1
        assert set(host_cfg.stop_status_codes) >= {403, 429, 503}
        assert host_cfg.max_retries >= 0
        assert host_cfg.backoff_base_sec > 0
        assert host_cfg.backoff_cap_sec >= host_cfg.backoff_base_sec


def test_http_politeness_nested_immutability() -> None:
    from mma_model.sources.http_politeness import load_http_politeness

    cfg = load_http_politeness(POLITE_PATH)
    with pytest.raises(Exception):
        cfg.user_agent = "mutated"  # type: ignore[misc]
    with pytest.raises(Exception):
        cfg.hosts["ufcstats.com"].max_concurrency = 99  # type: ignore[misc]


def test_http_politeness_nested_config_drift_fails_closed(tmp_path: Path) -> None:
    from mma_model.sources.http_politeness import HttpPolitenessError, load_http_politeness

    payload = json.loads(POLITE_PATH.read_text(encoding="utf-8"))
    payload["hosts"]["ufcstats.com"]["max_concurrency"] = 4
    bad = tmp_path / "http_polite_bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HttpPolitenessError, match="max_concurrency"):
        load_http_politeness(bad)


def test_http_politeness_missing_contact_fails_closed(tmp_path: Path) -> None:
    from mma_model.sources.http_politeness import HttpPolitenessError, load_http_politeness

    payload = json.loads(POLITE_PATH.read_text(encoding="utf-8"))
    del payload["contact"]
    bad = tmp_path / "http_polite_nocontact.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HttpPolitenessError):
        load_http_politeness(bad)


def test_http_politeness_missing_stop_codes_fails_closed(tmp_path: Path) -> None:
    from mma_model.sources.http_politeness import HttpPolitenessError, load_http_politeness

    payload = json.loads(POLITE_PATH.read_text(encoding="utf-8"))
    payload["hosts"]["ufcstats.com"]["stop_status_codes"] = [200]
    bad = tmp_path / "http_polite_nostop.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HttpPolitenessError, match="stop_status_codes"):
        load_http_politeness(bad)
