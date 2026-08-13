"""Tests for ufcstats HTML parsers."""

from pathlib import Path

from mma_model.labels.outcomes import NormalizationStatus, normalize_outcome_from_method
from mma_model.ufcstats.parsers import (
    EventFightRow,
    parse_completed_events,
    parse_event_fights,
    parse_fight_totals,
    parse_fight_winner_id,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LABEL_FIGHTS = FIXTURES / "ufcstats" / "event_fights_labels.html"


def test_parse_completed_events_minimal():
    html = """
    <table class="b-statistics__table-events">
      <tbody>
        <tr class="b-statistics__table-row">
          <td><i class="b-statistics__table-content">
            <a href="http://www.ufcstats.com/event-details/abc123" class="b-link">UFC 999</a>
            <span class="b-statistics__date">January 01, 2020</span>
          </i></td>
          <td>Las Vegas</td>
        </tr>
      </tbody>
    </table>
    """
    rows = parse_completed_events(html)
    assert len(rows) == 1
    assert rows[0].ufcstats_id == "abc123"
    assert "UFC 999" in rows[0].name


def test_parse_fight_totals_sample():
    html = open("tests/fixtures/fight_full.html", encoding="utf-8").read()
    totals = parse_fight_totals(html)
    assert len(totals) == 2
    assert totals[0].sig_str_landed == 86
    assert totals[0].sig_str_attempted == 189

def test_parse_fight_winner_id_fixture():
    html = open("tests/fixtures/fight_full.html", encoding="utf-8").read()
    assert parse_fight_winner_id(html) == "76e2870ffafbe38f"


def _fights_by_id() -> dict[str, EventFightRow]:
    html = LABEL_FIGHTS.read_text(encoding="utf-8")
    return {row.fight_id: row for row in parse_event_fights(html)}


def test_fighter_name_with_method_substrings_is_not_parsed_as_method() -> None:
    row = _fights_by_id()["nameleak001"]
    assert row.fighter_a_name == "Nicole Decker"
    assert row.fighter_b_name == "Kona Diaz"
    assert row.method == "U-DEC"
    assert "DEC" in row.fighter_a_name.upper()
    assert "KO" in row.fighter_b_name.upper()


def test_parse_event_fights_draw_nc_technical_decision_early_malformed() -> None:
    by_id = _fights_by_id()

    draw = by_id["draw001"]
    assert draw.method == "Draw"
    assert draw.winner_id is None
    assert normalize_outcome_from_method(draw.method).result_class.value == "draw"

    nc = by_id["nc001"]
    assert nc.method == "NC"
    assert nc.fighter_b_name == "Nick Diaz"
    assert nc.method != nc.fighter_b_name
    assert normalize_outcome_from_method(nc.method).result_class.value == "no_contest"

    tech = by_id["techdec001"]
    assert tech.method == "Technical Decision"
    assert normalize_outcome_from_method(tech.method).method.value == "technical_decision"

    early = by_id["early001"]
    assert early.method == "KO/TKO"
    assert early.fight_round == 1
    assert early.time_str == "1:10"

    malformed = by_id["malformed001"]
    assert malformed.method == "maybe a KO?"
    malformed_norm = normalize_outcome_from_method(malformed.method)
    assert malformed_norm.status is NormalizationStatus.UNKNOWN
    assert malformed_norm.result_class.value == "unknown"
    assert "KO" in malformed.fighter_b_name.upper()

    empty = by_id["empty001"]
    assert empty.method == ""
    empty_norm = normalize_outcome_from_method(empty.method)
    assert empty_norm.status is NormalizationStatus.MISSING
    assert empty_norm.result_class.value == "pending"

    simple = by_id["simple001"]
    assert simple.method == "KO/TKO"
    assert simple.fight_round == 3
    assert simple.time_str == "2:15"


def test_parse_malformed_method_feeds_unknown_not_pending() -> None:
    row = _fights_by_id()["malformed001"]
    assert row.method == "maybe a KO?"
    got = normalize_outcome_from_method(row.method)
    assert got.status is NormalizationStatus.UNKNOWN
    assert got.status is not NormalizationStatus.MISSING
    assert got.result_class.value == "unknown"


def test_existing_event_details_sample_still_parses_method() -> None:
    html = (FIXTURES / "sources" / "ufcstats" / "event_details_sample.html").read_text(
        encoding="utf-8"
    )
    rows = parse_event_fights(html)
    assert len(rows) == 1
    assert rows[0].method == "KO/TKO"
    assert rows[0].fighter_a_name == "Alice Alpha"

