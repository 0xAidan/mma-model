"""PIT publication-proxy rule contract tests (DWCS-102 Task 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mma_model.sources.policy import load_source_policy

ROOT = Path(__file__).resolve().parents[2]
PROXY_PATH = ROOT / "config" / "sources" / "pit_proxy_v1.json"


def test_load_pit_proxy_rule_silver_ceiling() -> None:
    from mma_model.sources.pit_proxy import load_pit_proxy_rule

    policy = load_source_policy()
    rule = load_pit_proxy_rule(PROXY_PATH)
    assert rule.rule_id == "event_completion_plus_delay"
    assert rule.delay_iso8601 == "P1D"
    assert rule.max_quality_tier_when_proxy == "silver"
    assert rule.max_quality_tier_when_proxy in policy.observation_metadata.quality_tier_values


def test_proxy_rule_forbidden_for_mutable_profile() -> None:
    from mma_model.sources.pit_proxy import PitProxyError, load_pit_proxy_rule

    rule = load_pit_proxy_rule(PROXY_PATH)
    assert "mutable_profile_aggregate" in rule.forbidden_for
    with pytest.raises(PitProxyError, match="forbidden_for"):
        rule.assert_allowed_for("mutable_profile_aggregate")


def test_proxy_cannot_be_gold() -> None:
    from mma_model.sources.pit_proxy import PitProxyError, load_pit_proxy_rule

    rule = load_pit_proxy_rule(PROXY_PATH)
    assert rule.max_quality_tier_when_proxy != "gold"
    with pytest.raises(PitProxyError, match="gold"):
        rule.assert_quality_tier_allowed("gold")


def test_pit_proxy_nested_immutability() -> None:
    from mma_model.sources.pit_proxy import load_pit_proxy_rule

    rule = load_pit_proxy_rule(PROXY_PATH)
    with pytest.raises(Exception):
        rule.applies_to = ("x",)  # type: ignore[misc]
    with pytest.raises(Exception):
        rule.forbidden_for = ("y",)  # type: ignore[misc]


def test_pit_proxy_nested_config_drift_fails_closed(tmp_path: Path) -> None:
    from mma_model.sources.pit_proxy import PitProxyError, load_pit_proxy_rule

    payload = json.loads(PROXY_PATH.read_text(encoding="utf-8"))
    payload["max_quality_tier_when_proxy"] = "gold"
    bad = tmp_path / "pit_proxy_bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PitProxyError, match="silver"):
        load_pit_proxy_rule(bad)


def test_pit_proxy_missing_required_fields_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.pit_proxy import PitProxyError, load_pit_proxy_rule

    bad = tmp_path / "pit_proxy_missing.json"
    bad.write_text(json.dumps({"rule_id": "x"}), encoding="utf-8")
    with pytest.raises(PitProxyError):
        load_pit_proxy_rule(bad)
