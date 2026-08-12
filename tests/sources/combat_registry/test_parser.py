"""Combat Registry public parser tests (DWCS-105)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mma_model.sources.combat_registry.errors import ParserSchemaDriftError
from mma_model.sources.combat_registry.parser import parse_results_page

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/combat_registry"


def test_parse_results_sample() -> None:
    html = (FIXTURES / "results_sample.html").read_text(encoding="utf-8")
    parsed = parse_results_page(html)
    assert parsed["fighter_external_id"] == "cr-100"
    ids = {row["external_bout_id"]: row for row in parsed["bouts"]}
    assert ids["cr-pro-1"]["regulated_us"] == "true"
    assert ids["cr-am-us-1"]["classification"] == "amateur"
    assert ids["cr-am-us-1"]["regulated_us"] == "true"


def test_missing_schema_raises() -> None:
    with pytest.raises(ParserSchemaDriftError, match="missing results table"):
        parse_results_page("<html><body>no table</body></html>")
