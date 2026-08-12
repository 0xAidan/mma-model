"""Sherdog public parser tests (DWCS-105)."""

from __future__ import annotations

from pathlib import Path

from mma_model.sources.sherdog_public.parser import parse_fighter_page

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/sherdog"


def test_parse_fighter_public_sample() -> None:
    html = (FIXTURES / "fighter_public_sample.html").read_text(encoding="utf-8")
    parsed = parse_fighter_page(html)
    assert parsed["fighter_external_id"] == "sh-100"
    assert parsed["fighter_name"] == "Alex Sample"
    ids = [row["external_bout_id"] for row in parsed["bouts"]]
    assert "sh-pro-1" in ids
    conflict = next(row for row in parsed["bouts"] if row["external_bout_id"] == "tb-conflict")
    assert conflict["result"] == "loss"
