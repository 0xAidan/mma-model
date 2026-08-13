"""Sparse-first The Odds API historical backfill (DWCS-205)."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.jobs.locking import FileFlockLock, OverlapProtection, hold_overlap_lock
from mma_model.odds.coverage_report import (
    BatchCostRecord,
    CoverageCell,
    OddsCoverageReport,
    PlannedWorkItem,
    build_odds_coverage_report,
    cells_from_snapshot_quotes,
    encode_availability_ids_for_ledger,
    encode_quote_ids_for_ledger,
)
from mma_model.odds.job_ledger import (
    BATCH_EVENT_ID,
    batch_idempotency_key,
    find_successful_batch_run,
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
    OddsScheduleContract,
    RequestPurpose,
    SnapshotCutoffError,
    assert_snapshot_at_or_before,
    compute_batch_key,
    compute_idempotency_key,
    estimate_endpoint_cost,
    load_default_schedule_contract,
    normalize_markets,
    normalize_regions,
    sparse_checkpoint_cutoff,
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
class BackfillResult:
    series: str
    from_year: int
    attempted: int
    succeeded: int
    deferred: int
    exhausted: int
    failed: int
    duplicates: int
    skipped_before_history: int
    skipped_future_checkpoint: int
    batches: int
    remaining_source: str
    coverage: OddsCoverageReport

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["coverage"] = self.coverage.as_dict()
        return payload


def _parse_event_start(value: Any) -> datetime:
    if isinstance(value, datetime):
        return ensure_utc(value, field="event_start")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text), field="event_start")


def _coerce_utc(value: datetime | None, *, field: str) -> datetime | None:
    """Normalize SQLite-naive timestamps from the ledger into aware UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return ensure_utc(value, field=field)


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


