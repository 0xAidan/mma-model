"""Typed ESPN public-JSON failures (schema only; HTTP blocks use SourceBlockedError)."""

from __future__ import annotations


class EspnSchemaError(ValueError):
    """Raised when ESPN JSON is present but not the expected scoreboard shape."""
