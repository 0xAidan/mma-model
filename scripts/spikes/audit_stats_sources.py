#!/usr/bin/env python3
"""DWCS-003 read-only spike: audit licensed stats/identity production sources.

Builds a reproducible scorecard for BALLDONTLIE (provisional primary), API-Sports
(coverage probe), and SportsDataIO / Combat Registry (documented fields + quote
checklist). Applies the deterministic production source decision tree.

Never scrapes Tapology, Sherdog, FightMatrix, UFC/UFCStats HTML, or Bet365.
Missing credentials are recorded as not_configured / unknown — never as zero
coverage. Sportsbook odds are out of scope for source selection.

Phase 0 permits an explicit hard blocker when credentials/quotes are absent; the
acceptance evidence is the reproducible blocked/unknown state plus a corrected,
fixture-testable measurement path — not invented provider coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

AccessStatus = Literal[
    "not_configured",
    "auth_failed",
    "entitlement_blocked",
    "ok",
    "request_failed",
]
ObservationStatus = Literal["present", "absent", "unknown", "request_failed"]
MetricStatus = Literal["measured", "unknown", "blocked", "not_applicable"]
GateStatus = Literal["pass", "fail", "unknown", "blocked"]
DecisionPath = Literal[
    "balldontlie_primary",
    "sportsdataio_primary",
    "combat_registry_primary",
    "hard_blocker",
]
CaptureMode = Literal["fixtures", "live", "mixed"]
OutcomePairStatus = Literal["agree", "disagree", "unknown"]

SCORECARD_SCHEMA_VERSION = 1
TICKET_ID = "DWCS-003"
DIFFICULT_IDENTITY_SEED = "dwcs-003-difficult-identities-v1"
DIFFICULT_IDENTITY_SAMPLE_SIZE = 50
EVENT_COVERAGE_MIN = 0.98
BOUT_COVERAGE_MIN = 0.98
OUTCOME_AGREEMENT_MIN = 0.99
API_SPORTS_NON_OVERLAP_MIN = 0.10
MONTHLY_BUDGET_CAP_CENTS = 10000
THE_ODDS_API_CENTS = 3000
BALLDONTLIE_GOAT_CENTS = 3999
API_SPORTS_PROBE_CENTS = 1000

# Backward-compatible float views (derived from cents; not used for arithmetic).
MONTHLY_BUDGET_CAP_USD = MONTHLY_BUDGET_CAP_CENTS / 100
BALLDONTLIE_GOAT_USD = BALLDONTLIE_GOAT_CENTS / 100
THE_ODDS_API_USD = THE_ODDS_API_CENTS / 100
API_SPORTS_PROBE_USD = API_SPORTS_PROBE_CENTS / 100

BALLDONTLIE_BASE = "https://api.balldontlie.io/mma/v1"
API_SPORTS_BASE = "https://v1.mma.api-sports.io"

REQUIRED_FIGHT_FIELDS = (
    "id",
    "fighter1",
    "fighter2",
    "status",
    "date",
)
REQUIRED_STAT_FIELDS = (
    "significant_strikes_landed",
    "takedowns_landed",
    "control_time_seconds",
)

PROHIBITED_PRODUCTION_SOURCES = (
    "tapology_scrape",
    "sherdog_scrape",
    "fightmatrix_scrape",
    "ufcstats_html_scrape",
    "ufc_html_scrape",
    "bet365_scrape",
)

SCORECARD_SCHEMA_KEYS = (
    "schema_version",
    "ticket",
    "captured_at",
    "capture_mode",
    "live_measurements_claimed",
    "manifest",
    "audit_universes",
    "providers",
    "decision",
    "prohibited_sources",
    "evidence_timestamps",
    "budget_context",
    "handoff",
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

PAYLOAD_KEY_FRAGMENTS = (
    "raw_payload",
    "sample_fight",
    "sample_event",
    "full_response",
    "licensed_payload",
)


def normalize_fighter_name(name: str) -> str:
    """Lowercase, strip accents/punctuation, and collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9\s]", " ", ascii_only.lower())
    return " ".join(cleaned.split())


def usd_to_cents(value: Any) -> int:
    """Quantize a dollar amount to integer cents (half-up)."""
    quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def _as_access_status(value: Any) -> AccessStatus:
    text = str(value or "request_failed")
    allowed: set[str] = {
        "not_configured",
        "auth_failed",
        "entitlement_blocked",
        "ok",
        "request_failed",
    }
    if text in allowed:
        return text  # type: ignore[return-value]
    return "request_failed"


def _as_gate_status(value: Any) -> GateStatus:
    text = str(value or "unknown")
    if text in {"pass", "fail", "unknown", "blocked"}:
        return text  # type: ignore[return-value]
    return "unknown"


def cents_to_usd_str(cents: int) -> str:
    """Decimal-safe USD string with exact cents."""
    return f"{(Decimal(cents) / Decimal(100)).quantize(Decimal('0.01'))}"


def money_amount(*, cents: int) -> dict[str, Any]:
    return {"usd_cents": cents, "usd": cents_to_usd_str(cents)}


