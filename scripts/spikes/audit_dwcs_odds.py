#!/usr/bin/env python3
"""DWCS-000 read-only spike: capture live DWCS odds coverage evidence.

Discovers MMA events from The Odds API (and optional trial vendors when keys are
present), reconciles them to an official bout list, records bookmaker/market
presence separately from request failures, and writes a sanitized coverage
summary. Never commits or prints API keys or live prices when --redact is set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from mma_model.config import get_settings

BoutStatus = Literal["present", "absent", "unresolved"]
PresenceStatus = Literal["present", "absent", "request_failed"]
MatrixStatus = Literal["pass", "fail", "blocked", "unknown"]
DecisionPath = Literal[
    "licensed_bet365_primary",
    "the_odds_api_reference_fallback",
    "hard_blocker",
]

DEFAULT_SPORT = "mma_mixed_martial_arts"
DEFAULT_REGIONS = "us,uk,eu,au"
# The Odds API catalogs Bet365 Australia as bet365_au (region au), not bare bet365.
DEFAULT_BET365_ALIASES = ("bet365", "bet365_au")
DEFAULT_BOOKMAKERS = (
    "bet365",
    "bet365_au",
    "draftkings",
    "fanduel",
    "betmgm",
    "williamhill_us",
)
DEFAULT_MARKETS = ("h2h", "totals", "method", "round")
MAX_MANUAL_BET365_SAMPLES = 5
Bet365DwcsStatus = Literal[
    "present",
    "scoped_absent",
    "request_failed",
    "unresolved",
]
MATRIX_KEYS = (
    "moneyline",
    "totals",
    "method",
    "round",
    "lock_events",
    "historical_replay",
    "rights",
    "monthly_quote",
)
QUOTA_HEADER_KEYS = (
    "x-requests-remaining",
    "x-requests-used",
    "x-requests-last",
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
PRICE_KEYS = frozenset(
    {
        "price",
        "displayed_price",
        "american",
        "decimal",
        "odds",
        "opening_price",
        "closing_price",
    }
)

THE_ODDS_API_BASE = "https://api.the-odds-api.com/v4"


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
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bout_participants_match(bout: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    fighters = {
        normalize_fighter_name(str(bout["fighter_a"])),
        normalize_fighter_name(str(bout["fighter_b"])),
    }
    teams = {
        normalize_fighter_name(str(event.get("home_team", ""))),
        normalize_fighter_name(str(event.get("away_team", ""))),
    }
    return fighters == teams and "" not in fighters


def _within_time_window(
    bout: Mapping[str, Any],
    event: Mapping[str, Any],
    max_delta_minutes: int,
) -> bool:
    bout_start = _parse_iso_utc(str(bout["scheduled_start"]))
    event_start = _parse_iso_utc(str(event["commence_time"]))
    delta = abs((bout_start - event_start).total_seconds())
    return delta <= max_delta_minutes * 60


def match_bout_to_event(
    bout: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    max_delta_minutes: int = 30,
) -> dict[str, Any] | None:
    """Return the unique matching provider event, or None if zero/ambiguous."""
    candidates = [
        dict(event)
        for event in events
        if _bout_participants_match(bout, event)
        and _within_time_window(bout, event, max_delta_minutes)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def classify_official_bouts(
    official_bouts: Sequence[Mapping[str, Any]],
    provider_events: Sequence[Mapping[str, Any]],
    *,
    max_delta_minutes: int = 30,
) -> list[dict[str, Any]]:
    """Classify each official bout as present, absent, or unresolved."""
    rows: list[dict[str, Any]] = []
    for bout in official_bouts:
        participant_hits = [
            dict(event)
            for event in provider_events
            if _bout_participants_match(bout, event)
        ]
        timed_hits = [
            event
            for event in participant_hits
            if _within_time_window(bout, event, max_delta_minutes)
        ]
        status: BoutStatus
        matched_event_id: str | None = None
        reason: str
        if len(timed_hits) == 1:
            status = "present"
            matched_event_id = str(timed_hits[0].get("id"))
            reason = "unique participant+time match"
        elif len(timed_hits) > 1:
            status = "unresolved"
            reason = "multiple participant+time matches"
        elif len(participant_hits) > 0:
            status = "unresolved"
            reason = "participant match outside time window or ambiguous timing"
        else:
            status = "absent"
            reason = "no participant match in provider event list"
        rows.append(
            {
                "bout_id": str(bout["bout_id"]),
                "fighter_a": str(bout["fighter_a"]),
                "fighter_b": str(bout["fighter_b"]),
                "scheduled_start": str(bout["scheduled_start"]),
                "status": status,
                "matched_event_id": matched_event_id,
                "reason": reason,
            }
        )
    return rows


def bookmaker_market_presence(
    discovery: Mapping[str, Any],
    *,
    bookmaker_keys: Sequence[str],
    market_keys: Sequence[str],
) -> dict[str, dict[str, PresenceStatus]]:
    """Map bookmaker×market to present/absent/request_failed."""
    matrix: dict[str, dict[str, PresenceStatus]] = {
        bookmaker: {market: "absent" for market in market_keys}
        for bookmaker in bookmaker_keys
    }
    if discovery.get("status") == "request_failed":
        for bookmaker in bookmaker_keys:
            for market in market_keys:
                matrix[bookmaker][market] = "request_failed"
        return matrix

    observed: dict[str, set[str]] = {}
    for bookmaker in discovery.get("bookmakers", []) or []:
        key = str(bookmaker.get("key", "")).lower()
        markets = {
            str(market.get("key", "")).lower()
            for market in (bookmaker.get("markets") or [])
            if market.get("key")
        }
        if key:
            observed[key] = markets

    for bookmaker in bookmaker_keys:
        seen = observed.get(bookmaker.lower(), set())
        for market in market_keys:
            matrix[bookmaker][market] = "present" if market.lower() in seen else "absent"
    return matrix


def normalize_bookmaker_aliases(aliases: Sequence[str] | None) -> list[str]:
    """Normalize configurable Bet365 bookmaker aliases (deduped, lowercased)."""
    source = aliases if aliases is not None else DEFAULT_BET365_ALIASES
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in source:
        key = str(alias).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def extract_market_timestamp_evidence(
    discovery: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Keep per-market last_update evidence; drop prices and full market payloads."""
    rows: list[dict[str, str]] = []
    if discovery.get("status") == "request_failed":
        return rows
    for bookmaker in discovery.get("bookmakers", []) or []:
        bookmaker_key = str(bookmaker.get("key", "")).strip().lower()
        if not bookmaker_key:
            continue
        for market in bookmaker.get("markets") or []:
            market_key = str(market.get("key", "")).strip().lower()
            last_update = market.get("last_update")
            if not market_key or not last_update:
                continue
            rows.append(
                {
                    "bookmaker_key": bookmaker_key,
                    "market_key": market_key,
                    "last_update": str(last_update),
                }
            )
    return rows


