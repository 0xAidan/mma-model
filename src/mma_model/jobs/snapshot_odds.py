"""Due live odds snapshot job with flock + idempotency (DWCS-205)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.jobs.locking import (
    FileFlockLock,
    OverlapProtection,
    hold_overlap_lock,
)
from mma_model.odds.job_ledger import (
    JobLedgerDuplicate,
    find_successful_run,
    last_success_at_for_event,
    record_job_run,
)
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.quota_budget import QuotaBudgetState, plan_request_budget
from mma_model.odds.schedule import (
    DueAction,
    DueWorkItem,
    OddsScheduleContract,
    compute_due_work,
    load_default_schedule_contract,
)
from mma_model.odds.snapshot import run_odds_snapshot
from mma_model.odds.types import PROVIDER_THE_ODDS_API


@dataclass(frozen=True)
class SnapshotOddsJobResult:
    as_of: str
    action: str
    processed: int
    due: int
    no_op: int
    deferred: int
    duplicates: int
    failures: int
    items: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quota_allows(
    session: Session,
    *,
    item: DueWorkItem,
    contract: OddsScheduleContract,
) -> bool:
    decision = plan_request_budget(
        session,
        endpoint="current_odds",
        markets=item.markets,
        regions=item.region,
        provider=PROVIDER_THE_ODDS_API,
        as_of=item.as_of,
        contract=contract,
    )
    return decision.state == QuotaBudgetState.ALLOWED


def run_snapshot_odds_job(
    session: Session,
    *,
    as_of: datetime,
    events: Sequence[Mapping[str, Any]],
    lock: OverlapProtection | None = None,
    lock_path: Path | None = None,
    contract: OddsScheduleContract | None = None,
    markets: str | None = None,
    region: str | None = None,
    offline_fixtures: bool = False,
    fixture_dir: Path | None = None,
    execute: bool = True,
) -> SnapshotOddsJobResult:
    """Evaluate due work and optionally execute snapshots under one-writer lock."""
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    markets_v = markets or sched.default_markets
    region_v = region or sched.default_region

    active_lock: OverlapProtection
    if lock is not None:
        active_lock = lock
    else:
        path = lock_path or Path("/tmp") / "mma-snapshot-odds.lock"
        active_lock = FileFlockLock(path)

    item_rows: list[dict[str, Any]] = []
    counts = {
        "processed": 0,
        "due": 0,
        "no_op": 0,
        "deferred": 0,
        "duplicates": 0,
        "failures": 0,
    }

    with hold_overlap_lock(active_lock):
        for event in events:
            event_id = str(event["event_id"])
            event_start = event["event_start"]
            if isinstance(event_start, str):
                from datetime import datetime as _dt

                raw = event_start.strip()
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                event_start = ensure_utc(_dt.fromisoformat(raw), field="event_start")
            else:
                event_start = ensure_utc(event_start, field="event_start")

            last_success = last_success_at_for_event(
                session,
                event_id=event_id,
                provider=PROVIDER_THE_ODDS_API,
                region=region_v,
                markets=markets_v,
            )
            # Provisional due calc; final-hour may flip to deferred after budget.
            provisional = compute_due_work(
                as_of=stamp,
                event_id=event_id,
                event_start=event_start,
                last_success_at=last_success,
                provider=PROVIDER_THE_ODDS_API,
                markets=markets_v,
                region=region_v,
                contract=sched,
                quota_allows=True,
            )
            quota_ok = True
            if provisional.requires_quota_headroom or provisional.action == DueAction.DUE:
                quota_ok = _quota_allows(session, item=provisional, contract=sched)
            item = compute_due_work(
                as_of=stamp,
                event_id=event_id,
                event_start=event_start,
                last_success_at=last_success,
                provider=PROVIDER_THE_ODDS_API,
                markets=markets_v,
                region=region_v,
                contract=sched,
                quota_allows=quota_ok,
            )

            row: dict[str, Any] = {
                "event_id": event_id,
                "action": item.action.value,
                "reason": item.reason,
                "window_name": item.window_name,
                "idempotency_key": item.idempotency_key,
                "estimated_cost": item.estimated_cost,
            }
            counts["processed"] += 1

            if item.action == DueAction.NO_OP:
                counts["no_op"] += 1
                item_rows.append(row)
                continue
            if item.action == DueAction.NOT_DUE:
                item_rows.append(row)
                continue
            if item.action == DueAction.DEFERRED_QUOTA:
                counts["deferred"] += 1
                assert item.idempotency_key is not None
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="deferred_quota",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    detail=item.reason,
                )
                item_rows.append(row)
                continue

            # DUE
            counts["due"] += 1
            assert item.idempotency_key is not None
            prior = find_successful_run(session, idempotency_key=item.idempotency_key)
            if prior is not None:
                counts["duplicates"] += 1
                row["action"] = "duplicate"
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="duplicate",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    detail="prior_success",
                )
                item_rows.append(row)
                continue

            if not execute:
                item_rows.append(row)
                continue

            if not quota_ok:
                counts["deferred"] += 1
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="deferred_quota",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    detail="budget_blocked_before_execute",
                )
                row["action"] = DueAction.DEFERRED_QUOTA.value
                item_rows.append(row)
                continue

            try:
                result = run_odds_snapshot(
                    session,
                    series=sched.series,
                    provider=PROVIDER_THE_ODDS_API,
                    markets=markets_v,
                    regions=region_v,
                    historical_date=None,
                    fixture_dir=fixture_dir,
                    offline_fixtures=offline_fixtures,
                    observed_at=stamp,
                )
                expected = result.quota.get("expected_cost")
                actual = 0 if expected is None else int(expected)
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="success",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    estimated_cost=item.estimated_cost,
                    actual_cost=actual,
                    window_name=item.window_name,
                    detail=f"inserted={result.inserted};deduped={result.deduped}",
                )
                row["inserted"] = result.inserted
                row["actual_cost"] = actual
            except JobLedgerDuplicate:
                counts["duplicates"] += 1
                row["action"] = "duplicate"
            except Exception as exc:  # noqa: BLE001 — job boundary records class
                counts["failures"] += 1
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="failed",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    error_class=type(exc).__name__,
                    detail=str(exc)[:500],
                )
                row["action"] = "failed"
                row["error_class"] = type(exc).__name__
            item_rows.append(row)

    return SnapshotOddsJobResult(
        as_of=stamp.isoformat().replace("+00:00", "Z"),
        action="snapshot-odds",
        processed=counts["processed"],
        due=counts["due"],
        no_op=counts["no_op"],
        deferred=counts["deferred"],
        duplicates=counts["duplicates"],
        failures=counts["failures"],
        items=tuple(item_rows),
    )


__all__ = ["SnapshotOddsJobResult", "run_snapshot_odds_job"]
