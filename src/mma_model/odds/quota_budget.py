"""Quota budget / cost estimation for odds jobs (DWCS-205).

Uses the provider cost contract plus persisted raw/inferred quota provenance.
Never assumes unused monthly quota. Never exceeds actual remaining.
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
    RequestPurpose,
    estimate_endpoint_cost,
    load_default_schedule_contract,
    normalize_markets,
    normalize_regions,
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
    purpose: RequestPurpose
    preserves_reserve: bool


def cost_from_quota_headers(quota: QuotaHeaders) -> int | None:
    """Return known cost; missing provenance yields None (never invent 0)."""
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_PROVIDER:
        assert quota.requests_last is not None
        return int(quota.requests_last)
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_INFERRED_EMPTY:
        return 0
    if quota.requests_last_source == REQUESTS_LAST_SOURCE_MISSING:
        return None
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
    if int(row.requests_remaining) < 0:
        return None, "malformed_negative_remaining"
    return int(row.requests_remaining), "persisted_quota_observation"


def evaluate_quota_budget(
    *,
    estimated_cost: int,
    remaining: int | None,
    purpose: RequestPurpose,
    contract: OddsScheduleContract | None = None,
    remaining_source: str = "caller",
    allow_missing_remaining_override: bool = False,
) -> QuotaBudgetDecision:
    """Decide allow / deferred / exhausted with purpose-aware reserve rules."""
    if estimated_cost < 0:
        raise ValueError("estimated_cost must be nonnegative")
    sched = contract or load_default_schedule_contract()
    reserve = int(sched.quota.run_reserve)
    preserves = purpose.value in sched.quota.preserve_reserve_for
    may_spend = purpose.value in sched.quota.may_spend_reserve_for
    if not preserves and not may_spend:
        raise ValueError(f"purpose {purpose.value!r} not configured in schedule quota")

    if remaining is None:
        if allow_missing_remaining_override and remaining_source.startswith("override"):
            # Explicit bounded operator override path only.
            return QuotaBudgetDecision(
                state=QuotaBudgetState.ALLOWED,
                estimated_cost=estimated_cost,
                remaining=None,
                run_reserve=reserve,
                spendable=None,
                reason="explicit_bounded_operator_override",
                remaining_source=remaining_source,
                purpose=purpose,
                preserves_reserve=preserves,
            )
        return QuotaBudgetDecision(
            state=QuotaBudgetState.DEFERRED,
            estimated_cost=estimated_cost,
            remaining=None,
            run_reserve=reserve,
            spendable=None,
            reason="missing_remaining_fail_closed",
            remaining_source=remaining_source,
            purpose=purpose,
            preserves_reserve=preserves,
        )

    remaining_i = int(remaining)
    if remaining_i <= 0:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.EXHAUSTED,
            estimated_cost=estimated_cost,
            remaining=remaining_i,
            run_reserve=reserve,
            spendable=0,
            reason="quota_remaining_nonpositive",
            remaining_source=remaining_source,
            purpose=purpose,
            preserves_reserve=preserves,
        )

    spendable = (
        remaining_i
        if may_spend and not preserves
        else max(0, remaining_i - reserve)
    )

    if estimated_cost > remaining_i:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.EXHAUSTED,
            estimated_cost=estimated_cost,
            remaining=remaining_i,
            run_reserve=reserve,
            spendable=spendable,
            reason="estimated_cost_exceeds_remaining",
            remaining_source=remaining_source,
            purpose=purpose,
            preserves_reserve=preserves,
        )
    if estimated_cost > spendable:
        return QuotaBudgetDecision(
            state=QuotaBudgetState.DEFERRED,
            estimated_cost=estimated_cost,
            remaining=remaining_i,
            run_reserve=reserve,
            spendable=spendable,
            reason=(
                "estimated_cost_exceeds_spendable_after_run_reserve"
                if preserves
                else "estimated_cost_exceeds_spendable"
            ),
            remaining_source=remaining_source,
            purpose=purpose,
            preserves_reserve=preserves,
        )
    return QuotaBudgetDecision(
        state=QuotaBudgetState.ALLOWED,
        estimated_cost=estimated_cost,
        remaining=remaining_i,
        run_reserve=reserve,
        spendable=spendable,
        reason=(
            "within_remaining_including_reserve"
            if may_spend and not preserves
            else "within_remaining_and_run_reserve"
        ),
        remaining_source=remaining_source,
        purpose=purpose,
        preserves_reserve=preserves,
    )


def plan_request_budget(
    session: Session | None,
    *,
    endpoint: str,
    markets: str | None,
    regions: str | None,
    provider: str,
    as_of: datetime,
    purpose: RequestPurpose,
    contract: OddsScheduleContract | None = None,
    remaining_override: int | None = None,
) -> QuotaBudgetDecision:
    """Estimate cost and evaluate against persisted (or override) remaining."""
    sched = contract or load_default_schedule_contract()
    markets_n = None if markets is None else normalize_markets(markets)
    regions_n = None if regions is None else normalize_regions(regions)
    cost = estimate_endpoint_cost(
        endpoint=endpoint,
        markets=markets_n,
        regions=regions_n,
        contract=sched,
    )
    if remaining_override is not None:
        if remaining_override < 0:
            raise ValueError("remaining_override must be nonnegative")
        remaining, source = int(remaining_override), "override_bounded"
        allow_missing = True
    elif session is None:
        remaining, source = None, "no_session"
        allow_missing = False
    else:
        remaining, source = latest_remaining_from_observations(
            session, provider=provider, as_of=as_of
        )
        allow_missing = False
    return evaluate_quota_budget(
        estimated_cost=cost,
        remaining=remaining,
        purpose=purpose,
        contract=sched,
        remaining_source=source,
        allow_missing_remaining_override=allow_missing,
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
