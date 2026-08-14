"""Shared identity resolver constants (DWCS-104)."""

from __future__ import annotations

RESOLVER_VERSION = "1"

RULE_EXACT_SOURCE_EXTERNAL_ID = "exact_source_external_id"
RULE_EXACT_WIKIDATA = "exact_wikidata"
RULE_NAME_DOB_UNIQUE = "exact_normalized_name_dob_unique"
RULE_NAME_CONTEXT_UNIQUE = "exact_normalized_name_opponent_event_date_unique"
RULE_QUEUE_SAME_NAME = "same_normalized_name_queue"
RULE_QUEUE_FUZZY = "fuzzy_or_alias_candidate_queue"
RULE_QUEUE_AMBIGUOUS = "ambiguous_identity_queue"
RULE_QUEUE_CONFLICT = "identity_conflict_queue"
RULE_CREATE_NEW = "create_canonical_for_new_external"
RULE_BLOCKED = "unresolved_blocks_scoring"

ALLOWED_RESOLVE_SOURCES = frozenset(
    {
        "espn",
        "espn_public",
        "ufcstats_public",
        "mma_ai_bootstrap",
        "tapology_public",
        "sherdog_public",
        "combat_registry",
        "wikidata",
        "dwcs_manifest",
        "bestfightodds_archive",
        "the_odds_api",
        "sportsdataio",
        "balldontlie",
        "explicit_missing",
    }
)
