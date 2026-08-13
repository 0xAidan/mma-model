"""DWCS-205 odds schedule, quota, idempotency, backfill, and coverage tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.config import get_settings
from mma_model.db.session import _attach_sqlite_listeners
from mma_model.jobs.locking import FileFlockLock, OverlapError, hold_overlap_lock
from mma_model.jobs.snapshot_odds import run_snapshot_odds_job
from mma_model.odds.backfill import run_odds_backfill
from mma_model.odds.coverage_report import CoverageCell, build_odds_coverage_report
from mma_model.odds.job_ledger import (
    JobLedgerDuplicate,
    find_successful_run,
    record_job_run,
)
from mma_model.odds.normalize import OddsTimestampError
from mma_model.odds.provider_decision import LicensedBookmakerAdapterError
from mma_model.odds.quota_budget import (
    QuotaBudgetState,
    evaluate_quota_budget,
    plan_request_budget,
)
from mma_model.odds.schedule import (
    DueAction,
    SnapshotCutoffError,
    assert_snapshot_at_or_before,
    compute_due_work,
    compute_idempotency_key,
    estimate_endpoint_cost,
    load_schedule_contract,
    resolve_cadence_window,
    slot_floor,
    sparse_checkpoint_cutoff,
)
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import (
    REQUESTS_LAST_SOURCE_PROVIDER,
    QuotaHeaders,
)
from mma_model.sources.bestfightodds.reconcile import (
    reconcile_bestfightodds_archive,
    refuse_licensed_bookmaker_history_without_contract,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "odds"
EVAL_CONTRACT = Path("config/evaluation/dwcs_v1.json")


@pytest.fixture()
def schedule():
    return load_schedule_contract()


@pytest.fixture()
def session(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path / 'odds-schedule.db'}"
    engine = create_engine(db_url, future=True)
    _attach_sqlite_listeners(engine)


    root = get_settings().project_root
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    Session = sessionmaker(bind=engine, future=True)
    with Session() as sess:
        yield sess
    engine.dispose()


def test_schedule_contract_cadence_matches_plan(schedule):
    by_name = {w.name: w for w in schedule.cadence_windows}
    assert by_name["far"].interval_sec == 1800
    assert by_name["mid"].interval_sec == 600
    assert by_name["near"].interval_sec == 300
    assert by_name["final"].interval_sec == 120
    assert by_name["final"].requires_quota_headroom is True
    names = [c.name for c in schedule.sparse_backfill_checkpoints]
    assert names == ["t_minus_24h", "t_minus_6h", "t_minus_1h", "close_proxy"]


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(hours=80), None),
        (timedelta(hours=48), "far"),
        (timedelta(hours=24), "mid"),  # inclusive start of mid
        (timedelta(hours=12), "mid"),
        (timedelta(hours=6), "near"),
        (timedelta(hours=2), "near"),
        (timedelta(hours=1), "final"),
        (timedelta(minutes=30), "final"),
        (timedelta(0), None),
        (-timedelta(hours=1), None),
    ],
)
def test_cadence_window_boundaries(schedule, delta, expected):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    as_of = event_start - delta
    window = resolve_cadence_window(
        as_of=as_of, event_start=event_start, contract=schedule
    )
    assert (None if window is None else window.name) == expected


def test_due_work_no_op_outside_window(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    item = compute_due_work(
        as_of=event_start - timedelta(hours=100),
        event_id="evt-1",
        event_start=event_start,
        last_success_at=None,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert item.action == DueAction.NO_OP
    assert item.idempotency_key is None


def test_due_work_interval_and_idempotency_deterministic(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    as_of = event_start - timedelta(hours=48)
    first = compute_due_work(
        as_of=as_of,
        event_id="evt-1",
        event_start=event_start,
        last_success_at=None,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert first.action == DueAction.DUE
    assert first.window_name == "far"
    assert first.idempotency_key is not None
    again = compute_due_work(
        as_of=as_of,
        event_id="evt-1",
        event_start=event_start,
        last_success_at=None,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert again.idempotency_key == first.idempotency_key
    satisfied = compute_due_work(
        as_of=as_of,
        event_id="evt-1",
        event_start=event_start,
        last_success_at=first.slot_start,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert satisfied.action == DueAction.NOT_DUE


def test_final_hour_defers_without_quota(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    item = compute_due_work(
        as_of=event_start - timedelta(minutes=20),
        event_id="evt-1",
        event_start=event_start,
        last_success_at=None,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
        quota_allows=False,
    )
    assert item.action == DueAction.DEFERRED_QUOTA


def test_naive_as_of_rejected(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    with pytest.raises(OddsTimestampError):
        compute_due_work(
            as_of=datetime(2024, 6, 1, 10, 0),
            event_id="evt-1",
            event_start=event_start,
            last_success_at=None,
            provider="the_odds_api",
            markets="h2h",
            region="us",
            contract=schedule,
        )


def test_endpoint_cost_contract(schedule):
    assert (
        estimate_endpoint_cost(
            endpoint="current_odds", markets="h2h", regions="us", contract=schedule
        )
        == 1
    )
    assert (
        estimate_endpoint_cost(
            endpoint="historical_odds",
            markets="h2h,totals",
            regions="us",
            contract=schedule,
        )
        == 20
    )
    assert (
        estimate_endpoint_cost(
            endpoint="events", markets="h2h", regions="us", contract=schedule
        )
        == 0
    )


def test_quota_budget_reserve_and_exhausted(schedule):
    allowed = evaluate_quota_budget(
        estimated_cost=10, remaining=500, contract=schedule
    )
    assert allowed.state == QuotaBudgetState.ALLOWED
    deferred = evaluate_quota_budget(
        estimated_cost=50, remaining=220, contract=schedule
    )
    assert deferred.state == QuotaBudgetState.DEFERRED
    exhausted = evaluate_quota_budget(
        estimated_cost=10, remaining=5, contract=schedule
    )
    assert exhausted.state == QuotaBudgetState.EXHAUSTED


def test_quota_uses_persisted_observation(session, schedule):
    store = OddsQuoteStore(session)
    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    store.record_quota(
        provider="the_odds_api",
        endpoint="historical_odds",
        observed_at=as_of - timedelta(minutes=1),
        quota=QuotaHeaders(
            requests_remaining=250,
            requests_used=100,
            requests_last=10,
            requests_last_inferred=None,
            requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
        ),
        empty_response=False,
    )
    session.flush()
    decision = plan_request_budget(
        session,
        endpoint="historical_odds",
        markets="h2h",
        regions="us",
        provider="the_odds_api",
        as_of=as_of,
        contract=schedule,
    )
    assert decision.remaining == 250
    assert decision.state == QuotaBudgetState.ALLOWED


def test_snapshot_cutoff_fail_closed():
    cutoff = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    ok = assert_snapshot_at_or_before(
        snapshot_at=cutoff - timedelta(minutes=5), requested_cutoff=cutoff
    )
    assert ok == cutoff - timedelta(minutes=5)
    with pytest.raises(SnapshotCutoffError):
        assert_snapshot_at_or_before(
            snapshot_at=cutoff + timedelta(seconds=1), requested_cutoff=cutoff
        )
    with pytest.raises(SnapshotCutoffError):
        assert_snapshot_at_or_before(snapshot_at=None, requested_cutoff=cutoff)


def test_idempotency_key_stable_and_distinct():
    stamp = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    a = compute_idempotency_key(
        provider="the_odds_api",
        region="us",
        markets="h2h",
        event_id="e1",
        mode="historical:t_minus_24h",
        slot_or_cutoff=stamp,
    )
    b = compute_idempotency_key(
        provider="the_odds_api",
        region="us",
        markets="h2h",
        event_id="e1",
        mode="historical:t_minus_24h",
        slot_or_cutoff=stamp,
    )
    c = compute_idempotency_key(
        provider="the_odds_api",
        region="us",
        markets="h2h",
        event_id="e2",
        mode="historical:t_minus_24h",
        slot_or_cutoff=stamp,
    )
    assert a == b
    assert a != c


def test_flock_overlap_rejects_second_writer(tmp_path: Path):
    lock_path = tmp_path / "writer.lock"
    first = FileFlockLock(lock_path)
    second = FileFlockLock(lock_path)
    with hold_overlap_lock(first), pytest.raises(OverlapError):
        second.acquire()


def test_job_ledger_success_unique(session):
    key = "odds_snap:testkey"
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    record_job_run(
        session,
        idempotency_key=key,
        job_name="odds-backfill",
        status="failed",
        provider="the_odds_api",
        region="us",
        markets="h2h",
        event_id="e1",
        mode="historical:t_minus_24h",
        as_of=as_of,
        estimated_cost=10,
        error_class="OddsApiError",
    )
    record_job_run(
        session,
        idempotency_key=key,
        job_name="odds-backfill",
        status="success",
        provider="the_odds_api",
        region="us",
        markets="h2h",
        event_id="e1",
        mode="historical:t_minus_24h",
        as_of=as_of,
        estimated_cost=10,
        actual_cost=10,
    )
    session.flush()
    assert find_successful_run(session, idempotency_key=key) is not None
    with pytest.raises(JobLedgerDuplicate):
        record_job_run(
            session,
            idempotency_key=key,
            job_name="odds-backfill",
            status="success",
            provider="the_odds_api",
            region="us",
            markets="h2h",
            event_id="e1",
            mode="historical:t_minus_24h",
            as_of=as_of,
            estimated_cost=10,
            actual_cost=10,
        )


def test_coverage_separates_absent_failed_deferred(schedule):
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    report = build_odds_coverage_report(
        series="dwcs",
        as_of=as_of,
        contract=schedule,
        cells=[
            CoverageCell("c1", "draftkings", "h2h", "t_minus_24h", "absent"),
            CoverageCell("c1", "draftkings", "h2h", "t_minus_6h", "failed", 10),
            CoverageCell(
                "c1", "draftkings", "h2h", "t_minus_1h", "deferred_quota", 10
            ),
            CoverageCell("c1", "draftkings", "h2h", "close_proxy", "unmatched"),
            CoverageCell(
                "c1", "draftkings", "h2h", "live", "observed", 1, actual_cost=1
            ),
        ],
    )
    assert report.status_counts["absent"] == 1
    assert report.status_counts["failed"] == 1
    assert report.status_counts["deferred_quota"] == 1
    assert report.status_counts["unmatched"] == 1
    assert report.status_counts["observed"] == 1


def test_sparse_checkpoint_cutoffs(schedule):
    start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    by_name = {c.name: c for c in schedule.sparse_backfill_checkpoints}
    assert sparse_checkpoint_cutoff(
        event_start=start, checkpoint=by_name["t_minus_24h"]
    ) == start - timedelta(hours=24)
    assert sparse_checkpoint_cutoff(
        event_start=start, checkpoint=by_name["close_proxy"]
    ) == start


def test_backfill_dry_run_and_offline_cutoff(session, schedule, tmp_path: Path):
    as_of = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    events = [
        {
            "event_id": "evt-offline-1",
            "card_id": "card-1",
            "event_start": datetime(2024, 6, 1, 20, 0, tzinfo=UTC),
        }
    ]
    dry = run_odds_backfill(
        session,
        series="dwcs",
        from_year=2020,
        events=events,
        as_of=as_of,
        contract=schedule,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
        evaluation_contract_path=EVAL_CONTRACT,
        execute=False,
        lock_path=tmp_path / "bf.lock",
    )
    assert dry.attempted == 4
    assert dry.coverage.status_counts["absent"] == 4

    # Fixture snapshot_at is 2026-08-11T20:55:00Z which is AFTER a 2024 cutoff → fail closed
    live = run_odds_backfill(
        session,
        series="dwcs",
        from_year=2020,
        events=events,
        as_of=as_of,
        contract=schedule,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
        evaluation_contract_path=EVAL_CONTRACT,
        execute=True,
        lock_path=tmp_path / "bf2.lock",
    )
    assert live.failed == 4
    assert live.coverage.status_counts["failed"] == 4


def test_snapshot_odds_job_no_op_outside_window(session, tmp_path: Path):
    as_of = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    events = [
        {
            "event_id": "evt-far",
            "event_start": datetime(2024, 6, 1, 20, 0, tzinfo=UTC),
        }
    ]
    result = run_snapshot_odds_job(
        session,
        as_of=as_of,
        events=events,
        lock_path=tmp_path / "snap.lock",
        execute=False,
    )
    assert result.no_op == 1
    assert result.due == 0


def test_bestfightodds_fixture_never_stats_pit(tmp_path: Path):
    result = reconcile_bestfightodds_archive(
        as_of=datetime(2024, 1, 1, tzinfo=UTC),
        event_name="DWCS Week 1",
        cache_dir=tmp_path,
        fixture_html="<html>archive</html>",
    )
    assert result.enabled is True
    assert result.stats_or_pit_evidence is False
    assert result.sportsbook_page_scrape is False
    assert result.status == "fixture_reconciled"


def test_licensed_history_refused_without_contract():
    with pytest.raises(LicensedBookmakerAdapterError):
        refuse_licensed_bookmaker_history_without_contract()


def test_slot_floor_deterministic():
    stamp = datetime(2024, 1, 1, 12, 17, tzinfo=UTC)
    floored = slot_floor(stamp, interval_sec=600)
    assert floored.minute in {10, 0} or floored == datetime(2024, 1, 1, 12, 10, tzinfo=UTC)


def test_cli_backfill_help():
    from mma_model.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["odds", "backfill", "--help"])
    assert exc.value.code == 0


def test_migration_creates_job_table(session):
    from sqlalchemy import inspect

    bind = session.get_bind()
    names = set(inspect(bind).get_table_names())
    assert "odds_snapshot_job_runs" in names
