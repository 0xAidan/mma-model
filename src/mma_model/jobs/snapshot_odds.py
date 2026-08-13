"""Due live odds snapshot job with flock + idempotency (DWCS-205)."""

from __future__ import annotations

from collections import defaultdict
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
    find_successful_run,
    record_job_run,
    slot_succeeded,
)
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.quota_budget import (
    QuotaBudgetState,
    cost_from_quota_headers,
    plan_request_budget,
)
from mma_model.odds.schedule import (
    DueAction,
    DueWorkItem,
    OddsScheduleContract,
    RequestPurpose,
    compute_due_work,
    load_default_schedule_contract,
    normalize_markets,
    normalize_regions,
)
from mma_model.odds.snapshot import run_odds_snapshot
from mma_model.odds.types import (
    REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    REQUESTS_LAST_SOURCE_MISSING,
    REQUESTS_LAST_SOURCE_PROVIDER,
    PROVIDER_THE_ODDS_API,
    QuotaHeaders,
)


@dataclass(frozen=True)
class SnapshotOddsJobResult:
    as_of: str
    action: str
    processed: int
    due: int
    no_op: int
    deferred: int
    exhausted: int
    duplicates: int
    failures: int
    batches: int
    upcoming_event_count: int
    items: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_event_start(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field="event_start")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text), field="event_start")


def _quota_headers_from_result(quota: Mapping[str, Any]) -> QuotaHeaders:
    source = str(quota.get("requests_last_source") or REQUESTS_LAST_SOURCE_MISSING)
    return QuotaHeaders(
        requests_remaining=quota.get("x-requests-remaining"),
        requests_used=quota.get("x-requests-used"),
        requests_last=quota.get("x-requests-last"),
        requests_last_inferred=quota.get("requests_last_inferred"),
        requests_last_source=source,
    )


def _actual_cost_fields(quota: Mapping[str, Any]) -> tuple[int | None, str]:
    headers = _quota_headers_from_result(quota)
    cost = cost_from_quota_headers(headers)
    if headers.requests_last_source == REQUESTS_LAST_SOURCE_PROVIDER:
        return cost, "provider"
    if headers.requests_last_source == REQUESTS_LAST_SOURCE_INFERRED_EMPTY:
        return 0, "inferred_empty_zero"
    return None, "missing"