def _parse_iso_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filter_bouts_by_year(
    bouts: Sequence[Mapping[str, Any]],
    start_year: int,
    end_year: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bout in bouts:
        year = bout.get("calendar_year")
        if year is None:
            continue
        year_i = int(year)
        if start_year <= year_i <= end_year:
            rows.append(dict(bout))
    return rows


def extract_entrants(bouts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Unique DWCS entrants keyed by ESPN athlete id (fallback: normalized name)."""
    by_key: dict[str, dict[str, Any]] = {}
    for bout in bouts:
        for participant in bout.get("participants") or []:
            if not isinstance(participant, Mapping):
                continue
            display = str(participant.get("display_name") or "").strip()
            normalized = str(
                participant.get("normalized_name")
                or normalize_fighter_name(display)
            ).strip()
            athlete_id = participant.get("espn_athlete_id")
            key = str(athlete_id) if athlete_id not in (None, "") else f"name:{normalized}"
            if key not in by_key:
                by_key[key] = {
                    "entrant_key": key,
                    "espn_athlete_id": str(athlete_id) if athlete_id else None,
                    "display_name": display,
                    "normalized_name": normalized,
                    "calendar_years": [],
                    "series_variants": [],
                    "version_states": [],
                    "bout_ids": [],
                }
            row = by_key[key]
            year = bout.get("calendar_year")
            if year is not None and int(year) not in row["calendar_years"]:
                row["calendar_years"].append(int(year))
            variant = bout.get("series_variant")
            if variant and variant not in row["series_variants"]:
                row["series_variants"].append(str(variant))
            state = bout.get("version_state")
            if state and state not in row["version_states"]:
                row["version_states"].append(str(state))
            bout_id = bout.get("bout_id")
            if bout_id and bout_id not in row["bout_ids"]:
                row["bout_ids"].append(str(bout_id))
    entrants = list(by_key.values())
    entrants.sort(key=lambda row: (row["normalized_name"], row["entrant_key"]))
    return entrants


def _identity_difficulty_score(
    entrant: Mapping[str, Any],
    last_name_counts: Mapping[str, int],
) -> int:
    display = str(entrant.get("display_name") or "")
    normalized = str(entrant.get("normalized_name") or "")
    score = 0
    if any(ord(ch) > 127 for ch in display):
        score += 100
    if "-" in display or "'" in display:
        score += 40
    tokens = [t for t in normalized.split() if t]
    if len(tokens) >= 3:
        score += 30
    if len(tokens) >= 4:
        score += 15
    if tokens and len(tokens[-1]) <= 3:
        score += 25
    last = tokens[-1] if tokens else ""
    if last and last_name_counts.get(last, 0) >= 2:
        score += 35
    if "brazil" in {str(v) for v in entrant.get("series_variants") or []}:
        score += 50
    if "reversed_to_no_contest" in {str(v) for v in entrant.get("version_states") or []}:
        score += 45
    if "jr" in tokens or "sr" in tokens:
        score += 20
    return score


def difficult_identity_selection_method() -> str:
    return (
        "Deterministic ranking of 2023–2025 DWCS entrants by identity-difficulty "
        f"score (unicode/non-ASCII +{100}, hyphen/apostrophe +{40}, "
        f">=3 name tokens +{30}, short last name +{25}, colliding last name +{35}, "
        f"Brazil series +{50}, reversal participant +{45}, jr/sr +{20}), "
        f"tie-broken by sha256({DIFFICULT_IDENTITY_SEED}|entrant_key). "
        f"Take the top {DIFFICULT_IDENTITY_SAMPLE_SIZE}."
    )


def select_difficult_identity_sample(
    entrants: Sequence[Mapping[str, Any]],
    *,
    size: int = DIFFICULT_IDENTITY_SAMPLE_SIZE,
) -> list[dict[str, Any]]:
    last_name_counts: dict[str, int] = {}
    for entrant in entrants:
        tokens = str(entrant.get("normalized_name") or "").split()
        if tokens:
            last_name_counts[tokens[-1]] = last_name_counts.get(tokens[-1], 0) + 1

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for entrant in entrants:
        score = _identity_difficulty_score(entrant, last_name_counts)
        tie = _stable_hash(f"{DIFFICULT_IDENTITY_SEED}|{entrant['entrant_key']}")
        ranked.append((-score, tie, dict(entrant)))
    ranked.sort(key=lambda item: (item[0], item[1]))
    selected: list[dict[str, Any]] = []
    for neg_score, tie, entrant in ranked[:size]:
        row = dict(entrant)
        row["difficulty_score"] = -neg_score
        row["tie_break_hash"] = tie
        selected.append(row)
    return selected


def make_rate_metric(
    *,
    numerator: int | None,
    denominator: int | None,
    status: MetricStatus,
    reason: str | None = None,
) -> dict[str, Any]:
    rate: float | None
    if (
        status == "measured"
        and numerator is not None
        and denominator is not None
        and denominator > 0
    ):
        rate = numerator / denominator
    else:
        rate = None
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "status": status,
        "reason": reason,
    }


def classify_provider_access(
    *,
    api_key: str | None,
    http_status: int | None,
    body: Mapping[str, Any] | None,
) -> AccessStatus:
    if not api_key:
        return "not_configured"
    if http_status is None:
        return "request_failed"
    if http_status in {401, 403}:
        text = json.dumps(body or {}).lower()
        if "tier" in text or "entitlement" in text or "plan" in text or "access" in text:
            return "entitlement_blocked"
        return "auth_failed"
    if http_status >= 400:
        return "request_failed"
    return "ok"


def classify_observation_status(
    *,
    access_status: AccessStatus,
    matched: bool,
    request_failed: bool,
) -> ObservationStatus:
    if access_status in {"not_configured", "auth_failed", "entitlement_blocked"}:
        return "unknown"
    if request_failed or access_status == "request_failed":
        return "request_failed"
    if matched:
        return "present"
    return "absent"


def evaluate_rights_gate(rights: Mapping[str, Any]) -> dict[str, Any]:
    storage = rights.get("storage_allowed")
    modeling = rights.get("modeling_allowed")
    if storage is True and modeling is True:
        status: GateStatus = "pass"
    elif storage is False or modeling is False:
        status = "fail"
    else:
        status = "unknown"
    return {
        "status": status,
        "storage_allowed": storage,
        "modeling_allowed": modeling,
        "source": rights.get("source"),
        "citation": rights.get("citation"),
        "notes": rights.get("notes"),
        "checked_at": rights.get("checked_at"),
    }


def evaluate_budget_gate(
    *,
    recurring_monthly_cents: int,
    cap_cents: int = MONTHLY_BUDGET_CAP_CENTS,
    components_cents: Mapping[str, int],
) -> dict[str, Any]:
    status: GateStatus = "pass" if recurring_monthly_cents <= cap_cents else "fail"
    return {
        "status": status,
        "recurring_monthly": money_amount(cents=recurring_monthly_cents),
        "cap": money_amount(cents=cap_cents),
        "components": {
            name: money_amount(cents=int(cents)) for name, cents in components_cents.items()
        },
        # Compatibility aliases for older tests/callers.
        "recurring_monthly_usd": cents_to_usd_str(recurring_monthly_cents),
        "cap_usd": cents_to_usd_str(cap_cents),
    }


def build_vendor_request_checklist(vendor: str) -> dict[str, Any]:
    """Fields that require a written vendor response; unanswered items are blockers."""
    common_items = [
        {
            "id": "fields_event_bout_result_profile_stat",
            "question": (
                "Confirm event/bout/result/profile/stat fields for DWCS + regional history"
            ),
            "status": "unanswered",
            "blocker": True,
        },
        {
            "id": "storage_modeling_rights",
            "question": (
                "Written rights to store, remodel, and retain observations "
                "for betting models"
            ),
            "status": "unanswered",
            "blocker": True,
        },
        {
            "id": "retention_revision_history",
            "question": "Retention window and revision/history semantics for mutable results",
            "status": "unanswered",
            "blocker": True,
        },
        {
            "id": "sla_uptime_support",
            "question": "SLA / uptime / support response commitments",
            "status": "unanswered",
            "blocker": True,
        },
        {
            "id": "monthly_price",
            "question": "Monthly price for required feeds within or above $100 combined budget",
            "status": "unanswered",
            "blocker": True,
        },
    ]
    documented: list[dict[str, Any]]
    if vendor == "sportsdataio":
        documented = [
            {
                "id": "public_dwcs_schedule_claim",
                "question": (
                    "Public workflow guide states DWCS events are "
                    "confirmed/updated when announced"
                ),
                "status": "documented_public",
                "blocker": False,
                "citation": "https://sportsdata.io/developers/workflow-guide/mma",
            },
            {
                "id": "public_fight_stat_fields",
                "question": "Public data dictionary documents fight/fighter stat field names",
                "status": "documented_public",
                "blocker": False,
                "citation": "https://sportsdata.io/developers/data-dictionary/mma",
            },
            {
                "id": "public_sla_marketing",
                "question": (
                    "Marketing pages advertise SLAs with 24/7 support "
                    "(not a signed contract)"
                ),
                "status": "documented_public",
                "blocker": False,
                "citation": "https://sportsdata.io/mma-ufc-api",
            },
            {
                "id": "public_price",
                "question": "Public MMA monthly price",
                "status": "unanswered",
                "blocker": True,
                "notes": "Pricing is sales-quoted; no redistributable public MMA SKU price found",
            },
        ]
    elif vendor == "combat_registry":
        documented = [
            {
                "id": "abc_registry_criteria",
                "question": (
                    "ABC MMA record-keeper criteria require official "
                    "result-backed identities/records"
                ),
                "status": "documented_public",
                "blocker": False,
                "citation": "https://www.abcboxing.com/mma-record-keeper-criteria/",
            },
            {
                "id": "portal_exists",
                "question": "Combat Registry portal exists for commissions/promoters",
                "status": "documented_public",
                "blocker": False,
                "citation": "https://app.combatreg.com/",
            },
            {
                "id": "public_api_price_rights",
                "question": "Public API pricing and commercial reuse rights",
                "status": "unanswered",
                "blocker": True,
                "notes": "No public developer pricing/rights page located; written quote required",
            },
        ]
    else:
        documented = []
    items = documented + [
        item
        for item in common_items
        if item["id"] not in {d["id"] for d in documented}
    ]
    unanswered = [item for item in items if item["status"] == "unanswered"]
    return {
        "vendor": vendor,
        "status": "quote_pending" if unanswered else "complete",
        "items": items,
        "unanswered_blocker_count": len(unanswered),
        "request_response_checklist": [
            (
                "Send written request covering fields, rights, retention, "
                "revisions, SLA, monthly price"
            ),
            "Record vendor contact, request timestamp, and response timestamp",
            "Attach redacted written response summary (no credentials)",
            "Mark each checklist item answered/unanswered; unanswered remains a blocker",
        ],
    }


def bout_fingerprint(bout_like: Mapping[str, Any]) -> str | None:
    """Stable participant+date fingerprint for overlap checks."""
    names: set[str] = set()
    participants = bout_like.get("participants")
    if isinstance(participants, Sequence):
        for participant in participants:
            if isinstance(participant, Mapping):
                names.add(
                    normalize_fighter_name(
                        str(
                            participant.get("normalized_name")
                            or participant.get("display_name")
                            or participant.get("name")
                            or ""
                        )
                    )
                )
    if not names:
        for key in ("fighter1", "fighter2", "fighter_a", "fighter_b"):
            value = bout_like.get(key)
            if isinstance(value, Mapping):
                names.add(normalize_fighter_name(str(value.get("name") or "")))
            elif value:
                names.add(normalize_fighter_name(str(value)))
    names.discard("")
    if len(names) != 2:
        return None
    date_raw = (
        bout_like.get("occurrence_timestamp")
        or bout_like.get("date")
        or bout_like.get("start")
        or bout_like.get("scheduled_start")
    )
    if not date_raw:
        return None
    try:
        day = _parse_iso_utc(str(date_raw)[:10] + "T00:00:00+00:00").date().isoformat()
    except ValueError:
        return None
    return f"{day}|{'|'.join(sorted(names))}"


def match_bout_to_provider_fight(
    bout: Mapping[str, Any],
    fights: Sequence[Mapping[str, Any]],
    *,
    max_day_delta: int = 1,
) -> dict[str, Any] | None:
    participants = bout.get("participants") or []
    names = {
        normalize_fighter_name(str(p.get("normalized_name") or p.get("display_name") or ""))
        for p in participants
        if isinstance(p, Mapping)
    }
    names.discard("")
    if len(names) != 2:
        return None
    bout_ts = bout.get("occurrence_timestamp") or bout.get("publication_timestamp")
    bout_date = None
    if bout_ts:
        bout_date = _parse_iso_utc(str(bout_ts)).date()

    candidates: list[dict[str, Any]] = []
    for fight in fights:
        f1 = fight.get("fighter1") if isinstance(fight.get("fighter1"), Mapping) else {}
        f2 = fight.get("fighter2") if isinstance(fight.get("fighter2"), Mapping) else {}
        fight_names = {
            normalize_fighter_name(str(f1.get("name") or "")),
            normalize_fighter_name(str(f2.get("name") or "")),
        }
        fight_names.discard("")
        if fight_names != names:
            continue
        if bout_date is not None:
            fight_date_raw = fight.get("date") or fight.get("start")
            if fight_date_raw:
                try:
                    fight_date = _parse_iso_utc(
                        str(fight_date_raw)[:10] + "T00:00:00+00:00"
                    ).date()
                except ValueError:
                    fight_date = None
                if (
                    fight_date is not None
                    and abs((fight_date - bout_date).days) > max_day_delta
                ):
                    continue
        candidates.append(dict(fight))
    if len(candidates) == 1:
        return candidates[0]
    return None


def extract_manifest_event_night_outcome(bout: Mapping[str, Any]) -> dict[str, Any]:
    night = bout.get("event_night_result")
    if not isinstance(night, Mapping):
        return {
            "mapping_status": "unmapped",
            "class": None,
            "winner_normalized": None,
        }
    result_class = night.get("class")
    winner_display = night.get("winner_display_name") or night.get("winner_normalized")
    winner_normalized = (
        normalize_fighter_name(str(winner_display)) if winner_display else None
    )
    if result_class not in {"decisive", "draw", "no_contest"}:
        return {
            "mapping_status": "unmapped",
            "class": result_class,
            "winner_normalized": winner_normalized,
        }
    if result_class == "decisive" and not winner_normalized:
        return {
            "mapping_status": "unmapped",
            "class": result_class,
            "winner_normalized": None,
        }
    return {
        "mapping_status": "mapped",
        "class": result_class,
        "winner_normalized": winner_normalized,
    }


def extract_provider_fight_outcome(fight: Mapping[str, Any]) -> dict[str, Any]:
    """Map a provider fight payload to event-night class + winner when unambiguous."""
    f1 = fight.get("fighter1") if isinstance(fight.get("fighter1"), Mapping) else {}
    f2 = fight.get("fighter2") if isinstance(fight.get("fighter2"), Mapping) else {}
    f1_name = normalize_fighter_name(str(f1.get("name") or ""))
    f2_name = normalize_fighter_name(str(f2.get("name") or ""))
    status_text = str(fight.get("status") or "").lower()
    method_text = str(fight.get("result_method") or fight.get("method") or "").lower()
    combined = f"{status_text} {method_text}"

    if "no contest" in combined or "no_contest" in combined or status_text == "nc":
        return {
            "mapping_status": "mapped",
            "class": "no_contest",
            "winner_normalized": None,
        }
    if "draw" in combined:
        return {
            "mapping_status": "mapped",
            "class": "draw",
            "winner_normalized": None,
        }

    winner_name: str | None = None
    winner = fight.get("winner")
    if isinstance(winner, Mapping) and winner.get("name"):
        winner_name = normalize_fighter_name(str(winner.get("name")))
    elif isinstance(winner, str) and winner.strip():
        winner_name = normalize_fighter_name(winner)
    else:
        winner_id = fight.get("result_winner_id") or fight.get("winner_id")
        if winner_id is not None:
            if str(f1.get("id")) == str(winner_id):
                winner_name = f1_name or None
            elif str(f2.get("id")) == str(winner_id):
                winner_name = f2_name or None
            else:
                return {
                    "mapping_status": "unmapped",
                    "class": None,
                    "winner_normalized": None,
                }

    if winner_name:
        if winner_name not in {f1_name, f2_name}:
            return {
                "mapping_status": "ambiguous",
                "class": "decisive",
                "winner_normalized": winner_name,
            }
        return {
            "mapping_status": "mapped",
            "class": "decisive",
            "winner_normalized": winner_name,
        }

    # Explicit winner flags on fighters, if present.
    f1_won = f1.get("winner") is True or f1.get("is_winner") is True
    f2_won = f2.get("winner") is True or f2.get("is_winner") is True
    if f1_won and not f2_won and f1_name:
        return {
            "mapping_status": "mapped",
            "class": "decisive",
            "winner_normalized": f1_name,
        }
    if f2_won and not f1_won and f2_name:
        return {
            "mapping_status": "mapped",
            "class": "decisive",
            "winner_normalized": f2_name,
        }
    if f1_won and f2_won:
        return {
            "mapping_status": "ambiguous",
            "class": None,
            "winner_normalized": None,
        }
    return {
        "mapping_status": "unmapped",
        "class": None,
        "winner_normalized": None,
    }


def classify_outcome_pair(
    manifest_bout: Mapping[str, Any],
    provider_fight: Mapping[str, Any] | None,
) -> dict[str, Any]:
    manifest = extract_manifest_event_night_outcome(manifest_bout)
    if provider_fight is None:
        return {
            "status": "unknown",
            "reason": "provider_bout_unmatched",
            "manifest_class": manifest.get("class"),
            "provider_class": None,
            "winner_agree": None,
        }
    provider = extract_provider_fight_outcome(provider_fight)
    if manifest["mapping_status"] != "mapped":
        return {
            "status": "unknown",
            "reason": "manifest_unmapped",
            "manifest_class": manifest.get("class"),
            "provider_class": provider.get("class"),
            "winner_agree": None,
        }
    if provider["mapping_status"] != "mapped":
        return {
            "status": "unknown",
            "reason": f"provider_{provider['mapping_status']}",
            "manifest_class": manifest.get("class"),
            "provider_class": provider.get("class"),
            "winner_agree": None,
        }
    if manifest["class"] != provider["class"]:
        return {
            "status": "disagree",
            "reason": "class_mismatch",
            "manifest_class": manifest["class"],
            "provider_class": provider["class"],
            "winner_agree": False,
        }
    if manifest["class"] == "decisive":
        winner_agree = manifest["winner_normalized"] == provider["winner_normalized"]
        return {
            "status": "agree" if winner_agree else "disagree",
            "reason": "winner_match" if winner_agree else "winner_mismatch",
            "manifest_class": manifest["class"],
            "provider_class": provider["class"],
            "winner_agree": winner_agree,
        }
    return {
        "status": "agree",
        "reason": "class_match",
        "manifest_class": manifest["class"],
        "provider_class": provider["class"],
        "winner_agree": True,
    }


def compute_outcome_agreement(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Agreement over comparable mapped pairs only; unknowns are excluded."""
    comparable = [pair for pair in pairs if pair.get("status") in {"agree", "disagree"}]
    unknown = [pair for pair in pairs if pair.get("status") == "unknown"]
    numerator = sum(1 for pair in comparable if pair.get("status") == "agree")
    metric = make_rate_metric(
        numerator=numerator,
        denominator=len(comparable),
        status="measured" if comparable else "unknown",
        reason=(
            None
            if comparable
            else "no_comparable_mapped_pairs"
        ),
    )
    metric["excluded_unknown_count"] = len(unknown)
    metric["denominator_policy"] = "comparable_mapped_pairs_only"
    return metric


def compute_event_coverage(
    *,
    matched_manifest_event_ids: Sequence[str] | set[str],
    manifest_event_ids: Sequence[str] | set[str],
) -> dict[str, Any]:
    """Unique matched DWCS events / frozen unique manifest events."""
    matched = {str(event_id) for event_id in matched_manifest_event_ids}
    universe = {str(event_id) for event_id in manifest_event_ids}
    matched &= universe
    return make_rate_metric(
        numerator=len(matched),
        denominator=len(universe),
        status="measured" if universe else "unknown",
        reason=None if universe else "empty_manifest_events",
    )


def compute_bout_coverage(
    *,
    matched_bout_ids: Sequence[str] | set[str],
    manifest_bout_ids: Sequence[str] | set[str],
) -> dict[str, Any]:
    matched = {str(bout_id) for bout_id in matched_bout_ids}
    universe = {str(bout_id) for bout_id in manifest_bout_ids}
    matched &= universe
    return make_rate_metric(
        numerator=len(matched),
        denominator=len(universe),
        status="measured" if universe else "unknown",
        reason=None if universe else "empty_manifest_bouts",
    )


def summarize_difficult_identity_probe(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_size: int,
) -> dict[str, Any]:
    """Aggregate hit/miss/unknown for the deterministic difficult-identity sample."""
    hit = sum(1 for row in results if row.get("status") == "hit")
    miss = sum(1 for row in results if row.get("status") == "miss")
    unknown = sum(1 for row in results if row.get("status") == "unknown")
    probed = hit + miss + unknown
    return {
        "status": "measured" if probed else "unknown",
        "expected_size": expected_size,
        "probed": probed,
        "hit": hit,
        "miss": miss,
        "unknown": unknown,
        "hit_rate": (hit / probed) if probed else None,
        "reason": None if probed else "not_probed",
        "denominator_policy": "hit_miss_unknown_partition_of_probed_sample",
    }


def compute_api_sports_non_overlap(
    provider_history_bouts: Sequence[Mapping[str, Any]],
    dwcs_fingerprints: set[str],
) -> dict[str, Any]:
    """Share of provider pre-DWCS history bouts that do not fingerprint-match DWCS."""
    total = 0
    non_overlap = 0
    for bout in provider_history_bouts:
        fingerprint = bout_fingerprint(bout)
        if fingerprint is None:
            continue
        total += 1
        if fingerprint not in dwcs_fingerprints:
            non_overlap += 1
    return make_rate_metric(
        numerator=non_overlap,
        denominator=total,
        status="measured" if total else "unknown",
        reason=None if total else "no_fingerprintable_provider_history",
    )


def normalize_api_sports_fight(bout_like: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize API-Sports history rows into the shared fight outcome shape."""
    fight = dict(bout_like)
    if "fighter1" not in fight and fight.get("fighters"):
        fighters = fight.get("fighters")
        if isinstance(fighters, Sequence) and len(fighters) >= 2:
            left = fighters[0] if isinstance(fighters[0], Mapping) else {"name": fighters[0]}
            right = fighters[1] if isinstance(fighters[1], Mapping) else {"name": fighters[1]}
            fight["fighter1"] = dict(left)
            fight["fighter2"] = dict(right)
    # Common alternate keys seen in record payloads.
    if "result_method" not in fight and fight.get("method"):
        fight["result_method"] = fight.get("method")
    if "winner" not in fight and isinstance(fight.get("winner_name"), str):
        fight["winner"] = {"name": fight["winner_name"]}
    return fight


def build_api_sports_overlapping_outcome_pairs(
    provider_history_bouts: Sequence[Mapping[str, Any]],
    dwcs_bouts: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Map fingerprint-overlapping provider bouts to manifest event-night outcomes.

    Ambiguous or unmapped provider/manifest rows become ``unknown`` and are
    excluded from the agreement denominator — never treated as disagreement.
    """
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    for bout in dwcs_bouts:
        fingerprint = bout_fingerprint(bout)
        if fingerprint is None:
            continue
        by_fingerprint.setdefault(fingerprint, []).append(dict(bout))

    pairs: list[dict[str, Any]] = []
    for provider_bout in provider_history_bouts:
        fingerprint = bout_fingerprint(provider_bout)
        if fingerprint is None:
            continue
        matches = by_fingerprint.get(fingerprint) or []
        if not matches:
            # Non-overlapping history bout — not part of accuracy sample.
            continue
        if len(matches) != 1:
            pairs.append(
                {
                    "status": "unknown",
                    "reason": "ambiguous_manifest_fingerprint_match",
                    "manifest_class": None,
                    "provider_class": None,
                    "winner_agree": None,
                    "fingerprint": fingerprint,
                }
            )
            continue
        provider_fight = normalize_api_sports_fight(provider_bout)
        pair = classify_outcome_pair(matches[0], provider_fight)
        pair["fingerprint"] = fingerprint
        pairs.append(pair)
    return pairs


def compute_api_sports_accuracy(
    overlapping_pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    agreement = compute_outcome_agreement(overlapping_pairs)
    status: GateStatus
    if agreement["status"] != "measured" or agreement["rate"] is None:
        status = "unknown"
    elif agreement["rate"] >= OUTCOME_AGREEMENT_MIN:
        status = "pass"
    else:
        status = "fail"
    return {
        "status": status,
        "outcome_agreement": agreement,
        "threshold": OUTCOME_AGREEMENT_MIN,
        "overlapping_pair_count": len(overlapping_pairs),
    }


def evaluate_required_features(
    sample_fights: Sequence[Mapping[str, Any]],
    sample_stats: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not sample_fights:
        return {
            "status": "unknown",
            "reason": "no_sample_fights",
            "missing_fight_fields": list(REQUIRED_FIGHT_FIELDS),
            "missing_stat_fields": list(REQUIRED_STAT_FIELDS),
        }
    missing_fight: set[str] = set()
    for fight in sample_fights:
        for field in REQUIRED_FIGHT_FIELDS:
            if field not in fight or fight.get(field) in (None, "", []):
                missing_fight.add(field)
    missing_stat: set[str] = set()
    if sample_stats is None:
        return {
            "status": "unknown",
            "reason": "stat_samples_not_probed",
            "missing_fight_fields": sorted(missing_fight),
            "missing_stat_fields": list(REQUIRED_STAT_FIELDS),
        }
    if not sample_stats:
        return {
            "status": "fail",
            "reason": "stat_samples_empty",
            "missing_fight_fields": sorted(missing_fight),
            "missing_stat_fields": list(REQUIRED_STAT_FIELDS),
        }
    for stat in sample_stats:
        for field in REQUIRED_STAT_FIELDS:
            if field not in stat or stat.get(field) is None:
                missing_stat.add(field)
    if missing_fight or missing_stat:
        return {
            "status": "fail",
            "reason": "required_fields_missing",
            "missing_fight_fields": sorted(missing_fight),
            "missing_stat_fields": sorted(missing_stat),
        }
    return {
        "status": "pass",
        "reason": None,
        "missing_fight_fields": [],
        "missing_stat_fields": [],
    }


def evaluate_pit_fitness(probe: Mapping[str, Any]) -> dict[str, Any]:
    """Measure or leave unknown; never auto-fail solely because HTTP succeeded."""
    latencies = list(probe.get("latencies_ms") or [])
    latency_p50 = None
    if latencies:
        ordered = sorted(float(value) for value in latencies)
        latency_p50 = ordered[len(ordered) // 2]

    reconstruction = probe.get("pre_fight_reconstruction_status")
    revision = probe.get("revision_support_status")
    reasons: list[str] = []
    if reconstruction not in {"pass", "fail"}:
        reasons.append("pre_fight_reconstruction_unproven")
        reconstruction_status: GateStatus = "unknown"
    else:
        reconstruction_status = _as_gate_status(reconstruction)
    if revision not in {"pass", "fail"}:
        reasons.append("revision_support_unproven")
        revision_status: GateStatus = "unknown"
    else:
        revision_status = _as_gate_status(revision)

    if reconstruction_status == "pass" and revision_status == "pass":
        status: GateStatus = "pass"
        reason = None
    elif reconstruction_status == "fail" or revision_status == "fail":
        status = "fail"
        reason = "pit_dimension_failed"
    else:
        status = "unknown"
        reason = ",".join(reasons) if reasons else "pit_dimensions_unknown"

    null_rates = probe.get("field_null_rates")
    if not isinstance(null_rates, Mapping):
        null_rates = {
            "status": "unknown",
            "reason": "field_null_rates_not_probed",
            "fields": {},
        }

    return {
        "status": status,
        "reason": reason,
        "pre_fight_reconstruction": reconstruction_status,
        "revision_support": revision_status,
        "latency_ms_p50": latency_p50,
        "request_cost_units": probe.get("request_count"),
        "field_null_rates": dict(null_rates),
    }


def technical_gates_pass(gates: Mapping[str, Any]) -> bool:
    def _ge(value: Any, threshold: float) -> bool:
        return isinstance(value, (int, float)) and float(value) >= threshold

    return (
        str(gates.get("metrics_status") or "") == "measured"
        and _ge(gates.get("event_coverage_rate"), EVENT_COVERAGE_MIN)
        and _ge(gates.get("bout_coverage_rate"), BOUT_COVERAGE_MIN)
        and _ge(gates.get("outcome_agreement_rate"), OUTCOME_AGREEMENT_MIN)
        and gates.get("required_features_status") == "pass"
        and gates.get("pit_fitness_status") == "pass"
    )


def _balldontlie_public_rights(*, checked_at: str) -> dict[str, Any]:
    return {
        "storage_allowed": True,
        "modeling_allowed": True,
        "source": "written_terms",
        "citation": "https://balldontlie.io/terms.html",
        "notes": (
            "Terms §6 expressly permit store/archive/modify/analyze and AI/ML "
            "training from lawfully obtained Data. Coverage remains best-effort; "
            "docs state only UFC is comprehensive."
        ),
        "checked_at": checked_at,
        "coverage_caveat_citation": "https://mma.balldontlie.io/",
        "price": money_amount(cents=BALLDONTLIE_GOAT_CENTS),
        "price_citation": "https://mma.balldontlie.io/",
        "rate_limit_rpm_goat": 600,
    }


def _api_sports_public_rights(*, checked_at: str) -> dict[str, Any]:
    return {
        "storage_allowed": None,
        "modeling_allowed": None,
        "source": "written_terms_ambiguous",
        "citation": "https://api-sports.io/terms",
        "notes": (
            "Terms place responsibility on the user to obtain league/rights-holder "
            "authorization for commercial/betting uses; no explicit grant equivalent "
            "to BALLDONTLIE §6 storage+modeling permission. Treat rights as unknown "
            "until clarified in writing."
        ),
        "checked_at": checked_at,
        "price_notes": (
            "Free tier 100 req/day; PRO advertised near ~$10/month range on product "
            "page digit display — confirm cart price before spend."
        ),
        "price_citation": "https://api-sports.io/sports/mma",
        "probe_budget": money_amount(cents=API_SPORTS_PROBE_CENTS),
    }


def apply_stats_source_decision_tree(
    *,
    balldontlie_gates: Mapping[str, Any],
    api_sports_gates: Mapping[str, Any],
    sportsdataio_status: str = "quote_pending",
    combat_registry_status: str = "quote_pending",
    sportsdataio_gates: Mapping[str, Any] | None = None,
    combat_registry_gates: Mapping[str, Any] | None = None,
    monthly_budget_cents: int | None = None,
    monthly_budget_usd: float | None = None,
    budget_cap_cents: int = MONTHLY_BUDGET_CAP_CENTS,
    budget_cap_usd: float | None = None,
) -> dict[str, Any]:
    """Apply plan §4 production source decision tree exactly."""
    if monthly_budget_cents is None:
        if monthly_budget_usd is None:
            monthly_budget_cents = THE_ODDS_API_CENTS + BALLDONTLIE_GOAT_CENTS
        else:
            monthly_budget_cents = usd_to_cents(monthly_budget_usd)
    if budget_cap_usd is not None:
        budget_cap_cents = usd_to_cents(budget_cap_usd)

    bdl_technical = technical_gates_pass(balldontlie_gates)
    bdl_rights = balldontlie_gates.get("rights_status") == "pass"
    bdl_budget = balldontlie_gates.get("budget_status") == "pass"
    adopt_bdl = bdl_technical and bdl_rights and bdl_budget

    def _fallback_adopt(name: str, gates: Mapping[str, Any] | None, quote_status: str) -> bool:
        if not gates:
            return False
        if quote_status != "complete" and gates.get("quote_status") != "complete":
            return False
        return (
            technical_gates_pass(gates)
            and gates.get("rights_status") == "pass"
            and gates.get("budget_status") == "pass"
        )

    adopt_sdio = _fallback_adopt("sportsdataio", sportsdataio_gates, sportsdataio_status)
    adopt_combat = _fallback_adopt(
        "combat_registry", combat_registry_gates, combat_registry_status
    )

    api_access = str(api_sports_gates.get("access_status") or "not_configured")
    non_overlap = api_sports_gates.get("non_overlap_rate")
    api_keep = (
        api_access == "ok"
        and isinstance(non_overlap, (int, float))
        and float(non_overlap) >= API_SPORTS_NON_OVERLAP_MIN
        and api_sports_gates.get("accuracy_status") == "pass"
    )

    ranked: list[dict[str, Any]] = []
    if adopt_bdl:
        primary: str | None = "balldontlie"
        path: DecisionPath = "balldontlie_primary"
        hard_blocker = False
        rationale = (
            "BALLDONTLIE cleared >=98% event/bout coverage, >=99% outcome agreement, "
            "required features/PIT fitness, written storage/modeling rights, and budget."
        )
    elif adopt_sdio:
        primary = "sportsdataio"
        path = "sportsdataio_primary"
        hard_blocker = False
        rationale = (
            "BALLDONTLIE failed adoption gates; SportsDataIO written quote/rights and "
            "measured technical thresholds cleared the same decision tree."
        )
    elif adopt_combat:
        primary = "combat_registry"
        path = "combat_registry_primary"
        hard_blocker = False
        rationale = (
            "BALLDONTLIE and SportsDataIO unavailable/failing; Combat Registry written "
            "quote/rights and measured technical thresholds cleared adoption."
        )
    else:
        primary = None
        path = "hard_blocker"
        hard_blocker = True
        rationale = (
            "No provider cleared all adoption gates. BALLDONTLIE "
            f"(technical={bdl_technical}, rights={bdl_rights}, budget={bdl_budget}, "
            f"metrics_status={balldontlie_gates.get('metrics_status')}); "
            f"SportsDataIO quote_status={sportsdataio_status}; "
            f"Combat Registry quote_status={combat_registry_status}. "
            "Missing quote/credentials remain hard blockers. "
            "Prohibited scraping is rejected."
        )
        ranked.extend(
            [
                {
                    "rank": 1,
                    "source": "sportsdataio",
                    "status": sportsdataio_status,
                    "role": "preferred_paid_fallback_upgrade",
                    "requires": (
                        "complete_quote+rights+budget+same_technical_thresholds"
                    ),
                },
                {
                    "rank": 2,
                    "source": "combat_registry",
                    "status": combat_registry_status,
                    "role": "authoritative_identity_record_layer",
                    "requires": (
                        "complete_quote+rights+budget+same_technical_thresholds"
                    ),
                },
                {
                    "rank": 3,
                    "source": "api_sports",
                    "status": "probe_keep" if api_keep else "probe_cancel_or_blocked",
                    "role": "one_month_non_overlap_probe",
                    "requires": (
                        f">={API_SPORTS_NON_OVERLAP_MIN:.0%} non-overlapping pre-DWCS "
                        "bouts plus accuracy; else cancel"
                    ),
                },
            ]
        )

    return {
        "primary": primary,
        "path": path,
        "hard_blocker": hard_blocker,
        "rationale": rationale,
        "api_sports_probe_keep": api_keep,
        "ranked_lawful_fallbacks": ranked,
        "prohibited_scraping_selected": False,
        "gates": {
            "balldontlie": {
                "event_coverage_min": EVENT_COVERAGE_MIN,
                "bout_coverage_min": BOUT_COVERAGE_MIN,
                "outcome_agreement_min": OUTCOME_AGREEMENT_MIN,
                "event_coverage_rate": balldontlie_gates.get("event_coverage_rate"),
                "bout_coverage_rate": balldontlie_gates.get("bout_coverage_rate"),
                "outcome_agreement_rate": balldontlie_gates.get("outcome_agreement_rate"),
                "required_features_status": balldontlie_gates.get(
                    "required_features_status"
                ),
                "pit_fitness_status": balldontlie_gates.get("pit_fitness_status"),
                "rights_status": balldontlie_gates.get("rights_status"),
                "budget_status": balldontlie_gates.get("budget_status"),
                "metrics_status": balldontlie_gates.get("metrics_status"),
                "technical_pass": bdl_technical,
                "adopt": adopt_bdl,
            },
            "sportsdataio": {
                "quote_status": sportsdataio_status,
                "adopt": adopt_sdio,
                "gates": dict(sportsdataio_gates or {}),
            },
            "combat_registry": {
                "quote_status": combat_registry_status,
                "adopt": adopt_combat,
                "gates": dict(combat_registry_gates or {}),
            },
            "api_sports": {
                "non_overlap_min": API_SPORTS_NON_OVERLAP_MIN,
                "non_overlap_rate": non_overlap,
                "accuracy_status": api_sports_gates.get("accuracy_status"),
                "access_status": api_access,
                "probe_keep": api_keep,
            },
            "budget": {
                "monthly": money_amount(cents=monthly_budget_cents),
                "cap": money_amount(cents=budget_cap_cents),
                "within_cap": monthly_budget_cents <= budget_cap_cents,
            },
        },
    }


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (
            key,
            "[REDACTED]"
            if any(frag in key.lower() for frag in SECRET_KEY_FRAGMENTS)
            else value,
        )
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_scorecard(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove secrets and full licensed payloads from a scorecard-like object."""

    def _walk(value: Any) -> Any:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                key_l = str(key).lower()
                if any(frag in key_l for frag in SECRET_KEY_FRAGMENTS):
                    continue
                if any(frag in key_l for frag in PAYLOAD_KEY_FRAGMENTS):
                    continue
                if key_l in {"request_url", "url"} and isinstance(item, str):
                    out[key] = _redact_url(item)
                else:
                    out[key] = _walk(item)
            return out
        if isinstance(value, list):
            return [_walk(item) for item in value]
        if isinstance(value, str):
            redacted = value
            if re.search(r"(?i)(api[_-]?key=|authorization:\s*)\S+", redacted):
                redacted = re.sub(
                    r"(?i)(api[_-]?key=|authorization:\s*)(\S+)",
                    r"\1[REDACTED]",
                    redacted,
                )
            return redacted
        return value

    return _walk(dict(payload))


def _empty_provider_metrics(
    denominator_events: int,
    denominator_bouts: int,
    denominator_difficult: int,
) -> dict[str, Any]:
    unknown = "not_configured"
    return {
        "event_coverage": make_rate_metric(
            numerator=None,
            denominator=denominator_events,
            status="unknown",
            reason=unknown,
        ),
        "bout_coverage": make_rate_metric(
            numerator=None,
            denominator=denominator_bouts,
            status="unknown",
            reason=unknown,
        ),
        "outcome_agreement": {
            **make_rate_metric(
                numerator=None,
                denominator=None,
                status="unknown",
                reason=unknown,
            ),
            "excluded_unknown_count": None,
            "denominator_policy": "comparable_mapped_pairs_only",
        },
        "difficult_identity_coverage": {
            "status": "unknown",
            "expected_size": denominator_difficult,
            "probed": None,
            "hit": None,
            "miss": None,
            "unknown": None,
            "hit_rate": None,
            "reason": unknown,
            "denominator_policy": "hit_miss_unknown_partition_of_probed_sample",
        },
        "profile_coverage": make_rate_metric(
            numerator=None,
            denominator=denominator_difficult,
            status="unknown",
            reason=unknown,
        ),
        "stat_coverage": make_rate_metric(
            numerator=None,
            denominator=denominator_bouts,
            status="unknown",
            reason=unknown,
        ),
        "field_null_rates": {"status": "unknown", "reason": unknown, "fields": {}},
        "required_features": {
            "status": "unknown",
            "reason": unknown,
            "missing_fight_fields": [],
            "missing_stat_fields": [],
        },
        "pit_fitness": {
            "status": "unknown",
            "reason": unknown,
            "pre_fight_reconstruction": "unknown",
            "revision_support": "unknown",
            "latency_ms_p50": None,
            "request_cost_units": None,
            "field_null_rates": {"status": "unknown", "reason": unknown, "fields": {}},
        },
        "year_diagnostics": {
            "years_with_any_provider_dwcs_named_events": None,
            "manifest_calendar_years": None,
            "note": (
                "Year diagnostics are informational only and are never used as "
                "event_coverage numerator/denominator."
            ),
        },
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional_env_key(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def measure_balldontlie_from_observations(
    *,
    bouts: Sequence[Mapping[str, Any]],
    provider_fights: Sequence[Mapping[str, Any]],
    difficult_identity_results: Sequence[Mapping[str, Any]],
    sample_stats: Sequence[Mapping[str, Any]] | None = None,
    latencies_ms: Sequence[float] | None = None,
    request_count: int | None = None,
    pre_fight_reconstruction_status: str | None = None,
    revision_support_status: str | None = None,
    field_null_rates: Mapping[str, Any] | None = None,
    years_with_any_provider_dwcs_named_events: int | None = None,
) -> dict[str, Any]:
    """Pure measurement path used by live probes and synthetic fixtures."""
    event_ids = {str(bout["event_id"]) for bout in bouts}
    bout_ids = {str(bout["bout_id"]) for bout in bouts}
    matched_bout_ids: list[str] = []
    matched_event_ids: set[str] = set()
    outcome_pairs: list[dict[str, Any]] = []
    matched_fights: list[dict[str, Any]] = []

    for bout in bouts:
        matched = match_bout_to_provider_fight(bout, provider_fights)
        pair = classify_outcome_pair(bout, matched)
        outcome_pairs.append(pair)
        if matched is None:
            continue
        matched_bout_ids.append(str(bout["bout_id"]))
        matched_event_ids.add(str(bout["event_id"]))
        matched_fights.append(matched)

    difficult = summarize_difficult_identity_probe(
        difficult_identity_results,
        expected_size=DIFFICULT_IDENTITY_SAMPLE_SIZE,
    )
    required = evaluate_required_features(matched_fights, sample_stats)
    pit = evaluate_pit_fitness(
        {
            "latencies_ms": list(latencies_ms or []),
            "request_count": request_count,
            "pre_fight_reconstruction_status": pre_fight_reconstruction_status,
            "revision_support_status": revision_support_status,
            "field_null_rates": field_null_rates,
        }
    )
    return {
        "access_status": "ok",
        "metrics_status": "measured",
        "event_coverage": compute_event_coverage(
            matched_manifest_event_ids=matched_event_ids,
            manifest_event_ids=event_ids,
        ),
        "bout_coverage": compute_bout_coverage(
            matched_bout_ids=matched_bout_ids,
            manifest_bout_ids=bout_ids,
        ),
        "outcome_agreement": compute_outcome_agreement(outcome_pairs),
        "difficult_identity_coverage": difficult,
        "profile_coverage": make_rate_metric(
            numerator=difficult.get("hit"),
            denominator=difficult.get("probed"),
            status="measured" if difficult.get("probed") else "unknown",
            reason=difficult.get("reason"),
        ),
        "stat_coverage": make_rate_metric(
            numerator=len(sample_stats) if sample_stats is not None else None,
            denominator=len(matched_bout_ids) if sample_stats is not None else len(bout_ids),
            status="measured" if sample_stats is not None else "unknown",
            reason=None if sample_stats is not None else "stat_samples_not_probed",
        ),
        "required_features": required,
        "pit_fitness": pit,
        "year_diagnostics": {
            "years_with_any_provider_dwcs_named_events": (
                years_with_any_provider_dwcs_named_events
            ),
            "manifest_calendar_years": sorted(
                {int(bout["calendar_year"]) for bout in bouts if bout.get("calendar_year")}
            ),
            "note": (
                "Year diagnostics are informational only and are never used as "
                "event_coverage numerator/denominator."
            ),
        },
        "matched_bout_ids": matched_bout_ids,
        "matched_event_ids": sorted(matched_event_ids),
        "outcome_pairs": outcome_pairs,
    }


def probe_balldontlie_live(
    *,
    api_key: str,
    bouts: Sequence[Mapping[str, Any]],
    difficult_identities: Sequence[Mapping[str, Any]],
    timeout_sec: float = 20.0,
    max_requests: int = 120,
) -> dict[str, Any]:
    """Measured live probe. Returns sanitized metrics only (no full payloads)."""
    headers = {"Authorization": api_key}
    request_count = 0
    latencies: list[float] = []
    events_by_year: dict[int, list[dict[str, Any]]] = {}
    access: AccessStatus = "ok"
    error: str | None = None

    def _get(path: str, params: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        nonlocal request_count, access, error
        if request_count >= max_requests:
            return 429, {"error": "local_max_requests"}
        request_count += 1
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                response = client.get(
                    f"{BALLDONTLIE_BASE}{path}",
                    headers=headers,
                    params=dict(params or {}),
                )
            latencies.append((time.perf_counter() - started) * 1000.0)
            try:
                body = response.json()
            except ValueError:
                body = None
            access = classify_provider_access(
                api_key=api_key,
                http_status=response.status_code,
                body=body if isinstance(body, Mapping) else None,
            )
            if access != "ok":
                error = f"http_{response.status_code}"
            return response.status_code, body
        except httpx.HTTPError as exc:
            access = "request_failed"
            error = type(exc).__name__
            return 0, None

    years = sorted(
        {int(bout["calendar_year"]) for bout in bouts if bout.get("calendar_year") is not None}
    )
    for year in years:
        _status, body = _get("/events", {"year": year, "per_page": 100})
        if access != "ok":
            break
        data = body.get("data") if isinstance(body, Mapping) else None
        if isinstance(data, list):
            events_by_year[year] = [dict(item) for item in data if isinstance(item, Mapping)]

    if access != "ok":
        return {
            "access_status": access,
            "error": error,
            "request_count": request_count,
            "latencies_ms": latencies,
            "metrics_status": "blocked" if access != "not_configured" else "unknown",
        }

    provider_fights: list[dict[str, Any]] = []
    years_with_dwcs_named = 0
    for year, events in events_by_year.items():
        year_had_dwcs_named = False
        for event in events:
            if request_count >= max_requests:
                break
            event_id = event.get("id")
            if event_id is None:
                continue
            name = str(event.get("name") or "")
            if "contender" not in name.lower() and "dwcs" not in name.lower():
                continue
            year_had_dwcs_named = True
            _status, body = _get("/fights", {"event_ids[]": event_id, "per_page": 100})
            if access != "ok":
                break
            data = body.get("data") if isinstance(body, Mapping) else None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, Mapping):
                        row = dict(item)
                        row["_event_year"] = year
                        if "date" not in row and event.get("date"):
                            row["date"] = event.get("date")
                        provider_fights.append(row)
        if year_had_dwcs_named:
            years_with_dwcs_named += 1
        if access != "ok":
            break

    difficult_results: list[dict[str, Any]] = []
    for entrant in difficult_identities:
        if request_count >= max_requests or access != "ok":
            difficult_results.append(
                {
                    "entrant_key": entrant.get("entrant_key"),
                    "status": "unknown",
                    "reason": "request_budget_exhausted_or_access_lost",
                }
            )
            continue
        _status, body = _get(
            "/fighters",
            {"search": entrant["display_name"], "per_page": 5},
        )
        if access != "ok":
            difficult_results.append(
                {
                    "entrant_key": entrant.get("entrant_key"),
                    "status": "unknown",
                    "reason": str(access),
                }
            )
            continue
        data = body.get("data") if isinstance(body, Mapping) else None
        target = normalize_fighter_name(str(entrant["normalized_name"]))
        hit = False
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, Mapping):
                    continue
                if normalize_fighter_name(str(item.get("name") or "")) == target:
                    hit = True
                    break
        difficult_results.append(
            {
                "entrant_key": entrant.get("entrant_key"),
                "status": "hit" if hit else "miss",
                "reason": None,
            }
        )

    measured = measure_balldontlie_from_observations(
        bouts=bouts,
        provider_fights=provider_fights,
        difficult_identity_results=difficult_results,
        sample_stats=None,
        latencies_ms=latencies,
        request_count=request_count,
        pre_fight_reconstruction_status=None,
        revision_support_status=None,
        field_null_rates={
            "status": "unknown",
            "reason": "field_null_rates_not_probed",
            "fields": {},
        },
        years_with_any_provider_dwcs_named_events=years_with_dwcs_named,
    )
    measured["access_status"] = access
    measured["error"] = error
    measured["request_count"] = request_count
    measured["latencies_ms"] = latencies
    measured["provider_dwcs_named_fight_count"] = len(provider_fights)
    measured["crosswalk_note"] = (
        "Bout/event matches use normalized participant names + date only; "
        "no ESPN↔BALLDONTLIE id map is assumed."
    )
    return measured


def measure_api_sports_from_observations(
    *,
    provider_history_bouts: Sequence[Mapping[str, Any]],
    dwcs_bouts: Sequence[Mapping[str, Any]],
    overlapping_outcome_pairs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    fingerprints = {
        fingerprint
        for bout in dwcs_bouts
        if (fingerprint := bout_fingerprint(bout)) is not None
    }
    non_overlap = compute_api_sports_non_overlap(provider_history_bouts, fingerprints)
    if overlapping_outcome_pairs is None:
        overlapping_outcome_pairs = build_api_sports_overlapping_outcome_pairs(
            provider_history_bouts,
            dwcs_bouts,
        )
    accuracy = compute_api_sports_accuracy(overlapping_outcome_pairs)
    return {
        "access_status": "ok",
        "non_overlapping_pre_dwcs_bouts": non_overlap,
        "non_overlap_rate": non_overlap.get("rate"),
        "overlapping_outcome_pairs": list(overlapping_outcome_pairs),
        "accuracy": accuracy,
        "accuracy_status": accuracy["status"],
    }


def probe_api_sports_live(
    *,
    api_key: str,
    dwcs_bouts: Sequence[Mapping[str, Any]],
    timeout_sec: float = 20.0,
    max_requests: int = 30,
) -> dict[str, Any]:
    headers = {"x-apisports-key": api_key}
    request_count = 0
    latencies: list[float] = []
    access: AccessStatus = "ok"
    error: str | None = None

    def _get(path: str, params: Mapping[str, Any] | None = None) -> tuple[int, Any]:
        nonlocal request_count, access, error
        if request_count >= max_requests:
            return 429, {"error": "local_max_requests"}
        request_count += 1
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                response = client.get(
                    f"{API_SPORTS_BASE}{path}",
                    headers=headers,
                    params=dict(params or {}),
                )
            latencies.append((time.perf_counter() - started) * 1000.0)
            try:
                body = response.json()
            except ValueError:
                body = None
            access = classify_provider_access(
                api_key=api_key,
                http_status=response.status_code,
                body=body if isinstance(body, Mapping) else None,
            )
            if access != "ok":
                error = f"http_{response.status_code}"
            return response.status_code, body
        except httpx.HTTPError as exc:
            access = "request_failed"
            error = type(exc).__name__
            return 0, None

    _status, _body = _get("/status")
    if access != "ok":
        return {
            "access_status": access,
            "error": error,
            "request_count": request_count,
            "latencies_ms": latencies,
            "non_overlap_rate": None,
            "accuracy_status": "unknown",
            "non_overlapping_pre_dwcs_bouts": make_rate_metric(
                numerator=None,
                denominator=None,
                status="unknown",
                reason=str(access),
            ),
        }

    # Bounded fighter-history probe for non-overlap math when the API returns records.
    history_bouts: list[dict[str, Any]] = []
    # Prefer a tiny deterministic sample of DWCS entrants for history pulls.
    entrants = extract_entrants(dwcs_bouts)[:5]
    for entrant in entrants:
        if request_count >= max_requests:
            break
        _status, body = _get("/fighters", {"search": entrant["display_name"]})
        if access != "ok":
            break
        response = body.get("response") if isinstance(body, Mapping) else None
        fighter_id = None
        if isinstance(response, list) and response:
            first = response[0]
            if isinstance(first, Mapping):
                fighter_id = first.get("id")
        if fighter_id is None:
            continue
        _status, rec_body = _get("/fighters/records", {"id": fighter_id})
        if access != "ok":
            break
        # API-Sports record payloads vary; accept explicit history arrays when present.
        payload = rec_body.get("response") if isinstance(rec_body, Mapping) else None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, Mapping) and isinstance(item.get("fights"), list):
                    for fight in item["fights"]:
                        if isinstance(fight, Mapping):
                            history_bouts.append(dict(fight))

    if not history_bouts:
        return {
            "access_status": access,
            "error": error,
            "request_count": request_count,
            "latencies_ms": latencies,
            "non_overlap_rate": None,
            "accuracy_status": "unknown",
            "non_overlapping_pre_dwcs_bouts": make_rate_metric(
                numerator=None,
                denominator=None,
                status="unknown",
                reason="provider_pre_dwcs_history_unavailable_or_unparsed",
            ),
            "accuracy": {
                "status": "unknown",
                "outcome_agreement": compute_outcome_agreement([]),
                "threshold": OUTCOME_AGREEMENT_MIN,
            },
        }

    measured = measure_api_sports_from_observations(
        provider_history_bouts=history_bouts,
        dwcs_bouts=dwcs_bouts,
        overlapping_outcome_pairs=None,
    )
    measured["access_status"] = access
    measured["error"] = error
    measured["request_count"] = request_count
    measured["latencies_ms"] = latencies
    # Do not persist full pair payloads in live returns used by scorecards.
    measured.pop("overlapping_outcome_pairs", None)
    return measured


def _fallback_gates_from_vendor_notes(
    notes: Mapping[str, Any],
    *,
    checklist_status: str,
) -> dict[str, Any] | None:
    """Build adoption gates only when a complete quote supplies measured thresholds."""
    if checklist_status != "complete" and notes.get("quote_status") != "complete":
        return None
    if "event_coverage_rate" not in notes:
        return None
    return {
        "quote_status": "complete",
        "metrics_status": notes.get("metrics_status", "measured"),
        "event_coverage_rate": notes.get("event_coverage_rate"),
        "bout_coverage_rate": notes.get("bout_coverage_rate"),
        "outcome_agreement_rate": notes.get("outcome_agreement_rate"),
        "required_features_status": notes.get("required_features_status", "unknown"),
        "pit_fitness_status": notes.get("pit_fitness_status", "unknown"),
        "rights_status": notes.get("rights_status", "unknown"),
        "budget_status": notes.get("budget_status", "unknown"),
    }


def build_scorecard(
    *,
    bouts: Sequence[Mapping[str, Any]],
    captured_at: str,
    capture_mode: CaptureMode,
    balldontlie_key: str | None,
    api_sports_key: str | None,
    vendor_notes: Mapping[str, Any],
    live_observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    filtered = filter_bouts_by_year(bouts, 2023, 2025)
    entrants = extract_entrants(filtered)
    difficult = select_difficult_identity_sample(entrants)
    event_ids = sorted({str(bout["event_id"]) for bout in filtered})
    bout_ids = sorted({str(bout["bout_id"]) for bout in filtered})

    rights_checked_at = captured_at
    bdl_rights = evaluate_rights_gate(_balldontlie_public_rights(checked_at=rights_checked_at))
    api_rights = evaluate_rights_gate(_api_sports_public_rights(checked_at=rights_checked_at))

    live = dict(live_observations or {})
    bdl_live = live.get("balldontlie") if isinstance(live.get("balldontlie"), Mapping) else None
    api_live = live.get("api_sports") if isinstance(live.get("api_sports"), Mapping) else None

    if balldontlie_key and bdl_live is None and capture_mode in {"live", "mixed"}:
        bdl_live = probe_balldontlie_live(
            api_key=balldontlie_key,
            bouts=filtered,
            difficult_identities=difficult,
        )
    if api_sports_key and api_live is None and capture_mode in {"live", "mixed"}:
        api_live = probe_api_sports_live(api_key=api_sports_key, dwcs_bouts=filtered)

    if not balldontlie_key and bdl_live is None:
        bdl_access: AccessStatus = "not_configured"
        bdl_metrics = _empty_provider_metrics(len(event_ids), len(bout_ids), len(difficult))
        bdl_metrics_status = "unknown"
        bdl_error = "BALLDONTLIE_API_KEY not set"
    elif bdl_live is None:
        bdl_access = "not_configured"
        bdl_metrics = _empty_provider_metrics(len(event_ids), len(bout_ids), len(difficult))
        bdl_metrics_status = "unknown"
        bdl_error = "live probe not executed"
    else:
        bdl_access = _as_access_status(bdl_live.get("access_status") or "request_failed")
        bdl_error = bdl_live.get("error")
        if bdl_access != "ok" or "event_coverage" not in bdl_live:
            bdl_metrics = _empty_provider_metrics(
                len(event_ids), len(bout_ids), len(difficult)
            )
            reason = str(bdl_access if bdl_access != "ok" else "incomplete_observation")
            for key in (
                "event_coverage",
                "bout_coverage",
                "profile_coverage",
                "stat_coverage",
            ):
                bdl_metrics[key]["reason"] = reason
                bdl_metrics[key]["status"] = "unknown"
            bdl_metrics["outcome_agreement"]["reason"] = reason
            bdl_metrics["difficult_identity_coverage"]["reason"] = reason
            bdl_metrics_status = (
                "blocked" if bdl_access not in {"not_configured", "ok"} else "unknown"
            )
        else:
            bdl_metrics = {
                "event_coverage": bdl_live["event_coverage"],
                "bout_coverage": bdl_live["bout_coverage"],
                "outcome_agreement": bdl_live["outcome_agreement"],
                "difficult_identity_coverage": bdl_live["difficult_identity_coverage"],
                "profile_coverage": bdl_live["profile_coverage"],
                "stat_coverage": bdl_live["stat_coverage"],
                "field_null_rates": bdl_live.get("pit_fitness", {}).get(
                    "field_null_rates",
                    {"status": "unknown", "reason": "not_probed", "fields": {}},
                ),
                "required_features": bdl_live["required_features"],
                "pit_fitness": bdl_live["pit_fitness"],
                "year_diagnostics": bdl_live.get("year_diagnostics"),
            }
            bdl_metrics_status = "measured"

    if not api_sports_key and api_live is None:
        api_access: AccessStatus = "not_configured"
        api_error = "API_SPORTS_KEY not set"
        api_non_overlap = None
        api_accuracy: GateStatus = "unknown"
        api_metrics_status = "unknown"
        api_non_overlap_metric = make_rate_metric(
            numerator=None,
            denominator=None,
            status="unknown",
            reason="not_configured",
        )
    elif api_live is None:
        api_access = "not_configured"
        api_error = "live probe not executed"
        api_non_overlap = None
        api_accuracy = "unknown"
        api_metrics_status = "unknown"
        api_non_overlap_metric = make_rate_metric(
            numerator=None,
            denominator=None,
            status="unknown",
            reason="live_probe_not_executed",
        )
    else:
        api_access = _as_access_status(api_live.get("access_status") or "request_failed")
        api_error = api_live.get("error")
        api_non_overlap = api_live.get("non_overlap_rate")
        api_accuracy = _as_gate_status(api_live.get("accuracy_status") or "unknown")
        api_metrics_status = "measured" if api_access == "ok" else "blocked"
        api_non_overlap_metric = api_live.get("non_overlapping_pre_dwcs_bouts") or (
            make_rate_metric(
                numerator=None,
                denominator=None,
                status="unknown",
                reason=str(api_access),
            )
        )

    sports_checklist = build_vendor_request_checklist("sportsdataio")
    combat_checklist = build_vendor_request_checklist("combat_registry")
    sports_notes = (
        dict(vendor_notes["sportsdataio"])
        if isinstance(vendor_notes.get("sportsdataio"), Mapping)
        else {}
    )
    combat_notes = (
        dict(vendor_notes["combat_registry"])
        if isinstance(vendor_notes.get("combat_registry"), Mapping)
        else {}
    )
    if sports_notes:
        sports_checklist = {**sports_checklist, **sports_notes}
    if combat_notes:
        combat_checklist = {**combat_checklist, **combat_notes}

    components_cents = {
        "the_odds_api_reference": THE_ODDS_API_CENTS,
        "balldontlie_goat_if_adopted": BALLDONTLIE_GOAT_CENTS,
    }
    recurring_cents = THE_ODDS_API_CENTS + BALLDONTLIE_GOAT_CENTS
    budget_gate = evaluate_budget_gate(
        recurring_monthly_cents=recurring_cents,
        cap_cents=MONTHLY_BUDGET_CAP_CENTS,
        components_cents=components_cents,
    )

    required_features = _as_gate_status(
        bdl_metrics.get("required_features", {}).get("status") or "unknown"
    )
    pit_fitness = _as_gate_status(
        bdl_metrics.get("pit_fitness", {}).get("status") or "unknown"
    )

    bdl_gates = {
        "event_coverage_rate": bdl_metrics["event_coverage"]["rate"],
        "bout_coverage_rate": bdl_metrics["bout_coverage"]["rate"],
        "outcome_agreement_rate": bdl_metrics["outcome_agreement"]["rate"],
        "required_features_status": required_features,
        "pit_fitness_status": pit_fitness,
        "rights_status": bdl_rights["status"],
        "budget_status": budget_gate["status"],
        "metrics_status": bdl_metrics_status,
    }
    api_gates = {
        "access_status": api_access,
        "non_overlap_rate": api_non_overlap,
        "accuracy_status": api_accuracy,
    }
    sports_gates = _fallback_gates_from_vendor_notes(
        sports_notes,
        checklist_status=str(sports_checklist.get("status") or "quote_pending"),
    )
    combat_gates = _fallback_gates_from_vendor_notes(
        combat_notes,
        checklist_status=str(combat_checklist.get("status") or "quote_pending"),
    )
    decision = apply_stats_source_decision_tree(
        balldontlie_gates=bdl_gates,
        api_sports_gates=api_gates,
        sportsdataio_status=str(sports_checklist.get("status") or "quote_pending"),
        combat_registry_status=str(combat_checklist.get("status") or "quote_pending"),
        sportsdataio_gates=sports_gates,
        combat_registry_gates=combat_gates,
        monthly_budget_cents=recurring_cents,
        budget_cap_cents=MONTHLY_BUDGET_CAP_CENTS,
    )

    live_claimed = False
    if capture_mode == "live" and bdl_metrics_status == "measured":
        live_claimed = True
    if capture_mode == "fixtures":
        live_claimed = False

    return {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "ticket": TICKET_ID,
        "captured_at": captured_at,
        "capture_mode": capture_mode,
        "live_measurements_claimed": live_claimed,
        "acceptance_evidence_mode": (
            "phase0_credential_blocker_with_executable_measurement_path"
            if bdl_access == "not_configured"
            else "measured_or_blocked_probe"
        ),
        "manifest": {
            "path": "data/manifests/dwcs_bouts_v1.jsonl",
            "filtered_years": [2023, 2024, 2025],
            "bout_count": len(bout_ids),
            "event_count": len(event_ids),
            "entrant_count": len(entrants),
        },
        "audit_universes": {
            "dwcs_2023_2025": {
                "bout_ids_sample": bout_ids[:5],
                "bout_count": len(bout_ids),
                "event_count": len(event_ids),
                "entrant_count": len(entrants),
            },
            "difficult_identities": {
                "size": len(difficult),
                "selection_method": difficult_identity_selection_method(),
                "seed": DIFFICULT_IDENTITY_SEED,
                "sample": [
                    {
                        "entrant_key": row["entrant_key"],
                        "espn_athlete_id": row.get("espn_athlete_id"),
                        "display_name": row["display_name"],
                        "normalized_name": row["normalized_name"],
                        "difficulty_score": row.get("difficulty_score"),
                    }
                    for row in difficult
                ],
            },
        },
        "providers": {
            "balldontlie": {
                "role": "provisional_primary",
                "access_status": bdl_access,
                "error": bdl_error,
                "metrics_status": bdl_metrics_status,
                "metrics": bdl_metrics,
                "rights": bdl_rights,
                "budget": {
                    "listed_monthly": money_amount(cents=BALLDONTLIE_GOAT_CENTS),
                    "citation": "https://mma.balldontlie.io/",
                },
                "docs_citations": [
                    "https://mma.balldontlie.io/",
                    "https://balldontlie.io/terms.html",
                ],
                "coverage_doc_caveat": (
                    "Provider docs state only UFC coverage is comprehensive; DWCS/regional "
                    "must be measured, never inferred from league catalog listing."
                ),
            },
            "api_sports": {
                "role": "one_month_coverage_probe",
                "access_status": api_access,
                "error": api_error,
                "metrics_status": api_metrics_status,
                "metrics": {
                    "non_overlapping_pre_dwcs_bouts": api_non_overlap_metric,
                    "accuracy": {"status": api_accuracy},
                },
                "rights": api_rights,
                "budget": {
                    "probe_budget": money_amount(cents=API_SPORTS_PROBE_CENTS),
                    "citation": "https://api-sports.io/sports/mma",
                },
                "docs_citations": [
                    "https://api-sports.io/sports/mma",
                    "https://api-sports.io/documentation/mma/v1",
                    "https://api-sports.io/terms",
                ],
            },
            "sportsdataio": {
                "role": "preferred_paid_fallback_upgrade",
                "access_status": "not_configured",
                "documented_public": sports_checklist,
                "adoption_gates": sports_gates,
                "rights": evaluate_rights_gate(
                    {
                        "storage_allowed": sports_notes.get("storage_allowed"),
                        "modeling_allowed": sports_notes.get("modeling_allowed"),
                        "source": sports_notes.get("rights_source", "no_written_response"),
                        "citation": sports_notes.get("rights_citation"),
                        "notes": (
                            "Requires written quote; public marketing SLA is not a contract"
                        ),
                        "checked_at": rights_checked_at,
                    }
                ),
                "docs_citations": [
                    "https://sportsdata.io/developers/workflow-guide/mma",
                    "https://sportsdata.io/developers/data-dictionary/mma",
                    "https://sportsdata.io/mma-ufc-api",
                ],
            },
            "combat_registry": {
                "role": "authoritative_identity_record_layer",
                "access_status": "not_configured",
                "documented_public": combat_checklist,
                "adoption_gates": combat_gates,
                "rights": evaluate_rights_gate(
                    {
                        "storage_allowed": combat_notes.get("storage_allowed"),
                        "modeling_allowed": combat_notes.get("modeling_allowed"),
                        "source": combat_notes.get("rights_source", "no_written_response"),
                        "citation": combat_notes.get(
                            "rights_citation",
                            "https://www.abcboxing.com/mma-record-keeper-criteria/",
                        ),
                        "notes": (
                            "ABC criteria describe registry obligations; "
                            "commercial API rights/price unanswered"
                        ),
                        "checked_at": rights_checked_at,
                    }
                ),
                "docs_citations": [
                    "https://www.abcboxing.com/mma-record-keeper-criteria/",
                    "https://app.combatreg.com/",
                ],
            },
        },
        "decision": decision,
        "prohibited_sources": {
            "rejected": list(PROHIBITED_PRODUCTION_SOURCES),
            "rule": "Never silently fall back to prohibited scraping",
        },
        "evidence_timestamps": {
            "scorecard_captured_at": captured_at,
            "balldontlie_terms_checked_at": rights_checked_at,
            "api_sports_terms_checked_at": rights_checked_at,
            "sportsdataio_docs_checked_at": rights_checked_at,
            "combat_registry_docs_checked_at": rights_checked_at,
        },
        "budget_context": budget_gate,
        "handoff": {
            "next_ticket": "DWCS-102",
            "contract": (
                "Implement exactly one selected primary adapter only if decision.primary "
                "is set; otherwise keep production stats ingest blocked and pursue ranked "
                "lawful fallbacks. Do not enable UFCStats/Tapology/Sherdog scrapers."
            ),
            "odds_note": (
                "Missing Bet365 feed is irrelevant to stats/identity source selection; "
                "odds remain optional enrichment (DWCS-000)."
            ),
            "phase0_note": (
                "Phase 0 permits an explicit hard blocker when credentials/quotes are "
                "absent. Acceptance evidence is the reproducible blocked/unknown state "
                "plus the executable measurement path, not invented live coverage."
            ),
        },
    }


def write_scorecard(scorecard: Mapping[str, Any], path: Path, *, redact: bool) -> None:
    payload = redact_scorecard(scorecard) if redact else dict(scorecard)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DWCS-003 licensed stats/identity source audit (read-only)"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manifests/dwcs_bouts_v1.jsonl"),
        help="Frozen DWCS bout manifest JSONL",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/research/stats-source-scorecard.json"),
    )
    parser.add_argument(
        "--capture-time",
        default=None,
        help="Fixed ISO-8601 UTC capture timestamp for deterministic regeneration",
    )
    parser.add_argument(
        "--capture-mode",
        choices=["fixtures", "live", "mixed"],
        default="fixtures",
        help="fixtures=offline/not_configured evidence; live=credentialed probes",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Strip secrets and full payloads from the written scorecard",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional dotenv path (e.g. sibling checkout .env); never committed",
    )
    parser.add_argument(
        "--vendor-notes",
        type=Path,
        default=None,
        help="Optional JSON object with SportsDataIO/Combat Registry quote responses",
    )
    parser.add_argument(
        "--balldontlie-key-env",
        default="BALLDONTLIE_API_KEY",
        help="Env var holding BALLDONTLIE API key",
    )
    parser.add_argument(
        "--api-sports-key-env",
        default="API_SPORTS_KEY",
        help="Env var holding API-Sports key (also checks API_SPORTS_API_KEY)",
    )
    parser.add_argument(
        "--max-live-requests-balldontlie",
        type=int,
        default=120,
    )
    parser.add_argument(
        "--max-live-requests-api-sports",
        type=int,
        default=30,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.env_file is not None:
        if not args.env_file.is_file():
            print(f"--env-file not found: {args.env_file}", file=sys.stderr)
            return 2
        try:
            from dotenv import load_dotenv
        except ImportError:
            print("python-dotenv required to load --env-file", file=sys.stderr)
            return 2
        load_dotenv(args.env_file, override=False)

    if not args.manifest.is_file():
        print(f"manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    captured_at = args.capture_time
    if not captured_at:
        captured_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    else:
        captured_at = _parse_iso_utc(captured_at).replace(microsecond=0).isoformat()

    vendor_notes: dict[str, Any] = {}
    if args.vendor_notes:
        loaded = _load_json(args.vendor_notes)
        if not isinstance(loaded, Mapping):
            print("--vendor-notes must be a JSON object", file=sys.stderr)
            return 2
        vendor_notes = dict(loaded)

    bdl_key = _optional_env_key(args.balldontlie_key_env)
    api_key = _optional_env_key(args.api_sports_key_env, "API_SPORTS_API_KEY")

    bouts = _load_jsonl(args.manifest)
    live_observations: dict[str, Any] | None = None
    if args.capture_mode in {"live", "mixed"}:
        live_observations = {}
        filtered = filter_bouts_by_year(bouts, 2023, 2025)
        entrants = extract_entrants(filtered)
        difficult = select_difficult_identity_sample(entrants)
        if bdl_key:
            live_observations["balldontlie"] = probe_balldontlie_live(
                api_key=bdl_key,
                bouts=filtered,
                difficult_identities=difficult,
                max_requests=args.max_live_requests_balldontlie,
            )
        if api_key:
            live_observations["api_sports"] = probe_api_sports_live(
                api_key=api_key,
                dwcs_bouts=filtered,
                max_requests=args.max_live_requests_api_sports,
            )

    scorecard = build_scorecard(
        bouts=bouts,
        captured_at=captured_at,
        capture_mode=args.capture_mode,
        balldontlie_key=bdl_key,
        api_sports_key=api_key,
        vendor_notes=vendor_notes,
        live_observations=live_observations,
    )
    write_scorecard(scorecard, args.out, redact=True)

    print(
        json.dumps(
            {
                "wrote": str(args.out),
                "capture_mode": scorecard["capture_mode"],
                "live_measurements_claimed": scorecard["live_measurements_claimed"],
                "balldontlie_access": scorecard["providers"]["balldontlie"]["access_status"],
                "api_sports_access": scorecard["providers"]["api_sports"]["access_status"],
                "decision_path": scorecard["decision"]["path"],
                "primary": scorecard["decision"]["primary"],
                "hard_blocker": scorecard["decision"]["hard_blocker"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
