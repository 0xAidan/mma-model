"""Sparse-first The Odds API historical backfill (DWCS-205)."""

from __future__ import annotations

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
)
from mma_model.odds.job_ledger import (
    JobLedgerDuplicate,
    find_successful_run,
    record_job_run,
)
from mma_model.odds.normalize import ensure_utc
from mma_model.odds.quota_budget import QuotaBudgetState, plan_request_budget
from mma_model.odds.schedule import (
    OddsScheduleContract,
    SnapshotCutoffError,
    assert_snapshot_at_or_before,
    compute_idempotency_key,
    load_default_schedule_contract,
    sparse_checkpoint_cutoff,
)
from mma_model.odds.snapshot import run_odds_snapshot
from mma_model.odds.types import PROVIDER_THE_ODDS_API


@dataclass(frozen=True)
class BackfillResult:
    series: str
    from_year: int
    attempted: int
    succeeded: int
    deferred: int
    failed: int
    duplicates: int
    skipped_before_history: int
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


def run_odds_backfill(
    session: Session,
    *,
    series: str = "dwcs",
    from_year: int = 2020,
    events: Sequence[Mapping[str, Any]],
    as_of: datetime,
    contract: OddsScheduleContract | None = None,
    markets: str | None = None,
    region: str | None = None,
    lock: OverlapProtection | None = None,
    lock_path: Path | None = None,
    offline_fixtures: bool = False,
    fixture_dir: Path | None = None,
    evaluation_contract_path: Path | None = None,
    execute: bool = True,
) -> BackfillResult:
    """Backfill sparse T−24h/T−6h/T−1h/close-proxy checkpoints from ``from_year``.

    ``evaluation_contract_path`` is accepted for the CLI contract flag and must
    exist when provided; modeling evaluation itself is out of scope.
    """
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
    markets_v = markets or sched.default_markets
    region_v = region or sched.default_region
    history_floor = sched.historical_available_from

    active_lock = lock or FileFlockLock(
        lock_path or Path("/tmp") / "mma-odds-backfill.lock"
    )

    cells: list[CoverageCell] = []
    attempted = succeeded = deferred = failed = duplicates = skipped = 0

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
                            market=markets_v,
                            time_label=checkpoint.name,
                            status="absent",
                            estimated_cost=0,
                            detail="before_historical_availability",
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
                attempted += 1
                prior = find_successful_run(session, idempotency_key=key)
                if prior is not None:
                    duplicates += 1
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
                            time_label=checkpoint.name,
                            status="observed",
                            estimated_cost=0,
                            actual_cost=prior.actual_cost,
                            detail="idempotent_replay",
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
                    contract=sched,
                )
                if budget.state == QuotaBudgetState.EXHAUSTED:
                    failed += 1
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
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
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
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        detail=budget.reason,
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
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
                            market=markets_v,
                            time_label=checkpoint.name,
                            status="absent",
                            estimated_cost=budget.estimated_cost,
                            detail="dry_run",
                        )
                    )
                    continue

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
                    # snapshot_at already validated inside run_odds_snapshot when enforced
                    snap = (
                        None
                        if result.snapshot_at is None
                        else ensure_utc(
                            datetime.fromisoformat(
                                result.snapshot_at.replace("Z", "+00:00")
                            ),
                            field="snapshot_at",
                        )
                    )
                    if snap is not None:
                        assert_snapshot_at_or_before(
                            snapshot_at=snap, requested_cutoff=cutoff
                        )
                    actual = result.quota.get("expected_cost")
                    actual_i = 0 if actual is None else int(actual)
                    try:
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
                            requested_cutoff=cutoff,
                            snapshot_at=snap,
                            estimated_cost=budget.estimated_cost,
                            actual_cost=actual_i,
                            detail=(
                                f"inserted={result.inserted};deduped={result.deduped}"
                            ),
                        )
                    except JobLedgerDuplicate:
                        duplicates += 1
                        cells.append(
                            CoverageCell(
                                card_id=card_id,
                                bookmaker_key="*",
                                market=markets_v,
                                time_label=checkpoint.name,
                                status="observed",
                                estimated_cost=budget.estimated_cost,
                                detail="race_duplicate",
                            )
                        )
                        continue
                    succeeded += 1
                    status = "observed" if result.quote_count or result.inserted else "absent"
                    # Empty historical response is absent, not failed.
                    if result.empty and result.quote_count == 0:
                        status = "absent"
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
                            time_label=checkpoint.name,
                            status=status,
                            estimated_cost=budget.estimated_cost,
                            actual_cost=actual_i,
                            detail="historical_snapshot",
                        )
                    )
                except SnapshotCutoffError as exc:
                    failed += 1
                    record_job_run(
                        session,
                        idempotency_key=key,
                        job_name="odds-backfill",
                        status="failed",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=event_id,
                        mode=mode,
                        as_of=stamp,
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        error_class="SnapshotCutoffError",
                        detail=str(exc)[:500],
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
                            time_label=checkpoint.name,
                            status="failed",
                            estimated_cost=budget.estimated_cost,
                            detail="cutoff_leakage",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    record_job_run(
                        session,
                        idempotency_key=key,
                        job_name="odds-backfill",
                        status="failed",
                        provider=PROVIDER_THE_ODDS_API,
                        region=region_v,
                        markets=markets_v,
                        event_id=event_id,
                        mode=mode,
                        as_of=stamp,
                        requested_cutoff=cutoff,
                        estimated_cost=budget.estimated_cost,
                        error_class=type(exc).__name__,
                        detail=str(exc)[:500],
                    )
                    cells.append(
                        CoverageCell(
                            card_id=card_id,
                            bookmaker_key="*",
                            market=markets_v,
                            time_label=checkpoint.name,
                            status="failed",
                            estimated_cost=budget.estimated_cost,
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
        failed=failed,
        duplicates=duplicates,
        skipped_before_history=skipped,
        coverage=coverage,
    )


__all__ = ["BackfillResult", "run_odds_backfill"]
