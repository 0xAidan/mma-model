"""Frozen publication-proxy rule for historical immutable bout facts (DWCS-102)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class PitProxyError(ValueError):
    """Raised when the PIT proxy contract is missing, mutable, or drifted."""


class PitProxyRule(BaseModel):
    """Immutable publication-proxy ceiling; never gold, never mutable profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_version: str
    delay_iso8601: str
    applies_to: tuple[str, ...]
    forbidden_for: tuple[str, ...]
    max_quality_tier_when_proxy: Literal["silver"]

    @model_validator(mode="after")
    def _validate_rule(self) -> PitProxyRule:
        if self.max_quality_tier_when_proxy != "silver":
            raise PitProxyError(
                "max_quality_tier_when_proxy must be silver "
                f"(got {self.max_quality_tier_when_proxy!r})"
            )
        if "mutable_profile_aggregate" not in self.forbidden_for:
            raise PitProxyError(
                "forbidden_for must include mutable_profile_aggregate"
            )
        if self.delay_iso8601 != "P1D":
            raise PitProxyError(
                f"delay_iso8601 must be P1D (got {self.delay_iso8601!r})"
            )
        if not self.applies_to:
            raise PitProxyError("applies_to must be non-empty")
        return self

    def assert_allowed_for(self, fact_kind: str) -> None:
        if fact_kind in self.forbidden_for:
            raise PitProxyError(
                f"publication proxy forbidden_for={fact_kind!r}"
            )
        if fact_kind not in self.applies_to:
            raise PitProxyError(
                f"publication proxy does not apply_to={fact_kind!r}"
            )

    def assert_quality_tier_allowed(self, quality_tier: str) -> None:
        if quality_tier == "gold":
            raise PitProxyError("proxy cannot be gold")
        if quality_tier != self.max_quality_tier_when_proxy:
            raise PitProxyError(
                f"proxy quality_tier must be {self.max_quality_tier_when_proxy!r} "
                f"(got {quality_tier!r})"
            )


def default_pit_proxy_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "sources" / "pit_proxy_v1.json"


def load_pit_proxy_rule(path: Path | None = None) -> PitProxyRule:
    """Load the pinned PIT proxy JSON and fail closed on drift."""
    proxy_path = path or default_pit_proxy_path()
    try:
        raw = json.loads(proxy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PitProxyError(f"invalid pit proxy JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PitProxyError("pit proxy root must be an object")
    try:
        return PitProxyRule.model_validate(raw)
    except PitProxyError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise PitProxyError(str(exc)) from exc
