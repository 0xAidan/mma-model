"""Authoritative DWCS-203 quote eligibility decision identity (cycle-free).

Shared by the lifecycle resolver and DWCS-204 evidence DTOs without importing
odds package ``__init__`` or value packages.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Final

QUOTE_ELIGIBILITY_DECISION_VERSION: Final = "quote_eligibility_v1"
RECOGNIZED_QUOTE_ELIGIBILITY_DECISION_VERSIONS: Final[frozenset[str]] = frozenset(
    {QUOTE_ELIGIBILITY_DECISION_VERSION}
)
LIFECYCLE_UNRESOLVED: Final = "unresolved"

# Mirrors lifecycle VALUE_BLOCKING_LIFECYCLES for eligible-evidence validation.
ELIGIBILITY_BLOCKING_LIFECYCLES: Final[frozenset[str]] = frozenset(
    {
        "stale",
        "missing_unknown",
        "locked",
        "cancelled",
        "replaced",
        "review_blocked",
    }
)


def compute_quote_eligibility_decision_identity(
    *,
    quote_id: int,
    evaluated_at: datetime,
    eligible: bool,
    reason: str,
    selection_identity: str,
    resolved_bout_id: str | None,
    quote_availability_at_decision: str,
    quote_freshness_at: datetime | None,
    lifecycle_state_at_decision: str,
    decision_version: str,
) -> str:
    """Authoritative content identity for a DWCS-203 quote eligibility decision."""
    as_of = evaluated_at.astimezone(UTC).isoformat()
    freshness = (
        None
        if quote_freshness_at is None
        else quote_freshness_at.astimezone(UTC).isoformat()
    )
    payload = "|".join(
        [
            str(int(quote_id)),
            as_of,
            "1" if eligible else "0",
            str(reason),
            str(selection_identity),
            "" if resolved_bout_id is None else str(resolved_bout_id),
            str(quote_availability_at_decision),
            "" if freshness is None else freshness,
            str(lifecycle_state_at_decision),
            str(decision_version),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"qe_v1:{digest}"
