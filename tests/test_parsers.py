"""Tests for ufcstats HTML parsers."""

from mma_model.ufcstats.parsers import parse_completed_events, parse_event_fights, parse_fight_totals


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