def queried_bet365_keys(
    bookmaker_keys: Sequence[str],
    *,
    aliases: Sequence[str],
) -> list[str]:
    """Intersection of requested bookmakers and configured Bet365 aliases."""
    alias_set = {alias.lower() for alias in aliases}
    queried: list[str] = []
    seen: set[str] = set()
    for key in bookmaker_keys:
        lowered = str(key).strip().lower()
        if lowered in alias_set and lowered not in seen:
            seen.add(lowered)
            queried.append(lowered)
    return queried


BET365_AU_CATALOG_CONTEXT = (
    "The Odds API currently lists bet365_au as paid-tier and limited to AFL/NRL "
    "h2h/spreads/totals coverage. MMA absence on a live query may reflect "
    "provider/catalog/plan coverage and is not evidence that the underlying "
    "sportsbook universally lacks DWCS. The live event query remains the only "
    "event-specific evidence; do not infer beyond it."
)


def build_bet365_observation_scope(
    *,
    provider: str,
    regions: str | None,
    bookmaker_keys: Sequence[str],
    aliases: Sequence[str],
) -> dict[str, Any]:
    """Record how Bet365 was probed, including Odds API parameter precedence.

    When both ``bookmakers`` and ``regions`` are sent, The Odds API gives
    ``bookmakers`` priority. Region selection must not be described as the
    effective bookmaker-probe scope in that mode.
    """
    requested_bookmakers = [
        str(key).strip() for key in bookmaker_keys if str(key).strip()
    ]
    queried = queried_bet365_keys(requested_bookmakers, aliases=aliases)
    regions_requested = regions
    if requested_bookmakers:
        effective_query_mode = "bookmakers"
        query_mode_note = (
            "The Odds API bookmakers parameter takes precedence when both "
            "bookmakers and regions are sent; regions_requested is informational "
            "and does not itself scope the bookmaker probe."
        )
        regions_effective_for_bookmaker_probe = None
    else:
        effective_query_mode = "regions"
        query_mode_note = (
            "No bookmakers parameter was supplied; the bookmaker probe is scoped "
            "by the regions parameter."
        )
        regions_effective_for_bookmaker_probe = regions_requested
    return {
        "provider": provider,
        "effective_query_mode": effective_query_mode,
        "bookmaker_keys_requested": requested_bookmakers,
        "bookmaker_keys_queried": queried,
        "regions_requested": regions_requested,
        "regions_effective_for_bookmaker_probe": regions_effective_for_bookmaker_probe,
        "aliases_configured": list(aliases),
        "query_mode_note": query_mode_note,
        "catalog_context": {"bet365_au": BET365_AU_CATALOG_CONTEXT},
    }


def resolve_bet365_alias_statuses(
    presence: Mapping[str, Mapping[str, Any]],
    *,
    aliases: Sequence[str],
    market_key: str = "h2h",
) -> list[PresenceStatus | None]:
    """Collect presence statuses for configured Bet365 aliases on one event."""
    statuses: list[PresenceStatus | None] = []
    market = market_key.lower()
    for alias in aliases:
        markets = presence.get(alias) or presence.get(alias.lower())
        if not isinstance(markets, Mapping):
            statuses.append(None)
            continue
        status = markets.get(market)
        if status in {"present", "absent", "request_failed"}:
            statuses.append(status)  # type: ignore[arg-type]
        else:
            statuses.append(None)
    return statuses


def summarize_scoped_bet365_observation(
    alias_status_rows: Sequence[Sequence[PresenceStatus | None]],
    *,
    provider: str,
    regions: str,
    bookmaker_keys: Sequence[str],
    aliases: Sequence[str],
) -> dict[str, Any]:
    """Summarize Bet365×DWCS evidence without claiming universal absence."""
    scope = build_bet365_observation_scope(
        provider=provider,
        regions=regions,
        bookmaker_keys=bookmaker_keys,
        aliases=aliases,
    )
    queried = list(scope["bookmaker_keys_queried"])
    flat: list[PresenceStatus] = [
        status
        for row in alias_status_rows
        for status in row
        if status is not None
    ]
    if not queried:
        return {
            "bet365_present_on_dwcs": None,
            "bet365_query_status": None,
            "bet365_dwcs_status": "unresolved",
            "bet365_observation_scope": scope,
        }
    if not flat:
        return {
            "bet365_present_on_dwcs": None,
            "bet365_query_status": None,
            "bet365_dwcs_status": "unresolved",
            "bet365_observation_scope": scope,
        }
    if any(status == "present" for status in flat):
        return {
            "bet365_present_on_dwcs": True,
            "bet365_query_status": "ok",
            "bet365_dwcs_status": "present",
            "bet365_observation_scope": scope,
        }
    if any(status == "request_failed" for status in flat):
        return {
            "bet365_present_on_dwcs": None,
            "bet365_query_status": "request_failed",
            "bet365_dwcs_status": "request_failed",
            "bet365_observation_scope": scope,
        }
    if all(status == "absent" for status in flat):
        return {
            "bet365_present_on_dwcs": False,
            "bet365_query_status": "ok",
            "bet365_dwcs_status": "scoped_absent",
            "bet365_observation_scope": scope,
        }
    return {
        "bet365_present_on_dwcs": None,
        "bet365_query_status": "request_failed",
        "bet365_dwcs_status": "request_failed",
        "bet365_observation_scope": scope,
    }


