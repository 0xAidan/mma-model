"""Parse public Combat Registry / commission result HTML (DWCS-105)."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mma_model.sources.combat_registry.errors import ParserSchemaDriftError

SOURCE_COMBAT_REGISTRY = "combat_registry"
REQUIRED_SCHEMA = "combat_registry_public_results_v1"
REQUIRED_HEADERS = (
    "result",
    "fighter",
    "opponent",
    "event",
    "date",
    "method",
    "round",
    "time",
    "class",
    "commission",
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


def _us_commission(name: str | None) -> str:
    if not name:
        return "unknown"
    lowered = name.casefold()
    us_markers = (
        "nevada",
        "california",
        "texas",
        "florida",
        "new york",
        "new jersey",
        "colorado",
        "arizona",
        "ohio",
        "athletic commission",
        "csac",
        "nsc",
    )
    if any(marker in lowered for marker in us_markers):
        return "true"
    return "unknown"


def parse_results_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(f'table[data-schema="{REQUIRED_SCHEMA}"]')
    if table is None:
        raise ParserSchemaDriftError(f"missing results table schema {REQUIRED_SCHEMA}")

    headers = [_header_key(th.get_text()) for th in table.select("thead th")]
    if tuple(headers) != REQUIRED_HEADERS:
        raise ParserSchemaDriftError(f"results headers drifted: {headers}")

    fighter_id = str(table.get("data-fighter-id") or "").strip()
    fighter_name = str(table.get("data-fighter-name") or "").strip()
    if not fighter_id or not fighter_name:
        raise ParserSchemaDriftError("missing fighter id or name on results table")

    bouts: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str, int]] = set()
    left_truncated = str(table.get("data-left-truncated") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    source_url = str(table.get("data-source-url") or "").strip() or None
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
        opponent_link = cells[2].select_one("a[href]")
        opponent_id = None
        if opponent_link is not None:
            path = urlparse(str(opponent_link.get("href") or "")).path.strip("/")
            parts = [p for p in path.split("/") if p]
            if parts:
                opponent_id = parts[-1]
        opponent_id = opponent_id or row.get("data-opponent-id")
        if opponent_id and opponent_id == fighter_id:
            raise ParserSchemaDriftError("swapped or self opponent id")
        classification = str(row.get("data-classification") or values["class"] or "unknown").strip().lower()
        if classification not in {"professional", "amateur", "unknown"}:
            classification = "unknown"
        commission = _optional(values["commission"])
        regulated = str(row.get("data-regulated-us") or _us_commission(commission)).strip().lower()
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
                "commission": commission,
                "adjudicated_at": row.get("data-adjudicated-at"),
                "source_published_at": row.get("data-source-published-at"),
                "missing_reason": row.get("data-missing-reason"),
                "left_truncated": left_truncated,
            }
        )

    return {
        "source": SOURCE_COMBAT_REGISTRY,
        "fighter_external_id": fighter_id,
        "fighter_name": fighter_name,
        "wikidata_id": table.get("data-wikidata-id") or None,
        "current_record": None,
        "explicit_pre_fight_record": None,
        "bouts": bouts,
        "next_url": None,
        "left_truncated": left_truncated,
        "source_url": source_url,
    }
