#!/usr/bin/env python3
"""DWCS-002: build verified 2017–2025 DWCS event/bout manifests.

Default path is offline and deterministic: read committed minimal factual
fixtures under tests/fixtures/manifests/source/, reconcile event-night vs
current results, write versioned JSONL manifests + counts/mismatch reports,
and optionally verify expected universe totals.

Optional ``--refresh-espn`` re-extracts factual fields from ESPN's undocumented
public site/core JSON endpoints and rewrites the committed source fixtures.
That network path is a spike convenience only — not a production dependency —
and must never scrape prohibited HTML sources.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

ResultClass = Literal["decisive", "draw", "no_contest", "unknown"]
SeriesVariant = Literal["standard", "brazil"]

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DIR = REPO_ROOT / "tests" / "fixtures" / "manifests" / "source"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "manifests"
EVENTS_FACTS_NAME = "espn_events_facts_v1.jsonl"
BOUTS_FACTS_NAME = "espn_bouts_facts_v1.jsonl"
RECON_FACTS_NAME = "event_night_reconciliations_v1.jsonl"
CANCEL_FACTS_NAME = "cancellations_replacements_v1.jsonl"

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_ID = "dwcs_universe"
MANIFEST_VERSION = "1.0.0"

EXPECTED_ALL_CARDS = 89
EXPECTED_ALL_BOUTS = 440
EXPECTED_STANDARD_CARDS = 86
EXPECTED_STANDARD_BOUTS = 425
EXPECTED_BRAZIL_CARDS = 3
EXPECTED_BRAZIL_BOUTS = 15
EXPECTED_EVENT_NIGHT = {"decisive": 438, "draw": 1, "no_contest": 1}
EXPECTED_CURRENT = {"decisive": 431, "draw": 1, "no_contest": 8}

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_CORE_STATUS = (
    "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/"
    "{eid}/competitions/{cid}/status"
)

SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "secret",
    "credential",
)


def normalize_fighter_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", ascii_only.lower())
    return " ".join(cleaned.split())


def canonical_participant_pair(names: Sequence[str]) -> tuple[str, str]:
    """Deterministic unordered participant pair key (sorted normalized names)."""
    if len(names) != 2:
        raise ValueError(f"expected exactly two participants, got {len(names)}")
    left, right = sorted(normalize_fighter_name(n) for n in names)
    if not left or not right:
        raise ValueError("participant names must be non-empty after normalization")
    if left == right:
        raise ValueError(f"duplicate normalized participant name within bout: {left}")
    return left, right


def ordered_participants(parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order participants by normalized display name for stable IDs/fields."""
    enriched: list[tuple[str, dict[str, Any]]] = []
    for part in parts:
        name = str(part.get("display_name") or "")
        enriched.append((normalize_fighter_name(name), dict(part)))
    enriched.sort(key=lambda item: (item[0], str(item[1].get("espn_athlete_id") or "")))
    return [item[1] for item in enriched]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"missing JSONL fixture: {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"JSONL row must be an object at {path}:{line_no}")
            rows.append(payload)
    return rows


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_season_week(name: str) -> tuple[int | None, int | None]:
    lower = name.lower()
    match = re.search(r"season\s+(\d+).*week\s+(\d+)", lower)
    if match:
        return int(match.group(1)), int(match.group(2))
    brazil = re.search(r"brazil\s+(\d+)", lower)
    if brazil:
        return None, int(brazil.group(1))
    return None, None


