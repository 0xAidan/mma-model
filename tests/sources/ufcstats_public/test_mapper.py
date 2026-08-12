"""UFCStats public mapper tests (DWCS-102 Task 4)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.pit_proxy import load_pit_proxy_rule
from mma_model.sources.policy import load_source_policy
from mma_model.sources.ufcstats_public.mapper import (
    ReservedAttributeKeyError,
    map_fight_to_observations,
)
from mma_model.sources.ufcstats_public.parser import parse_fight_details

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/ufcstats"
UTC = timezone.utc


def test_mapper_sets_first_class_pit_and_quality_fields() -> None:
    policy = load_source_policy()
    proxy = load_pit_proxy_rule()
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    rows = map_fight_to_observations(
        parsed=parsed,
        observed_at=observed,
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        source_published_at=None,
        source_updated_at=None,
        proxy=proxy,
        payload_hash="a" * 64,
    )
    assert rows
    assert [r.external_id for r in rows] == sorted(r.external_id for r in rows)
    for row in rows:
        assert row.quality_tier in policy.observation_metadata.quality_tier_values
        assert row.timestamp_quality in policy.observation_metadata.timestamp_quality_values
        assert row.timestamp_quality_source is not None
        assert row.observed_at == observed
        assert row.observed_at != row.effective_at
        if row.timestamp_quality == "publication_proxy":
            assert row.proxy_published_at is not None
            assert row.quality_tier == "silver"
        for reserved in policy.observation_metadata.reserved_attribute_keys:
            assert reserved not in row.attributes
        assert "significant_strikes_landed" in row.attributes


def test_mapper_rejects_reserved_attribute_key_collision() -> None:
    policy = load_source_policy()
    assert "quality_tier" in policy.observation_metadata.reserved_attribute_keys
    parsed = {
        "external_fight_id": "x",
        "fighter_a": {"name": "A", "id": "a1", "stats": {"quality_tier": "gold"}},
        "fighter_b": {"name": "B", "id": "b1", "stats": {}},
    }
    with pytest.raises(ReservedAttributeKeyError, match="quality_tier"):
        map_fight_to_observations(
            parsed=parsed,
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            effective_at=datetime(2019, 1, 1, tzinfo=UTC),
            source_published_at=None,
            source_updated_at=None,
            proxy=None,
            payload_hash="b" * 64,
        )


def test_mapper_gold_when_source_published_at_present() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    rows = map_fight_to_observations(
        parsed=parsed,
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        source_published_at=datetime(2019, 1, 2, tzinfo=UTC),
        source_updated_at=datetime(2019, 1, 2, tzinfo=UTC),
        proxy=None,
        payload_hash="c" * 64,
    )
    assert all(r.quality_tier == "gold" for r in rows)
    assert all(r.timestamp_quality == "direct_source_timestamp" for r in rows)
    assert all(r.proxy_published_at is None for r in rows)


def test_mapper_never_backdates_observed_at() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    rows = map_fight_to_observations(
        parsed=parsed,
        observed_at=observed,
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        source_published_at=None,
        source_updated_at=None,
        proxy=load_pit_proxy_rule(),
        payload_hash="d" * 64,
    )
    assert all(r.observed_at == observed for r in rows)
    assert all(r.observed_at != r.effective_at for r in rows)
