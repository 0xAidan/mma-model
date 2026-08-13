"""Canonical sportsbook key helpers (DWCS-202).

Bet365 identity uses an explicit alias set — never broad prefix matching.
"""

from __future__ import annotations

from typing import Final

# Phase 0 / The Odds API aliases treated as Bet365 identity.
BET365_BOOKMAKER_ALIASES: Final[frozenset[str]] = frozenset({"bet365", "bet365_au"})


def normalize_bookmaker_key(bookmaker_key: str) -> str:
    """Lowercase trimmed bookmaker key for comparisons."""
    return str(bookmaker_key).strip().lower()


def is_bet365_bookmaker_key(bookmaker_key: str) -> bool:
    """True only for the explicit Bet365 alias set."""
    return normalize_bookmaker_key(bookmaker_key) in BET365_BOOKMAKER_ALIASES
