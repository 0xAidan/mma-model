"""Shared DWCS-105 history constants."""

from __future__ import annotations

RECONSTRUCTION_VERSION = "1"
PARSER_VERSION_TAPOLOGY = "tapology_public_parser@1"
PARSER_VERSION_SHERDOG = "sherdog_public_parser@1"
PARSER_VERSION_COMBAT_REGISTRY = "combat_registry_parser@1"

SOURCE_TAPOLOGY = "tapology_public"
SOURCE_SHERDOG = "sherdog_public"
SOURCE_COMBAT_REGISTRY = "combat_registry"
SOURCE_WIKIDATA = "wikidata"
SOURCE_SPORTSDATAIO = "sportsdataio"
SOURCE_BALLDONTLIE = "balldontlie"

REGIONAL_FALLBACK_ORDER = (
    SOURCE_TAPOLOGY,
    SOURCE_SHERDOG,
    SOURCE_COMBAT_REGISTRY,
)

# Result-fact precedence after ingest: commission/registry overrides, then
# Tapology breadth, then Sherdog secondary. Disagreements still emit conflicts.
RESULT_PRECEDENCE = (
    SOURCE_COMBAT_REGISTRY,
    SOURCE_TAPOLOGY,
    SOURCE_SHERDOG,
)

ENTITY_REGIONAL_BOUT = "regional_bout"
ENTITY_HISTORY_CONFLICT = "history_conflict"
ENTITY_SOURCE_FAILURE = "source_failure"
ENTITY_CURRENT_RECORD = "current_record"
ENTITY_EXPLICIT_PRE_FIGHT = "explicit_pre_fight_record"

MAX_PAGES_PER_RUN = 8
MAX_DEPTH = 2
MAX_BOUTS_PER_FIGHTER = 64

PROBE_PATHS = {
    SOURCE_TAPOLOGY: ("tapology.com", "/rankings/"),
    SOURCE_SHERDOG: ("sherdog.com", "/events/"),
    SOURCE_COMBAT_REGISTRY: ("combatreg.com", "/"),
}

LOGIN_WALL_MARKERS = (
    "data-access=\"login_required\"",
    "app.combatreg.com",
    "please sign in",
    "please log in",
    "login to continue",
    "sign in to continue",
)

PAYWALL_MARKERS = (
    "paywall",
    "subscribe to continue",
    "subscription required",
    "members only",
)

UNRESOLVED_IDENTITY_STATUSES = frozenset({"blocked", "queued", "unresolved"})

SOURCE_CLASS = {
    SOURCE_TAPOLOGY: "public extraction",
    SOURCE_SHERDOG: "public extraction",
    SOURCE_COMBAT_REGISTRY: "official record",
}

PARSER_VERSIONS = {
    SOURCE_TAPOLOGY: PARSER_VERSION_TAPOLOGY,
    SOURCE_SHERDOG: PARSER_VERSION_SHERDOG,
    SOURCE_COMBAT_REGISTRY: PARSER_VERSION_COMBAT_REGISTRY,
}
