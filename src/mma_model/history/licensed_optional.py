"""Optional SportsDataIO / BALLDONTLIE validation under recorded limitations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mma_model.history.constants import SOURCE_BALLDONTLIE, SOURCE_SPORTSDATAIO
from mma_model.sources.policy import load_source_policy

LICENSED_LIMITATION_REASONS = {
    SOURCE_SPORTSDATAIO: "historical_2023_2024_entitlement_blocked_on_measured_key",
    SOURCE_BALLDONTLIE: "control_time_below_required_feature_gate_pit_unknown",
}


def licensed_optional_validation_status(
    *,
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Record optional licensed validation as source_failed under known limits.

    No network calls. Absence of a licensed primary is not a DWCS-105 stop.
    """
    policy = load_source_policy()
    observed = observed_at or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    for source in (SOURCE_SPORTSDATAIO, SOURCE_BALLDONTLIE):
        role = policy.roles[source]
        rows.append(
            {
                "source": source,
                "role": role.role,
                "status": "source_failed",
                "reason": LICENSED_LIMITATION_REASONS[source],
                "limitations": role.limitations,
                "decision_primary": policy.licensed_audit_status.decision_primary,
                "observed_at": observed.astimezone(timezone.utc).isoformat(),
                "network": False,
            }
        )
    return rows
