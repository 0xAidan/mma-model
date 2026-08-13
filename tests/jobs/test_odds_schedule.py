"""DWCS-205 odds schedule, quota, idempotency, backfill, and coverage tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from mma_model.config import get_settings
from mma_model.db.session import _attach_sqlite_listeners
from mma_model.db.tables.core import CanonicalEvent
from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.db.tables.odds_jobs import OddsSnapshotJobRun
from mma_model.jobs.locking import FileFlockLock, OverlapError, hold_overlap_lock
from mma_model.jobs.snapshot_odds import run_snapshot_odds_job
from mma_model.odds.backfill import run_odds_backfill
from mma_model.odds.coverage_report import CoverageCell, build_odds_coverage_report
from mma_model.odds.events_for_schedule import (
    classify_event_status_for_tests,
    load_dwcs_schedule_events,
    load_upcoming_dwcs_events_from_db,
)
from mma_model.odds.job_ledger import (
    JobLedgerDuplicate,
    JobLedgerTimeError,
    find_successful_run,
    record_job_run,
)
from mma_model.odds.normalize import OddsTimestampError
from mma_model.odds.provider_decision import LicensedBookmakerAdapterError
from mma_model.odds.quota_budget import (
    QuotaBudgetState,
    cost_from_quota_headers,
    evaluate_quota_budget,
    plan_request_budget,
)
from mma_model.odds.schedule import (
    PINNED_SCHEDULE_CONTRACT_HASH,
    DueAction,
    RequestPurpose,
    ScheduleContractError,
    SnapshotCutoffError,
    assert_plan_visible_schedule_bytes_match,
    assert_snapshot_at_or_before,
    compute_batch_key,
    compute_due_work,
    compute_idempotency_key,
    estimate_endpoint_cost,
    load_schedule_contract,
    normalize_markets,
    normalize_regions,
    resolve_cadence_window,
    slot_floor,
    slot_floor_in_window,
    sparse_checkpoint_cutoff,
    window_bounds,
)
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import (
    REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    REQUESTS_LAST_SOURCE_MISSING,
    REQUESTS_LAST_SOURCE_PROVIDER,
    QuotaHeaders,
)
from mma_model.sources.bestfightodds.reconcile import (
    BestFightOddsPolicyError,
    reconcile_bestfightodds_archive,
    refuse_licensed_bookmaker_history_without_contract,
    validate_bestfightodds_archive_url,
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


def _seed_event(
    session,
    *,
    event_id: str,
    start: datetime,
    status: str = "scheduled",
    series: str = "dwcs",
    name: str = "DWCS Test",
) -> None:
    session.add(
        CanonicalEvent(
            id=event_id,
            name=name,
            series=series,
            status=status,
            scheduled_start_at=start,
        )
    )
    session.flush()


def test_schedule_contract_pinned_and_deep(schedule):
    assert schedule.content_hash == PINNED_SCHEDULE_CONTRACT_HASH
    assert_plan_visible_schedule_bytes_match()
    by_name = {w.name: w for w in schedule.cadence_windows}
    assert by_name["far"].interval_sec == 1800
    assert by_name["mid"].interval_sec == 600
    assert by_name["near"].interval_sec == 300
    assert by_name["final"].interval_sec == 120
    assert by_name["final"].requires_quota_headroom is True
    names = [c.name for c in schedule.sparse_backfill_checkpoints]
    assert names == ["t_minus_24h", "t_minus_6h", "t_minus_1h", "close_proxy"]
    with pytest.raises(TypeError):
        schedule.quota.cost_fixed["events"] = 9  # type: ignore[index]
    with pytest.raises(ScheduleContractError):
        load_schedule_contract(
            raw_bytes=schedule.raw_bytes.replace(b"1.1.0", b"9.9.9", 1)
        )


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(hours=80), None),
        (timedelta(hours=48), "far"),
        (timedelta(hours=24), "mid"),
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
        slot_already_succeeded=False,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert item.action == DueAction.NO_OP
    assert item.idempotency_key is None


def test_window_anchored_first_slot_due_despite_prior_window_success(schedule):
    """Epoch anchoring skipped first polls; window anchoring must not."""
    event_start = datetime(2024, 6, 1, 20, 7, tzinfo=UTC)  # off-grid vs epoch
    mid = next(w for w in schedule.cadence_windows if w.name == "mid")
    window_start, _ = window_bounds(event_start=event_start, window=mid)
    # First instant of mid window.
    as_of = window_start
    first = compute_due_work(
        as_of=as_of,
        event_id="evt-1",
        event_start=event_start,
        slot_already_succeeded=False,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert first.action == DueAction.DUE
    assert first.window_name == "mid"
    assert first.slot_start == window_start
    # Prior-window success must not suppress this first mid slot.
    again = compute_due_work(
        as_of=as_of,
        event_id="evt-1",
        event_start=event_start,
        slot_already_succeeded=False,
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
        slot_already_succeeded=True,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
    )
    assert satisfied.action == DueAction.NOT_DUE


def test_slot_floor_in_window_exact_boundaries(schedule):
    window_start = datetime(2024, 6, 1, 0, 7, tzinfo=UTC)
    # Exact window start is slot 0.
    assert slot_floor_in_window(
        window_start, window_start=window_start, interval_sec=600
    ) == window_start
    # Just before next boundary stays on first slot.
    assert slot_floor_in_window(
        window_start + timedelta(seconds=599),
        window_start=window_start,
        interval_sec=600,
    ) == window_start
    assert slot_floor_in_window(
        window_start + timedelta(seconds=600),
        window_start=window_start,
        interval_sec=600,
    ) == window_start + timedelta(seconds=600)
    # Off-grid vs Unix epoch: epoch floor would differ from window floor.
    stamp = window_start + timedelta(minutes=17)
    window_slot = slot_floor_in_window(
        stamp, window_start=window_start, interval_sec=600
    )
    epoch_slot = slot_floor(stamp, interval_sec=600)
    assert window_slot != epoch_slot
    assert window_slot == window_start + timedelta(minutes=10)


def test_transition_t24_t6_t1_first_slots_due(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    for name, offset in (("mid", 24), ("near", 6), ("final", 1)):
        as_of = event_start - timedelta(hours=offset)
        item = compute_due_work(
            as_of=as_of,
            event_id="evt-1",
            event_start=event_start,
            slot_already_succeeded=False,
            provider="the_odds_api",
            markets="h2h",
            region="us",
            contract=schedule,
        )
        assert item.action == DueAction.DUE
        assert item.window_name == name


def test_final_hour_defers_and_exhausts(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    deferred = compute_due_work(
        as_of=event_start - timedelta(minutes=20),
        event_id="evt-1",
        event_start=event_start,
        slot_already_succeeded=False,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
        quota_state="deferred",
    )
    assert deferred.action == DueAction.DEFERRED_QUOTA
    exhausted = compute_due_work(
        as_of=event_start - timedelta(minutes=20),
        event_id="evt-1",
        event_start=event_start,
        slot_already_succeeded=False,
        provider="the_odds_api",
        markets="h2h",
        region="us",
        contract=schedule,
        quota_state="exhausted",
    )
    assert exhausted.action == DueAction.EXHAUSTED_QUOTA


def test_naive_as_of_rejected(schedule):
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    with pytest.raises(OddsTimestampError):
        compute_due_work(
            as_of=datetime(2024, 6, 1, 10, 0),
            event_id="evt-1",
            event_start=event_start,
            slot_already_succeeded=False,
            provider="the_odds_api",
            markets="h2h",
            region="us",
            contract=schedule,
        )


def test_endpoint_cost_and_market_region_normalize(schedule):
    assert (
        estimate_endpoint_cost(
            endpoint="current_odds", markets="h2h", regions="us", contract=schedule
        )
        == 1
    )
    assert (
        estimate_endpoint_cost(
            endpoint="historical_odds",
            markets="totals,h2h",
            regions="uk,us",
            contract=schedule,
        )
        == 40
    )
    assert normalize_markets("totals,h2h") == "h2h,totals"
    assert normalize_regions("uk,us") == "uk,us"
    assert (
        estimate_endpoint_cost(endpoint="events", markets=None, regions=None, contract=schedule)
        == 0
    )
    with pytest.raises(ValueError):
        normalize_markets("h2h,h2h")
    with pytest.raises(ValueError):
        normalize_markets("")


def test_quota_missing_remaining_fail_closed(schedule):
    decision = evaluate_quota_budget(
        estimated_cost=1,
        remaining=None,
        purpose=RequestPurpose.LIVE_ORDINARY,
        contract=schedule,
    )
    assert decision.state == QuotaBudgetState.DEFERRED
    assert decision.reason == "missing_remaining_fail_closed"
    override = evaluate_quota_budget(
        estimated_cost=1,
        remaining=None,
        purpose=RequestPurpose.LIVE_ORDINARY,
        contract=schedule,
        remaining_source="override_bounded",
        allow_missing_remaining_override=True,
    )
    assert override.state == QuotaBudgetState.ALLOWED


def test_quota_malformed_and_stale_observation(session, schedule):
    store = OddsQuoteStore(session)
    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    store.record_quota(
        provider="the_odds_api",
        endpoint="current_odds",
        observed_at=as_of + timedelta(hours=1),  # after as_of → invisible
        quota=QuotaHeaders(
            requests_remaining=500,
            requests_used=1,
            requests_last=1,
            requests_last_inferred=None,
            requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
        ),
        empty_response=False,
    )
    session.flush()
    missing = plan_request_budget(
        session,
        endpoint="current_odds",
        markets="h2h",
        regions="us",
        provider="the_odds_api",
        as_of=as_of,
        purpose=RequestPurpose.LIVE_ORDINARY,
        contract=schedule,
    )
    assert missing.state == QuotaBudgetState.DEFERRED
    assert missing.remaining_source == "missing_observation"

    # DB CHECKs reject negative remaining; still prove loader fail-closes on it.
    fake_row = SimpleNamespace(
        requests_remaining=-3, observed_at=as_of - timedelta(minutes=1)
    )
    with patch.object(session, "scalar", return_value=fake_row):
        from mma_model.odds.quota_budget import latest_remaining_from_observations

        remaining, source = latest_remaining_from_observations(
            session, provider="the_odds_api", as_of=as_of, contract=schedule
        )
    assert remaining is None and source == "malformed_negative_remaining"
    malformed = evaluate_quota_budget(
        estimated_cost=1,
        remaining=None,
        purpose=RequestPurpose.LIVE_ORDINARY,
        contract=schedule,
        remaining_source=source,
    )
    assert malformed.state == QuotaBudgetState.DEFERRED


def test_quota_reserve_purpose_boundaries(schedule):
    # Ordinary live preserves reserve=200: remaining 205, cost 10 → spendable 5 → deferred
    deferred = evaluate_quota_budget(
        estimated_cost=10,
        remaining=205,
        purpose=RequestPurpose.LIVE_ORDINARY,
        contract=schedule,
    )
    assert deferred.state == QuotaBudgetState.DEFERRED
    # Final-hour may spend reserve: remaining 205, cost 10 → allowed
    final_ok = evaluate_quota_budget(
        estimated_cost=10,
        remaining=205,
        purpose=RequestPurpose.LIVE_FINAL,
        contract=schedule,
    )
    assert final_ok.state == QuotaBudgetState.ALLOWED
    assert final_ok.reason == "within_remaining_including_reserve"
    # Final-hour still cannot exceed actual remaining
    final_ex = evaluate_quota_budget(
        estimated_cost=10,
        remaining=5,
        purpose=RequestPurpose.LIVE_FINAL,
        contract=schedule,
    )
    assert final_ex.state == QuotaBudgetState.EXHAUSTED
    backfill = evaluate_quota_budget(
        estimated_cost=10,
        remaining=205,
        purpose=RequestPurpose.BACKFILL,
        contract=schedule,
    )
    assert backfill.state == QuotaBudgetState.DEFERRED


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
        purpose=RequestPurpose.BACKFILL,
        contract=schedule,
    )
    assert decision.remaining == 250
    assert decision.state == QuotaBudgetState.ALLOWED


def test_actual_cost_provenance_none_vs_zero():
    missing = QuotaHeaders(
        requests_remaining=10,
        requests_used=1,
        requests_last=None,
        requests_last_inferred=None,
        requests_last_source=REQUESTS_LAST_SOURCE_MISSING,
    )
    assert cost_from_quota_headers(missing) is None
    inferred = QuotaHeaders(
        requests_remaining=10,
        requests_used=1,
        requests_last=None,
        requests_last_inferred=0,
        requests_last_source=REQUESTS_LAST_SOURCE_INFERRED_EMPTY,
    )
    assert cost_from_quota_headers(inferred) == 0
    provider = QuotaHeaders(
        requests_remaining=10,
        requests_used=1,
        requests_last=3,
        requests_last_inferred=None,
        requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
    )
    assert cost_from_quota_headers(provider) == 3


def test_snapshot_cutoff_and_as_of_pit():
    cutoff = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    as_of = datetime(2024, 1, 2, tzinfo=UTC)
    ok = assert_snapshot_at_or_before(
        snapshot_at=cutoff - timedelta(minutes=5),
        requested_cutoff=cutoff,
        as_of=as_of,
    )
    assert ok == cutoff - timedelta(minutes=5)
    with pytest.raises(SnapshotCutoffError):
        assert_snapshot_at_or_before(
            snapshot_at=cutoff + timedelta(seconds=1),
            requested_cutoff=cutoff,
            as_of=as_of,
        )
    with pytest.raises(SnapshotCutoffError):
        assert_snapshot_at_or_before(
            snapshot_at=cutoff,
            requested_cutoff=as_of + timedelta(hours=1),
            as_of=as_of,
        )
    with pytest.raises(SnapshotCutoffError):
        assert_snapshot_at_or_before(snapshot_at=None, requested_cutoff=cutoff)


def test_idempotency_and_batch_keys_stable():
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
    batch = compute_batch_key(
        provider="the_odds_api",
        region="us",
        markets="h2h",
        mode="live:far",
        slot_or_cutoff=stamp,
    )
    assert "e1" not in batch


def test_flock_overlap_rejects_second_writer(tmp_path: Path):
    lock_path = tmp_path / "writer.lock"
    first = FileFlockLock(lock_path)
    second = FileFlockLock(lock_path)
    with hold_overlap_lock(first), pytest.raises(OverlapError):
        second.acquire()


def test_job_ledger_explicit_finished_at(session):
    key = "odds_snap:testkey"
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    finished = as_of + timedelta(minutes=5)
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
        finished_at=finished,
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
        finished_at=finished,
        estimated_cost=10,
        actual_cost=10,
        actual_cost_source="provider",
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
            finished_at=finished,
            estimated_cost=10,
            actual_cost=10,
            actual_cost_source="provider",
        )
    with pytest.raises(JobLedgerTimeError):
        record_job_run(
            session,
            idempotency_key="odds_snap:badtime",
            job_name="odds-backfill",
            status="failed",
            provider="the_odds_api",
            region="us",
            markets="h2h",
            event_id="e1",
            mode="historical:t_minus_24h",
            as_of=as_of,
            finished_at=as_of - timedelta(seconds=1),
            estimated_cost=0,
        )
    with pytest.raises(ValueError):
        record_job_run(
            session,
            idempotency_key="odds_snap:nocostsrc",
            job_name="odds-backfill",
            status="success",
            provider="the_odds_api",
            region="us",
            markets="h2h",
            event_id="e1",
            mode="historical:t_minus_24h",
            as_of=as_of,
            finished_at=finished,
            estimated_cost=1,
            actual_cost=1,
        )


def test_coverage_separates_statuses_and_batch_costs(schedule):
    from mma_model.odds.coverage_report import BatchCostRecord, PlannedWorkItem

    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    report = build_odds_coverage_report(
        series="dwcs",
        as_of=as_of,
        contract=schedule,
        cells=[
            CoverageCell("c1", "draftkings", "h2h", "t_minus_24h", "absent"),
            CoverageCell("c1", "draftkings", "h2h", "t_minus_6h", "failed"),
            CoverageCell(
                "c1", "draftkings", "h2h", "t_minus_1h", "deferred_quota"
            ),
            CoverageCell("c1", "draftkings", "h2h", "close_proxy", "unmatched"),
            CoverageCell(
                "c1",
                "draftkings",
                "h2h",
                "live",
                "observed",
                matched=True,
                quote_count=2,
                quote_eligible_count=1,
                match_reason="alias_effective_maps_to_card_bout",
            ),
            CoverageCell(
                "c1",
                "fanduel",
                "h2h",
                "live",
                "observed",
                matched=True,
                quote_count=1,
                quote_eligible_count=0,
                detail="collection_observed_not_value_ready",
            ),
        ],
        batch_costs=[
            BatchCostRecord(
                batch_key="b1",
                estimated_cost=10,
                actual_cost=10,
                actual_cost_known=True,
                actual_cost_source="provider",
                remaining_source="persisted_quota_observation",
            ),
            BatchCostRecord(
                batch_key="b2",
                estimated_cost=10,
                actual_cost=None,
                actual_cost_known=False,
                actual_cost_source="missing",
                remaining_source="persisted_quota_observation",
            ),
        ],
        planned=[
            PlannedWorkItem(
                card_id="c1",
                time_label="t_minus_24h",
                market="h2h",
                estimated_cost=10,
                batch_key="b3",
            )
        ],
    )
    assert report.status_counts["absent"] == 1
    assert report.status_counts["failed"] == 1
    assert report.status_counts["deferred_quota"] == 1
    assert report.status_counts["unmatched"] == 1
    assert report.status_counts["observed"] == 2
    assert report.known_actual_cost_total == 10
    assert report.unknown_actual_cost_batches == 1
    assert report.estimated_cost_total == 20
    assert report.collection_only is True
    assert report.as_dict()["value_ready"] is False
    assert len(report.planned) == 1


def test_sparse_checkpoint_cutoffs(schedule):
    start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    by_name = {c.name: c for c in schedule.sparse_backfill_checkpoints}
    assert sparse_checkpoint_cutoff(
        event_start=start, checkpoint=by_name["t_minus_24h"]
    ) == start - timedelta(hours=24)
    assert sparse_checkpoint_cutoff(
        event_start=start, checkpoint=by_name["close_proxy"]
    ) == start


def test_backfill_skips_future_checkpoints_and_pit(session, schedule, tmp_path: Path):
    as_of = datetime(2024, 5, 31, 12, 0, tzinfo=UTC)  # before event; some cuts future
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
        finished_at=as_of,
        contract=schedule,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
        evaluation_contract_path=EVAL_CONTRACT,
        execute=False,
        lock_path=tmp_path / "bf.lock",
        remaining_override=10_000,
    )
    assert dry.skipped_future_checkpoint >= 1
    # Historical run after event with fixture snapshot after cutoffs → fail closed
    late = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    live = run_odds_backfill(
        session,
        series="dwcs",
        from_year=2020,
        events=events,
        as_of=late,
        finished_at=late,
        contract=schedule,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
        evaluation_contract_path=EVAL_CONTRACT,
        execute=True,
        lock_path=tmp_path / "bf2.lock",
        remaining_override=10_000,
    )
    assert live.failed == 4
    assert live.coverage.status_counts["failed"] == 4


def test_live_upcoming_from_db_not_frozen_manifest(session):
    as_of = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    _seed_event(
        session,
        event_id="live-upcoming-1",
        start=as_of + timedelta(hours=30),
        status="upcoming",
    )
    _seed_event(
        session,
        event_id="live-completed-1",
        start=as_of + timedelta(hours=20),
        status="completed",
    )
    _seed_event(
        session,
        event_id="live-cancelled-1",
        start=as_of + timedelta(hours=25),
        status="cancelled",
    )
    rows = load_upcoming_dwcs_events_from_db(session, as_of=as_of)
    assert [r["event_id"] for r in rows] == ["live-upcoming-1"]
    assert classify_event_status_for_tests("scheduled") == "upcoming"
    assert classify_event_status_for_tests("completed") == "completed"
    assert classify_event_status_for_tests("cancelled") == "cancelled"
    # Frozen manifest has no 2026+ production schedule; live must not use it.
    manifest = load_dwcs_schedule_events()
    assert all(r["event_start"].year <= 2025 for r in manifest)
    assert all(r["source"] == "frozen_manifest" for r in manifest)


def test_snapshot_odds_job_zero_upcoming_explicit(session, tmp_path: Path):
    as_of = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    result = run_snapshot_odds_job(
        session,
        as_of=as_of,
        finished_at=as_of,
        events=[],
        lock_path=tmp_path / "snap.lock",
        execute=False,
        remaining_override=500,
    )
    assert result.upcoming_event_count == 0
    assert result.items[0]["action"] == "no_upcoming_events"


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
        finished_at=as_of,
        events=events,
        lock_path=tmp_path / "snap.lock",
        execute=False,
        remaining_override=500,
    )
    assert result.no_op == 1
    assert result.due == 0


def test_multi_event_same_slot_one_batch(session, tmp_path: Path, schedule):
    as_of = datetime(2024, 5, 30, 20, 0, tzinfo=UTC)  # 48h before Jun 1 20:00
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    events = [
        {"event_id": "e-a", "event_start": event_start},
        {"event_id": "e-b", "event_start": event_start},
    ]
    calls: list[str] = []

    def fake_snapshot(*_args, **_kwargs):
        calls.append("call")

        class _R:
            inserted = 0
            deduped = 0
            snapshot_at = None
            quota = {
                "x-requests-remaining": 1000,
                "x-requests-used": 1,
                "x-requests-last": 1,
                "requests_last_source": REQUESTS_LAST_SOURCE_PROVIDER,
            }

        return _R()

    with patch(
        "mma_model.jobs.snapshot_odds.run_odds_snapshot", side_effect=fake_snapshot
    ):
        result = run_snapshot_odds_job(
            session,
            as_of=as_of,
            finished_at=as_of,
            events=events,
            lock_path=tmp_path / "batch.lock",
            execute=True,
            remaining_override=500,
            contract=schedule,
        )
    assert result.due == 2
    assert result.batches == 1
    assert len(calls) == 1


def test_injected_failure_rolls_back_partial_snapshot(session, tmp_path: Path, schedule):
    as_of = datetime(2024, 5, 30, 20, 0, tzinfo=UTC)
    event_start = datetime(2024, 6, 1, 20, 0, tzinfo=UTC)
    events = [{"event_id": "e-fail", "event_start": event_start}]

    def boom(session_arg, **_kwargs):
        session_arg.add(
            OddsQuotaObservation(
                provider="the_odds_api",
                endpoint="current_odds",
                observed_at=as_of,
                requests_remaining=999,
                requests_used=1,
                requests_last=1,
                requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
                empty_response=False,
            )
        )
        session_arg.flush()
        raise RuntimeError("injected_failure")

    with patch("mma_model.jobs.snapshot_odds.run_odds_snapshot", side_effect=boom):
        result = run_snapshot_odds_job(
            session,
            as_of=as_of,
            finished_at=as_of,
            events=events,
            lock_path=tmp_path / "fail.lock",
            execute=True,
            remaining_override=500,
            contract=schedule,
        )
        session.commit()
    assert result.failures == 1
    assert (
        session.scalar(
            select(OddsQuotaObservation).where(
                OddsQuotaObservation.requests_remaining == 999
            ).limit(1)
        )
        is None
    )
    failed = session.scalars(
        select(OddsSnapshotJobRun).where(OddsSnapshotJobRun.status == "failed")
    ).all()
    assert len(failed) == 1
    assert failed[0].error_class == "RuntimeError"


def test_bestfightodds_url_rejects_lookalikes():
    ok = validate_bestfightodds_archive_url(
        "https://www.bestfightodds.com/archive/mma"
    )
    assert ok.startswith("https://www.bestfightodds.com/")
    with pytest.raises(BestFightOddsPolicyError):
        validate_bestfightodds_archive_url(
            "https://www.bestfightodds.com.evil.com/archive"
        )
    with pytest.raises(BestFightOddsPolicyError):
        validate_bestfightodds_archive_url("http://www.bestfightodds.com/archive")
    with pytest.raises(BestFightOddsPolicyError):
        validate_bestfightodds_archive_url(
            "https://user:pass@www.bestfightodds.com/archive"
        )


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


def test_cli_backfill_and_due_help():
    from mma_model.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["odds", "backfill", "--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc2:
        main(["jobs", "snapshot-odds", "--help"])
    assert exc2.value.code == 0


def test_migration_creates_job_table(session):
    names = set(inspect(session.get_bind()).get_table_names())
    assert "odds_snapshot_job_runs" in names
    cols = {c["name"] for c in inspect(session.get_bind()).get_columns(
        "odds_snapshot_job_runs"
    )}
    assert "actual_cost_source" in cols
    assert "remaining_source" in cols
    assert "snapshot_quote_ids" in cols


def test_cumulative_batch_quota_exhaustion(schedule):
    from mma_model.odds.quota_budget import RunningQuotaLedger

    # remaining 230, reserve 200 → spendable 30 for backfill; three 10-credit batches
    ledger = RunningQuotaLedger(remaining=230, remaining_source="test")
    allowed = 0
    deferred = 0
    for _ in range(5):
        decision = ledger.evaluate(
            estimated_cost=10, purpose=RequestPurpose.BACKFILL, contract=schedule
        )
        if decision.state == QuotaBudgetState.ALLOWED:
            ledger.reserve(10)
            allowed += 1
        else:
            deferred += 1
    assert allowed == 3
    assert deferred == 2
    assert ledger.reserved == 30


def test_remaining_override_bounded_to_monthly_limit(schedule):
    from mma_model.odds.quota_budget import validate_remaining_override

    value, source = validate_remaining_override(100, contract=schedule)
    assert value == 100
    assert source.startswith("override_bounded:")
    with pytest.raises(ValueError):
        validate_remaining_override(schedule.quota.monthly_limit + 1, contract=schedule)
    with pytest.raises(ValueError):
        validate_remaining_override(-1, contract=schedule)


def test_quota_stale_billing_cycle_and_max_age(session, schedule):
    from mma_model.odds.quota_budget import latest_remaining_from_observations

    store = OddsQuoteStore(session)
    as_of = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    # Prior calendar month → stale billing cycle
    store.record_quota(
        provider="the_odds_api",
        endpoint="events",
        observed_at=datetime(2024, 5, 31, 23, 0, tzinfo=UTC),
        quota=QuotaHeaders(
            requests_remaining=9000,
            requests_used=1,
            requests_last=0,
            requests_last_inferred=None,
            requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
        ),
        empty_response=False,
    )
    session.flush()
    remaining, source = latest_remaining_from_observations(
        session, provider="the_odds_api", as_of=as_of, contract=schedule
    )
    assert remaining is None
    assert source == "stale_billing_cycle"

    # Same month but older than max age
    store.record_quota(
        provider="the_odds_api",
        endpoint="events",
        observed_at=as_of - timedelta(seconds=schedule.quota.remaining_max_age_sec + 10),
        quota=QuotaHeaders(
            requests_remaining=8000,
            requests_used=1,
            requests_last=0,
            requests_last_inferred=None,
            requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
        ),
        empty_response=False,
    )
    session.flush()
    # May still be stale_billing_cycle if age crosses month; force same-month old via patch
    same_month_old = as_of - timedelta(days=10)
    if same_month_old.month == as_of.month:
        store.record_quota(
            provider="the_odds_api",
            endpoint="events",
            observed_at=as_of - timedelta(seconds=schedule.quota.remaining_max_age_sec + 60),
            quota=QuotaHeaders(
                requests_remaining=7000,
                requests_used=1,
                requests_last=0,
                requests_last_inferred=None,
                requests_last_source=REQUESTS_LAST_SOURCE_PROVIDER,
            ),
            empty_response=False,
        )
        session.flush()
    remaining2, source2 = latest_remaining_from_observations(
        session, provider="the_odds_api", as_of=as_of, contract=schedule
    )
    assert remaining2 is None
    assert source2 in {"stale_max_age", "stale_billing_cycle"}


def test_bootstrap_no_observation_then_allowed(session, schedule, tmp_path: Path):
    from mma_model.odds.quota_budget import open_quota_ledger

    as_of = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    ledger = open_quota_ledger(
        session,
        provider="the_odds_api",
        as_of=as_of,
        contract=schedule,
        allow_bootstrap=True,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
    )
    assert ledger.remaining == 500
    assert ledger.remaining_source.startswith("bootstrap")
    decision = ledger.evaluate(
        estimated_cost=1, purpose=RequestPurpose.LIVE_ORDINARY, contract=schedule
    )
    assert decision.state == QuotaBudgetState.ALLOWED


def test_bootstrap_failure_stays_deferred(session, schedule, tmp_path: Path):
    from mma_model.odds.quota_budget import open_quota_ledger

    as_of = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    ledger = open_quota_ledger(
        session,
        provider="the_odds_api",
        as_of=as_of,
        contract=schedule,
        allow_bootstrap=True,
        offline_fixtures=True,
        fixture_dir=tmp_path / "missing-fixtures",
    )
    assert ledger.remaining is None
    decision = ledger.evaluate(
        estimated_cost=1, purpose=RequestPurpose.LIVE_ORDINARY, contract=schedule
    )
    assert decision.state == QuotaBudgetState.DEFERRED


def test_normalize_markets_lowercases_and_rejects_ci_dupes():
    assert normalize_markets("H2H,totals") == "h2h,totals"
    with pytest.raises(ValueError):
        normalize_markets("h2h,H2H")


def test_changed_event_start_uses_canonical_not_manifest(session):
    as_of = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    _seed_event(
        session,
        event_id="live-move-1",
        start=as_of + timedelta(hours=10),
        status="scheduled",
    )
    rows = load_upcoming_dwcs_events_from_db(session, as_of=as_of)
    assert rows[0]["event_id"] == "live-move-1"
    # Late correction: move start further out but still in horizon
    event = session.get(CanonicalEvent, "live-move-1")
    assert event is not None
    event.scheduled_start_at = as_of + timedelta(hours=40)
    session.flush()
    rows2 = load_upcoming_dwcs_events_from_db(session, as_of=as_of)
    assert rows2[0]["event_start"] == as_of + timedelta(hours=40)
    # Manifest must not be consulted / mixed in
    assert rows2[0]["source"] == "canonical_db"


def test_dry_run_uses_planned_not_absent(session, schedule, tmp_path: Path):
    as_of = datetime(2024, 6, 2, 0, 0, tzinfo=UTC)
    events = [
        {
            "event_id": "evt-dry",
            "card_id": "card-dry",
            "event_start": datetime(2024, 6, 1, 20, 0, tzinfo=UTC),
        }
    ]
    dry = run_odds_backfill(
        session,
        series="dwcs",
        from_year=2020,
        events=events,
        as_of=as_of,
        finished_at=as_of,
        contract=schedule,
        offline_fixtures=True,
        fixture_dir=FIXTURE_DIR,
        evaluation_contract_path=EVAL_CONTRACT,
        execute=False,
        lock_path=tmp_path / "dry.lock",
        remaining_override=10_000,
    )
    assert len(dry.coverage.planned) == 4
    assert all(item.detail == "dry_run_not_requested" for item in dry.coverage.planned)
    # Planned work must not be labeled absent
    assert dry.coverage.status_counts["absent"] == 0


def test_ledger_sql_rejects_success_without_token(session):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO odds_snapshot_job_runs ("
                "id, idempotency_key, success_token, job_name, status, provider, "
                "region, markets, event_id, mode, as_of, estimated_cost, "
                "started_at, finished_at, created_at"
                ") VALUES ("
                "'x1', 'k1', NULL, 'odds-backfill', 'success', 'the_odds_api', "
                "'us', 'h2h', 'e1', 'historical:t_minus_24h', :as_of, 0, "
                ":as_of, :as_of, :as_of)"
            ),
            {"as_of": datetime(2024, 1, 1, tzinfo=UTC)},
        )
        session.flush()
    session.rollback()


def test_coverage_scopes_quotes_to_card_and_alias_pit(session):
    from mma_model.db.tables.core import CanonicalBout, CanonicalFighter
    from mma_model.db.tables.odds import OddsEventRow, OddsProviderEventAlias, OddsQuote
    from mma_model.odds.coverage_report import cells_from_snapshot_quotes

    as_of = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
    session.add(CanonicalFighter(id="f-a", display_name="A"))
    session.add(CanonicalFighter(id="f-b", display_name="B"))
    session.add(CanonicalFighter(id="f-c", display_name="C"))
    session.add(CanonicalFighter(id="f-d", display_name="D"))
    session.add(
        CanonicalEvent(
            id="card-a", name="Card A", series="dwcs", status="completed"
        )
    )
    session.add(
        CanonicalEvent(
            id="card-b", name="Card B", series="dwcs", status="completed"
        )
    )
    session.flush()
    session.add(
        CanonicalBout(
            id="bout-a",
            event_id="card-a",
            fighter_a_id="f-a",
            fighter_b_id="f-b",
            status="completed",
        )
    )
    session.add(
        CanonicalBout(
            id="bout-b",
            event_id="card-b",
            fighter_a_id="f-c",
            fighter_b_id="f-d",
            status="completed",
        )
    )
    session.add(
        OddsEventRow(
            id="oe-a",
            provider="the_odds_api",
            external_event_id="ext-a",
            sport_key="mma_mixed_martial_arts",
            commence_time=as_of,
            home_team="A",
            away_team="B",
        )
    )
    session.add(
        OddsEventRow(
            id="oe-b",
            provider="the_odds_api",
            external_event_id="ext-b",
            sport_key="mma_mixed_martial_arts",
            commence_time=as_of,
            home_team="C",
            away_team="D",
        )
    )
    # Alias active now for B, but historically (as_of) card-a owns ext-a
    session.add(
        OddsProviderEventAlias(
            id="alias-a1",
            provider="the_odds_api",
            external_event_id="ext-a",
            bout_id="bout-a",
            alias_version=1,
            status="superseded",
            match_rule="provider_id",
            created_at=as_of - timedelta(days=2),
            superseded_at=as_of + timedelta(days=1),
        )
    )
    session.add(
        OddsProviderEventAlias(
            id="alias-a2",
            provider="the_odds_api",
            external_event_id="ext-a",
            bout_id="bout-b",
            alias_version=2,
            status="active",
            match_rule="provider_id",
            created_at=as_of + timedelta(days=1),
            superseded_at=None,
        )
    )
    q1 = OddsQuote(
        dedupe_key="dq1",
        dedupe_version=2,
        provider="the_odds_api",
        bookmaker_key="draftkings",
        bookmaker_title="DraftKings",
        region="us",
        event_id="oe-a",
        external_event_id="ext-a",
        market_family="moneyline",
        provider_market_key="h2h",
        outcome_key="fighter_a",
        outcome_label="A",
        line_point=None,
        price_decimal=1.9,
        availability="available",
        observed_at=as_of,
        source_updated_at=as_of,
        commence_time=as_of,
        snapshot_at=as_of,
        raw_ref="r1",
    )
    q2 = OddsQuote(
        dedupe_key="dq2",
        dedupe_version=2,
        provider="the_odds_api",
        bookmaker_key="draftkings",
        bookmaker_title="DraftKings",
        region="us",
        event_id="oe-b",
        external_event_id="ext-b",
        market_family="moneyline",
        provider_market_key="h2h",
        outcome_key="fighter_a",
        outcome_label="C",
        line_point=None,
        price_decimal=1.8,
        availability="available",
        observed_at=as_of + timedelta(days=3),
        source_updated_at=as_of + timedelta(days=3),
        commence_time=as_of,
        snapshot_at=as_of + timedelta(days=3),
        raw_ref="r2",
    )
    session.add(q1)
    session.add(q2)
    session.flush()

    # Card A at historical as_of sees only q1 via superseded-but-then-effective alias v1
    cells_a = cells_from_snapshot_quotes(
        session,
        card_id="card-a",
        time_label="t_minus_24h",
        market="h2h",
        provider="the_odds_api",
        region="us",
        as_of=as_of,
        quote_ids=[q1.id, q2.id],
        snapshot_at=as_of,
    )
    assert len(cells_a) == 1
    assert cells_a[0].status == "observed"
    assert cells_a[0].matched is True
    assert q2.id not in cells_a[0].quote_ids

    # Later as_of after replacement: alias v1 superseded, v2 maps to card-b
    later = as_of + timedelta(days=2)
    cells_a_later = cells_from_snapshot_quotes(
        session,
        card_id="card-a",
        time_label="t_minus_24h",
        market="h2h",
        provider="the_odds_api",
        region="us",
        as_of=later,
        quote_ids=[q1.id],
        snapshot_at=as_of,
    )
    assert cells_a_later[0].status == "absent"