def refresh_espn_facts(
    *,
    source_dir: Path,
    through_year: int,
    client: httpx.Client | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pull ESPN yearly scoreboards and write minimal factual fixtures."""
    owns_client = client is None
    http = client or httpx.Client(timeout=60.0)
    events: list[dict[str, Any]] = []
    bouts: list[dict[str, Any]] = []
    try:
        for year in range(2017, through_year + 1):
            response = http.get(ESPN_SCOREBOARD, params={"dates": year, "limit": 1000})
            response.raise_for_status()
            payload = response.json()
            for event in payload.get("events") or []:
                name = str(event.get("name") or "")
                if "contender" not in name.lower():
                    continue
                espn_event_id = str(event["id"])
                variant: SeriesVariant = (
                    "brazil" if "brazil" in name.lower() else "standard"
                )
                season_number, week_number = _parse_season_week(name)
                competitions = event.get("competitions") or []
                venue_raw = (competitions[0].get("venue") if competitions else None) or {}
                address = venue_raw.get("address") or {}
                venue = None
                if venue_raw:
                    venue = {
                        "name": venue_raw.get("fullName"),
                        "city": address.get("city"),
                        "state": address.get("state"),
                        "country": address.get("country"),
                    }
                events.append(
                    {
                        "espn_event_id": espn_event_id,
                        "name": name,
                        "short_name": event.get("shortName"),
                        "occurrence_date": event.get("date"),
                        "calendar_year": year,
                        "series_variant": variant,
                        "season_number": season_number,
                        "week_number": week_number,
                        "status": ((event.get("status") or {}).get("type") or {}).get(
                            "name"
                        ),
                        "venue": venue,
                        "bout_count": len(competitions),
                        "source": {
                            "provider": "espn_site_api_undocumented_public",
                            "scoreboard_url": (
                                f"{ESPN_SCOREBOARD}?dates={year}&limit=1000"
                            ),
                            "event_url": (
                                "https://www.espn.com/mma/fightcenter/_/id/"
                                f"{espn_event_id}/league/ufc"
                            ),
                        },
                    }
                )
                for competition in competitions:
                    cid = str(competition["id"])
                    participants = []
                    for competitor in competition.get("competitors") or []:
                        athlete = competitor.get("athlete") or {}
                        participants.append(
                            {
                                "espn_athlete_id": str(
                                    athlete.get("id") or competitor.get("id") or ""
                                ),
                                "display_name": athlete.get("displayName")
                                or athlete.get("fullName")
                                or "",
                                "winner": competitor.get("winner"),
                                "order": competitor.get("order"),
                            }
                        )
                    winners = [part["winner"] for part in participants]
                    result_name = None
                    result_display = None
                    if winners.count(True) == 1:
                        current_result: ResultClass = "decisive"
                        status_url = None
                    else:
                        status_url = ESPN_CORE_STATUS.format(eid=espn_event_id, cid=cid)
                        status_payload = http.get(status_url).json()
                        result = status_payload.get("result") or {}
                        result_name = result.get("name")
                        result_display = result.get("displayName")
                        normalized = str(result_name or "").replace("_", "-").lower()
                        if normalized == "draw":
                            current_result = "draw"
                        elif normalized in {"no-contest", "nc"}:
                            current_result = "no_contest"
                        else:
                            current_result = "unknown"
                    source: dict[str, Any] = {
                        "provider": "espn_site_api_undocumented_public",
                        "fightcenter_url": (
                            "https://www.espn.com/mma/fightcenter/_/id/"
                            f"{espn_event_id}/league/ufc"
                        ),
                    }
                    if status_url:
                        source["competition_status_url"] = status_url
                    bouts.append(
                        {
                            "espn_event_id": espn_event_id,
                            "espn_competition_id": cid,
                            "occurrence_start": competition.get("date")
                            or competition.get("startDate"),
                            "occurrence_end": competition.get("endDate"),
                            "weight_class": (competition.get("type") or {}).get("text")
                            or (competition.get("type") or {}).get("abbreviation"),
                            "participants": participants,
                            "current_result_class": current_result,
                            "current_result_name": result_name,
                            "current_result_display": result_display,
                            "status_name": (
                                (competition.get("status") or {}).get("type") or {}
                            ).get("name"),
                            "source": source,
                        }
                    )
    finally:
        if owns_client:
            http.close()

    events_sorted = sorted(
        events, key=lambda row: (row["occurrence_date"] or "", row["espn_event_id"])
    )
    bouts_sorted = sorted(
        bouts,
        key=lambda row: (row["espn_event_id"], row["espn_competition_id"]),
    )
    write_jsonl(source_dir / EVENTS_FACTS_NAME, events_sorted)
    write_jsonl(source_dir / BOUTS_FACTS_NAME, bouts_sorted)
    return events_sorted, bouts_sorted


def _quality_flags_for_event(event: Mapping[str, Any]) -> list[str]:
    flags = [
        "espn_undocumented_public_json",
        "ufcstats_event_id_unmapped",
        "ufc_com_event_id_unmapped",
        "publication_timestamp_unknown",
        "cancellations_not_enumerated_from_scoreboard",
    ]
    if not event.get("occurrence_date"):
        flags.append("occurrence_timestamp_missing")
    else:
        flags.append("occurrence_timestamp_from_espn_event_date")
    if event.get("series_variant") == "brazil":
        flags.append("brazil_air_date_may_differ_from_occurrence_date")
    return flags


def _quality_flags_for_bout(
    bout: Mapping[str, Any],
    *,
    has_recon: bool,
    version_state: str,
) -> list[str]:
    flags = [
        "espn_undocumented_public_json",
        "ufcstats_bout_id_unmapped",
    ]
    if not bout.get("occurrence_start"):
        flags.append("occurrence_timestamp_missing")
    else:
        flags.append("occurrence_timestamp_from_espn_competition_date")
    if not has_recon and bout.get("current_result_class") != "decisive":
        flags.append("event_night_result_assumed_equal_current_without_override")
    if version_state == "reversed_to_no_contest":
        flags.append("event_night_from_documented_reconciliation")
    if version_state == "unchanged" and has_recon:
        flags.append("event_night_confirmed_by_documented_reconciliation")
    return flags


def build_manifests(
    *,
    events_facts: Sequence[Mapping[str, Any]],
    bouts_facts: Sequence[Mapping[str, Any]],
    reconciliations: Sequence[Mapping[str, Any]],
    cancellations_replacements: Sequence[Mapping[str, Any]] | None = None,
    through_year: int,
    built_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Normalize facts into versioned event/bout manifests plus reports."""
    built = built_at or datetime.now(UTC).replace(microsecond=0).isoformat()
    cancel_rows = list(cancellations_replacements or [])

    events_by_id = {str(row["espn_event_id"]): dict(row) for row in events_facts}
    if len(events_by_id) != len(events_facts):
        raise ValueError("duplicate espn_event_id in event facts")

    recon_by_comp = {
        str(row["espn_competition_id"]): dict(row) for row in reconciliations
    }
    if len(recon_by_comp) != len(reconciliations):
        raise ValueError("duplicate espn_competition_id in reconciliations")

    filtered_events = [
        events_by_id[eid]
        for eid in sorted(
            events_by_id,
            key=lambda key: (
                events_by_id[key].get("occurrence_date") or "",
                key,
            ),
        )
        if int(events_by_id[eid].get("calendar_year") or 0) <= through_year
    ]
    allowed_event_ids = {str(row["espn_event_id"]) for row in filtered_events}

    event_manifest: list[dict[str, Any]] = []
    for event in filtered_events:
        espn_event_id = str(event["espn_event_id"])
        related_changes = [
            {
                "kind": row.get("kind"),
                "original_participants": row.get("original_participants"),
                "replacement_participants": row.get("replacement_participants"),
                "evidence_urls": row.get("evidence_urls") or [],
                "notes": row.get("notes"),
            }
            for row in cancel_rows
            if str(row.get("espn_event_id")) == espn_event_id
        ]
        event_manifest.append(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "manifest_id": MANIFEST_ID,
                "manifest_version": MANIFEST_VERSION,
                "event_id": f"dwcs:event:espn:{espn_event_id}",
                "espn_event_id": espn_event_id,
                "ufcstats_event_id": None,
                "name": event.get("name"),
                "short_name": event.get("short_name"),
                "series_variant": event.get("series_variant"),
                "season_number": event.get("season_number"),
                "week_number": event.get("week_number"),
                "calendar_year": event.get("calendar_year"),
                "status": "completed"
                if str(event.get("status") or "").endswith("FINAL")
                or event.get("status") == "STATUS_FINAL"
                else event.get("status"),
                "occurrence_timestamp": event.get("occurrence_date"),
                "publication_timestamp": None,
                "venue": event.get("venue"),
                "bout_count_source": event.get("bout_count"),
                "cancellations_replacements": related_changes,
                "source_ids": {
                    "espn_event_id": espn_event_id,
                    "ufcstats_event_id": None,
                    "ufc_com_event_id": None,
                },
                "source_urls": {
                    "espn_fightcenter": (event.get("source") or {}).get("event_url"),
                    "espn_scoreboard": (event.get("source") or {}).get("scoreboard_url"),
                },
                "data_quality_flags": _quality_flags_for_event(event),
                "built_at": built,
            }
        )

    bout_manifest: list[dict[str, Any]] = []
    pairs_by_event: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for bout in sorted(
        bouts_facts,
        key=lambda row: (str(row["espn_event_id"]), str(row["espn_competition_id"])),
    ):
        espn_event_id = str(bout["espn_event_id"])
        if espn_event_id not in allowed_event_ids:
            continue
        espn_competition_id = str(bout["espn_competition_id"])
        participants = ordered_participants(bout.get("participants") or [])
        if len(participants) != 2:
            raise ValueError(
                f"bout {espn_competition_id} does not have exactly two participants"
            )
        names = [str(part.get("display_name") or "") for part in participants]
        pair = canonical_participant_pair(names)
        if pair in pairs_by_event[espn_event_id]:
            raise ValueError(
                f"duplicate canonical participant pair {pair} in event {espn_event_id}"
            )
        pairs_by_event[espn_event_id].add(pair)

        current_class = str(bout.get("current_result_class") or "unknown")
        recon = recon_by_comp.get(espn_competition_id)
        if recon is not None:
            event_night_class = str(recon.get("event_night_result_class") or "unknown")
            version_state = str(recon.get("version_state") or "documented")
            event_night_winner_id = recon.get("event_night_winner_espn_athlete_id")
            event_night_winner_name = recon.get("event_night_winner_name")
            evidence_urls = list(recon.get("evidence_urls") or [])
            recon_notes = recon.get("notes")
            # Guard: reconciliation current class must match ESPN current class.
            recon_current = str(recon.get("current_result_class") or "")
            if recon_current and recon_current != current_class:
                raise ValueError(
                    f"reconciliation current_result_class mismatch for "
                    f"{espn_competition_id}: fixture={recon_current} espn={current_class}"
                )
        else:
            event_night_class = current_class
            version_state = "assumed_equal_to_current"
            event_night_winner_id = None
            event_night_winner_name = None
            evidence_urls = []
            recon_notes = None
            if current_class == "decisive":
                winners = [
                    part
                    for part in participants
                    if part.get("winner") is True
                ]
                if len(winners) == 1:
                    event_night_winner_id = winners[0].get("espn_athlete_id")
                    event_night_winner_name = winners[0].get("display_name")

        event_row = events_by_id[espn_event_id]
        bout_manifest.append(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "manifest_id": MANIFEST_ID,
                "manifest_version": MANIFEST_VERSION,
                "bout_id": f"dwcs:bout:espn:{espn_competition_id}",
                "event_id": f"dwcs:event:espn:{espn_event_id}",
                "espn_event_id": espn_event_id,
                "espn_competition_id": espn_competition_id,
                "ufcstats_bout_id": None,
                "series_variant": event_row.get("series_variant"),
                "calendar_year": event_row.get("calendar_year"),
                "season_number": event_row.get("season_number"),
                "week_number": event_row.get("week_number"),
                "status": "occurred",
                "occurrence_timestamp": bout.get("occurrence_start"),
                "occurrence_end_timestamp": bout.get("occurrence_end"),
                "publication_timestamp": None,
                "weight_class": bout.get("weight_class"),
                "participants": [
                    {
                        "espn_athlete_id": part.get("espn_athlete_id") or None,
                        "display_name": part.get("display_name"),
                        "normalized_name": normalize_fighter_name(
                            str(part.get("display_name") or "")
                        ),
                        "current_winner_flag": part.get("winner"),
                    }
                    for part in participants
                ],
                "canonical_participant_pair": list(pair),
                "event_night_result": {
                    "class": event_night_class,
                    "winner_espn_athlete_id": event_night_winner_id,
                    "winner_display_name": event_night_winner_name,
                },
                "current_result": {
                    "class": current_class,
                    "espn_result_name": bout.get("current_result_name"),
                    "espn_result_display": bout.get("current_result_display"),
                },
                "version_state": version_state,
                "reconciliation_evidence_urls": evidence_urls,
                "reconciliation_notes": recon_notes,
                "source_ids": {
                    "espn_event_id": espn_event_id,
                    "espn_competition_id": espn_competition_id,
                    "ufcstats_bout_id": None,
                },
                "source_urls": {
                    "espn_fightcenter": (bout.get("source") or {}).get("fightcenter_url"),
                    "espn_competition_status": (bout.get("source") or {}).get(
                        "competition_status_url"
                    ),
                },
                "data_quality_flags": _quality_flags_for_bout(
                    bout,
                    has_recon=recon is not None,
                    version_state=version_state,
                ),
                "built_at": built,
            }
        )

    # Attach actual occurred bout counts onto events for referential clarity.
    bouts_per_event = Counter(row["espn_event_id"] for row in bout_manifest)
    for event in event_manifest:
        event["occurred_bout_count"] = bouts_per_event.get(event["espn_event_id"], 0)

    counts, mismatches = summarize(
        event_manifest,
        bout_manifest,
        through_year=through_year,
        built_at=built,
    )
    return event_manifest, bout_manifest, counts, mismatches


