"""Sparse-first The Odds API historical backfill (DWCS-205)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mma_model.jobs.locking import FileFlockLock, OverlapProtection, hold_overlap_lock
from mma_model.odds.coverage_report import (
    CoverageCell,
    OddsCoverageReport,
    build_odds_coverage_report,
    cells_from_persisted_quotes,
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
    OddsScheduleContract,
    RequestPurpose,
    SnapshotCutoffError,
    assert_snapshot_at_or_before,
    compute_batch_key,
    compute_idempotency_key,
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
) -> BackfillResult:
    """Backfill sparse checkpoints with PIT fail-closed cutoffs and savepoints."""
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

    active_lock = lock or FileFlockLock(
        lock_path or Path("/tmp") / "mma-odds-backfill.lock"
    )

    cells: list[CoverageCell] = []
    attempted = succeeded = deferred = exhausted = failed = duplicates = 0
    skipped = skipped_future = batches = 0

    # Collect executable units keyed by sport-wide cutoff batch.
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)

    with hold_overlap_lock(active_lock):
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
                            estimated_cost=0,
                            detail="before_historical_availability",
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
                            estimated_cost=0,
                            detail="future_checkpoint_after_as_of",
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
                    cells.extend(
                        cells_from_persisted_quotes(
                            session,
                            card_id=card_id,
                            time_label=checkpoint.name,
                            market=market_token,
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            as_of=stamp,
                            estimated_cost=0,
                            actual_cost=None,
                            actual_cost_known=False,
                        )
                    )
                    continue

                budget = plan_request_budget(
                    session,
                    endpoint="historical_odds",
                    markets=markets_v,
                    regions=region_v,
                    provider=PROVIDER_THE_ODDS_API,
                    as_of=stamp,
                    purpose=RequestPurpose.BACKFILL,
                    contract=sched,
                    remaining_override=remaining_override,
                )
                if budget.state == QuotaBudgetState.EXHAUSTED:
                    exhausted += 1
                    record_job_run(
                        session,
                        idempotency_key=key,
                        job_name="odds-backfill",
                        status="exhausted",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=event_id,
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=market_token,
                            time_label=checkpoint.name,
                            status="failed",
                            estimated_cost=budget.estimated_cost,
                            detail="quota_exhausted",
                        )
                    )
                    continue
                if budget.state == QuotaBudgetState.DEFERRED:
                    deferred += 1
                    record_job_run(
                        session,
                        idempotency_key=key,
                        job_name="odds-backfill",
                        status="deferred_quota",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=event_id,
                        mode=mode,
                        as_of=stamp,
                        finished_at=completion,
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=market_token,
                            time_label=checkpoint.name,
                            status="deferred_quota",
                            estimated_cost=budget.estimated_cost,
                            detail=budget.reason,
                        )
                    )
                    continue

                if not execute:
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=market_token,
                            time_label=checkpoint.name,
                            status="absent",
                            estimated_cost=budget.estimated_cost,
                            detail="dry_run",
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
                        "estimated_cost": budget.estimated_cost,
                    }
                )

        for batch_key, members in pending.items():
            batches += 1
            cutoff = members[0]["cutoff"]
            mode = members[0]["mode"]
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
                for index, member in enumerate(members):
                    if find_successful_run(session, idempotency_key=member["key"]):
                        duplicates += 1
                        continue
                    is_primary = index == 0
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
                        estimated_cost=member["estimated_cost"] if is_primary else 0,
                        actual_cost=actual if is_primary else None,
                        actual_cost_source=source if is_primary else None,
                        detail=(
                            f"batch={batch_key};inserted={result.inserted};"
                            f"deduped={result.deduped}"
                        ),
                    )
                    succeeded += 1
                    cells.extend(
                        cells_from_persisted_quotes(
                            session,
                            card_id=member["card_id"],
                            time_label=member["checkpoint"],
                            market=market_token,
                            provider=PROVIDER_THE_ODDS_API,
                            region=region_v,
                            as_of=stamp,
                            estimated_cost=member["estimated_cost"] if is_primary else 0,
                            actual_cost=actual if is_primary and actual is not None else None,
                            actual_cost_known=bool(is_primary and actual is not None),
                        )
                    )
                nested.commit()
            except SnapshotCutoffError as exc:
                nested.rollback()
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
                        estimated_cost=member["estimated_cost"],
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
                            estimated_cost=member["estimated_cost"],
                            detail="cutoff_leakage",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                nested.rollback()
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
                        estimated_cost=member["estimated_cost"],
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
                            estimated_cost=member["estimated_cost"],
                            detail=type(exc).__name__,
                        )
                    )

    coverage = build_odds_coverage_report(
        series=series, as_of=stamp, cells=cells, contract=sched
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
        coverage=coverage,
    )


__all__ = ["BackfillResult", "run_odds_backfill"]
