"""Per-host HTTP politeness contract (DWCS-102)."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class HttpPolitenessError(ValueError):
    """Raised when politeness config is missing, mutable, or drifted."""


REQUIRED_HOSTS: tuple[str, ...] = (
    "ufcstats.com",
    "tapology.com",
    "sherdog.com",
    "combatreg.com",
    "bestfightodds.com",
)
REQUIRED_STOP_STATUS_CODES: frozenset[int] = frozenset({403, 429, 503})


class HostPoliteness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_delay_sec: float
    max_concurrency: int
    max_retries: int
    backoff_base_sec: float
    backoff_cap_sec: float
    stop_status_codes: tuple[int, ...]
    allowed_path_prefixes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_host(self) -> HostPoliteness:
        if self.max_concurrency != 1:
            raise HttpPolitenessError(
                f"max_concurrency must be 1 (got {self.max_concurrency})"
            )
        if self.min_delay_sec < 0:
            raise HttpPolitenessError("min_delay_sec must be >= 0")
        if self.max_retries < 0:
            raise HttpPolitenessError("max_retries must be >= 0")
        if self.backoff_base_sec <= 0:
            raise HttpPolitenessError("backoff_base_sec must be > 0")
        if self.backoff_cap_sec < self.backoff_base_sec:
            raise HttpPolitenessError("backoff_cap_sec must be >= backoff_base_sec")
        missing = REQUIRED_STOP_STATUS_CODES - set(self.stop_status_codes)
        if missing:
            raise HttpPolitenessError(
                f"stop_status_codes missing required codes {sorted(missing)}"
            )
        return self


class HttpPolitenessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    contract_id: str = "http_politeness"
    contract_version: str = "1"
    user_agent: str
    contact: str
    hosts: Mapping[str, HostPoliteness]

    @field_validator("hosts", mode="before")
    @classmethod
    def _parse_hosts(cls, value: object) -> Mapping[str, HostPoliteness]:
        if not isinstance(value, Mapping):
            raise HttpPolitenessError("hosts must be a mapping")
        parsed = {
            str(host): HostPoliteness.model_validate(spec) for host, spec in value.items()
        }
        return MappingProxyType(parsed)

    @model_validator(mode="after")
    def _validate_config(self) -> HttpPolitenessConfig:
        if not self.user_agent.strip():
            raise HttpPolitenessError("user_agent is required")
        if not self.contact.strip():
            raise HttpPolitenessError("contact is required")
        missing = set(REQUIRED_HOSTS) - set(self.hosts.keys())
        if missing:
            raise HttpPolitenessError(f"hosts missing required entries: {sorted(missing)}")
        object.__setattr__(self, "hosts", MappingProxyType(dict(self.hosts)))
        return self


def default_http_politeness_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "config"
        / "sources"
        / "http_politeness_v1.json"
    )


def load_http_politeness(path: Path | None = None) -> HttpPolitenessConfig:
    """Load pinned HTTP politeness JSON and fail closed on drift."""
    polite_path = path or default_http_politeness_path()
    try:
        raw = json.loads(polite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HttpPolitenessError(f"invalid http politeness JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise HttpPolitenessError("http politeness root must be an object")
    try:
        return HttpPolitenessConfig.model_validate(raw)
    except HttpPolitenessError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        raise HttpPolitenessError(str(exc)) from exc
