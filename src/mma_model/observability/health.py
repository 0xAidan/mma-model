"""Operational health contract (DWCS-403).

Statuses: healthy, missing, stale, blocked, failed.
Rollup severity: green / yellow / red.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from mma_model.observability.logging import redact_secrets, redact_value
from mma_model.observability.schema import (
    HEALTH_SCHEMA_PATH,
    validate_health_payload,
)
from mma_model.quality.constants import EXIT_OK, EXIT_STRICT_BLOCKERS
from mma_model.quality.models import GateAssessment, GateResult

HEALTH_COMPONENT_NAMES: tuple[str, ...] = (
    "sources",
    "identity",
    "odds",
    "model",
    "publish",
    "grade",
    "backup",
    "quota",
    "staleness",
)

# Components whose ``missing`` status is a production blocker (red).
REQUIRED_COMPONENTS: frozenset[str] = frozenset(
    {
        "sources",
        "identity",
        "model",
        "publish",
        "grade",
    }
)

HEALTH_SCHEMA_VERSION = 1
HEALTH_CONTRACT_ID = "dwcs_health"
HEALTH_CONTRACT_VERSION = "1.0.0"
HEALTH_TICKET = "DWCS-403"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    MISSING = "missing"
    STALE = "stale"
    BLOCKED = "blocked"
    FAILED = "failed"


class HealthSeverity(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


def severity_for(
    status: HealthStatus | str,
    *,
    component: str | None = None,
    required: bool | None = None,
) -> HealthSeverity:
    """Map status → green/yellow/red.

    - healthy → green
    - stale / missing (non-blocking) → yellow
    - blocked / failed / missing required production input → red
    """
    status_val = HealthStatus(status) if not isinstance(status, HealthStatus) else status
    if status_val == HealthStatus.HEALTHY:
        return HealthSeverity.GREEN
    if status_val == HealthStatus.STALE:
        return HealthSeverity.YELLOW
    if status_val == HealthStatus.MISSING:
        is_required = (
            bool(required)
            if required is not None
            else (component in REQUIRED_COMPONENTS if component else False)
        )
        return HealthSeverity.RED if is_required else HealthSeverity.YELLOW
    return HealthSeverity.RED


@dataclass(frozen=True)
class HealthComponent:
    name: str
    status: HealthStatus
    severity: HealthSeverity
    as_of: str
    detail: str
    counts: Mapping[str, Any] = field(default_factory=dict)
    hashes: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "as_of": self.as_of,
            "detail": redact_secrets(self.detail),
            "name": self.name,
            "severity": self.severity.value,
            "status": self.status.value,
        }
        counts = redact_value(dict(self.counts))
        hashes = redact_value(dict(self.hashes))
        if counts:
            payload["counts"] = counts
        if hashes:
            payload["hashes"] = hashes
        return payload


@dataclass(frozen=True)
class HealthReport:
    as_of: str
    components: tuple[HealthComponent, ...]
    rollup: HealthSeverity
    ok: bool
    exit_code: int
    blocker_codes: tuple[str, ...]
    series: str = "dwcs"
    schema_version: int = HEALTH_SCHEMA_VERSION
    contract_id: str = HEALTH_CONTRACT_ID
    contract_version: str = HEALTH_CONTRACT_VERSION
    ticket: str = HEALTH_TICKET

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "blocker_codes": list(self.blocker_codes),
            "components": [c.to_dict() for c in self.components],
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "rollup": self.rollup.value,
            "schema_version": self.schema_version,
            "series": self.series,
            "ticket": self.ticket,
        }

    def to_gate_result(self) -> GateResult:
        """Adapter for DWCS-402 promotion ``health_result`` consumption."""
        assessments: list[GateAssessment] = []
        passed: list[str] = []
        for component in self.components:
            code = f"health.{component.name}"
            is_red = component.severity == HealthSeverity.RED
            assessments.append(
                GateAssessment(
                    code=code,
                    segment=component.name,
                    status="fail" if is_red else "pass",
                    blocking=is_red,
                    reason=component.detail,
                )
            )
            if not is_red:
                passed.append(code)
        return GateResult(
            ok=self.ok,
            exit_code=self.exit_code,
            blocker_codes=self.blocker_codes,
            passed_codes=tuple(passed),
            informational_codes=(),
            gates=tuple(assessments),
        )


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def make_component(
    name: str,
    status: HealthStatus | str,
    *,
    detail: str = "",
    as_of: str | None = None,
    counts: Mapping[str, Any] | None = None,
    hashes: Mapping[str, str] | None = None,
    required: bool | None = None,
) -> HealthComponent:
    status_val = HealthStatus(status) if not isinstance(status, HealthStatus) else status
    return HealthComponent(
        name=name,
        status=status_val,
        severity=severity_for(status_val, component=name, required=required),
        as_of=as_of or _utc_now_iso(),
        detail=redact_secrets(detail or status_val.value),
        counts=dict(counts or {}),
        hashes=dict(hashes or {}),
    )


def build_health_report(
    components: Sequence[HealthComponent | Mapping[str, Any]],
    *,
    as_of: str | None = None,
    series: str = "dwcs",
) -> HealthReport:
    """Assemble a health report and compute rollup / blockers."""
    stamp = as_of or _utc_now_iso()
    resolved: list[HealthComponent] = []
    for item in components:
        if isinstance(item, HealthComponent):
            resolved.append(item)
            continue
        name = str(item["name"])
        status = HealthStatus(str(item["status"]))
        required = item.get("required")
        required_flag = bool(required) if required is not None else None
        sev = item.get("severity")
        severity = (
            HealthSeverity(str(sev))
            if sev is not None
            else severity_for(status, component=name, required=required_flag)
        )
        resolved.append(
            HealthComponent(
                name=name,
                status=status,
                severity=severity,
                as_of=str(item.get("as_of") or stamp),
                detail=redact_secrets(str(item.get("detail") or status.value)),
                counts=dict(item.get("counts") or {}),
                hashes={str(k): str(v) for k, v in dict(item.get("hashes") or {}).items()},
            )
        )

    # Stable component order for deterministic JSON.
    order = {name: index for index, name in enumerate(HEALTH_COMPONENT_NAMES)}
    resolved.sort(key=lambda c: (order.get(c.name, 999), c.name))

    blockers = tuple(
        f"health.{c.name}.{c.status.value}"
        for c in resolved
        if c.severity == HealthSeverity.RED
    )
    if any(c.severity == HealthSeverity.RED for c in resolved):
        rollup = HealthSeverity.RED
    elif any(c.severity == HealthSeverity.YELLOW for c in resolved):
        rollup = HealthSeverity.YELLOW
    else:
        rollup = HealthSeverity.GREEN

    ok = rollup != HealthSeverity.RED
    return HealthReport(
        as_of=stamp,
        components=tuple(resolved),
        rollup=rollup,
        ok=ok,
        exit_code=EXIT_OK if ok else EXIT_STRICT_BLOCKERS,
        blocker_codes=blockers,
        series=series,
    )


def default_missing_report(*, as_of: str | None = None, series: str = "dwcs") -> HealthReport:
    """All known components present as ``missing`` (required → red)."""
    stamp = as_of or _utc_now_iso()
    components = [
        make_component(
            name,
            HealthStatus.MISSING,
            detail=f"{name} health not yet probed",
            as_of=stamp,
        )
        for name in HEALTH_COMPONENT_NAMES
    ]
    return build_health_report(components, as_of=stamp, series=series)


def dumps_health(report: HealthReport) -> str:
    """Deterministic sorted-key JSON (trailing newline)."""
    return (
        json.dumps(report.to_dict(), sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    )


def load_health_state(path: Path) -> HealthReport:
    """Load a component state snapshot JSON for CLI / tests."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "components" in raw:
        as_of = raw.get("as_of")
        series = str(raw.get("series") or "dwcs")
        return build_health_report(
            list(raw["components"]),
            as_of=str(as_of) if as_of else None,
            series=series,
        )
    if isinstance(raw, list):
        return build_health_report(raw)
    raise ValueError("health state must be a component list or report object")


def validate_health_json(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any] | None = None,
) -> None:
    """Validate against ``output/contracts/health.schema.json``."""
    validate_health_payload(payload, schema=schema)


__all__ = [
    "HEALTH_COMPONENT_NAMES",
    "HEALTH_CONTRACT_ID",
    "HEALTH_CONTRACT_VERSION",
    "HEALTH_SCHEMA_PATH",
    "HEALTH_SCHEMA_VERSION",
    "HEALTH_TICKET",
    "REQUIRED_COMPONENTS",
    "HealthComponent",
    "HealthReport",
    "HealthSeverity",
    "HealthStatus",
    "build_health_report",
    "default_missing_report",
    "dumps_health",
    "load_health_state",
    "make_component",
    "severity_for",
    "validate_health_json",
]
