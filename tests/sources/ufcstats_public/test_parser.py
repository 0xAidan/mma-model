"""UFCStats public parser tests (DWCS-102 Task 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mma_model.sources.ufcstats_public.errors import ParticipantError
from mma_model.sources.ufcstats_public.parser import (
    ParserSchemaDriftError,
    parse_event_details,
    parse_fight_details,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/ufcstats"


def test_parse_fight_details_sample() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    assert parsed["external_fight_id"]
    assert parsed["fighter_a"]["name"]
    assert parsed["fighter_b"]["name"]
    assert "significant_strikes_landed" in parsed["fighter_a"]["stats"]


def test_parse_event_details_sample() -> None:
    html = (FIXTURES / "event_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_event_details(html)
    assert parsed["event_name"]
    assert len(parsed["fights"]) == 1
    assert parsed["fights"][0]["external_fight_id"] == "fight001abc"


def test_schema_drift_raises() -> None:
    html = (FIXTURES / "fight_details_schema_drift.html").read_text(encoding="utf-8")
    with pytest.raises(ParserSchemaDriftError):
        parse_fight_details(html)


def test_missing_participant_raises() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    # Remove second person block.
    html = html.replace(
        """<div class="b-fight-details__person">
      <i class="b-fight-details__person-status b-fight-details__person-status_style_gray">L</i>
      <div>
        <h3><a class="b-fight-details__person-link" href="http://www.ufcstats.com/fighter-details/fighterB001">Bob Beta</a></h3>
      </div>
    </div>""",
        "",
        1,
    )
    with pytest.raises(ParticipantError, match="2 participants"):
        parse_fight_details(html)


def test_duplicate_participant_raises() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    html = html.replace("fighterB001", "fighterA001")
    with pytest.raises(ParticipantError, match="duplicate"):
        parse_fight_details(html)


def test_malformed_stat_label_raises() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    html = html.replace("12 of 20", "twelve of twenty", 1)
    with pytest.raises(ParserSchemaDriftError, match="of-pattern"):
        parse_fight_details(html)


def test_malformed_round_time_raises() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    html = html.replace("Time:</i> 1:30", "Time:</i> first-roundish", 1)
    with pytest.raises(ParserSchemaDriftError, match="time"):
        parse_fight_details(html)