def run_snapshot_odds_job(
    session: Session,
    *,
    as_of: datetime,
    events: Sequence[Mapping[str, Any]],
    finished_at: datetime | None = None,
    lock: OverlapProtection | None = None,
    lock_path: Path | None = None,
    contract: OddsScheduleContract | None = None,
    markets: str | None = None,
    region: str | None = None,
    offline_fixtures: bool = False,
    fixture_dir: Path | None = None,
    execute: bool = True,
    remaining_override: int | None = None,
) -> SnapshotOddsJobResult:
    """Evaluate due work and execute batched sport-wide snapshots under flock.

    ``events`` must already be canonical upcoming rows from the target DB.
    Empty upcoming input is reported explicitly (not a silent success).
    """
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    completion = ensure_utc(finished_at or stamp, field="finished_at")
    markets_v = normalize_markets(markets or sched.default_markets)
    region_v = normalize_regions(region or sched.default_region)

    active_lock: OverlapProtection = lock or FileFlockLock(
        lock_path or Path("/tmp") / "mma-snapshot-odds.lock"
    )

    item_rows: list[dict[str, Any]] = []
    counts = {
        "processed": 0,
        "due": 0,
        "no_op": 0,
        "deferred": 0,
        "exhausted": 0,
        "duplicates": 0,
        "failures": 0,
        "batches": 0,
    }

    if not events:
        return SnapshotOddsJobResult(
            as_of=stamp.isoformat().replace("+00:00", "Z"),
            action="snapshot-odds",
            processed=0,
            due=0,
            no_op=0,
            deferred=0,
            exhausted=0,
            duplicates=0,
            failures=0,
            batches=0,
            upcoming_event_count=0,
            items=(
                {
                    "action": "no_upcoming_events",
                    "reason": "canonical_db_returned_zero_upcoming_dwcs_events",
                },
            ),
        )

    with hold_overlap_lock(active_lock):
        due_items: list[DueWorkItem] = []
        for event in events:
            event_id = str(event["event_id"])
            event_start = _parse_event_start(event["event_start"])
            provisional = compute_due_work(
                as_of=stamp,
                event_id=event_id,
                event_start=event_start,
                slot_already_succeeded=False,
                provider=PROVIDER_THE_ODDS_API,
                markets=markets_v,
                region=region_v,
                contract=sched,
            )
            succeeded = bool(
                provisional.idempotency_key
                and slot_succeeded(session, idempotency_key=provisional.idempotency_key)
            )
            purpose = provisional.purpose or RequestPurpose.LIVE_ORDINARY
            budget = plan_request_budget(
                session,
                endpoint="current_odds",
                markets=markets_v,
                regions=region_v,
                provider=PROVIDER_THE_ODDS_API,
                as_of=stamp,
                purpose=purpose,
                contract=sched,
                remaining_override=remaining_override,
            )
            quota_state = None
            if budget.state == QuotaBudgetState.EXHAUSTED:
                quota_state = "exhausted"
            elif budget.state == QuotaBudgetState.DEFERRED:
                quota_state = "deferred"

            item = compute_due_work(
                as_of=stamp,
                event_id=event_id,
                event_start=event_start,
                slot_already_succeeded=succeeded,
                provider=PROVIDER_THE_ODDS_API,
                markets=markets_v,
                region=region_v,
                contract=sched,
                quota_state=quota_state,
            )
            counts["processed"] += 1
            row: dict[str, Any] = {
                "event_id": event_id,
                "action": item.action.value,
                "reason": item.reason,
                "window_name": item.window_name,
                "slot_start": None
                if item.slot_start is None
                else item.slot_start.isoformat().replace("+00:00", "Z"),
                "idempotency_key": item.idempotency_key,
                "batch_key": item.batch_key,
                "estimated_cost": item.estimated_cost,
                "purpose": None if item.purpose is None else item.purpose.value,
            }
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
                    finished_at=completion,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    detail=item.reason,
                )
                item_rows.append(row)
                continue
            if item.action == DueAction.EXHAUSTED_QUOTA:
                counts["exhausted"] += 1
                assert item.idempotency_key is not None
                record_job_run(
                    session,
                    idempotency_key=item.idempotency_key,
                    job_name="snapshot-odds",
                    status="exhausted",
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=f"live:{item.window_name}",
                    as_of=stamp,
                    finished_at=completion,
                    estimated_cost=item.estimated_cost,
                    window_name=item.window_name,
                    detail=item.reason,
                )
                item_rows.append(row)
                continue

            counts["due"] += 1
            due_items.append(item)
            item_rows.append(row)

        if not execute:
            return SnapshotOddsJobResult(
                as_of=stamp.isoformat().replace("+00:00", "Z"),
                action="snapshot-odds",
                processed=counts["processed"],
                due=counts["due"],
                no_op=counts["no_op"],
                deferred=counts["deferred"],
                exhausted=counts["exhausted"],
                duplicates=counts["duplicates"],
                failures=counts["failures"],
                batches=0,
                upcoming_event_count=len(events),
                items=tuple(item_rows),
            )

        batches: dict[str, list[DueWorkItem]] = defaultdict(list)
        for item in due_items:
            assert item.batch_key is not None
            batches[item.batch_key].append(item)

        for batch_key, members in batches.items():
            counts["batches"] += 1
            representative = members[0]
            # One sport-wide provider call per batch.
            nested = session.begin_nested()
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
                actual, source = _actual_cost_fields(result.quota)
                for member in members:
                    assert member.idempotency_key is not None
                    if find_successful_run(
                        session, idempotency_key=member.idempotency_key
                    ):
                        counts["duplicates"] += 1
                        record_job_run(
                            session,
                            idempotency_key=member.idempotency_key,
                            job_name="snapshot-odds",
                            status="duplicate",
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            markets=markets_v,
                            event_id=member.event_id,
                            mode=f"live:{member.window_name}",
                            as_of=stamp,
                            finished_at=completion,
                            estimated_cost=0,
                            window_name=member.window_name,
                            detail=f"prior_success;batch={batch_key}",
                        )
                        continue
                    is_primary = member is representative
                    record_job_run(
                        session,
                        idempotency_key=member.idempotency_key,
                        job_name="snapshot-odds",
                        status="success",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member.event_id,
                        mode=f"live:{member.window_name}",
                        as_of=stamp,
                        finished_at=completion,
                        estimated_cost=member.estimated_cost if is_primary else 0,
                        actual_cost=actual if is_primary else None,
                        actual_cost_source=source if is_primary else None,
                        window_name=member.window_name,
                        detail=(
                            f"batch={batch_key};inserted={result.inserted};"
                            f"deduped={result.deduped}"
                        ),
                    )
                nested.commit()
            except Exception as exc:  # noqa: BLE001 — job boundary
                nested.rollback()
                counts["failures"] += len(members)
                for member in members:
                    assert member.idempotency_key is not None
                    record_job_run(
                        session,
                        idempotency_key=member.idempotency_key,
                        job_name="snapshot-odds",
                        status="failed",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member.event_id,
                        mode=f"live:{member.window_name}",
                        as_of=stamp,
                        finished_at=completion,
                        estimated_cost=member.estimated_cost,
                        window_name=member.window_name,
                        error_class=type(exc).__name__,
                        detail=str(exc)[:500],
                    )

    return SnapshotOddsJobResult(
        as_of=stamp.isoformat().replace("+00:00", "Z"),
        action="snapshot-odds",
        processed=counts["processed"],
        due=counts["due"],
        no_op=counts["no_op"],
        deferred=counts["deferred"],
        exhausted=counts["exhausted"],
        duplicates=counts["duplicates"],
        failures=counts["failures"],
        batches=counts["batches"],
        upcoming_event_count=len(events),
        items=tuple(item_rows),
    )


__all__ = ["SnapshotOddsJobResult", "run_snapshot_odds_job"]
