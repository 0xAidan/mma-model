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
    open_quota_ledger,
)
from mma_model.odds.schedule import (
    DueAction,
    DueWorkItem,
    OddsScheduleContract,
    RequestPurpose,
    compute_due_work,
    estimate_endpoint_cost,
    load_default_schedule_contract,
    normalize_markets,
    normalize_regions,
)
from mma_model.odds.snapshot import run_odds_snapshot
from mma_model.odds.types import (
    PROVIDER_THE_ODDS_API,
    REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    REQUESTS_LAST_SOURCE_MISSING,
    REQUESTS_LAST_SOURCE_PROVIDER,
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
    remaining_source: str
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


def _actual_cost_fields(quota: Mapping[str, Any]) -> tuple[int | None, str]:
    headers = QuotaHeaders(
        requests_remaining=quota.get("x-requests-remaining"),
        requests_used=quota.get("x-requests-used"),
        requests_last=quota.get("x-requests-last"),
        requests_last_inferred=quota.get("requests_last_inferred"),
        requests_last_source=str(
            quota.get("requests_last_source") or REQUESTS_LAST_SOURCE_MISSING
        ),
    )
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
    allow_bootstrap: bool = True,
) -> SnapshotOddsJobResult:
    """Evaluate due work and execute batched sport-wide snapshots under flock.

    ``events`` must already be canonical upcoming rows from the target DB.
    Empty upcoming input is reported explicitly (not a silent success).
    Batch budgets are cumulative within the run.
    """
    sched = contract or load_default_schedule_contract()
    stamp = ensure_utc(as_of, field="as_of")
    completion = ensure_utc(finished_at or stamp, field="finished_at")
    markets_v = normalize_markets(markets or sched.default_markets)
    region_v = normalize_regions(region or sched.default_region)
    batch_cost_est = estimate_endpoint_cost(
        endpoint="current_odds",
        markets=markets_v,
        regions=region_v,
        contract=sched,
    )

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
            remaining_source="n/a",
            items=(
                {
                    "action": "no_upcoming_events",
                    "reason": "canonical_db_returned_zero_upcoming_dwcs_events",
                },
            ),
        )

    with hold_overlap_lock(active_lock):
        ledger = open_quota_ledger(
            session,
            provider=PROVIDER_THE_ODDS_API,
            as_of=stamp,
            contract=sched,
            remaining_override=remaining_override,
            allow_bootstrap=allow_bootstrap,
            offline_fixtures=offline_fixtures,
            fixture_dir=fixture_dir,
        )

        # First pass: due without quota; collect candidates by batch.
        candidates: list[DueWorkItem] = []
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
            item = compute_due_work(
                as_of=stamp,
                event_id=event_id,
                event_start=event_start,
                slot_already_succeeded=succeeded,
                provider=PROVIDER_THE_ODDS_API,
                markets=markets_v,
                region=region_v,
                contract=sched,
                quota_state=None,
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
            candidates.append(item)
            item_rows.append(row)

        batches_map: dict[str, list[DueWorkItem]] = defaultdict(list)
        for item in candidates:
            assert item.batch_key is not None
            batches_map[item.batch_key].append(item)

        due_batches: list[tuple[str, list[DueWorkItem]]] = []
        for batch_key in sorted(batches_map):
            members = batches_map[batch_key]
            purpose = members[0].purpose or RequestPurpose.LIVE_ORDINARY
            budget = ledger.evaluate(
                estimated_cost=batch_cost_est,
                purpose=purpose,
                contract=sched,
            )
            if budget.state == QuotaBudgetState.EXHAUSTED:
                counts["exhausted"] += len(members)
                for member in members:
                    assert member.idempotency_key is not None
                    record_job_run(
                        session,
                        idempotency_key=member.idempotency_key,
                        job_name="snapshot-odds",
                        status="exhausted",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member.event_id,
                        mode=f"live:{member.window_name}",
                        as_of=stamp,
                        finished_at=completion,
                        estimated_cost=0,
                        window_name=member.window_name,
                        remaining_source=budget.remaining_source,
                        detail=budget.reason,
                    )
                continue
            if budget.state == QuotaBudgetState.DEFERRED:
                counts["deferred"] += len(members)
                for member in members:
                    assert member.idempotency_key is not None
                    record_job_run(
                        session,
                        idempotency_key=member.idempotency_key,
                        job_name="snapshot-odds",
                        status="deferred_quota",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member.event_id,
                        mode=f"live:{member.window_name}",
                        as_of=stamp,
                        finished_at=completion,
                        estimated_cost=0,
                        window_name=member.window_name,
                        remaining_source=budget.remaining_source,
                        detail=budget.reason,
                    )
                continue
            counts["due"] += len(members)
            ledger.reserve(batch_cost_est)
            due_batches.append((batch_key, members))

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
                batches=len(due_batches),
                upcoming_event_count=len(events),
                remaining_source=ledger.remaining_source,
                items=tuple(item_rows),
            )

        for batch_key, members in due_batches:
            counts["batches"] += 1
            representative = members[0]
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
                remaining_hdr = result.quota.get("x-requests-remaining")
                if remaining_hdr is not None:
                    ledger.apply_provider_remaining(
                        int(remaining_hdr), source="provider_header_after_request"
                    )
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
                            remaining_source=ledger.remaining_source,
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
                        estimated_cost=batch_cost_est if is_primary else 0,
                        actual_cost=actual if is_primary else None,
                        actual_cost_source=source if is_primary else None,
                        remaining_source=ledger.remaining_source,
                        snapshot_quote_ids=tuple(result.quote_ids),
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
                        estimated_cost=0,
                        window_name=member.window_name,
                        remaining_source=ledger.remaining_source,
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
        remaining_source=ledger.remaining_source,
        items=tuple(item_rows),
    )


__all__ = ["SnapshotOddsJobResult", "run_snapshot_odds_job"]
