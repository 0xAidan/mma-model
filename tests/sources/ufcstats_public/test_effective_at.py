"""effective_at must derive from source/manifest dates (never fabricated)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mma_model.sources.pit_proxy import load_pit_proxy_rule
from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter
from mma_model.sources.ufcstats_public.errors import ParserSchemaDriftError
from mma_model.sources.ufcstats_public.mapper import map_fight_to_observations
from mma_model.sources.ufcstats_public.parser import parse_event_details, parse_fight_details

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/ufcstats"
UTC = timezone.utc


def _event_html(date_line: str | None) -> str:
    date_li = (
        f'<li class="b-list__box-list-item"><i class="b-list__box-item-title">Date:</i> {date_line}</li>'
        if date_line is not None
        else ""
    )
    return f"""
<!DOCTYPE html><html><body>
<section class="b-statistics__section_details">
  <span class="b-content__title-highlight">DWCS Sample Card</span>
  <ul>{date_li}
    <li class="b-list__box-list-item"><i class="b-list__box-item-title">Location:</i> Las Vegas, Nevada, USA</li>
  </ul>
</section>
<table class="b-fight-details__table"><tbody>
<tr class="b-fight-details__table-row b-fight-details__table-row__hover" data-link="http://www.ufcstats.com/fight-details/fight001abc">
  <td>
    <p class="b-fight-details__table-text"><a href="http://www.ufcstats.com/fighter-details/fighterA001">Alice Alpha</a></p>
    <p class="b-fight-details__table-text"><a href="http://www.ufcstats.com/fighter-details/fighterB001">Bob Beta</a></p>
  </td>
  <td class="l-page_align_left"><p class="b-fight-details__table-text">KO/TKO</p></td>
  <td class="b-fight-details__table-col">3</td>
  <td class="b-fight-details__table-col">2:15</td>
</tr>
</tbody></table>
</body></html>
"""


@pytest.mark.parametrize(
    ("date_text", "year"),
    [
        ("July 11, 2017", 2017),
        ("August 4, 2020", 2020),
        ("September 9, 2025", 2025),
    ],
)
def test_parse_event_details_exposes_event_date(date_text: str, year: int) -> None:
    parsed = parse_event_details(_event_html(date_text))
    assert parsed["event_date"] is not None
    assert parsed["event_date"].year == year
    assert parsed["event_date"].tzinfo == UTC


def test_parse_event_details_missing_date_is_none() -> None:
    parsed = parse_event_details(_event_html(None))
    assert parsed["event_date"] is None


def test_adapter_uses_parsed_event_date_as_effective_at(tmp_path: Path) -> None:
    root = tmp_path / "fx"
    (root / "events").mkdir(parents=True)
    (root / "fights").mkdir(parents=True)
    (root / "events" / "evt1.html").write_text(
        _event_html("August 4, 2020"), encoding="utf-8"
    )
    (root / "fights" / "fight001abc.html").write_text(
        (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=root)
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    rows = list(
        adapter.iter_observations(event_external_ids=["evt1"], observed_at=observed)
    )
    assert rows
    assert all(r.observed_at == observed for r in rows)
    assert all(r.effective_at == datetime(2020, 8, 4, tzinfo=UTC) for r in rows)
    assert all(r.effective_at.year == 2020 for r in rows)
    assert all(r.proxy_published_at == datetime(2020, 8, 5, tzinfo=UTC) for r in rows)


def test_adapter_missing_date_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "fx"
    (root / "events").mkdir(parents=True)
    (root / "fights").mkdir(parents=True)
    (root / "events" / "evt1.html").write_text(_event_html(None), encoding="utf-8")
    (root / "fights" / "fight001abc.html").write_text(
        (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=root)
    with pytest.raises(ParserSchemaDriftError, match="effective_at|event_date"):
        list(
            adapter.iter_observations(
                event_external_ids=["evt1"],
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )


def test_adapter_accepts_explicit_manifest_effective_at(tmp_path: Path) -> None:
    root = tmp_path / "fx"
    (root / "events").mkdir(parents=True)
    (root / "fights").mkdir(parents=True)
    (root / "events" / "evt1.html").write_text(_event_html(None), encoding="utf-8")
    (root / "fights" / "fight001abc.html").write_text(
        (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    adapter = UfcstatsPublicAdapter.for_fixtures(fixture_root=root)
    manifest_effective = datetime(2017, 7, 11, tzinfo=UTC)
    rows = list(
        adapter.iter_observations(
            event_external_ids=["evt1"],
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            event_effective_at_by_id={"evt1": manifest_effective},
        )
    )
    assert all(r.effective_at == manifest_effective for r in rows)


def test_mapper_proxy_published_at_from_effective_not_fabricated_year() -> None:
    html = (FIXTURES / "fight_details_sample.html").read_text(encoding="utf-8")
    parsed = parse_fight_details(html)
    effective = datetime(2025, 9, 9, tzinfo=UTC)
    rows = map_fight_to_observations(
        parsed=parsed,
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=effective,
        source_published_at=None,
        source_updated_at=None,
        proxy=load_pit_proxy_rule(),
        payload_hash="e" * 64,
    )
    assert all(r.effective_at == effective for r in rows)
    assert all(r.proxy_published_at == datetime(2025, 9, 10, tzinfo=UTC) for r in rows)
    assert all(r.effective_at.year != 2019 for r in rows)
