"""Dashboard publish contract constants (DWCS-500)."""

from __future__ import annotations

from typing import Final

DASHBOARD_SCHEMA_VERSION: Final[int] = 1
DASHBOARD_CONTRACT_ID: Final[str] = "dwcs_dashboard"
DASHBOARD_CONTRACT_VERSION: Final[str] = "1.0.0"
DASHBOARD_TICKET: Final[str] = "DWCS-500"

RELEASE_JSON: Final[str] = "release.json"
MANIFEST_JSON: Final[str] = "manifest.json"
CURRENT_EVENT_JSON: Final[str] = "current-event.json"
MATCHUPS_JSON: Final[str] = "matchups.json"
PERFORMANCE_JSON: Final[str] = "performance.json"
HISTORY_JSON: Final[str] = "history.json"
HEALTH_JSON: Final[str] = "health.json"

DASHBOARD_RELEASE_FILES: Final[tuple[str, ...]] = (
    RELEASE_JSON,
    MANIFEST_JSON,
    CURRENT_EVENT_JSON,
    MATCHUPS_JSON,
    PERFORMANCE_JSON,
    HISTORY_JSON,
    HEALTH_JSON,
)

# Map DWCS-403 health component names → dashboard health projection names.
# Do not rewrite HEALTH_COMPONENT_NAMES; this is a published view only.
HEALTH_COMPONENT_MAP: Final[dict[str, str]] = {
    "sources": "data",
    "identity": "identity",
    "odds": "odds",
    "model": "model",
    "publish": "pipeline",
    "grade": "grading",
    "backup": "backup",
    "quota": "quota",
    "staleness": "freshness",
}

DASHBOARD_HEALTH_NAMES: Final[tuple[str, ...]] = (
    "pipeline",
    "data",
    "identity",
    "odds",
    "model",
    "grading",
    "backup",
    "quota",
    "freshness",
)

# Named set required by the plan; quota/freshness may remain as extras.
REQUIRED_DASHBOARD_HEALTH: Final[frozenset[str]] = frozenset(
    {
        "pipeline",
        "data",
        "identity",
        "odds",
        "model",
        "grading",
        "backup",
    }
)

SECRET_SCAN_PATTERNS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "api-key",
    "bearer ",
    "authorization:",
    "the_odds_api_key",
    "odds_api_key",
    "raw_payload",
    "licensed_raw",
    "provider_payload",
    "x-api-key",
)


__all__ = [
    "CURRENT_EVENT_JSON",
    "DASHBOARD_CONTRACT_ID",
    "DASHBOARD_CONTRACT_VERSION",
    "DASHBOARD_HEALTH_NAMES",
    "DASHBOARD_RELEASE_FILES",
    "DASHBOARD_SCHEMA_VERSION",
    "DASHBOARD_TICKET",
    "HEALTH_COMPONENT_MAP",
    "HEALTH_JSON",
    "HISTORY_JSON",
    "MANIFEST_JSON",
    "MATCHUPS_JSON",
    "PERFORMANCE_JSON",
    "RELEASE_JSON",
    "REQUIRED_DASHBOARD_HEALTH",
    "SECRET_SCAN_PATTERNS",
]
