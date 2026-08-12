"""Parse public UFCStats HTML snapshots into typed dicts (DWCS-102)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mma_model.sources.ufcstats_public.errors import (
    ParserSchemaDriftError,
    ParticipantError,
)

SOURCE_UFCSTATS_PUBLIC = "ufcstats_public"

__all__ = [
    "SOURCE_UFCSTATS_PUBLIC",
    "ParserSchemaDriftError",
    "ParticipantError",
    "parse_event_details",
    "parse_fight_details",
]

REQUIRED_TOTALS_HEADERS = (
    "fighter",
    "kd",
    "sig. str.",
    "sig. str. %",
    "total str.",
    "td",
    "td %",
    "sub. att",
    "rev.",
    "ctrl",
)


def _id_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return ""
    kind, entity_id = parts[-2], parts[-1]
    if kind not in {"fight-details", "event-details", "fighter-details"}:
        return ""
    if entity_id in {"fight-details", "event-details", "fighter-details"}:
        return ""
    return entity_id


def _parse_of_pattern(value: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)\s+of\s+(\d+)$", value.strip())
    if not match:
        raise ParserSchemaDriftError(f"malformed of-pattern stat: {value!r}")
    return int(match.group(1)), int(match.group(2))


def _parse_pct(value: str) -> float | None:
    text = value.strip()
    if text in {"---", "", "–"}:
        return None
    match = re.match(r"^(\d+)%$", text)
    if not match:
        raise ParserSchemaDriftError(f"malformed percent stat: {value!r}")
    return int(match.group(1)) / 100.0


def _parse_ctrl(value: str) -> int:
    text = value.strip()
    if text in {"---", ""}:
        return 0
    parts = text.split(":")
    if len(parts) != 2:
        raise ParserSchemaDriftError(f"malformed ctrl time: {value!r}")
    try:
        minutes, seconds = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ParserSchemaDriftError(f"malformed ctrl time: {value!r}") from exc
    return minutes * 60 + seconds


def _parse_int_label(value: str, *, label: str) -> int:
    text = value.strip()
    if not text.isdigit():
        raise ParserSchemaDriftError(f"malformed {label}: {value!r}")
    return int(text)


def parse_event_details(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("span.b-content__title-highlight")
    event_name = title.get_text(strip=True) if title else ""
    date_text = ""
    location = ""
    for item in soup.select("li.b-list__box-list-item"):
        label = item.select_one("i.b-list__box-item-title")
        if label is None:
            continue
        key = label.get_text(strip=True).rstrip(":").lower()
        value = item.get_text(" ", strip=True)
        value = value.replace(label.get_text(strip=True), "", 1).strip()
        if key == "date":
            date_text = value
        elif key == "location":
            location = value

    page_text = soup.get_text(" ", strip=True).lower()
    cancelled_evidence = any(
        marker in page_text
        for marker in ("event cancelled", "card cancelled", "cancelled event")
    )

    fights: list[dict[str, Any]] = []
    seen_fight_ids: set[str] = set()
    for tr in soup.select(
        "tr.b-fight-details__table-row.b-fight-details__table-row__hover"
    ):
        link = (tr.get("data-link") or "").strip()
        if "fight-details" not in link:
            continue
        fight_id = _id_from_url(link)
        if not fight_id:
            raise ParserSchemaDriftError(
                "event fight missing external_fight_id in data-link"
            )
        if fight_id in seen_fight_ids:
            raise ParserSchemaDriftError(
                f"duplicate external_fight_id in event card: {fight_id!r}"
            )
        seen_fight_ids.add(fight_id)
        f_links = tr.select('a[href*="fighter-details"]')
        if len(f_links) != 2:
            raise ParticipantError(
                f"event fight {fight_id} expected 2 participants, got {len(f_links)}"
            )
        fa_id = _id_from_url(f_links[0].get("href", ""))
        fb_id = _id_from_url(f_links[1].get("href", ""))
        if not fa_id or not fb_id:
            raise ParserSchemaDriftError(
                f"event fight {fight_id} missing fighter external ids"
            )
        if fa_id == fb_id:
            raise ParticipantError(
                f"event fight {fight_id} has duplicate participant ids"
            )
        fights.append(
            {
                "external_fight_id": fight_id,
                "fight_url": link,
                "fighter_a": {
                    "id": fa_id,
                    "name": f_links[0].get_text(strip=True),
                },
                "fighter_b": {
                    "id": fb_id,
                    "name": f_links[1].get_text(strip=True),
                },
            }
        )
    return {
        "event_name": event_name,
        "date_text": date_text,
        "location": location,
        "cancelled_evidence": cancelled_evidence,
        "has_fight_table": bool(
            soup.select("table.b-fight-details__table")
            or soup.select("tr.b-fight-details__table-row")
        ),
        "fights": fights,
    }


def parse_fight_details(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    persons = soup.select("div.b-fight-details__person")
    if len(persons) != 2:
        raise ParticipantError(
            f"fight details expected 2 participants, got {len(persons)}"
        )

    fighters: list[dict[str, Any]] = []
    winner_id: str | None = None
    for person in persons:
        link = person.select_one("a.b-fight-details__person-link")
        if link is None or not link.get("href"):
            raise ParticipantError("missing fighter link in fight details")
        fighter_id = _id_from_url(link["href"])
        name = link.get_text(strip=True)
        if not fighter_id or not name:
            raise ParticipantError("blank fighter identity in fight details")
        status = person.select_one("i.b-fight-details__person-status")
        classes = status.get("class") if status is not None else []
        if any("style_green" in str(c) for c in (classes or [])):
            winner_id = fighter_id
        fighters.append({"id": fighter_id, "name": name, "stats": {}})

    if fighters[0]["id"] == fighters[1]["id"]:
        raise ParticipantError("duplicate participant ids in fight details")

    method = _extract_labeled_text(soup, "Method")
    round_text = _extract_labeled_text(soup, "Round")
    time_text = _extract_labeled_text(soup, "Time")
    ending_round = _parse_int_label(round_text, label="round") if round_text else None
    if time_text and not re.match(r"^\d+:\d{2}$", time_text.strip()):
        raise ParserSchemaDriftError(f"malformed round time: {time_text!r}")

    totals_table = _find_totals_table(soup)
    headers = [
        th.get_text(" ", strip=True).lower() for th in totals_table.select("thead th")
    ]
    normalized = tuple(headers)
    if normalized[: len(REQUIRED_TOTALS_HEADERS)] != REQUIRED_TOTALS_HEADERS:
        raise ParserSchemaDriftError(
            f"schema drift in totals headers: got {headers!r}"
        )

    rows = totals_table.select("tbody tr")
    if not rows:
        raise ParserSchemaDriftError("missing totals body rows")
    cells = rows[0].select("td")
    if len(cells) < len(REQUIRED_TOTALS_HEADERS):
        raise ParserSchemaDriftError("totals row missing required columns")

    name_links = cells[0].select("p.b-fight-details__table-text a")
    if len(name_links) != 2:
        raise ParticipantError(
            f"totals table expected 2 fighter links, got {len(name_links)}"
        )

    col_vals: list[list[str]] = []
    for cell in cells[1:]:
        texts = [
            p.get_text(strip=True) for p in cell.select("p.b-fight-details__table-text")
        ]
        if len(texts) != 2:
            raise ParserSchemaDriftError(
                f"malformed dual-value totals cell: {texts!r}"
            )
        col_vals.append(texts)

    for idx, fighter in enumerate(fighters):
        link_id = _id_from_url(name_links[idx].get("href", ""))
        if link_id and link_id != fighter["id"]:
            raise ParticipantError("totals fighter id mismatch vs header persons")
        kd = _parse_int_label(col_vals[0][idx], label="KD")
        sig_l, sig_a = _parse_of_pattern(col_vals[1][idx])
        sig_pct = _parse_pct(col_vals[2][idx])
        tot_l, tot_a = _parse_of_pattern(col_vals[3][idx])
        td_l, td_a = _parse_of_pattern(col_vals[4][idx])
        td_pct = _parse_pct(col_vals[5][idx])
        sub_att = _parse_int_label(col_vals[6][idx], label="Sub. att")
        rev = _parse_int_label(col_vals[7][idx], label="Rev.")
        ctrl = _parse_ctrl(col_vals[8][idx])
        fighter["stats"] = {
            "kd": kd,
            "significant_strikes_landed": sig_l,
            "significant_strikes_attempted": sig_a,
            "significant_strikes_pct": sig_pct,
            "total_strikes_landed": tot_l,
            "total_strikes_attempted": tot_a,
            "takedowns_landed": td_l,
            "takedowns_attempted": td_a,
            "takedowns_pct": td_pct,
            "submission_attempts": sub_att,
            "reversals": rev,
            "control_seconds": ctrl,
        }

    fight_link = soup.select_one('a[href*="fight-details"]')
    external_fight_id = _id_from_url(fight_link["href"]) if fight_link else ""
    if not external_fight_id:
        raise ParserSchemaDriftError(
            "fight details missing external_fight_id (no fight-details link/id)"
        )

    return {
        "external_fight_id": external_fight_id,
        "fighter_a": fighters[0],
        "fighter_b": fighters[1],
        "winner_id": winner_id,
        "method": method,
        "ending_round": ending_round,
        "time_str": time_text or None,
    }


def _extract_labeled_text(soup: BeautifulSoup, label: str) -> str:
    for item in soup.select("i.b-fight-details__text-item"):
        label_text = item.get_text(" ", strip=True)
        if not label_text.lower().startswith(label.lower()):
            continue
        # Prefer value inside the label node after ':'.
        if ":" in label_text:
            after = label_text.split(":", 1)[1].strip()
            if after:
                return after
        # UFCStats often places the value as a following text sibling.
        sibling = item.next_sibling
        if sibling is not None:
            value = str(sibling).strip()
            if value:
                # Stop at next label boundary if multiple items share a parent.
                return value.split("\n")[0].strip()
        parent = item.parent
        if parent is not None:
            full = parent.get_text(" ", strip=True)
            # Find "<Label>:" and take the next token.
            marker = f"{label}:"
            lowered = full.lower()
            idx = lowered.find(marker.lower())
            if idx >= 0:
                remainder = full[idx + len(marker) :].strip()
                if remainder:
                    return remainder.split(" ")[0].strip()
    return ""


def _find_totals_table(soup: BeautifulSoup):
    for table in soup.select("table"):
        headers = [th.get_text(" ", strip=True) for th in table.select("thead th")]
        if not headers:
            continue
        if headers[0].lower().startswith("fighter") and any(
            "kd" == h.lower() for h in headers
        ):
            return table
    raise ParserSchemaDriftError("totals table with required KD column not found")
