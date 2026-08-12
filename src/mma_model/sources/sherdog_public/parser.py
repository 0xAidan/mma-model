"""Parse public Sherdog fighter HTML into typed dicts (DWCS-105)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mma_model.sources.sherdog_public.errors import ParserSchemaDriftError

SOURCE_SHERDOG_PUBLIC = "sherdog_public"
REQUIRED_SCHEMA = "sherdog_fight_history_v1"
REQUIRED_HEADERS = (
    "result",
    "opponent",
    "event",
    "date",
    "method",
    "round",
    "time",
    "class",
)


def _header_key(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _optional(value: str) -> str | None:
    text = value.strip()
    if text in {"", "—", "-", "n/a", "unknown"}:
        return None
    return text


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdigit():
        raise ParserSchemaDriftError(f"malformed round: {value!r}")
    return int(value)


def _optional_int_attr(row, key: str) -> int | None:
    raw = row.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    if not text.isdigit():
        raise ParserSchemaDriftError(f"malformed {key}: {raw!r}")
    return int(text)


def _parse_record(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    parts = text.replace(" ", "").split("-")
    if len(parts) < 2:
        return {"text": text, "wins": None, "losses": None, "draws": None, "no_contests": None}

    def _num(raw: str) -> int | None:
        return int(raw) if raw.isdigit() else None

    return {
        "text": text,
        "wins": _num(parts[0]),
        "losses": _num(parts[1]),
        "draws": _num(parts[2]) if len(parts) > 2 else 0,
        "no_contests": _num(parts[3]) if len(parts) > 3 else 0,
        "classification": "unknown",
    }


def parse_fighter_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    profile = soup.select_one("[data-fighter-id]")
    if profile is None:
        raise ParserSchemaDriftError("missing fighter profile data-fighter-id")
    fighter_id = str(profile.get("data-fighter-id") or "").strip()
    name_el = soup.select_one("[itemprop='name'], [data-name]")
    fighter_name = str(profile.get("data-name") or (name_el.get_text(strip=True) if name_el else "")).strip()
    if not fighter_id or not fighter_name:
        raise ParserSchemaDriftError("missing fighter id or name")

    table = soup.select_one(f'table[data-schema="{REQUIRED_SCHEMA}"]')
    if table is None:
        raise ParserSchemaDriftError(f"missing results table schema {REQUIRED_SCHEMA}")

    headers = [_header_key(th.get_text()) for th in table.select("thead th")]
    if tuple(headers) != REQUIRED_HEADERS:
        raise ParserSchemaDriftError(f"results headers drifted: {headers}")

    bouts: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str, int]] = set()
    left_truncated = str(profile.get("data-left-truncated") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    source_url = str(profile.get("data-source-url") or "").strip() or None
    for row in table.select("tbody tr"):
        bout_id = str(row.get("data-bout-id") or "").strip()
        if not bout_id:
            raise ParserSchemaDriftError("fight row missing data-bout-id")
        version_kind = str(row.get("data-version-kind") or "event_night").strip()
        revision = int(row.get("data-revision") or 1)
        dup_key = (bout_id, version_kind, revision)
        if dup_key in seen_ids:
            raise ParserSchemaDriftError(f"duplicate bout id: {bout_id}")
        seen_ids.add(dup_key)
        cells = row.select("td")
        if len(cells) != len(REQUIRED_HEADERS):
            raise ParserSchemaDriftError("fight row cell count drifted")
        values = {REQUIRED_HEADERS[i]: cells[i].get_text(" ", strip=True) for i in range(len(cells))}
        opponent_link = cells[1].select_one("a[href]")
        opponent_id = None
        if opponent_link is not None:
            path = urlparse(str(opponent_link.get("href") or "")).path.strip("/")
            parts = [p for p in path.split("/") if p]
            if parts:
                opponent_id = parts[-1]
        if opponent_id and opponent_id == fighter_id:
            raise ParserSchemaDriftError("swapped or self opponent id")
        classification = str(row.get("data-classification") or values["class"] or "unknown").strip().lower()
        if classification not in {"professional", "amateur", "unknown"}:
            classification = "unknown"
        regulated = str(row.get("data-regulated-us") or "unknown").strip().lower()
        if regulated not in {"true", "false", "unknown"}:
            regulated = "unknown"
        result = str(row.get("data-result") or values["result"] or "unknown").strip().lower()
        if result not in {"win", "loss", "draw", "nc", "unknown", "cancelled"}:
            result = "unknown"
        status = str(row.get("data-status") or "completed").strip().lower()
        version_kind = str(row.get("data-version-kind") or "event_night").strip()
        revision = int(row.get("data-revision") or 1)
        bouts.append(
            {
                "external_bout_id": bout_id,
                "opponent_name": values["opponent"],
                "opponent_external_id": opponent_id,
                "event_name": _optional(values["event"]),
                "event_date": _optional(values["date"]),
                "event_external_id": row.get("data-event-id"),
                "promotion": _optional(str(row.get("data-promotion") or "")),
                "method": _optional(values["method"]),
                "ending_round": _parse_int(_optional(values["round"])),
                "time_str": _optional(values["time"]),
                "scheduled_rounds": _optional_int_attr(row, "data-scheduled-rounds"),
                "classification": classification,
                "regulated_us": regulated,
                "result": result,
                "bout_status": status,
                "version_kind": version_kind,
                "revision": revision,
                "adjudicated_at": row.get("data-adjudicated-at"),
                "source_published_at": row.get("data-source-published-at"),
                "missing_reason": row.get("data-missing-reason"),
                "left_truncated": left_truncated,
            }
        )

    next_link = soup.select_one('a[rel="next"]')
    next_url = str(next_link.get("href")) if next_link is not None else None
    current = soup.select_one(".record, [data-record]")
    current_text = None
    if current is not None:
        current_text = str(current.get("data-record") or current.get_text(strip=True) or "") or None

    return {
        "source": SOURCE_SHERDOG_PUBLIC,
        "fighter_external_id": fighter_id,
        "fighter_name": fighter_name,
        "wikidata_id": profile.get("data-wikidata-id") or None,
        "current_record": _parse_record(current_text),
        "explicit_pre_fight_record": None,
        "bouts": bouts,
        "next_url": next_url,
        "left_truncated": left_truncated,
        "source_url": source_url,
    }
