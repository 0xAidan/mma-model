"""Deterministic canonical UUIDs for DWCS entities (never merge by name)."""

from __future__ import annotations

import uuid

# Stable namespace for DWCS canonical IDs (UUID5 over ESPN external IDs).
DWCS_UUID_NAMESPACE = uuid.UUID("6b897d0a-9b84-5863-8620-b9f1f75204a1")


def canonical_event_id(espn_event_id: str) -> str:
    return str(
        uuid.uuid5(DWCS_UUID_NAMESPACE, f"dwcs:event:espn:{espn_event_id.strip()}")
    )


def canonical_bout_id(espn_competition_id: str) -> str:
    return str(
        uuid.uuid5(
            DWCS_UUID_NAMESPACE, f"dwcs:bout:espn:{espn_competition_id.strip()}"
        )
    )


def canonical_fighter_id(espn_athlete_id: str) -> str:
    return str(
        uuid.uuid5(
            DWCS_UUID_NAMESPACE, f"dwcs:fighter:espn:{espn_athlete_id.strip()}"
        )
    )