def run_odds_backfill(
    session: Session,
    *,
    series: str = "dwcs",
    from_year: int = 2020,
    events: Sequence[Mapping[str, Any]],
    as_of: datetime,
    finished_at: datetime | None = None,
    contract: OddsScheduleContract | None = None,
    markets: str | None = None,
    region: str | None = None,
    lock: OverlapProtection | None = None,
    lock_path: Path | None = None,
    offline_fixtures: bool = False,
    fixture_dir: Path | None = None,
    evaluation_contract_path: Path | None = None,
    execute: bool = True,
    remaining_override: int | None = None,
    allow_bootstrap: bool = True,
) -> BackfillResult:
    """Backfill sparse checkpoints with cumulative batch budgets and savepoints."""
    if evaluation_contract_path is not None and not Path(evaluation_contract_path).is_file():
        raise FileNotFoundError(
            f"evaluation contract not found: {evaluation_contract_path}"
        )

    sched = contract or load_default_schedule_contract()
    if int(from_year) < int(sched.backfill_from_year):
        raise ValueError(
            f"from_year {from_year} precedes schedule backfill_from_year "
            f"{sched.backfill_from_year}"
        )
    stamp = ensure_utc(as_of, field="as_of")
    completion = ensure_utc(finished_at or stamp, field="finished_at")
    markets_v = normalize_markets(markets or sched.default_markets)
    region_v = normalize_regions(region or sched.default_region)
    history_floor = sched.historical_available_from
    market_token = markets_v.split(",")[0]
    batch_cost_est = estimate_endpoint_cost(
        endpoint="historical_odds",
        markets=markets_v,
        regions=region_v,
        contract=sched,
    )

    active_lock = lock or FileFlockLock(
        lock_path or Path("/tmp") / "mma-odds-backfill.lock"
    )

    cells: list[CoverageCell] = []
    planned: list[PlannedWorkItem] = []
    batch_costs: list[BatchCostRecord] = []
    attempted = succeeded = deferred = exhausted = failed = duplicates = 0
    skipped = skipped_future = batches = 0
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)

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

        for event in events:
            event_id = str(event["event_id"])
            card_id = str(event.get("card_id") or event_id)
            event_start = _parse_event_start(event["event_start"])
            if event_start.year < int(from_year):
                skipped += 1
                continue

            for checkpoint in sched.sparse_backfill_checkpoints:
                cutoff = sparse_checkpoint_cutoff(
                    event_start=event_start, checkpoint=checkpoint
                )
                if cutoff < history_floor:
                    skipped += 1
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=market_token,
                            time_label=checkpoint.name,
                            status="absent",
                            detail="before_historical_availability",
                            match_reason="before_historical_availability",
                        )
                    )
                    continue
                if cutoff > stamp:
                    skipped_future += 1
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=market_token,
                            time_label=checkpoint.name,
                            status="absent",
                            detail="future_checkpoint_after_as_of",
                            match_reason="future_checkpoint",
                        )
                    )
                    continue

                mode = f"historical:{checkpoint.name}"
                key = compute_idempotency_key(
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    event_id=event_id,
                    mode=mode,
                    slot_or_cutoff=cutoff,
                    key_version=sched.idempotency_key_version,
                )
                batch_key = compute_batch_key(
                    provider=PROVIDER_THE_ODDS_API,
                    region=region_v,
                    markets=markets_v,
                    mode=mode,
                    slot_or_cutoff=cutoff,
                    key_version=sched.idempotency_key_version,
                )
                attempted += 1
                if slot_succeeded(session, idempotency_key=key):
                    duplicates += 1
                    prior = find_successful_run(session, idempotency_key=key)
                    prior_ids = (
                        []
                        if prior is None
                        else json_loads_ids(prior.snapshot_quote_ids)
                    )
                    prior_avail = (
                        []
                        if prior is None
                        else json_loads_ids(prior.snapshot_availability_ids)
                    )
                    cells.extend(
                        cells_from_snapshot_quotes(
                            session,
                            card_id=card_id,
                            time_label=checkpoint.name,
                            market=market_token,
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            match_reconciliation_as_of=stamp,
                            quote_ids=prior_ids,
                            quote_snapshot_at=_coerce_utc(
                                prior.snapshot_at if prior else None,
                                field="snapshot_at",
                            ),
                            requested_cutoff=_coerce_utc(
                                prior.requested_cutoff if prior else cutoff,
                                field="requested_cutoff",
                            ),
                            availability_observation_ids=prior_avail,
                            include_unassigned=True,
                        )
                    )
                    continue

                # Late member of an already-paid batch: reuse without re-spend.
                batch_prior = find_successful_batch_run(session, batch_key=batch_key)
                if batch_prior is not None:
                    duplicates += 1
                    prior_ids = json_loads_ids(batch_prior.snapshot_quote_ids)
                    prior_avail = json_loads_ids(batch_prior.snapshot_availability_ids)
                    batch_snap = _coerce_utc(
                        batch_prior.snapshot_at, field="snapshot_at"
                    )
                    if not slot_succeeded(session, idempotency_key=key):
                        record_job_run(
                            session,
                            idempotency_key=key,
                            job_name="odds-backfill",
                            status="success",
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            markets=markets_v,
                            event_id=event_id,
                            mode=mode,
                            as_of=stamp,
                            finished_at=completion,
                            requested_cutoff=cutoff,
                            snapshot_at=batch_snap,
                            estimated_cost=0,
                            actual_cost=None,
                            actual_cost_source=None,
                            remaining_source=batch_prior.remaining_source,
                            snapshot_quote_ids=prior_ids,
                            snapshot_availability_ids=prior_avail,
                            detail=(
                                f"batch_reuse={batch_key};"
                                f"{encode_quote_ids_for_ledger(prior_ids)};"
                                f"{encode_availability_ids_for_ledger(prior_avail)}"
                            ),
                        )
                        succeeded += 1
                    cells.extend(
                        cells_from_snapshot_quotes(
                            session,
                            card_id=card_id,
                            time_label=checkpoint.name,
                            market=market_token,
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            match_reconciliation_as_of=stamp,
                            quote_ids=prior_ids,
                            quote_snapshot_at=batch_snap,
                            requested_cutoff=cutoff,
                            availability_observation_ids=prior_avail,
                            include_unassigned=True,
                        )
                    )
                    continue

                pending[batch_key].append(
                    {
                        "event_id": event_id,
                        "card_id": card_id,
                        "checkpoint": checkpoint.name,
                        "cutoff": cutoff,
                        "mode": mode,
                        "key": key,
                        "batch_key": batch_key,
                    }
                )

        for batch_key in sorted(pending):
            members = pending[batch_key]
            cutoff = members[0]["cutoff"]
            mode = members[0]["mode"]
            # Re-evaluate immediately before each batch against newest remaining.
            budget = ledger.evaluate(
                estimated_cost=batch_cost_est,
                purpose=RequestPurpose.BACKFILL,
                contract=sched,
            )
            if budget.state == QuotaBudgetState.EXHAUSTED:
                exhausted += len(members)
                for member in members:
                    record_job_run(
                        session,
                        idempotency_key=member["key"],
                        job_name="odds-backfill",
                        status="exhausted",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member["event_id"],
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=0,
                        remaining_source=budget.remaining_source,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=member["card_id"],
                            bookmaker_key="*",
                            market=market_token,
                            time_label=member["checkpoint"],
                            status="failed",
                            detail="quota_exhausted",
                            match_reason="quota_exhausted",
                        )
                    )
                continue
            if budget.state == QuotaBudgetState.DEFERRED:
                deferred += len(members)
                for member in members:
                    record_job_run(
                        session,
                        idempotency_key=member["key"],
                        job_name="odds-backfill",
                        status="deferred_quota",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member["event_id"],
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=0,
                        remaining_source=budget.remaining_source,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=member["card_id"],
                            bookmaker_key="*",
                            market=market_token,
                            time_label=member["checkpoint"],
                            status="deferred_quota",
                            detail=budget.reason,
                            match_reason="quota_deferred",
                        )
                    )
                continue

            if not execute:
                for member in members:
                    planned.append(
                        PlannedWorkItem(
                            card_id=member["card_id"],
                            time_label=member["checkpoint"],
                            market=market_token,
                            estimated_cost=batch_cost_est if member is members[0] else 0,
                            batch_key=batch_key,
                            detail="dry_run_not_requested",
                        )
                    )
                # Reserve so subsequent planned batches see cumulative spend.
                ledger.reserve(batch_cost_est)
                batches += 1
                continue

            ledger.reserve(batch_cost_est)
            batches += 1
            nested = session.begin_nested()
            try:
                result = run_odds_snapshot(
                    session,
                    series=series,
                    provider=PROVIDER_THE_ODDS_API,
                    markets=markets_v,
                    regions=region_v,
                    historical_date=cutoff,
                    fixture_dir=fixture_dir,
                    offline_fixtures=offline_fixtures,
                    observed_at=stamp,
                    enforce_historical_cutoff=True,
                )
                snap = (
                    None
                    if result.snapshot_at is None
                    else ensure_utc(
                        datetime.fromisoformat(result.snapshot_at.replace("Z", "+00:00")),
                        field="snapshot_at",
                    )
                )
                assert_snapshot_at_or_before(
                    snapshot_at=snap, requested_cutoff=cutoff, as_of=stamp
                )
                actual, source = _actual_cost_fields(result.quota)
                quote_ids = tuple(result.quote_ids)
                avail_ids = tuple(result.availability_observation_ids)
                remaining_hdr = result.quota.get("x-requests-remaining")
                if remaining_hdr is not None:
                    ledger.apply_provider_remaining(
                        int(remaining_hdr), source="provider_header_after_request"
                    )
                else:
                    # Keep this batch's reservation as spent when header missing.
                    pass
                batch_costs.append(
                    BatchCostRecord(
                        batch_key=batch_key,
                        estimated_cost=batch_cost_est,
                        actual_cost=actual if actual is not None else None,
                        actual_cost_known=actual is not None,
                        actual_cost_source=source if actual is not None else "missing",
                        remaining_source=budget.remaining_source,
                        detail=f"members={len(members)}",
                    )
                )
                if find_successful_batch_run(session, batch_key=batch_key) is None:
                    record_job_run(
                        session,
                        idempotency_key=batch_idempotency_key(batch_key),
                        job_name="odds-backfill",
                        status="success",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=BATCH_EVENT_ID,
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        snapshot_at=snap,
                        estimated_cost=batch_cost_est,
                        actual_cost=actual,
                        actual_cost_source=source if actual is not None else "missing",
                        remaining_source=budget.remaining_source,
                        snapshot_quote_ids=quote_ids,
                        snapshot_availability_ids=avail_ids,
                        detail=(
                            f"batch={batch_key};members={len(members)};"
                            f"{encode_quote_ids_for_ledger(quote_ids)};"
                            f"{encode_availability_ids_for_ledger(avail_ids)}"
                        ),
                    )
                for index, member in enumerate(members):
                    if find_successful_run(session, idempotency_key=member["key"]):
                        duplicates += 1
                        continue
                    record_job_run(
                        session,
                        idempotency_key=member["key"],
                        job_name="odds-backfill",
                        status="success",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member["event_id"],
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        snapshot_at=snap,
                        estimated_cost=0,
                        actual_cost=None,
                        actual_cost_source=None,
                        remaining_source=budget.remaining_source,
                        snapshot_quote_ids=quote_ids,
                        snapshot_availability_ids=avail_ids,
                        detail=(
                            f"batch={batch_key};inserted={result.inserted};"
                            f"deduped={result.deduped};"
                            f"{encode_quote_ids_for_ledger(quote_ids)};"
                            f"{encode_availability_ids_for_ledger(avail_ids)}"
                        ),
                    )
                    succeeded += 1
                    cells.extend(
                        cells_from_snapshot_quotes(
                            session,
                            card_id=member["card_id"],
                            time_label=member["checkpoint"],
                            market=market_token,
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            match_reconciliation_as_of=stamp,
                            quote_ids=quote_ids,
                            quote_snapshot_at=snap,
                            requested_cutoff=cutoff,
                            availability_observation_ids=avail_ids,
                            include_unassigned=(index == 0),
                        )
                    )
                nested.commit()
            except SnapshotCutoffError as exc:
                nested.rollback()
                ledger.release(batch_cost_est)
                failed += len(members)
                for member in members:
                    record_job_run(
                        session,
                        idempotency_key=member["key"],
                        job_name="odds-backfill",
                        status="failed",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member["event_id"],
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=0,
                        remaining_source=budget.remaining_source,
                        error_class="SnapshotCutoffError",
                        detail=str(exc)[:500],
                    )
                    cells.append(
                        CoverageCell(
                            card_id=member["card_id"],
                            bookmaker_key="*",
                            market=market_token,
                            time_label=member["checkpoint"],
                            status="failed",
                            detail="cutoff_leakage",
                            match_reason="cutoff_leakage",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                nested.rollback()
                ledger.release(batch_cost_est)
                failed += len(members)
                for member in members:
                    record_job_run(
                        session,
                        idempotency_key=member["key"],
                        job_name="odds-backfill",
                        status="failed",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=member["event_id"],
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=0,
                        remaining_source=budget.remaining_source,
                        error_class=type(exc).__name__,
                        detail=str(exc)[:500],
                    )
                    cells.append(
                        CoverageCell(
                            card_id=member["card_id"],
                            bookmaker_key="*",
                            market=market_token,
                            time_label=member["checkpoint"],
                            status="failed",
                            detail=type(exc).__name__,
                            match_reason="exception",
                        )
                    )

    coverage = build_odds_coverage_report(
        series=series,
        as_of=stamp,
        cells=cells,
        batch_costs=batch_costs,
        planned=planned,
        contract=sched,
        match_reconciliation_as_of=stamp,
        match_clock_kind="retrospective_reconciliation",
    )
    return BackfillResult(
        series=series,
        from_year=int(from_year),
        attempted=attempted,
        succeeded=succeeded,
        deferred=deferred,
        exhausted=exhausted,
        failed=failed,
        duplicates=duplicates,
        skipped_before_history=skipped,
        skipped_future_checkpoint=skipped_future,
        batches=batches,
        remaining_source=ledger.remaining_source,
        coverage=coverage,
    )


def json_loads_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [int(x) for x in parsed]


__all__ = ["BackfillResult", "run_odds_backfill"]
