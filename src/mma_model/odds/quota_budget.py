"""Quota budget / cost estimation for odds jobs (DWCS-205).

Uses the provider cost contract plus persisted raw/inferred quota provenance.
Never assumes unused monthly quota. Never exceeds actual remaining under the
configured exclusive-API-key operational assumption. Never pre-authorizes N
batches against one unadjusted balance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.quota_bootstrap import bootstrap_quota_remaining
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
    exclusive_api_key_for_worker: bool
    quota_guarantee: str


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


def validate_remaining_override(
    remaining_override: int,
    *,
    contract: OddsScheduleContract | None = None,
) -> tuple[int, str]:
    """Require ``0 <= override <= monthly_limit``; return value + provenance."""
    sched = contract or load_default_schedule_contract()
    value = int(remaining_override)
    if value < 0:
        raise ValueError("remaining_override must be nonnegative")
    if value > int(sched.quota.monthly_limit):
        raise ValueError(
            f"remaining_override {value} exceeds monthly_limit "
            f"{sched.quota.monthly_limit}"
        )
    return value, f"override_bounded:{value}"


def _quota_guarantee(exclusive: bool) -> str:
    if exclusive:
        return "exclusive_worker_freshness_window"
    return "no_absolute_never_exceed_non_exclusive_key"


def _observation_freshness(
    *,
    observed_at: datetime,
    as_of: datetime,
    contract: OddsScheduleContract,
) -> str | None:
    """Return None when fresh; otherwise a stale/unknown reason code."""
    stamp = ensure_utc(as_of, field="as_of")
    observed = ensure_utc(observed_at, field="observed_at")
    if observed > stamp:
        return "future_observation"
    if contract.quota.billing_cycle == "calendar_month_utc":
        if (observed.year, observed.month) != (stamp.year, stamp.month):
            return "stale_billing_cycle"
    else:
        return "unsupported_billing_cycle"
    age = stamp - observed
    if age > timedelta(seconds=int(contract.quota.remaining_max_age_sec)):
        return "stale_max_age"
    return None


def latest_remaining_from_observations(
    session: Session,
    *,
    provider: str,
    as_of: datetime,
    contract: OddsScheduleContract | None = None,
) -> tuple[int | None, str]:
    """Read newest fresh persisted remaining credits at/before ``as_of`` (UTC)."""
    sched = contract or load_default_schedule_contract()
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
    observed = row.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    stale = _observation_freshness(
        observed_at=observed, as_of=stamp, contract=sched
    )
    if stale is not None:
        return None, stale
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
    exclusive = bool(sched.quota.exclusive_api_key_for_worker)
    guarantee = _quota_guarantee(exclusive)
    reserve = int(sched.quota.run_reserve)
    preserves = purpose.value in sched.quota.preserve_reserve_for
    may_spend = purpose.value in sched.quota.may_spend_reserve_for
    if not preserves and not may_spend:
        raise ValueError(f"purpose {purpose.value!r} not configured in schedule quota")

    def _decision(
        *,
        state: QuotaBudgetState,
        remaining_v: int | None,
        spendable: int | None,
        reason: str,
    ) -> QuotaBudgetDecision:
        return QuotaBudgetDecision(
            state=state,
            estimated_cost=estimated_cost,
            remaining=remaining_v,
            run_reserve=reserve,
            spendable=spendable,
            reason=reason,
            remaining_source=remaining_source,
            purpose=purpose,
            preserves_reserve=preserves,
            exclusive_api_key_for_worker=exclusive,
            quota_guarantee=guarantee,
        )

    if remaining is None:
        if allow_missing_remaining_override and remaining_source.startswith(
            "override_bounded"
        ):
            return _decision(
                state=QuotaBudgetState.ALLOWED,
                remaining_v=None,
                spendable=None,
                reason="explicit_bounded_operator_override",
            )
        return _decision(
            state=QuotaBudgetState.DEFERRED,
            remaining_v=None,
            spendable=None,
            reason="missing_remaining_fail_closed",
        )

    remaining_i = int(remaining)
    if remaining_i <= 0:
        return _decision(
            state=QuotaBudgetState.EXHAUSTED,
            remaining_v=remaining_i,
            spendable=0,
            reason="quota_remaining_nonpositive",
        )

    spendable = (
        remaining_i
        if may_spend and not preserves
        else max(0, remaining_i - reserve)
    )

    if estimated_cost > remaining_i:
        return _decision(
            state=QuotaBudgetState.EXHAUSTED,
            remaining_v=remaining_i,
            spendable=spendable,
            reason="estimated_cost_exceeds_remaining",
        )
    if estimated_cost > spendable:
        return _decision(
            state=QuotaBudgetState.DEFERRED,
            remaining_v=remaining_i,
            spendable=spendable,
            reason=(
                "estimated_cost_exceeds_spendable_after_run_reserve"
                if preserves
                else "estimated_cost_exceeds_spendable"
            ),
        )
    reason = (
        "within_remaining_including_reserve"
        if may_spend and not preserves
        else "within_remaining_and_run_reserve"
    )
    if not exclusive:
        reason = f"{reason}|non_exclusive_key_no_absolute_guarantee"
    return _decision(
        state=QuotaBudgetState.ALLOWED,
        remaining_v=remaining_i,
        spendable=spendable,
        reason=reason,
    )


@dataclass
class RunningQuotaLedger:
    """Per-run cumulative quota accounting so batches cannot overspend one balance."""

    remaining: int | None
    remaining_source: str
    reserved: int = 0
    decisions: list[QuotaBudgetDecision] = field(default_factory=list)

    @property
    def effective_remaining(self) -> int | None:
        if self.remaining is None:
            return None
        return int(self.remaining) - int(self.reserved)

    def evaluate(
        self,
        *,
        estimated_cost: int,
        purpose: RequestPurpose,
        contract: OddsScheduleContract | None = None,
    ) -> QuotaBudgetDecision:
        allow_override = self.remaining_source.startswith("override_bounded")
        decision = evaluate_quota_budget(
            estimated_cost=estimated_cost,
            remaining=self.effective_remaining,
            purpose=purpose,
            contract=contract,
            remaining_source=self.remaining_source,
            allow_missing_remaining_override=allow_override and self.remaining is None,
        )
        self.decisions.append(decision)
        return decision

    def reserve(self, estimated_cost: int) -> None:
        """Reserve cost for the batch about to execute (or dry-run plan)."""
        if estimated_cost < 0:
            raise ValueError("estimated_cost must be nonnegative")
        self.reserved += int(estimated_cost)

    def release(self, estimated_cost: int) -> None:
        """Release an unexecuted reservation (failed/aborted batch)."""
        if estimated_cost < 0:
            raise ValueError("estimated_cost must be nonnegative")
        self.reserved = max(0, int(self.reserved) - int(estimated_cost))

    def apply_provider_remaining(
        self, remaining: int | None, *, source: str
    ) -> None:
        """Replace absolute remaining after a paid response.

        Clears only the just-executed reservation accounting. Pending batches
        must be re-evaluated immediately before execution against this newest
        remaining — never pre-authorized against a stale balance.
        """
        self.remaining = remaining
        self.remaining_source = source
        self.reserved = 0


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
    ledger: RunningQuotaLedger | None = None,
    allow_bootstrap: bool = False,
    offline_fixtures: bool = False,
    fixture_dir: object | None = None,
) -> QuotaBudgetDecision:
    """Estimate cost and evaluate against persisted/override/bootstrap remaining.

    When ``ledger`` is provided, evaluation uses cumulative remaining after prior
    reservations in this run (never N independent authorizations of one balance).
    """
    sched = contract or load_default_schedule_contract()
    markets_n = None if markets is None else normalize_markets(markets)
    regions_n = None if regions is None else normalize_regions(regions)
    cost = estimate_endpoint_cost(
        endpoint=endpoint,
        markets=markets_n,
        regions=regions_n,
        contract=sched,
    )
    if ledger is not None:
        return ledger.evaluate(
            estimated_cost=cost, purpose=purpose, contract=sched
        )

    if remaining_override is not None:
        remaining, source = validate_remaining_override(
            remaining_override, contract=sched
        )
        return evaluate_quota_budget(
            estimated_cost=cost,
            remaining=remaining,
            purpose=purpose,
            contract=sched,
            remaining_source=source,
            allow_missing_remaining_override=False,
        )

    remaining, source = _resolve_remaining_for_run(
        session,
        provider=provider,
        as_of=as_of,
        contract=sched,
        allow_bootstrap=allow_bootstrap,
        offline_fixtures=offline_fixtures,
        fixture_dir=fixture_dir,
    )
    return evaluate_quota_budget(
        estimated_cost=cost,
        remaining=remaining,
        purpose=purpose,
        contract=sched,
        remaining_source=source,
        allow_missing_remaining_override=False,
    )


def _resolve_remaining_for_run(
    session: Session | None,
    *,
    provider: str,
    as_of: datetime,
    contract: OddsScheduleContract,
    allow_bootstrap: bool,
    offline_fixtures: bool,
    fixture_dir: object | None,
) -> tuple[int | None, str]:
    """Resolve remaining under exclusive-key assumption.

    Exclusive ownership enables freshness-window planning. Without it, persisted
    remaining is ignored and a zero-cost bootstrap is forced (or fail closed).
    """
    if session is None:
        return None, "no_session"

    exclusive = bool(contract.quota.exclusive_api_key_for_worker)
    if exclusive:
        remaining, source = latest_remaining_from_observations(
            session, provider=provider, as_of=as_of, contract=contract
        )
        if remaining is None and allow_bootstrap and contract.quota.bootstrap_enabled:
            remaining, source = bootstrap_quota_remaining(
                session,
                provider=provider,
                as_of=as_of,
                contract=contract,
                offline_fixtures=offline_fixtures,
                fixture_dir=fixture_dir,  # type: ignore[arg-type]
            )
        return remaining, source

    # Non-exclusive: do not trust persisted remaining across uncontrolled consumers.
    if not allow_bootstrap or not contract.quota.bootstrap_enabled:
        return None, "non_exclusive_key_fail_closed"
    remaining, source = bootstrap_quota_remaining(
        session,
        provider=provider,
        as_of=as_of,
        contract=contract,
        offline_fixtures=offline_fixtures,
        fixture_dir=fixture_dir,  # type: ignore[arg-type]
        force=True,
    )
    if remaining is None:
        return None, f"{source}|non_exclusive_key_fail_closed"
    return remaining, f"{source}|non_exclusive_forced_bootstrap"


def open_quota_ledger(
    session: Session | None,
    *,
    provider: str,
    as_of: datetime,
    contract: OddsScheduleContract | None = None,
    remaining_override: int | None = None,
    allow_bootstrap: bool = True,
    offline_fixtures: bool = False,
    fixture_dir: object | None = None,
) -> RunningQuotaLedger:
    """Open a run-scoped ledger, bootstrapping zero-cost when needed."""
    sched = contract or load_default_schedule_contract()
    if remaining_override is not None:
        remaining, source = validate_remaining_override(
            remaining_override, contract=sched
        )
        return RunningQuotaLedger(remaining=remaining, remaining_source=source)
    remaining, source = _resolve_remaining_for_run(
        session,
        provider=provider,
        as_of=as_of,
        contract=sched,
        allow_bootstrap=allow_bootstrap,
        offline_fixtures=offline_fixtures,
        fixture_dir=fixture_dir,
    )
    return RunningQuotaLedger(remaining=remaining, remaining_source=source)


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
    "RunningQuotaLedger",
    "cost_from_quota_headers",
    "evaluate_quota_budget",
    "latest_remaining_from_observations",
    "open_quota_ledger",
    "plan_request_budget",
    "sum_estimated_costs",
    "validate_remaining_override",
]
