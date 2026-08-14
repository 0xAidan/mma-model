"""Shared DWCS event-name filters (listing sources must not import jobs)."""

from __future__ import annotations

import re

_DWCS_NAME = re.compile(
    r"contender\s+series|dana\s+white.?s\s+contender|\bdwcs\b",
    re.IGNORECASE,
)
_BRAZIL = re.compile(r"\bbrazil\b", re.IGNORECASE)


def is_dwcs_event_name(name: str) -> bool:
    return bool(_DWCS_NAME.search(name or ""))


def series_for_event_name(name: str) -> str:
    if _BRAZIL.search(name or ""):
        return "dwcs_brazil"
    return "dwcs"


__all__ = ["is_dwcs_event_name", "series_for_event_name"]