def _is_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _is_secret_key(key) or key.lower() in {"apikey", "api_key"}:
            query.append((key, "[REDACTED]"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_summary(payload: Mapping[str, Any] | Sequence[Any] | Any) -> Any:
    """Recursively drop secrets and odds prices from a summary payload."""
    if isinstance(payload, Mapping):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            key_str = str(key)
            lowered = key_str.lower()
            if _is_secret_key(key_str):
                continue
            if lowered in PRICE_KEYS:
                continue
            if lowered in {"request_url", "url", "href"} and isinstance(value, str):
                redacted[key_str] = _redact_url(value)
                continue
            redacted[key_str] = redact_summary(value)
        return redacted
    if isinstance(payload, list):
        return [redact_summary(item) for item in payload]
    if isinstance(payload, tuple):
        return [redact_summary(item) for item in payload]
    if isinstance(payload, str) and "apiKey=" in payload:
        return re.sub(r"(apiKey=)[^&]+", r"\1[REDACTED]", payload, flags=re.IGNORECASE)
    return payload


def _matrix_row(status: MatrixStatus, evidence: str) -> dict[str, str]:
    return {"status": status, "evidence": evidence}


def _market_matrix_status(
    market_key: str,
    *,
    markets_observed: set[str],
    discovery_status: str | None,
) -> tuple[MatrixStatus, str]:
    if market_key in markets_observed:
        return "pass", f"{market_key} observed on a reconciled DWCS event"
    if discovery_status == "ok":
        return "fail", f"{market_key} not observed on reconciled DWCS market discovery"
    if discovery_status == "request_failed":
        return "unknown", f"{market_key} unevaluated because DWCS market discovery request failed"
    return "unknown", f"{market_key} unevaluated; no DWCS market discovery capture"


def validate_manual_bet365_samples(
    samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Enforce the plan maximum of five manual Bet365 samples."""
    if len(samples) > MAX_MANUAL_BET365_SAMPLES:
        raise ValueError(
            f"manual Bet365 samples exceed limit: got {len(samples)}, "
            f"at most {MAX_MANUAL_BET365_SAMPLES} allowed"
        )
    return [dict(sample) for sample in samples]


def missing_api_key_reason(env_name: str) -> str:
    """Build a blocked-reason string without duplicated env-var wording."""
    if env_name == "ODDS_API_KEY":
        return "ODDS_API_KEY unavailable; live odds audit not executed"
    return (
        f"{env_name} unavailable and ODDS_API_KEY unset; live odds audit not executed"
    )


def sanitize_provider_error(message: str, *, api_key: str = "") -> str:
    """Remove API keys and credential-bearing query params from error text."""
    text = str(message)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    text = re.sub(r"(apiKey=)[^&\s\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(api_key=)[^&\s\"']+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(Authorization:\s*)\S+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _unevaluated_matrix(*, evidence_note: str) -> dict[str, dict[str, str]]:
    matrix = {
        key: _matrix_row("unknown", evidence_note)
        for key in MATRIX_KEYS
    }
    matrix["rights"] = _matrix_row(
        "blocked",
        "Rights/trial evidence unavailable; no licensed coverage decision observed",
    )
    return matrix


def build_not_run_artifact(*, block_reason: str) -> dict[str, Any]:
    """Machine-readable blocked artifact when the live audit cannot run."""
    return {
        "ticket": "DWCS-000",
        "run_status": "not_run",
        "status": "blocked",
        "block_reason": block_reason,
        "provider": None,
        "sport": DEFAULT_SPORT,
        "captured_at": None,
        "snapshot_label": None,
        "regions": None,
        "bookmaker_keys": [],
        "market_keys": [],
        "bout_classifications": [],
        "events": [],
        "events_list": {"headers": {}, "schema_keys": []},
        "providers": {},
        "manual_bet365_samples": [],
        "pass_fail_matrix": _unevaluated_matrix(
            evidence_note="No live capture; evidence not observed"
        ),
        "decision": {
            "observed": False,
            "bet365_dwcs_status": "unresolved",
            "path": "hard_blocker",
            "rationale": (
                "No provider decision observed; audit not run. "
                "Missing credentials/trials are not evidence of market absence. "
                "Model-derived actionable price targets remain independent of this audit."
            ),
            "manual_sample_count": 0,
            "confirmed_manual_sample_count": 0,
            "the_odds_api_dwcs_events_found": 0,
        },
        "quota_fields_documented": False,
        "timestamp_fields_documented": False,
        "lock_fields_documented": False,
        "field_notes": {
            "timestamps": (
                "Static capability note: provider commence_time / market last_update are "
                "recorded only from live responses when the audit runs."
            ),
            "lock_events": (
                "Static capability note: lock/suspension support requires authenticated "
                "trial evidence; not inferred when the audit is not run."
            ),
            "quota": (
                "Static capability note: quota headers are recorded only from live "
                "provider responses when the audit runs."
            ),
            "historical_replay": (
                "Static product note: The Odds API documents historical snapshots on paid "
                "tiers; this artifact does not treat that documentation as capture evidence."
            ),
        },
        "redacted": True,
    }


def build_request_failed_artifact(
    *,
    failure_reason: str,
    sport: str,
    snapshot_label: str | None,
    regions: str | None,
    bookmaker_keys: Sequence[str],
    market_keys: Sequence[str],
    events_list_meta: Mapping[str, Any] | None = None,
    redact: bool = True,
    bet365_aliases: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Artifact for top-level /events HTTP, timeout, or payload failures."""
    safe_reason = sanitize_provider_error(failure_reason)
    aliases = normalize_bookmaker_aliases(bet365_aliases)
    scope = build_bet365_observation_scope(
        provider="the_odds_api",
        regions=regions,
        bookmaker_keys=bookmaker_keys,
        aliases=aliases,
    )
    events_meta = dict(events_list_meta or {})
    events_list = {
        "headers": {
            str(key).lower(): str(value)
            for key, value in dict(events_meta.get("headers") or {}).items()
            if str(key).lower() in QUOTA_HEADER_KEYS
            or str(key).lower().startswith("x-requests")
        },
        "schema_keys": [str(key) for key in list(events_meta.get("schema_keys") or [])],
    }
    providers = {
        "the_odds_api": {
            "status": "request_failed",
            "dwcs_events_found": 0,
            "bet365_present_on_dwcs": None,
            "bet365_query_status": "request_failed",
            "bet365_dwcs_status": "request_failed",
            "bet365_observation_scope": scope,
            "bet365_aliases": aliases,
            "markets_observed": [],
            "dwcs_market_discovery_status": "not_run",
            "quota": dict(events_list["headers"]),
            "timestamps_documented": False,
            "lock_events_supported": None,
            "historical_replay_supported": None,
            "rights_notes": None,
            "monthly_quote_usd": None,
            "regions": regions,
            "schema_keys": [],
            "events_list_schema_keys": list(events_list["schema_keys"]),
            "error": safe_reason,
        }
    }
    artifact: dict[str, Any] = {
        "ticket": "DWCS-000",
        "run_status": "request_failed",
        "status": "request_failed",
        "failure_reason": safe_reason,
        "provider": "the_odds_api",
        "sport": sport,
        "captured_at": None,
        "snapshot_label": snapshot_label,
        "regions": regions,
        "bookmaker_keys": list(bookmaker_keys),
        "bet365_aliases": aliases,
        "market_keys": list(market_keys),
        "bout_classifications": [],
        "events": [],
        "events_list": events_list,
        "providers": providers,
        "manual_bet365_samples": [],
        "pass_fail_matrix": _unevaluated_matrix(
            evidence_note="Top-level /events request failed; markets unevaluated"
        ),
        "decision": {
            "observed": False,
            "bet365_dwcs_status": "request_failed",
            "path": "hard_blocker",
            "rationale": (
                "No automatic bookmaker or reference moneyline evidence; "
                "top-level event-list request failed and is distinct from market absence. "
                "Line enrichment remains blocked, but model-derived actionable price "
                "targets are independent."
            ),
            "manual_sample_count": 0,
            "confirmed_manual_sample_count": 0,
            "the_odds_api_dwcs_events_found": 0,
            "bet365_observation_scope": scope,
        },
        "quota_fields_documented": bool(events_list["headers"]),
        "timestamp_fields_documented": False,
        "lock_fields_documented": False,
        "field_notes": {
            "timestamps": (
                "No event timestamps observed because /events request failed."
            ),
            "lock_events": (
                "Lock/suspension support was not evaluated; request failure is not absence."
            ),
            "quota": (
                "Quota headers are recorded only when present on the failed/partial response."
            ),
            "historical_replay": (
                "Historical replay was not evaluated from this failed capture."
            ),
            "bet365_scope": (
                "Bet365 observations record effective query mode. When bookmakers is "
                "sent with regions, bookmakers takes precedence and regions do not "
                "themselves scope the bookmaker probe. Request failure is not scoped "
                "absence. Catalog notes for bet365_au are interpretation context only."
            ),
        },
        "redacted": redact,
    }
    if redact:
        return redact_summary(artifact)
    return artifact


def build_pass_fail_matrix(evidence: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Build the DWCS-000 pass/fail matrix from captured evidence only."""
    providers = evidence.get("providers", {}) or {}
    odds = providers.get("the_odds_api", {}) or {}
    markets = {str(m).lower() for m in (odds.get("markets_observed") or [])}
    discovery_status = odds.get("dwcs_market_discovery_status")

    moneyline, moneyline_ev = _market_matrix_status(
        "h2h", markets_observed=markets, discovery_status=discovery_status
    )
    totals_status, totals_ev = _market_matrix_status(
        "totals", markets_observed=markets, discovery_status=discovery_status
    )
    method_status, method_ev = _market_matrix_status(
        "method", markets_observed=markets, discovery_status=discovery_status
    )
    round_status, round_ev = _market_matrix_status(
        "round", markets_observed=markets, discovery_status=discovery_status
    )

    lock_flag = odds.get("lock_events_supported")
    if lock_flag is True:
        lock_status: MatrixStatus = "pass"
        lock_ev = "Capture/trial evidence reports lock-event support on DWCS"
    elif lock_flag is False:
        lock_status = "fail"
        lock_ev = "Capture/trial evidence reports no lock-event support on DWCS"
    else:
        lock_status = "unknown"
        lock_ev = "Lock-event support not evaluated from capture/trial evidence"

    hist = odds.get("historical_replay_supported")
    if hist is True:
        hist_status: MatrixStatus = "pass"
        hist_ev = "Capture/trial evidence reports historical replay support"
    elif hist is False:
        hist_status = "fail"
        hist_ev = "Capture/trial evidence reports historical replay unavailable"
    else:
        hist_status = "unknown"
        hist_ev = "Historical replay not evaluated from capture/trial evidence"

    rights_raw = odds.get("rights_notes")
    rights_notes = str(rights_raw).strip() if rights_raw not in (None, "") else ""
    if rights_notes:
        lowered = rights_notes.lower()
        if "blocked" in lowered or ("bet365" in lowered and "not" in lowered):
            rights_status: MatrixStatus = "blocked"
            rights_ev = rights_notes
        else:
            rights_status = "pass"
            rights_ev = rights_notes
    else:
        rights_status = "unknown"
        rights_ev = "No capture/trial rights notes recorded"

    quote = odds.get("monthly_quote_usd")
    if isinstance(quote, (int, float)):
        quote_status: MatrixStatus = "pass"
        quote_ev = f"Captured/trial monthly quote recorded as ${quote}"
    else:
        quote_status = "unknown"
        quote_ev = "Monthly quote not recorded from capture/trial evidence"

    # Optional trial vendors can upgrade cells only with explicit evidence.
    for vendor_name, vendor in providers.items():
        if vendor_name == "the_odds_api" or not isinstance(vendor, Mapping):
            continue
        if vendor.get("status") in {"not_configured", "missing_credentials"}:
            continue
        if vendor.get("bet365_present_on_dwcs") is True:
            if vendor.get("rights_notes"):
                rights_status = "pass"
                rights_ev = f"{vendor_name}: {vendor.get('rights_notes')}"
            if vendor.get("lock_events_supported") is True:
                lock_status = "pass"
                lock_ev = f"{vendor_name}: lock events supported on DWCS"
            elif vendor.get("lock_events_supported") is False:
                lock_status = "fail"
                lock_ev = f"{vendor_name}: lock events not supported on DWCS"

    return {
        "moneyline": _matrix_row(moneyline, moneyline_ev),
        "totals": _matrix_row(totals_status, totals_ev),
        "method": _matrix_row(method_status, method_ev),
        "round": _matrix_row(round_status, round_ev),
        "lock_events": _matrix_row(lock_status, lock_ev),
        "historical_replay": _matrix_row(hist_status, hist_ev),
        "rights": _matrix_row(rights_status, rights_ev),
        "monthly_quote": _matrix_row(quote_status, quote_ev),
    }


def decide_provider_path(
    evidence: Mapping[str, Any],
    matrix: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Apply the plan decision gate using evidence only (no catalog inference)."""
    providers = evidence.get("providers", {}) or {}
    odds = providers.get("the_odds_api", {}) or {}
    manual_samples = evidence.get("manual_bet365_samples") or []

    bet365_flags: list[tuple[str, str]] = []
    for name, provider in providers.items():
        if not isinstance(provider, Mapping):
            continue
        if provider.get("status") in {"not_configured", "missing_credentials"}:
            # Missing credentials/trials are never observed absence.
            continue
        explicit = provider.get("bet365_dwcs_status")
        if explicit in {"present", "scoped_absent", "request_failed", "unresolved"}:
            bet365_flags.append((name, str(explicit)))
            continue
        flag = provider.get("bet365_present_on_dwcs")
        if flag is True:
            bet365_flags.append((name, "present"))
        elif flag is False:
            # False means scoped absence for that provider observation only.
            bet365_flags.append((name, "scoped_absent"))
        elif provider.get("status") == "request_failed" or (
            flag is None and provider.get("bet365_query_status") == "request_failed"
        ):
            bet365_flags.append((name, "request_failed"))

    if any(status == "present" for _, status in bet365_flags):
        bet365_status: Bet365DwcsStatus = "present"
    elif any(status == "request_failed" for _, status in bet365_flags):
        # Request failure is never collapsed into absence.
        bet365_status = "request_failed"
    elif any(status == "scoped_absent" for _, status in bet365_flags):
        bet365_status = "scoped_absent"
    else:
        bet365_status = "unresolved"

    confirmed_manual = [
        sample
        for sample in manual_samples
        if isinstance(sample, Mapping) and sample.get("matches_provider") is True
    ]
    if confirmed_manual:
        bet365_status = "present"

    path: DecisionPath
    rationale: str
    observed = bool(providers) or bool(manual_samples)
    odds_scope = odds.get("bet365_observation_scope") if isinstance(odds, Mapping) else None
    scope_note = ""
    if isinstance(odds_scope, Mapping):
        scope_note = (
            f" Observation scope: provider={odds_scope.get('provider')}, "
            f"effective_query_mode={odds_scope.get('effective_query_mode')}, "
            f"bookmaker_keys_queried={odds_scope.get('bookmaker_keys_queried')}, "
            f"regions_requested={odds_scope.get('regions_requested')}, "
            f"regions_effective_for_bookmaker_probe="
            f"{odds_scope.get('regions_effective_for_bookmaker_probe')}."
        )

    if bet365_status == "present" and matrix.get("rights", {}).get("status") == "pass":
        path = "licensed_bet365_primary"
        rationale = (
            "Evidence-backed Bet365×DWCS coverage with acceptable rights notes; "
            "automatic line enrichment is available alongside actionable price targets."
            + scope_note
        )
    elif matrix.get("moneyline", {}).get("status") == "pass":
        path = "the_odds_api_reference_fallback"
        if bet365_status == "scoped_absent":
            rationale = (
                "Bet365×DWCS was not observed for the queried bookmaker keys on The "
                "Odds API; this is scoped absence only (not universal Bet365 absence). "
                "When bookmakers and regions are both sent, bookmakers takes precedence, "
                "so region selection does not itself scope the bookmaker probe. Catalog "
                "notes that bet365_au is paid-tier and currently limited to AFL/NRL may "
                "explain MMA non-observation and must not be treated as sportsbook-wide "
                "DWCS absence. The Odds API provided observed DWCS h2h reference "
                "moneyline; core v1 uses sportsbook-agnostic actionable price targets "
                "when an automatic book line is unavailable."
                + scope_note
            )
        else:
            rationale = (
                "Bet365×DWCS not evidence-backed as present; The Odds API provided "
                "observed DWCS h2h reference moneyline; core v1 uses sportsbook-agnostic "
                "actionable price targets when an automatic book line is unavailable."
                + scope_note
            )
    else:
        path = "hard_blocker"
        rationale = (
            "No automatic bookmaker or reference moneyline evidence; line enrichment "
            "remains blocked, but model-derived actionable price targets are independent."
            + scope_note
        )

    return {
        "observed": observed,
        "bet365_dwcs_status": bet365_status,
        "path": path,
        "rationale": rationale,
        "manual_sample_count": len(manual_samples),
        "confirmed_manual_sample_count": len(confirmed_manual),
        "the_odds_api_dwcs_events_found": odds.get("dwcs_events_found", 0),
        "bet365_observation_scope": odds_scope,
    }


def _sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in QUOTA_HEADER_KEYS or key.lower().startswith("x-requests")
    }


def _schema_keys(payload: Any, *, limit: int = 40) -> list[str]:
    if isinstance(payload, Mapping):
        keys = sorted(str(key) for key in payload.keys())
        return keys[:limit]
    if isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
        keys = sorted({str(key) for item in payload for key in item.keys()})
        return keys[:limit]
    return []


def build_coverage_summary(
    *,
    sport: str,
    provider: str,
    captured_at: str,
    snapshot_label: str,
    official_bouts: Sequence[Mapping[str, Any]],
    provider_events: Sequence[Mapping[str, Any]],
    markets_by_event: Mapping[str, Mapping[str, Any]],
    regions: str,
    bookmaker_keys: Sequence[str],
    market_keys: Sequence[str],
    manual_bet365_samples: Sequence[Mapping[str, Any]],
    vendor_notes: Mapping[str, Any],
    redact: bool,
    events_list_meta: Mapping[str, Any] | None = None,
    bet365_aliases: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Assemble the DWCS-000 coverage summary object."""
    aliases = normalize_bookmaker_aliases(bet365_aliases)
    validated_samples = validate_manual_bet365_samples(manual_bet365_samples)
    classifications = classify_official_bouts(official_bouts, provider_events)
    present_event_ids = {
        row["matched_event_id"]
        for row in classifications
        if row["status"] == "present" and row.get("matched_event_id")
    }

    events_meta = dict(events_list_meta or {})
    events_list_headers = {
        str(key).lower(): str(value)
        for key, value in dict(events_meta.get("headers") or {}).items()
    }
    events_list_schema = [str(key) for key in list(events_meta.get("schema_keys") or [])]
    events_list = {
        "headers": {
            key: events_list_headers[key]
            for key in QUOTA_HEADER_KEYS
            if key in events_list_headers
        },
        "schema_keys": events_list_schema,
    }

    per_event_presence: dict[str, Any] = {}
    observed_markets: set[str] = set()
    quota: dict[str, str] = dict(events_list["headers"])
    market_schema_keys: list[str] = []
    dwcs_discovery_statuses: list[str] = []
    alias_status_rows: list[list[PresenceStatus | None]] = []

    for event_id, discovery in markets_by_event.items():
        presence = bookmaker_market_presence(
            discovery,
            bookmaker_keys=bookmaker_keys,
            market_keys=market_keys,
        )
        market_timestamps = extract_market_timestamp_evidence(discovery)
        per_event_presence[event_id] = {
            "status": discovery.get("status", "ok"),
            "error": discovery.get("error"),
            "presence": presence,
            "market_timestamps": market_timestamps,
            "schema_keys": list(discovery.get("schema_keys") or []),
            "headers": dict(discovery.get("headers") or {}),
        }
        is_dwcs = str(event_id) in {str(value) for value in present_event_ids}
        if is_dwcs:
            dwcs_discovery_statuses.append(str(discovery.get("status", "ok")))
            queried_aliases = queried_bet365_keys(bookmaker_keys, aliases=aliases)
            alias_status_rows.append(
                resolve_bet365_alias_statuses(
                    presence,
                    aliases=queried_aliases,
                    market_key="h2h",
                )
            )
            if discovery.get("status") != "request_failed":
                for markets in presence.values():
                    for market, status in markets.items():
                        if status == "present":
                            observed_markets.add(market)
        headers = discovery.get("headers") or {}
        for key in QUOTA_HEADER_KEYS:
            if key in headers and key not in quota:
                quota[key] = str(headers[key])
        if not market_schema_keys and discovery.get("schema_keys"):
            market_schema_keys = list(discovery.get("schema_keys") or [])

    if not dwcs_discovery_statuses:
        dwcs_market_discovery_status = "not_run"
    elif any(status == "ok" for status in dwcs_discovery_statuses):
        dwcs_market_discovery_status = "ok"
    elif all(status == "request_failed" for status in dwcs_discovery_statuses):
        dwcs_market_discovery_status = "request_failed"
    else:
        dwcs_market_discovery_status = "not_run"

    # Bet365 on DWCS only from successful reconciled-event discovery, via aliases.
    bet365_observation = summarize_scoped_bet365_observation(
        alias_status_rows,
        provider=provider,
        regions=regions,
        bookmaker_keys=bookmaker_keys,
        aliases=aliases,
    )

    timestamp_fields_documented = any(
        bool(event.get("commence_time")) for event in provider_events
    ) or any(
        bool(row.get("last_update"))
        for discovery in per_event_presence.values()
        for row in discovery.get("market_timestamps") or []
    )
    lock_fields_documented = any(
        isinstance(note, Mapping) and note.get("lock_events_supported") is not None
        for note in vendor_notes.values()
    )

    providers: dict[str, Any] = {
        "the_odds_api": {
            "dwcs_events_found": len(present_event_ids),
            "bet365_present_on_dwcs": bet365_observation["bet365_present_on_dwcs"],
            "bet365_query_status": bet365_observation["bet365_query_status"],
            "bet365_dwcs_status": bet365_observation["bet365_dwcs_status"],
            "bet365_observation_scope": bet365_observation["bet365_observation_scope"],
            "bet365_aliases": aliases,
            "markets_observed": sorted(observed_markets),
            "dwcs_market_discovery_status": dwcs_market_discovery_status,
            "quota": quota,
            "timestamps_documented": timestamp_fields_documented,
            "lock_events_supported": None,
            "historical_replay_supported": None,
            "rights_notes": None,
            "monthly_quote_usd": None,
            "regions": regions,
            "schema_keys": market_schema_keys,
            "events_list_schema_keys": events_list_schema,
        }
    }
    for vendor, note in vendor_notes.items():
        if vendor == "the_odds_api":
            if isinstance(note, Mapping):
                # Allow explicit captured overrides only when provided by caller.
                for key in (
                    "historical_replay_supported",
                    "monthly_quote_usd",
                    "rights_notes",
                    "lock_events_supported",
                ):
                    if key in note:
                        providers["the_odds_api"][key] = note[key]
            continue
        if isinstance(note, Mapping):
            row = dict(note)
            row.setdefault("status", "not_configured")
            if row.get("status") in {"not_configured", "missing_credentials"}:
                row["bet365_present_on_dwcs"] = None
            providers[vendor] = row
        else:
            providers[vendor] = {
                "status": "not_configured",
                "notes": str(note),
                "bet365_present_on_dwcs": None,
            }

    evidence = {
        "providers": providers,
        "manual_bet365_samples": validated_samples,
        "bout_classifications": classifications,
    }
    matrix = build_pass_fail_matrix(evidence)
    decision = decide_provider_path(evidence, matrix)

    events_out = []
    for event in provider_events:
        event_id = str(event.get("id"))
        events_out.append(
            {
                "id": event_id,
                "home_team": event.get("home_team"),
                "away_team": event.get("away_team"),
                "commence_time": event.get("commence_time"),
                "is_reconciled_dwcs": event_id in present_event_ids,
                "market_discovery": per_event_presence.get(event_id),
            }
        )

    summary: dict[str, Any] = {
        "ticket": "DWCS-000",
        "run_status": "captured",
        "status": "ok",
        "provider": provider,
        "sport": sport,
        "captured_at": captured_at,
        "snapshot_label": snapshot_label,
        "regions": regions,
        "bookmaker_keys": list(bookmaker_keys),
        "bet365_aliases": aliases,
        "market_keys": list(market_keys),
        "bout_classifications": classifications,
        "events": events_out,
        "events_list": events_list,
        "providers": providers,
        "manual_bet365_samples": validated_samples,
        "pass_fail_matrix": matrix,
        "decision": decision,
        "quota_fields_documented": bool(quota),
        "timestamp_fields_documented": timestamp_fields_documented,
        "lock_fields_documented": lock_fields_documented,
        "field_notes": {
            "timestamps": (
                "Provider event commence_time and sanitized per-market last_update are "
                "recorded only when present on captured responses; prices/outcomes are "
                "never retained under --redact."
            ),
            "lock_events": (
                "Static capability note: Bet365 streaming lock events require authenticated "
                "trial evidence; never inferred from missing credentials."
            ),
            "quota": (
                "Quota captured from /events and/or event-market response headers "
                "x-requests-remaining / x-requests-used / x-requests-last when present."
            ),
            "historical_replay": (
                "Static product note: historical snapshots may exist on paid tiers; matrix "
                "cells stay unknown until capture/trial evidence sets the flag."
            ),
            "bet365_scope": (
                "Bet365 observations record provider, configured aliases/keys "
                "(including bet365_au), and effective query mode. When bookmakers is "
                "sent with regions, bookmakers takes precedence and regions do not "
                "themselves scope the bookmaker probe. Scoped absence is never "
                "universal Bet365 absence. Catalog notes that bet365_au is paid-tier "
                "and currently limited to AFL/NRL are interpretation context only; the "
                "live event query remains the only event-specific evidence."
            ),
        },
        "redacted": redact,
    }
    if redact:
        return redact_summary(summary)
    return summary


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _extract_quota_headers(response: httpx.Response) -> dict[str, str]:
    return _sanitize_headers({k: v for k, v in response.headers.items()})


def fetch_provider_events(
    client: httpx.Client,
    *,
    api_key: str,
    sport: str,
) -> dict[str, Any]:
    """Fetch /events without raising; never include the raw API key in errors."""
    url = f"{THE_ODDS_API_BASE}/sports/{sport}/events"
    try:
        response = client.get(url, params={"apiKey": api_key, "dateFormat": "iso"})
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        return {
            "status": "request_failed",
            "events": [],
            "headers": {},
            "schema_keys": [],
            "error": sanitize_provider_error(str(exc), api_key=api_key),
        }
    except ValueError as exc:
        return {
            "status": "request_failed",
            "events": [],
            "headers": {},
            "schema_keys": [],
            "error": sanitize_provider_error(
                f"Invalid /events JSON payload: {exc}",
                api_key=api_key,
            ),
        }

    headers = _extract_quota_headers(response)
    if not isinstance(payload, list):
        return {
            "status": "request_failed",
            "events": [],
            "headers": headers,
            "schema_keys": _schema_keys(payload),
            "error": "Unexpected /events payload; expected a JSON list",
        }
    return {
        "status": "ok",
        "events": [dict(item) for item in payload],
        "headers": headers,
        "schema_keys": _schema_keys(payload),
        "error": None,
    }


def fetch_event_markets(
    client: httpx.Client,
    *,
    api_key: str,
    sport: str,
    event_id: str,
    regions: str,
    bookmakers: Sequence[str] | None = None,
) -> dict[str, Any]:
    url = f"{THE_ODDS_API_BASE}/sports/{sport}/events/{event_id}/markets"
    params: dict[str, str] = {
        "apiKey": api_key,
        "regions": regions,
        "dateFormat": "iso",
    }
    if bookmakers:
        params["bookmakers"] = ",".join(bookmakers)
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        return {
            "status": "request_failed",
            "error": sanitize_provider_error(str(exc), api_key=api_key),
            "bookmakers": [],
            "headers": {},
            "schema_keys": [],
        }
    except ValueError as exc:
        return {
            "status": "request_failed",
            "error": sanitize_provider_error(
                f"Invalid event markets JSON payload: {exc}",
                api_key=api_key,
            ),
            "bookmakers": [],
            "headers": {},
            "schema_keys": [],
        }
    bookmaker_rows = []
    if isinstance(payload, Mapping):
        bookmaker_rows = list(payload.get("bookmakers") or [])
        schema = _schema_keys(payload)
    else:
        schema = []
    return {
        "status": "ok",
        "bookmakers": bookmaker_rows,
        "headers": _extract_quota_headers(response),
        "schema_keys": schema,
    }


def run_audit(
    *,
    api_key: str,
    sport: str,
    regions: str,
    official_bouts: Sequence[Mapping[str, Any]],
    snapshot_label: str,
    bookmaker_keys: Sequence[str],
    market_keys: Sequence[str],
    manual_bet365_samples: Sequence[Mapping[str, Any]],
    vendor_notes: Mapping[str, Any],
    redact: bool,
    max_events_for_markets: int,
    client: httpx.Client | None = None,
    bet365_aliases: Sequence[str] | None = None,
) -> dict[str, Any]:
    captured_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    owns_client = client is None
    active_client = client if client is not None else httpx.Client(timeout=60.0)
    aliases = normalize_bookmaker_aliases(bet365_aliases)
    try:
        events_result = fetch_provider_events(active_client, api_key=api_key, sport=sport)
        if events_result.get("status") != "ok":
            return build_request_failed_artifact(
                failure_reason=str(events_result.get("error") or "events request failed"),
                sport=sport,
                snapshot_label=snapshot_label,
                regions=regions,
                bookmaker_keys=bookmaker_keys,
                market_keys=market_keys,
                events_list_meta={
                    "headers": events_result.get("headers") or {},
                    "schema_keys": events_result.get("schema_keys") or [],
                },
                redact=redact,
                bet365_aliases=aliases,
            )

        events = list(events_result.get("events") or [])
        events_headers = dict(events_result.get("headers") or {})
        events_schema = list(events_result.get("schema_keys") or [])

        # Prefer market discovery on reconciled DWCS candidates first.
        classifications = classify_official_bouts(official_bouts, events)
        preferred_ids = [
            str(row["matched_event_id"])
            for row in classifications
            if row.get("matched_event_id")
        ]
        remaining_ids = [
            str(event["id"])
            for event in events
            if str(event.get("id")) not in set(preferred_ids)
        ]
        ordered_ids = preferred_ids + remaining_ids
        markets_by_event: dict[str, dict[str, Any]] = {}
        for event_id in ordered_ids[: max(0, max_events_for_markets)]:
            markets_by_event[event_id] = fetch_event_markets(
                active_client,
                api_key=api_key,
                sport=sport,
                event_id=event_id,
                regions=regions,
                bookmakers=bookmaker_keys,
            )
    finally:
        if owns_client:
            active_client.close()

    return build_coverage_summary(
        sport=sport,
        provider="the_odds_api",
        captured_at=captured_at,
        snapshot_label=snapshot_label,
        official_bouts=official_bouts,
        provider_events=events,
        markets_by_event=markets_by_event,
        regions=regions,
        bookmaker_keys=bookmaker_keys,
        market_keys=market_keys,
        manual_bet365_samples=manual_bet365_samples,
        vendor_notes=vendor_notes,
        redact=redact,
        events_list_meta={"headers": events_headers, "schema_keys": events_schema},
        bet365_aliases=aliases,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", default=DEFAULT_SPORT)
    parser.add_argument("--regions", default=DEFAULT_REGIONS)
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Strip API keys and prices from the written summary (required for committed artifacts)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output/research/odds-coverage-summary.json"),
    )
    parser.add_argument(
        "--official-bouts",
        type=Path,
        required=True,
        help="JSON list of official bouts: bout_id, fighter_a, fighter_b, scheduled_start",
    )
    parser.add_argument(
        "--snapshot-label",
        default="ad-hoc",
        help="Label for capture timing, e.g. T-24h, T-6h, T-1h, T-10m",
    )
    parser.add_argument(
        "--bookmakers",
        default=",".join(DEFAULT_BOOKMAKERS),
        help="Comma-separated bookmaker keys to score for presence",
    )
    parser.add_argument(
        "--bet365-aliases",
        default=",".join(DEFAULT_BET365_ALIASES),
        help=(
            "Comma-separated The Odds API bookmaker keys treated as Bet365 "
            "(e.g. bet365,bet365_au). When --bookmakers is set, The Odds API gives "
            "that parameter precedence over --regions for the bookmaker probe."
        ),
    )
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_MARKETS),
        help="Comma-separated market keys to score for presence",
    )
    parser.add_argument(
        "--manual-bet365-samples",
        type=Path,
        default=None,
        help=(
            "Optional JSON list of manually sampled Bet365 displays "
            f"(no credentials; at most {MAX_MANUAL_BET365_SAMPLES})"
        ),
    )
    parser.add_argument(
        "--vendor-notes",
        type=Path,
        default=None,
        help="Optional JSON object of trial-vendor notes/status blocks",
    )
    parser.add_argument(
        "--max-events-for-markets",
        type=int,
        default=8,
        help="Cap event-market discovery calls to protect quota",
    )
    parser.add_argument(
        "--api-key-env",
        default="ODDS_API_KEY",
        help="Environment variable holding The Odds API key",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    official_bouts = _load_json(args.official_bouts)
    if not isinstance(official_bouts, list):
        print("--official-bouts must be a JSON list", file=sys.stderr)
        return 2

    manual_samples: list[dict[str, Any]] = []
    if args.manual_bet365_samples:
        loaded = _load_json(args.manual_bet365_samples)
        if not isinstance(loaded, list):
            print("--manual-bet365-samples must be a JSON list", file=sys.stderr)
            return 2
        try:
            manual_samples = validate_manual_bet365_samples(
                [dict(item) for item in loaded]
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    vendor_notes: dict[str, Any] = {
        "opticodds": {"status": "not_configured", "bet365_present_on_dwcs": None},
        "sportsgameodds": {"status": "not_configured", "bet365_present_on_dwcs": None},
        "sportsdataio": {"status": "not_configured", "bet365_present_on_dwcs": None},
    }
    if args.vendor_notes:
        loaded_notes = _load_json(args.vendor_notes)
        if not isinstance(loaded_notes, Mapping):
            print("--vendor-notes must be a JSON object", file=sys.stderr)
            return 2
        vendor_notes.update(dict(loaded_notes))

    bookmaker_keys = [part.strip() for part in args.bookmakers.split(",") if part.strip()]
    market_keys = [part.strip() for part in args.markets.split(",") if part.strip()]
    bet365_aliases = [
        part.strip() for part in args.bet365_aliases.split(",") if part.strip()
    ]

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        api_key = get_settings().odds_api_key.strip()
    if not api_key:
        summary = build_not_run_artifact(
            block_reason=missing_api_key_reason(args.api_key_env)
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(
            f"Missing API key. Wrote blocked not-run artifact to {args.out}",
            file=sys.stderr,
        )
        return 2

    summary = run_audit(
        api_key=api_key,
        sport=args.sport,
        regions=args.regions,
        official_bouts=official_bouts,
        snapshot_label=args.snapshot_label,
        bookmaker_keys=bookmaker_keys,
        market_keys=market_keys,
        manual_bet365_samples=manual_samples,
        vendor_notes=vendor_notes,
        redact=bool(args.redact),
        max_events_for_markets=args.max_events_for_markets,
        bet365_aliases=bet365_aliases,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")

    if summary.get("run_status") == "request_failed":
        failure_reason = sanitize_provider_error(
            str(summary.get("failure_reason") or "events request failed"),
            api_key=api_key,
        )
        print(
            f"Events request failed. Wrote failure artifact to {args.out}: {failure_reason}",
            file=sys.stderr,
        )
        return 1

    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
