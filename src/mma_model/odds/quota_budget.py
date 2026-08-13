"""Quota budget / cost estimation for odds jobs (DWCS-205).

Uses the provider cost contract plus persisted raw/inferred quota provenance.
Never exceeds configured monthly/run reserve; emits explicit deferred/exhausted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.schedule import (
    OddsScheduleContract,
    estimate_endpoint_cost,
    load_default_schedule_contract,
)
from mma_model.odds.types import (
    REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    REQUESTS_LAST_SOURCE_MISSING,
    REQUESTS_LAST_SOURCE_PROVIDER,
    QuotaHeaders,
)


class QuotaBudgetState(StrEnum):
    ALLOWED = "allowed"
    DEFERRED = "deferred"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class QuotaBudgetDecision:
    state: QuotaBudgetState
    estimated_cost: int
    remaining: int | None
    run_reserve: int
    spendable: int | None
    reason: str
    remaining_source: str


def cost_from_quota_headers(quota: QuotaHeaders) -> int:
    """Prefer provider last cost; empty is 0; missing is non-authoritative 0."""
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_PROVIDER:
        assert quota.requests_last is not None
        return int(quota.requests_last)
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_INFERRED_EMPTY:
        return 0
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_MISSING:
        # Missing provenance: do not invent a cost; treat as unknown (0 for ledger,
        # but callers should not treat as authoritative spend).
        return 0
    raise ValueError(f"unsupported requests_last_source: {quota.requests_last_source!r}")


def latest_remaining_from_observations(
    session: Session,
    *,
    provider: str,
    as_of: datetime,
) -> tuple[int | None, str]:
    """Read newest persisted remaining credits at/before ``as_of`` (UTC)."""
    stamp = ensure_utc(as_of, field="as_of")
    row = session.scalar(
        select(OddsQuotaObservation)
        .where(
            OddsQuotaObservation.provider == provider,
            OddsQuotaObservation.observed_at <= stamp,
        )
        .order_by(OddsQuotaObservation.observed_at.desc(), OddsQuotaObservation.id.desc())
        .limit(1)
    )
    if row is None:
        return None, "missing_observation"
    if row.requests_remaining is None:
        return None, "missing_remaining_header"
    return int(row.requests_remaining), "persisted_quota_observation"


def evaluate_quota_budget(
    *,
    estimated_cost: int,
    remaining: int | None,
    contract: OddsScheduleContract | None = None,
    remaining_source: str = "caller",
) -> QuotaBudgetDecision:
    """Decide allow / deferred / exhausted without exceeding monthly/run reserve."""
    if estimated_cost < 0:
        raise ValueError("estimated_cost must be nonnegative")
    sched = contract or load_default_schedule_contract()
    reserve = int(sched.quota.run_reserve)
    monthly = int(sched.quota.monthly_limit)

    if remaining is None:
        # Without a remaining reading, refuse spend that would exceed run reserve
        # relative to the configured monthly ceiling only when cost alone is huge.
        if estimated_cost > monthly:
            return QuotaBudgetDecision(
                state=QuotaBudgetState.EXHAUSTED,
                estimated_cost=estimated_cost,
                remaining=None,
                run_reserve=reserve,
                spendable=None,
                reason="estimated_cost_exceeds_monthly_limit_without_remaining",
                remaining_source=remaining_source,
            )
        if estimated_cost > max(0, monthly - reserve):
            return QuotaBudgetDecision(
                state=QuotaBudgetState.DEFERRED,
                estimated_cost=estimated_cost,
                remaining=None,
                run_reserve=reserve,
                spendable=None,
                reason="missing_remaining_defer_to_protect_reserve",
                remaining_source=remaining_source,
            )
        return QuotaBudgetDecision(
            state=QuotaBudgetState.ALLOWED,
            estimated_cost=estimated_cost,
            remaining=None,
            run_reserve=reserve,
            spendable=None,
            reason="missing_remaining_within_conservative_monthly_bound",
            remaining_source=remaining_source,
        )

    spendable = max(0, int(remaining) - reserve)
    if remaining <= 0:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.EXHAUSTED,
            estimated_cost=estimated_cost,
            remaining=remaining,
            run_reserve=reserve,
            spendable=0,
            reason="quota_remaining_nonpositive",
            remaining_source=remaining_source,
        )
    if estimated_cost > remaining:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.EXHAUSTED,
            estimated_cost=estimated_cost,
            remaining=remaining,
            run_reserve=reserve,
            spendable=spendable,
            reason="estimated_cost_exceeds_remaining",
            remaining_source=remaining_source,
        )
    if estimated_cost > spendable:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.DEFERRED,
            estimated_cost=estimated_cost,
            remaining=remaining,
            run_reserve=reserve,
            spendable=spendable,
            reason="estimated_cost_exceeds_spendable_after_run_reserve",
            remaining_source=remaining_source,
        )
    return QuotaBudgetDecision(
        state=QuotaBudgetState.ALLOWED,
        estimated_cost=estimated_cost,
        remaining=remaining,
        run_reserve=reserve,
        spendable=spendable,
        reason="within_remaining_and_run_reserve",
        remaining_source=remaining_source,
    )


def plan_request_budget(
    session: Session | None,
    *,
    endpoint: str,
    markets: str,
    regions: str,
    provider: str,
    as_of: datetime,
    contract: OddsScheduleContract | None = None,
    remaining_override: int | None = None,
) -> QuotaBudgetDecision:
    """Estimate cost and evaluate against persisted (or override) remaining."""
    sched = contract or load_default_schedule_contract()
    cost = estimate_endpoint_cost(
        endpoint=endpoint,
        markets=markets,
        regions=regions,
        contract=sched,
    )
    if remaining_override is not None:
        remaining, source = int(remaining_override), "override"
    elif session is None:
        remaining, source = None, "no_session"
    else:
        remaining, source = latest_remaining_from_observations(
            session, provider=provider, as_of=as_of
        )
    return evaluate_quota_budget(
        estimated_cost=cost,
        remaining=remaining,
        contract=sched,
        remaining_source=source,
    )


def sum_estimated_costs(costs: Sequence[int]) -> int:
    total = 0
    for value in costs:
        if value < 0:
            raise ValueError("cost components must be nonnegative")
        total += int(value)
    return total


__all__ = [
    "QuotaBudgetDecision",
    "QuotaBudgetState",
    "cost_from_quota_headers",
    "evaluate_quota_budget",
    "latest_remaining_from_observations",
    "plan_request_budget",
    "sum_estimated_costs",
]
