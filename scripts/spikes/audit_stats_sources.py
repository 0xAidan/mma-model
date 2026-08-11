#!/usr/bin/env python3
"""DWCS-003 read-only spike: audit licensed stats/identity production sources.

Builds a reproducible scorecard for BALLDONTLIE (provisional primary), API-Sports
(coverage probe), and SportsDataIO / Combat Registry (documented fields + quote
checklist). Applies the deterministic production source decision tree.

Never scrapes Tapology, Sherdog, FightMatrix, UFC/UFCStats HTML, or Bet365.
Missing credentials are recorded as not_configured / unknown — never as zero
coverage. Sportsbook odds are out of scope for source selection.
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

SCORECARD_SCHEMA_VERSION = 1
TICKET_ID = "DWCS-003"
DIFFICULT_IDENTITY_SEED = "dwcs-003-difficult-identities-v1"
DIFFICULT_IDENTITY_SAMPLE_SIZE = 50
EVENT_COVERAGE_MIN = 0.98
BOUT_COVERAGE_MIN = 0.98
OUTCOME_AGREEMENT_MIN = 0.99
API_SPORTS_NON_OVERLAP_MIN = 0.10
MONTHLY_BUDGET_CAP_USD = 100.0
BALLDONTLIE_GOAT_USD = 39.99
THE_ODDS_API_USD = 30.0
API_SPORTS_PROBE_USD = 10.0

BALLDONTLIE_BASE = "https://api.balldontlie.io/mma/v1"
API_SPORTS_BASE = "https://v1.mma.api-sports.io"

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
    recurring_monthly_usd: float,
    cap_usd: float,
    components: Mapping[str, float],
) -> dict[str, Any]:
    status: GateStatus = "pass" if recurring_monthly_usd <= cap_usd else "fail"
    return {
        "status": status,
        "recurring_monthly_usd": recurring_monthly_usd,
        "cap_usd": cap_usd,
        "components": dict(components),
    }


def build_vendor_request_checklist(vendor: str) -> dict[str, Any]:
    """Fields that require a written vendor response; unanswered items are blockers."""
    common_items = [
        {
            "id": "fields_event_bout_result_profile_stat",
            "question": "Confirm event/bout/result/profile/stat fields for DWCS + regional history",
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
                    fight_date = _parse_iso_utc(str(fight_date_raw)[:10] + "T00:00:00+00:00").date()
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


def compute_outcome_agreement(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    denominator = len(pairs)
    numerator = 0
    for pair in pairs:
        if pair.get("manifest_class") != pair.get("provider_class"):
            continue
        if pair.get("manifest_class") == "decisive" and not pair.get("winner_agree"):
            continue
        numerator += 1
    return make_rate_metric(
        numerator=numerator,
        denominator=denominator,
        status="measured" if denominator else "unknown",
        reason=None if denominator else "empty_pairs",
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
        "price_usd_monthly": BALLDONTLIE_GOAT_USD,
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
        "probe_budget_usd": API_SPORTS_PROBE_USD,
    }


def apply_stats_source_decision_tree(
    *,
    balldontlie_gates: Mapping[str, Any],
    api_sports_gates: Mapping[str, Any],
    sportsdataio_status: str,
    combat_registry_status: str,
    monthly_budget_usd: float,
    budget_cap_usd: float = MONTHLY_BUDGET_CAP_USD,
) -> dict[str, Any]:
    """Apply plan §4 production source decision tree exactly."""
    metrics_status = str(balldontlie_gates.get("metrics_status") or "unknown")
    event_rate = balldontlie_gates.get("event_coverage_rate")
    bout_rate = balldontlie_gates.get("bout_coverage_rate")
    outcome_rate = balldontlie_gates.get("outcome_agreement_rate")

    def _ge(value: Any, threshold: float) -> bool:
        return isinstance(value, (int, float)) and float(value) >= threshold

    bdl_technical = (
        metrics_status == "measured"
        and _ge(event_rate, EVENT_COVERAGE_MIN)
        and _ge(bout_rate, BOUT_COVERAGE_MIN)
        and _ge(outcome_rate, OUTCOME_AGREEMENT_MIN)
        and balldontlie_gates.get("required_features_status") == "pass"
        and balldontlie_gates.get("pit_fitness_status") == "pass"
    )
    bdl_rights = balldontlie_gates.get("rights_status") == "pass"
    bdl_budget = balldontlie_gates.get("budget_status") == "pass"
    adopt_bdl = bdl_technical and bdl_rights and bdl_budget

    api_access = str(api_sports_gates.get("access_status") or "not_configured")
    non_overlap = api_sports_gates.get("non_overlap_rate")
    api_keep = (
        api_access == "ok"
        and _ge(non_overlap, API_SPORTS_NON_OVERLAP_MIN)
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
    else:
        primary = None
        path = "hard_blocker"
        hard_blocker = True
        rationale = (
            "BALLDONTLIE did not clear all adoption gates "
            f"(technical={bdl_technical}, rights={bdl_rights}, budget={bdl_budget}, "
            f"metrics_status={metrics_status}). "
            "Lawful fallbacks require SportsDataIO and/or Combat Registry written "
            "quotes that clear the same technical + rights + budget gates. "
            "Prohibited scraping is rejected."
        )
        ranked.append(
            {
                "rank": 1,
                "source": "sportsdataio",
                "status": sportsdataio_status,
                "role": "preferred_paid_fallback_upgrade",
                "requires": "written_quote_fields_rights_retention_sla_price",
            }
        )
        ranked.append(
            {
                "rank": 2,
                "source": "combat_registry",
                "status": combat_registry_status,
                "role": "authoritative_identity_record_layer",
                "requires": "written_quote_api_rights_price",
            }
        )
        if api_keep:
            ranked.append(
                {
                    "rank": 3,
                    "source": "api_sports",
                    "status": "probe_keep",
                    "role": "one_month_non_overlap_probe",
                    "requires": "continue_only_while_non_overlap_and_accuracy_hold",
                }
            )
        else:
            ranked.append(
                {
                    "rank": 3,
                    "source": "api_sports",
                    "status": "probe_cancel_or_blocked",
                    "role": "one_month_non_overlap_probe",
                    "requires": (
                        f">={API_SPORTS_NON_OVERLAP_MIN:.0%} non-overlapping pre-DWCS "
                        "bouts plus accuracy; else cancel"
                    ),
                }
            )

    gate_detail = {
        "balldontlie": {
            "event_coverage_min": EVENT_COVERAGE_MIN,
            "bout_coverage_min": BOUT_COVERAGE_MIN,
            "outcome_agreement_min": OUTCOME_AGREEMENT_MIN,
            "event_coverage_rate": event_rate,
            "bout_coverage_rate": bout_rate,
            "outcome_agreement_rate": outcome_rate,
            "required_features_status": balldontlie_gates.get("required_features_status"),
            "pit_fitness_status": balldontlie_gates.get("pit_fitness_status"),
            "rights_status": balldontlie_gates.get("rights_status"),
            "budget_status": balldontlie_gates.get("budget_status"),
            "metrics_status": metrics_status,
            "technical_pass": bdl_technical,
            "adopt": adopt_bdl,
        },
        "api_sports": {
            "non_overlap_min": API_SPORTS_NON_OVERLAP_MIN,
            "non_overlap_rate": non_overlap,
            "accuracy_status": api_sports_gates.get("accuracy_status"),
            "access_status": api_access,
            "probe_keep": api_keep,
        },
        "budget": {
            "monthly_usd": monthly_budget_usd,
            "cap_usd": budget_cap_usd,
            "within_cap": monthly_budget_usd <= budget_cap_usd,
        },
    }

    return {
        "primary": primary,
        "path": path,
        "hard_blocker": hard_blocker,
        "rationale": rationale,
        "api_sports_probe_keep": api_keep,
        "ranked_lawful_fallbacks": ranked,
        "prohibited_scraping_selected": False,
        "gates": gate_detail,
    }


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, "[REDACTED]" if any(frag in key.lower() for frag in SECRET_KEY_FRAGMENTS) else value)
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
            for frag in ("apiKey=", "api_key=", "Authorization:"):
                if frag.lower() in redacted.lower():
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
    denominator_fighters: int,
) -> dict[str, Any]:
    unknown = "not_configured"
    return {
        "event_coverage": make_rate_metric(
            numerator=None, denominator=denominator_events, status="unknown", reason=unknown
        ),
        "bout_coverage": make_rate_metric(
            numerator=None, denominator=denominator_bouts, status="unknown", reason=unknown
        ),
        "outcome_agreement": make_rate_metric(
            numerator=None, denominator=denominator_bouts, status="unknown", reason=unknown
        ),
        "profile_coverage": make_rate_metric(
            numerator=None, denominator=denominator_fighters, status="unknown", reason=unknown
        ),
        "stat_coverage": make_rate_metric(
            numerator=None, denominator=denominator_bouts, status="unknown", reason=unknown
        ),
        "field_null_rates": {"status": "unknown", "reason": unknown, "fields": {}},
        "pit_fitness": {
            "status": "unknown",
            "reason": unknown,
            "pre_fight_reconstruction": None,
            "revision_support": None,
            "latency_ms_p50": None,
            "request_cost_units": None,
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


def probe_balldontlie_live(
    *,
    api_key: str,
    bouts: Sequence[Mapping[str, Any]],
    entrants: Sequence[Mapping[str, Any]],
    timeout_sec: float = 20.0,
    max_requests: int = 40,
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
            body: Any
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

    years = sorted({int(b["calendar_year"]) for b in bouts if b.get("calendar_year") is not None})
    for year in years:
        status, body = _get("/events", {"year": year, "per_page": 100})
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
            "matched_events": 0,
            "matched_bouts": 0,
            "outcome_pairs": [],
            "profile_hits": 0,
            "stat_hits": 0,
            "field_nulls": {},
        }

    # Map DWCS-looking events by normalized participant sets from fights when budget allows.
    provider_fights: list[dict[str, Any]] = []
    for year, events in events_by_year.items():
        for event in events:
            if request_count >= max_requests:
                break
            event_id = event.get("id")
            if event_id is None:
                continue
            name = str(event.get("name") or "")
            # Prefer likely DWCS rows; still do not invent coverage outside responses.
            if "contender" not in name.lower() and "dwcs" not in name.lower():
                continue
            status, body = _get("/fights", {"event_ids[]": event_id, "per_page": 100})
            if access != "ok":
                break
            data = body.get("data") if isinstance(body, Mapping) else None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, Mapping):
                        row = dict(item)
                        row["_event_year"] = year
                        provider_fights.append(row)

    matched_bouts = 0
    outcome_pairs: list[dict[str, Any]] = []
    for bout in bouts:
        matched = match_bout_to_provider_fight(bout, provider_fights)
        if matched is None:
            continue
        matched_bouts += 1
        manifest_class = None
        night = bout.get("event_night_result")
        if isinstance(night, Mapping):
            manifest_class = night.get("class")
        provider_class = "decisive"
        status_text = str(matched.get("status") or "").lower()
        if "draw" in status_text:
            provider_class = "draw"
        elif "no_contest" in status_text or status_text == "nc":
            provider_class = "no_contest"
        winner_norm = None
        if isinstance(night, Mapping):
            winner_norm = night.get("winner_normalized")
        # Provider winner agreement is unknown without stable winner id mapping.
        outcome_pairs.append(
            {
                "manifest_class": manifest_class,
                "provider_class": provider_class,
                "winner_agree": not winner_norm,
            }
        )

    profile_hits = 0
    for entrant in list(entrants)[: min(20, len(entrants))]:
        if request_count >= max_requests:
            break
        status, body = _get("/fighters", {"search": entrant["display_name"], "per_page": 5})
        if access != "ok":
            break
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, list):
            continue
        target = normalize_fighter_name(str(entrant["normalized_name"]))
        for item in data:
            if not isinstance(item, Mapping):
                continue
            if normalize_fighter_name(str(item.get("name") or "")) == target:
                profile_hits += 1
                break

    event_ids_manifest = {str(b["event_id"]) for b in bouts}
    # Event coverage uses provider DWCS-named events vs unique manifest events for years probed.
    matched_event_ids = {
        str(fight.get("event", {}).get("id"))
        for fight in provider_fights
        if isinstance(fight.get("event"), Mapping)
    }
    matched_events = len(matched_event_ids)
    # Without a reliable event id crosswalk, event coverage stays conservative:
    # count years with any DWCS-named event.
    years_with_dwcs_events = len(events_by_year)
    return {
        "access_status": access,
        "error": error,
        "request_count": request_count,
        "latencies_ms": latencies,
        "metrics_status": "measured",
        "matched_events": matched_events,
        "years_with_provider_events": years_with_dwcs_events,
        "matched_bouts": matched_bouts,
        "outcome_pairs": outcome_pairs,
        "profile_hits": profile_hits,
        "profile_probed": min(20, len(entrants)),
        "stat_hits": 0,
        "stat_note": "fight_stats not counted without fight id crosswalk under request budget",
        "field_nulls": {},
        "manifest_event_denominator": len(event_ids_manifest),
        "provider_dwcs_named_fight_count": len(provider_fights),
        "crosswalk_limitation": (
            "No ESPN↔BALLDONTLIE id map yet; bout matches use normalized names+date only. "
            "Coverage numerators are lower-bound observations, not catalog claims."
        ),
    }


def probe_api_sports_live(
    *,
    api_key: str,
    timeout_sec: float = 20.0,
    max_requests: int = 15,
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

    status, body = _get("/status")
    categories = None
    if access == "ok":
        status, cat_body = _get("/categories")
        if isinstance(cat_body, Mapping):
            categories = cat_body.get("response")

    return {
        "access_status": access,
        "error": error,
        "request_count": request_count,
        "latencies_ms": latencies,
        "status_endpoint_ok": status == 200 and access == "ok",
        "categories_observed": isinstance(categories, list),
        "non_overlap_rate": None,
        "non_overlap_status": "unknown",
        "non_overlap_reason": (
            "Regional non-overlap vs DWCS manifest not measured without lawful "
            "pre-DWCS bout inventory from this provider under current probe."
        ),
        "accuracy_status": "unknown",
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
    event_ids = sorted({str(b["event_id"]) for b in filtered})
    bout_ids = sorted({str(b["bout_id"]) for b in filtered})

    rights_checked_at = captured_at
    bdl_rights = evaluate_rights_gate(_balldontlie_public_rights(checked_at=rights_checked_at))
    api_rights = evaluate_rights_gate(_api_sports_public_rights(checked_at=rights_checked_at))

    live = dict(live_observations or {})
    bdl_live = live.get("balldontlie") if isinstance(live.get("balldontlie"), Mapping) else None
    api_live = live.get("api_sports") if isinstance(live.get("api_sports"), Mapping) else None

    if balldontlie_key and bdl_live is None and capture_mode in {"live", "mixed"}:
        bdl_live = probe_balldontlie_live(
            api_key=balldontlie_key, bouts=filtered, entrants=entrants
        )
    if api_sports_key and api_live is None and capture_mode in {"live", "mixed"}:
        api_live = probe_api_sports_live(api_key=api_sports_key)

    if not balldontlie_key:
        bdl_access: AccessStatus = "not_configured"
        bdl_metrics = _empty_provider_metrics(len(event_ids), len(bout_ids), len(entrants))
        bdl_metrics_status = "unknown"
        bdl_error = "BALLDONTLIE_API_KEY not set"
    elif bdl_live is None:
        bdl_access = "not_configured"
        bdl_metrics = _empty_provider_metrics(len(event_ids), len(bout_ids), len(entrants))
        bdl_metrics_status = "unknown"
        bdl_error = "live probe not executed"
    else:
        bdl_access = AccessStatus(str(bdl_live.get("access_status") or "request_failed"))
        if bdl_access != "ok":
            bdl_metrics = _empty_provider_metrics(len(event_ids), len(bout_ids), len(entrants))
            # Preserve access reason on metrics (not zero coverage).
            reason = str(bdl_access)
            metric_keys = (
                "event_coverage",
                "bout_coverage",
                "outcome_agreement",
                "profile_coverage",
                "stat_coverage",
            )
            for key in metric_keys:
                bdl_metrics[key]["reason"] = reason
                bdl_metrics[key]["status"] = "unknown"
            bdl_metrics_status = "blocked" if bdl_access != "not_configured" else "unknown"
            bdl_error = bdl_live.get("error")
        else:
            outcome = compute_outcome_agreement(bdl_live.get("outcome_pairs") or [])
            bdl_metrics = {
                "event_coverage": make_rate_metric(
                    numerator=int(bdl_live.get("years_with_provider_events") or 0),
                    denominator=len({int(b["calendar_year"]) for b in filtered}),
                    status="measured",
                    reason=str(bdl_live.get("crosswalk_limitation")),
                ),
                "bout_coverage": make_rate_metric(
                    numerator=int(bdl_live.get("matched_bouts") or 0),
                    denominator=len(bout_ids),
                    status="measured",
                    reason=str(bdl_live.get("crosswalk_limitation")),
                ),
                "outcome_agreement": outcome,
                "profile_coverage": make_rate_metric(
                    numerator=int(bdl_live.get("profile_hits") or 0),
                    denominator=int(bdl_live.get("profile_probed") or len(entrants)),
                    status="measured",
                ),
                "stat_coverage": make_rate_metric(
                    numerator=int(bdl_live.get("stat_hits") or 0),
                    denominator=len(bout_ids),
                    status="measured",
                    reason=str(bdl_live.get("stat_note")),
                ),
                "field_null_rates": {
                    "status": "unknown",
                    "reason": "null-rate probe limited without full fight_stats pull",
                    "fields": bdl_live.get("field_nulls") or {},
                },
                "pit_fitness": {
                    "status": "unknown",
                    "reason": "point-in-time reconstruction not proven in this spike",
                    "pre_fight_reconstruction": None,
                    "revision_support": None,
                    "latency_ms_p50": (
                        sorted(bdl_live.get("latencies_ms") or [])[
                            len(bdl_live.get("latencies_ms") or []) // 2
                        ]
                        if bdl_live.get("latencies_ms")
                        else None
                    ),
                    "request_cost_units": bdl_live.get("request_count"),
                },
            }
            bdl_metrics_status = "measured"
            bdl_error = bdl_live.get("error")

    if not api_sports_key:
        api_access: AccessStatus = "not_configured"
        api_error = "API_SPORTS_KEY not set"
        api_non_overlap = None
        api_accuracy: GateStatus = "unknown"
        api_metrics_status = "unknown"
    elif api_live is None:
        api_access = "not_configured"
        api_error = "live probe not executed"
        api_non_overlap = None
        api_accuracy = "unknown"
        api_metrics_status = "unknown"
    else:
        api_access = AccessStatus(str(api_live.get("access_status") or "request_failed"))
        api_error = api_live.get("error")
        api_non_overlap = api_live.get("non_overlap_rate")
        api_accuracy = GateStatus(str(api_live.get("accuracy_status") or "unknown"))
        api_metrics_status = "measured" if api_access == "ok" else "blocked"

    sports_checklist = build_vendor_request_checklist("sportsdataio")
    combat_checklist = build_vendor_request_checklist("combat_registry")
    if isinstance(vendor_notes.get("sportsdataio"), Mapping):
        sports_checklist = {**sports_checklist, **dict(vendor_notes["sportsdataio"])}
    if isinstance(vendor_notes.get("combat_registry"), Mapping):
        combat_checklist = {**combat_checklist, **dict(vendor_notes["combat_registry"])}

    budget_components = {
        "the_odds_api_reference": THE_ODDS_API_USD,
        "balldontlie_goat_if_adopted": BALLDONTLIE_GOAT_USD,
    }
    recurring = THE_ODDS_API_USD + BALLDONTLIE_GOAT_USD
    budget_gate = evaluate_budget_gate(
        recurring_monthly_usd=recurring,
        cap_usd=MONTHLY_BUDGET_CAP_USD,
        components=budget_components,
    )

    # Required features / PIT cannot pass without measured live fitness.
    required_features: GateStatus = "unknown"
    pit_fitness: GateStatus = "unknown"
    if bdl_metrics_status == "measured":
        # Spike cannot claim feature/PIT pass without explicit reconstruction evidence.
        required_features = "fail"
        pit_fitness = "fail"
        if isinstance(bdl_metrics.get("pit_fitness"), Mapping):
            bdl_metrics["pit_fitness"]["status"] = "fail"
            bdl_metrics["pit_fitness"]["reason"] = (
                "Measured endpoints responded, but pre-fight reconstruction and "
                "revision semantics were not evidenced."
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
    decision = apply_stats_source_decision_tree(
        balldontlie_gates=bdl_gates,
        api_sports_gates=api_gates,
        sportsdataio_status=str(sports_checklist.get("status") or "quote_pending"),
        combat_registry_status=str(combat_checklist.get("status") or "quote_pending"),
        monthly_budget_usd=recurring,
        budget_cap_usd=MONTHLY_BUDGET_CAP_USD,
    )

    live_claimed = capture_mode == "live" and bdl_metrics_status == "measured"
    if capture_mode == "fixtures":
        live_claimed = False

    return {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "ticket": TICKET_ID,
        "captured_at": captured_at,
        "capture_mode": capture_mode,
        "live_measurements_claimed": live_claimed,
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
                    "listed_monthly_usd": BALLDONTLIE_GOAT_USD,
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
                    "non_overlapping_pre_dwcs_bouts": make_rate_metric(
                        numerator=None,
                        denominator=None,
                        status="unknown",
                        reason=(
                            "not_configured"
                            if api_access == "not_configured"
                            else "not_measured"
                        ),
                    ),
                    "accuracy": {"status": api_accuracy},
                },
                "rights": api_rights,
                "budget": {
                    "probe_budget_usd": API_SPORTS_PROBE_USD,
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
                "rights": evaluate_rights_gate(
                    {
                        "storage_allowed": None,
                        "modeling_allowed": None,
                        "source": "no_written_response",
                        "citation": None,
                        "notes": "Requires written quote; public marketing SLA is not a contract",
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
                "rights": evaluate_rights_gate(
                    {
                        "storage_allowed": None,
                        "modeling_allowed": None,
                        "source": "no_written_response",
                        "citation": "https://www.abcboxing.com/mma-record-keeper-criteria/",
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
        default=40,
    )
    parser.add_argument(
        "--max-live-requests-api-sports",
        type=int,
        default=15,
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
        # Normalize to comparable ISO.
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
        if bdl_key:
            live_observations["balldontlie"] = probe_balldontlie_live(
                api_key=bdl_key,
                bouts=filtered,
                entrants=entrants,
                max_requests=args.max_live_requests_balldontlie,
            )
        if api_key:
            live_observations["api_sports"] = probe_api_sports_live(
                api_key=api_key,
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