def summarize(
    events: Sequence[Mapping[str, Any]],
    bouts: Sequence[Mapping[str, Any]],
    *,
    through_year: int,
    built_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic season counts and mismatch report."""
    by_year_cards: dict[str, Counter[str]] = defaultdict(Counter)
    by_year_bouts: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        year = str(event.get("calendar_year"))
        variant = str(event.get("series_variant") or "unknown")
        by_year_cards[year][variant] += 1
        by_year_cards[year]["all"] += 1
    for bout in bouts:
        year = str(bout.get("calendar_year"))
        variant = str(bout.get("series_variant") or "unknown")
        by_year_bouts[year][variant] += 1
        by_year_bouts[year]["all"] += 1

    event_night = Counter(str(b["event_night_result"]["class"]) for b in bouts)
    current = Counter(str(b["current_result"]["class"]) for b in bouts)
    standard_events = [e for e in events if e.get("series_variant") == "standard"]
    brazil_events = [e for e in events if e.get("series_variant") == "brazil"]
    standard_bouts = [b for b in bouts if b.get("series_variant") == "standard"]
    brazil_bouts = [b for b in bouts if b.get("series_variant") == "brazil"]

    counts = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": MANIFEST_ID,
        "manifest_version": MANIFEST_VERSION,
        "through_year": through_year,
        "built_at": built_at,
        "cards": {
            "all": len(events),
            "standard": len(standard_events),
            "brazil": len(brazil_events),
        },
        "bouts": {
            "all": len(bouts),
            "standard": len(standard_bouts),
            "brazil": len(brazil_bouts),
        },
        "event_night_results": dict(sorted(event_night.items())),
        "current_results": dict(sorted(current.items())),
        "by_calendar_year": {
            year: {
                "cards": dict(sorted(by_year_cards[year].items())),
                "bouts": dict(sorted(by_year_bouts[year].items())),
            }
            for year in sorted(set(by_year_cards) | set(by_year_bouts))
        },
        "expected": {
            "cards": {
                "all": EXPECTED_ALL_CARDS,
                "standard": EXPECTED_STANDARD_CARDS,
                "brazil": EXPECTED_BRAZIL_CARDS,
            },
            "bouts": {
                "all": EXPECTED_ALL_BOUTS,
                "standard": EXPECTED_STANDARD_BOUTS,
                "brazil": EXPECTED_BRAZIL_BOUTS,
            },
            "event_night_results": dict(EXPECTED_EVENT_NIGHT),
            "current_results": dict(EXPECTED_CURRENT),
        },
    }

    mismatches: list[dict[str, Any]] = []

    def expect_equal(path: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            mismatches.append(
                {
                    "path": path,
                    "actual": actual,
                    "expected": expected,
                    "severity": "mismatch",
                }
            )

    if through_year >= 2025:
        expect_equal("cards.all", counts["cards"]["all"], EXPECTED_ALL_CARDS)
        expect_equal("cards.standard", counts["cards"]["standard"], EXPECTED_STANDARD_CARDS)
        expect_equal("cards.brazil", counts["cards"]["brazil"], EXPECTED_BRAZIL_CARDS)
        expect_equal("bouts.all", counts["bouts"]["all"], EXPECTED_ALL_BOUTS)
        expect_equal(
            "bouts.standard", counts["bouts"]["standard"], EXPECTED_STANDARD_BOUTS
        )
        expect_equal("bouts.brazil", counts["bouts"]["brazil"], EXPECTED_BRAZIL_BOUTS)
        for key, expected in EXPECTED_EVENT_NIGHT.items():
            expect_equal(
                f"event_night_results.{key}",
                event_night.get(key, 0),
                expected,
            )
        for key, expected in EXPECTED_CURRENT.items():
            expect_equal(
                f"current_results.{key}",
                current.get(key, 0),
                expected,
            )

    # Structural / referential checks always run.
    event_ids = {e["event_id"] for e in events}
    bout_ids = [b["bout_id"] for b in bouts]
    if len(set(bout_ids)) != len(bout_ids):
        mismatches.append(
            {
                "path": "bout_id",
                "actual": "duplicates_present",
                "expected": "unique",
                "severity": "integrity_error",
            }
        )
    for bout in bouts:
        if bout["event_id"] not in event_ids:
            mismatches.append(
                {
                    "path": f"bout.event_id:{bout['bout_id']}",
                    "actual": bout["event_id"],
                    "expected": "known_event_id",
                    "severity": "referential_integrity",
                }
            )
        if bout.get("ufcstats_bout_id") is not None:
            mismatches.append(
                {
                    "path": f"bout.ufcstats_bout_id:{bout['bout_id']}",
                    "actual": bout.get("ufcstats_bout_id"),
                    "expected": None,
                    "severity": "unexpected_invented_id",
                }
            )
        for flag_name, value in (
            ("occurrence_timestamp", bout.get("occurrence_timestamp")),
            ("publication_timestamp", bout.get("publication_timestamp")),
        ):
            if value == "":
                mismatches.append(
                    {
                        "path": f"bout.{flag_name}:{bout['bout_id']}",
                        "actual": value,
                        "expected": "null_or_evidenced_timestamp",
                        "severity": "invented_empty_timestamp",
                    }
                )

    for event in events:
        if event.get("ufcstats_event_id") is not None:
            mismatches.append(
                {
                    "path": f"event.ufcstats_event_id:{event['event_id']}",
                    "actual": event.get("ufcstats_event_id"),
                    "expected": None,
                    "severity": "unexpected_invented_id",
                }
            )
        source_count = event.get("bout_count_source")
        occurred = event.get("occurred_bout_count")
        if source_count is not None and occurred is not None and source_count != occurred:
            mismatches.append(
                {
                    "path": f"event.bout_count:{event['event_id']}",
                    "actual": {"source": source_count, "occurred": occurred},
                    "expected": "equal",
                    "severity": "bout_count_drift",
                }
            )

    # Explicit unverifiable fields (not failures unless verify requires otherwise).
    open_gaps = [
        {
            "path": "ufcstats_ids",
            "actual": "unmapped",
            "expected": "lawful_mapped_ids_when_available",
            "severity": "unverifiable_without_approved_ufcstats_mapping",
        },
        {
            "path": "publication_timestamps",
            "actual": None,
            "expected": "evidenced_publication_time",
            "severity": "unverifiable_from_espn_scoreboard_alone",
        },
        {
            "path": "full_cancellation_replacement_ledger",
            "actual": "scoreboard_occurred_bouts_only",
            "expected": "complete_announced_card_diff",
            "severity": "unverifiable_without_licensed_schedule_history",
        },
    ]

    report = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": MANIFEST_ID,
        "manifest_version": MANIFEST_VERSION,
        "through_year": through_year,
        "built_at": built_at,
        "ok": len(mismatches) == 0,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "open_gaps": open_gaps,
        "source_caveats": [
            (
                "ESPN site/core JSON is an undocumented public read-only secondary "
                "reconciliation source for this Phase 0 spike, not a production dependency."
            ),
            (
                "No Bet365/Tapology/Sherdog/FightMatrix/UFC/UFCStats HTML scraping was used "
                "to build or refresh these manifests."
            ),
            (
                "UFC athlete pages and other linked official/public articles were used only "
                "as cited evidence for event-night vs current result reconciliations."
            ),
        ],
    }
    return counts, report


def assert_no_secrets(rows: Iterable[Mapping[str, Any]]) -> None:
    """Reject accidental secret-looking keys in committed outputs."""
    stack: list[Any] = list(rows)
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, value in item.items():
                key_l = str(key).lower()
                if any(fragment in key_l for fragment in SECRET_KEY_FRAGMENTS):
                    raise ValueError(f"refusing to emit secret-like key: {key}")
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def load_source_bundle(source_dir: Path) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    events = read_jsonl(source_dir / EVENTS_FACTS_NAME)
    bouts = read_jsonl(source_dir / BOUTS_FACTS_NAME)
    recon = read_jsonl(source_dir / RECON_FACTS_NAME)
    cancel_path = source_dir / CANCEL_FACTS_NAME
    cancels = read_jsonl(cancel_path) if cancel_path.is_file() else []
    return events, bouts, recon, cancels


def run_build(
    *,
    source_dir: Path,
    out_dir: Path,
    through_year: int,
    refresh_espn: bool,
    verify: bool,
    built_at: str | None = None,
) -> dict[str, Any]:
    if refresh_espn:
        refresh_espn_facts(source_dir=source_dir, through_year=through_year)

    events_facts, bouts_facts, recon, cancels = load_source_bundle(source_dir)
    events, bouts, counts, mismatches = build_manifests(
        events_facts=events_facts,
        bouts_facts=bouts_facts,
        reconciliations=recon,
        cancellations_replacements=cancels,
        through_year=through_year,
        built_at=built_at,
    )
    assert_no_secrets(events)
    assert_no_secrets(bouts)

    write_jsonl(out_dir / "dwcs_events_v1.jsonl", events)
    write_jsonl(out_dir / "dwcs_bouts_v1.jsonl", bouts)
    write_json(out_dir / "dwcs_counts_v1.json", counts)
    write_json(out_dir / "dwcs_mismatches_v1.json", mismatches)

    result = {
        "events_path": str(out_dir / "dwcs_events_v1.jsonl"),
        "bouts_path": str(out_dir / "dwcs_bouts_v1.jsonl"),
        "counts": counts,
        "mismatches": mismatches,
    }
    if verify and not mismatches["ok"]:
        raise SystemExit(
            "verification failed; see "
            f"{out_dir / 'dwcs_mismatches_v1.json'} "
            f"({mismatches['mismatch_count']} mismatches)"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--through",
        type=int,
        default=2025,
        help="Include completed DWCS cards with calendar_year <= this value",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help="Directory with committed minimal factual JSONL fixtures",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for versioned manifests and reports",
    )
    parser.add_argument(
        "--refresh-espn",
        action="store_true",
        help=(
            "Optional network refresh of ESPN factual fixtures "
            "(undocumented public JSON; not a production dependency)"
        ),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Exit non-zero when expected 2017–2025 universe counts mismatch",
    )
    parser.add_argument(
        "--built-at",
        default=None,
        help="Optional fixed UTC timestamp for deterministic tests",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_build(
        source_dir=args.source_dir,
        out_dir=args.out_dir,
        through_year=args.through,
        refresh_espn=args.refresh_espn,
        verify=args.verify,
        built_at=args.built_at,
    )
    counts = result["counts"]
    print(
        json.dumps(
            {
                "ok": result["mismatches"]["ok"],
                "cards": counts["cards"],
                "bouts": counts["bouts"],
                "event_night_results": counts["event_night_results"],
                "current_results": counts["current_results"],
                "mismatch_count": result["mismatches"]["mismatch_count"],
                "events_path": result["events_path"],
                "bouts_path": result["bouts_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
