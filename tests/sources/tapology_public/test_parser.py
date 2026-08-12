"""Tapology public parser tests (DWCS-105)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mma_model.sources.tapology_public.errors import ParserSchemaDriftError
from mma_model.sources.tapology_public.parser import parse_fighter_page

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/tapology"


def test_parse_fighter_public_sample() -> None:
    html = (FIXTURES / "fighter_public_sample.html").read_text(encoding="utf-8")
    parsed = parse_fighter_page(html)
    assert parsed["fighter_external_id"] == "tap-100"
    assert parsed["fighter_name"] == "Alex Sample"
    assert parsed["left_truncated"] is True
    ids = [row["external_bout_id"] for row in parsed["bouts"]]
    assert "tb-pro-1" in ids
    assert "tb-draw" in ids
    assert "tb-nc" in ids
    assert parsed["current_record"]["wins"] == 2
    assert parsed["explicit_pre_fight_record"]["wins"] == 2
    reversal = [row for row in parsed["bouts"] if row["external_bout_id"] == "tb-reversal-en"]
    assert {row["version_kind"] for row in reversal} == {"event_night", "current"}


def test_schema_drift_raises() -> None:
    html = (FIXTURES / "fighter_schema_drift.html").read_text(encoding="utf-8")
    with pytest.raises(ParserSchemaDriftError, match="headers drifted"):
        parse_fighter_page(html)


def test_missing_profile_raises() -> None:
    with pytest.raises(ParserSchemaDriftError, match="data-fighter-id"):
        parse_fighter_page("<html><body></body></html>")


def test_duplicate_bout_ids_raise() -> None:
    html = (FIXTURES / "fighter_duplicate.html").read_text(encoding="utf-8")
    with pytest.raises(ParserSchemaDriftError, match="duplicate"):
        parse_fighter_page(html)


def test_swapped_self_opponent_raises() -> None:
    html = (FIXTURES / "fighter_swapped.html").read_text(encoding="utf-8")
    with pytest.raises(ParserSchemaDriftError, match="swapped"):
        parse_fighter_page(html)


def test_malformed_round_raises() -> None:
    html = (FIXTURES / "fighter_public_sample.html").read_text(encoding="utf-8")
    html = html.replace("<td>1</td>\n      <td>1:30</td>", "<td>first</td>\n      <td>1:30</td>", 1)
    with pytest.raises(ParserSchemaDriftError, match="malformed round"):
        parse_fighter_page(html)
