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


def test_missing_external_fight_id_fails_closed() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    html = html.replace(
        '<a id="fight-url" href="http://www.ufcstats.com/fight-details/fight001abc">fight</a>',
        "",
        1,
    )
    with pytest.raises(ParserSchemaDriftError, match="external_fight_id"):
        parse_fight_details(html)


def test_blank_external_fight_id_fails_closed() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    html = html.replace(
        "http://www.ufcstats.com/fight-details/fight001abc",
        "http://www.ufcstats.com/fight-details/",
        1,
    )
    with pytest.raises(ParserSchemaDriftError, match="external_fight_id"):
        parse_fight_details(html)


def test_event_fight_missing_external_id_fails_closed() -> None:
    html = (FIXTURES / "event_details_sample.html").read_text(encoding="utf-8")
    html = html.replace(
        'data-link="http://www.ufcstats.com/fight-details/fight001abc"',
        'data-link="http://www.ufcstats.com/fight-details/"',
        1,
    )
    with pytest.raises(ParserSchemaDriftError, match="external_fight_id"):
        parse_event_details(html)


def test_event_duplicate_fight_ids_fail_closed() -> None:
    html = (FIXTURES / "event_details_sample.html").read_text(encoding="utf-8")
    # Duplicate the fight row with the same fight id.
    row = """
    <tr class="b-fight-details__table-row b-fight-details__table-row__hover" data-link="http://www.ufcstats.com/fight-details/fight001abc">
      <td>
        <p class="b-fight-details__table-text"><a href="http://www.ufcstats.com/fighter-details/fighterC001">Cara</a></p>
        <p class="b-fight-details__table-text"><a href="http://www.ufcstats.com/fighter-details/fighterD001">Dana</a></p>
      </td>
      <td class="l-page_align_left">
        <p class="b-fight-details__table-text">Lightweight</p>
        <p class="b-fight-details__table-text">DEC</p>
      </td>
      <td class="b-fight-details__table-col">3</td>
      <td class="b-fight-details__table-col">5:00</td>
    </tr>
"""
    html = html.replace("</tbody>", row + "</tbody>", 1)
    with pytest.raises(ParserSchemaDriftError, match="duplicate"):
        parse_event_details(html)
