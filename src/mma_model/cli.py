"""CLI: init-db, sync, odds, train, predict, source audit."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.config import get_settings
from mma_model.db.session import (
    _attach_sqlite_listeners,
    create_all_for_tests,
    init_db,
    session_scope,
)
from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey
from mma_model.dwcs.ingest import sync_dwcs_history
from mma_model.history.audit import (
    coverage_gates_ok,
    evaluate_sample_coverage,
    write_regional_coverage_doc,
)
from mma_model.history.constants import PROBE_PATHS, REGIONAL_FALLBACK_ORDER
from mma_model.history.probe import (
    not_run_live_probe_evidence as history_not_run_probe,
    run_bounded_live_probe as history_run_bounded_probe,
)
from mma_model.history.sync import load_upcoming_dwcs_fighters, sync_regional_history
from mma_model.identity.audit import build_identity_audit
from mma_model.identity.review import (
    ReviewDecisionError,
    apply_review_decision,
    list_reviews,
    reverse_review_decision,
)
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository
from mma_model.odds.bookmaker_audit import run_bookmaker_audit
from mma_model.odds.manual_price import parse_manual_price_observation
from mma_model.odds.normalize import parse_single_region
from mma_model.odds.price_guidance import (
    PriceGuidanceSelectionError,
    build_price_guidance,
)
from mma_model.odds.reconcile import OddsReconcileError, run_odds_reconcile
from mma_model.odds.backfill import run_odds_backfill
from mma_model.odds.events_for_schedule import (
    load_dwcs_schedule_events,
    load_upcoming_dwcs_events_from_db,
)
from mma_model.odds.job_ledger import slot_succeeded
from mma_model.odds.quota_budget import (
    QuotaBudgetState,
    plan_request_budget,
    validate_remaining_override,
)
from mma_model.odds.schedule import (
    DueAction,
    RequestPurpose,
    compute_due_work,
    compute_due_work_for_events,
)
from mma_model.jobs.snapshot_odds import run_snapshot_odds_job
from mma_model.jobs.db_guard import is_refused_jobs_tick_database_url
from mma_model.jobs.due import load_orchestrator_cadence
from mma_model.jobs.locking import FileFlockLock
from mma_model.jobs.orchestrator import TickOverlapError, run_jobs_tick
from mma_model.jobs.types import EventContext
from mma_model.odds.snapshot import (
    OddsConfigurationError,
    OddsOfflineModeError,
    require_disposable_database_url,
    resolve_odds_client,
    run_odds_audit,
    run_odds_snapshot,
    validate_requested_series,
)
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.the_odds_api import OddsApiError, fetch_mma_odds
from mma_model.features.audit import run_features_audit
from mma_model.backtest.contract import EvaluatorHashMismatchError
from mma_model.backtest.engine import BacktestError, execute_backtest_run
from mma_model.backtest.gates import DatabaseMutationError, EvidenceOverwriteError
from mma_model.backtest.metrics import (
    DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    DEFAULT_BACKTEST_BOOTSTRAP_SEED,
)
from mma_model.evaluation.contract import EvaluationContractError, load_evaluation_contract
from mma_model.recommend.policy import (
    PolicyContractDriftError,
    PolicyHashMismatch,
    RecommendationPolicyError,
)
from mma_model.recommend.replay import RecommendReplayError, execute_recommend_replay
from mma_model.grade.service import GradeLedgerError, audit_series
from mma_model.markets.derive import UnsupportedScheduleError
from mma_model.modeling.artifacts import (
    ArtifactError,
    RidgeSpecError,
    UntrustedArtifactError,
    load_artifact,
    load_feature_vector,
    load_ridge_spec,
)
from mma_model.modeling.baselines import (
    TrainError,
    predict_loaded_ridge,
    protocol_feature_vector,
    run_protocol_train,
    train_from_session,
)
from mma_model.modeling.calibration import CalibrationError, CalibrationLeakageError
from mma_model.modeling.joint import (
    PAYLOAD_KIND as JOINT_PAYLOAD_KIND,
    EarlyTechnicalOutcomeError,
    JointError,
    JointSpecError,
    MissingJointClassError,
    identify_model_family,
    load_joint_artifact,
    load_joint_spec,
    peek_artifact_payload_kind,
    predict_loaded_joint,
    protocol_joint_feature_vector,
    run_protocol_joint_train,
    train_joint_from_session,
)
from mma_model.modeling.promotion import (
    PromotionError,
    PromotionEvaluateRequiredError,
    PromotionGateError,
)
from mma_model.modeling.registry import (
    promote_candidate,
    retrain_fixed_spec,
    rollback_champion,
)
from mma_model.modeling.uncertainty import (
    DEFAULT_BOOTSTRAP_REPLICATES,
    DEFAULT_BOOTSTRAP_SEED,
    BootstrapError,
    run_model_calibrate,
)
from mma_model.modeling.splits import (
    HoldoutLockedError,
    SplitError,
    cards_from_manifest,
    cards_from_session,
    inspect_folds,
    protocol_fixture_cards,
    render_fold_plan,
)
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK, EXIT_STRICT_BLOCKERS
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.leakage import FutureRowLeakageError
from mma_model.quality.readonly import (
    CoverageDatabaseError,
    is_prohibited_live_url,
    open_readonly_sqlite_engine,
    readonly_session_factory,
)
from mma_model.quality.report import dumps_report, human_report, write_coverage_evidence
from mma_model.quality.schema import CoverageSchemaError
from mma_model.observability.health import (
    default_missing_report,
    dumps_health,
    load_health_state,
)
from mma_model.observability.publish_guard import PublishValidationError
from mma_model.observability.schema import HealthSchemaError, validate_health_payload
from mma_model.publish.public_sync import PublicSyncError, sync_web_assets
from mma_model.publish.publisher import publish_dashboard
from mma_model.sources.policy import load_source_policy
from mma_model.predict.backtest import walk_forward_backtest
from mma_model.predict.train import (
    DEPRECATED_RANDOM_SPLIT_NOTE,
    predict_fight_a_win_prob,
    train_and_save,
)
from mma_model.sources.combat_registry.client import CombatRegistryPublicClient
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.sherdog_public.client import SherdogPublicClient
from mma_model.sources.tapology_public.client import TapologyPublicClient
from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter
from mma_model.sources.ufcstats_public.client import UfcstatsPublicClient
from mma_model.sources.ufcstats_public.probe import (
    not_run_live_probe_evidence,
    run_bounded_live_probe,
)
from mma_model.ufcstats.client import UFCStatsClient
from mma_model.ufcstats.ingest import sync_pipeline

LIVE_DB_URLS = frozenset(
    {
        "sqlite:///data/mma.db",
        "sqlite:///./data/mma.db",
    }
)


def _parse_years(spec: str) -> range:
    if ":" not in spec:
        year = int(spec)
        return range(year, year + 1)
    start_s, stop_s = spec.split(":", 1)
    start = int(start_s)
    stop = int(stop_s)
    if stop < start:
        raise ValueError(f"invalid years range: {spec!r}")
    return range(start, stop + 1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mma-model")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create SQLite tables")

    p_sync = sub.add_parser("sync", help="Sync events/fights from ufcstats.com")
    p_sync.add_argument("--profile", default="default", help="profiles.yaml key")
    p_sync.add_argument(
        "--resume",
        action="store_true",
        help="For paginated profiles: continue from ingest cursor (next page)",
    )
    p_sync.add_argument(
        "--reset-cursor",
        action="store_true",
        help="Reset paginated ingest cursor to page 1 before this run",
    )

    p_odds = sub.add_parser(
        "odds",
        help=(
            "Fetch current MMA odds (legacy) or run snapshot/audit "
            "(DWCS-201; live ODDS_API_KEY or explicit --offline-fixtures)"
        ),
    )
    p_odds.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON (legacy fetch when no subcommand)",
    )
    odds_sub = p_odds.add_subparsers(dest="odds_cmd", required=False)
    p_odds_snap = odds_sub.add_parser(
        "snapshot",
        help="Normalize and append-only store The Odds API reference quotes",
    )
    p_odds_snap.add_argument(
        "--series",
        default="dwcs",
        help="Requested series label only (not canonical DWCS match; DWCS-203)",
    )
    p_odds_snap.add_argument(
        "--provider",
        default="the-odds-api",
        help="Odds provider id (DWCS-201: the-odds-api)",
    )
    p_odds_snap.add_argument(
        "--markets",
        default="h2h",
        help="Comma-separated provider markets (supported: h2h,totals)",
    )
    p_odds_snap.add_argument(
        "--regions",
        default="us",
        help="Exactly one Odds API region (multi-region rejected)",
    )
    p_odds_snap.add_argument(
        "--historical-date",
        default=None,
        help="ISO timestamp for historical snapshot (omit for current odds)",
    )
    p_odds_snap.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Explicit offline/test mode (requires --fixture-dir + disposable DB)",
    )
    p_odds_snap.add_argument(
        "--fixture-dir",
        type=Path,
        default=None,
        help="Explicit fixture directory for --offline-fixtures only",
    )
    p_odds_snap.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL (required disposable URL for --offline-fixtures)",
    )
    p_odds_audit = odds_sub.add_parser(
        "audit",
        help="Sanitized events/market-discovery/quota audit (no prices)",
    )
    p_odds_audit.add_argument("--series", default="dwcs")
    p_odds_audit.add_argument("--provider", default="the-odds-api")
    p_odds_audit.add_argument("--markets", default="h2h")
    p_odds_audit.add_argument(
        "--regions",
        default="us",
        help="Exactly one Odds API region (multi-region rejected)",
    )
    p_odds_audit.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Explicit offline/test mode (requires --fixture-dir + disposable DB)",
    )
    p_odds_audit.add_argument("--fixture-dir", type=Path, default=None)
    p_odds_audit.add_argument("--database-url", default=None)
    p_odds_books = odds_sub.add_parser(
        "audit-bookmakers",
        help=(
            "DWCS-202 Phase 0 honesty + sportsbook-agnostic fallback report "
            "(no licensed adapter invention)"
        ),
    )
    p_odds_books.add_argument(
        "--next-dwcs",
        action="store_true",
        help="Mark audit as next-DWCS readiness (bout match remains DWCS-203)",
    )
    p_odds_manual = odds_sub.add_parser(
        "record-manual-price",
        help="Append a user_observed (non-automated) price or lifecycle row",
    )
    p_odds_manual.add_argument(
        "--observation-json",
        type=Path,
        required=True,
        help="JSON file with book/region/market/outcome/price_or_lifecycle/time",
    )
    p_odds_manual.add_argument("--database-url", default=None)
    p_odds_reconcile = odds_sub.add_parser(
        "reconcile",
        help=(
            "DWCS-203 match provider odds events to canonical bouts "
            "(deterministic report; --strict exits nonzero on blockers)"
        ),
    )
    p_odds_reconcile.add_argument(
        "--next-dwcs",
        action="store_true",
        help="Require 100% exact active-bout matches for next-DWCS readiness",
    )
    p_odds_reconcile.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when blockers are present",
    )
    p_odds_reconcile.add_argument(
        "--golden-card",
        type=Path,
        default=None,
        help=(
            "Offline/test golden-card fixture only "
            "(requires --offline-fixtures + disposable --database-url)"
        ),
    )
    p_odds_reconcile.add_argument(
        "--offline-fixtures",
        action="store_true",
        help="Enable offline golden-card seeding (disposable DB required)",
    )
    p_odds_reconcile.add_argument(
        "--as-of",
        default=None,
        help="UTC ISO timestamp for next-DWCS card selection (default: now)",
    )
    p_odds_reconcile.add_argument(
        "--provider",
        default="the-odds-api",
        help="Odds provider id (DWCS-203: the-odds-api / the_odds_api)",
    )
    p_odds_reconcile.add_argument("--database-url", default=None)
    p_odds_guide = odds_sub.add_parser(
        "price-guidance",
        help="Emit fair/actionable/strong-value guidance (exact EV only if priced)",
    )
    p_odds_guide.add_argument("--family", default="moneyline")
    p_odds_guide.add_argument("--outcome", default="fighter_a")
    p_odds_guide.add_argument(
        "--line-point",
        type=float,
        default=None,
        help="Required for totals (1.5 or 2.5); rejected for non-line markets",
    )
    p_odds_guide.add_argument("--p50", type=float, required=True)
    p_odds_guide.add_argument("--p25", type=float, required=True)
    p_odds_guide.add_argument(
        "--maturity",
        default="qualified",
        help="qualified|experimental|blocked",
    )
    p_odds_guide.add_argument(
        "--observation-json",
        type=Path,
        default=None,
        help="Optional user_observed price JSON for exact EV confirmation",
    )
    p_odds_guide.add_argument("--prob-ev-positive", type=float, default=None)

    p_odds_backfill = odds_sub.add_parser(
        "backfill",
        help="Sparse-first historical odds backfill from 2020 (DWCS-205)",
    )
    p_odds_backfill.add_argument("--series", default="dwcs")
    p_odds_backfill.add_argument(
        "--from",
        dest="from_year",
        type=int,
        default=2020,
        help="Earliest calendar year to include (default 2020)",
    )
    p_odds_backfill.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Evaluation contract path (accepted; evaluation itself out of scope)",
    )
    p_odds_backfill.add_argument("--markets", default="h2h")
    p_odds_backfill.add_argument("--regions", default="us")
    p_odds_backfill.add_argument(
        "--as-of",
        default=None,
        help="Explicit UTC as_of for quota reads (required for deterministic runs)",
    )
    p_odds_backfill.add_argument("--offline-fixtures", action="store_true")
    p_odds_backfill.add_argument("--fixture-dir", type=Path, default=None)
    p_odds_backfill.add_argument("--database-url", default=None)
    p_odds_backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute coverage/due work without calling the provider",
    )
    p_odds_backfill.add_argument(
        "--limit-events",
        type=int,
        default=None,
        help="Optional cap on events for smoke/offline runs",
    )
    p_odds_backfill.add_argument(
        "--remaining-override",
        type=int,
        default=None,
        help="Explicit override 0..monthly_limit (prefer --quota-bootstrap)",
    )
    p_odds_backfill.add_argument(
        "--quota-bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow zero-cost events bootstrap when remaining provenance is missing/stale "
        "(use --no-quota-bootstrap to fail closed without network bootstrap)",
    )

    p_odds_due = odds_sub.add_parser(
        "due",
        help="Deterministic due-work listing for an explicit UTC as_of (DWCS-205)",
    )
    p_odds_due.add_argument("--series", default="dwcs")
    p_odds_due.add_argument("--as-of", required=True, help="Explicit timezone-aware UTC")
    p_odds_due.add_argument("--markets", default="h2h")
    p_odds_due.add_argument("--regions", default="us")
    p_odds_due.add_argument(
        "--event-id",
        action="append",
        default=None,
        help="Optional event id filter (repeatable)",
    )
    p_odds_due.add_argument(
        "--event-start",
        default=None,
        help="Optional single event start ISO when --event-id is synthetic",
    )
    p_odds_due.add_argument(
        "--database-url",
        default=None,
        help="Target DB for canonical upcoming DWCS events (required unless synthetic --event-id/--event-start)",
    )
    p_odds_due.add_argument(
        "--remaining-override",
        type=int,
        default=None,
        help="Explicit override 0..monthly_limit (prefer --quota-bootstrap)",
    )
    p_odds_due.add_argument(
        "--quota-bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow zero-cost events bootstrap (default on; --no-quota-bootstrap fails closed)",
    )

    p_jobs = sub.add_parser("jobs", help="Host-scheduled job entrypoints (DWCS-205)")
    jobs_sub = p_jobs.add_subparsers(dest="jobs_cmd", required=True)
    p_jobs_snap = jobs_sub.add_parser(
        "snapshot-odds",
        help="Due live odds snapshot job under flock + idempotency",
    )
    p_jobs_snap.add_argument("--as-of", required=True, help="Explicit timezone-aware UTC")
    p_jobs_snap.add_argument("--series", default="dwcs")
    p_jobs_snap.add_argument("--markets", default="h2h")
    p_jobs_snap.add_argument("--regions", default="us")
    p_jobs_snap.add_argument("--database-url", default=None)
    p_jobs_snap.add_argument("--offline-fixtures", action="store_true")
    p_jobs_snap.add_argument("--fixture-dir", type=Path, default=None)
    p_jobs_snap.add_argument("--lock-path", type=Path, default=None)
    p_jobs_snap.add_argument("--dry-run", action="store_true")
    p_jobs_snap.add_argument("--limit-events", type=int, default=None)
    p_jobs_snap.add_argument(
        "--remaining-override",
        type=int,
        default=None,
        help="Explicit override 0..monthly_limit (prefer --quota-bootstrap)",
    )
    p_jobs_snap.add_argument(
        "--quota-bootstrap",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow zero-cost events bootstrap when remaining is missing/stale "
        "(use --no-quota-bootstrap to fail closed)",
    )
    p_jobs_tick = jobs_sub.add_parser(
        "tick",
        help="Event-relative scheduler tick (DWCS-401 discover-to-grade)",
    )
    p_jobs_tick.add_argument(
        "--now",
        required=True,
        help="Explicit timezone-aware UTC instant for due calculation",
    )
    p_jobs_tick.add_argument("--series", default="dwcs")
    p_jobs_tick.add_argument("--database-url", default=None)
    p_jobs_tick.add_argument("--lock-path", type=Path, default=None)
    p_jobs_tick.add_argument(
        "--dry-run",
        action="store_true",
        help="Print due plan JSON; no DB writes and no handler side effects",
    )
    p_jobs_tick.add_argument(
        "--event-id",
        default=None,
        help="Optional single event id for due calculation (tests/fixtures)",
    )
    p_jobs_tick.add_argument(
        "--event-start",
        default=None,
        help="Optional ISO UTC event start paired with --event-id",
    )

    p_train = sub.add_parser("train", help="Train logistic model on DB fights")
    p_train.add_argument(
        "--output",
        type=Path,
        default=Path("data/model_logistic.joblib"),
        help="Where to save the trained model",
    )
    p_train.add_argument("--min-prior-fights", type=int, default=1)

    p_pred = sub.add_parser("predict-fight", help="P(fighter A wins) for a fight id")
    p_pred.add_argument("--fight-id", required=True, help="ufcstats fight-details hex id")
    p_pred.add_argument(
        "--model",
        type=Path,
        default=Path("data/model_logistic.joblib"),
        help="Trained model from mma-model train",
    )

    p_bt = sub.add_parser(
        "backtest",
        help=(
            "Event-grouped walk-forward (DWCS-306). "
            "`backtest run` is the evidence path. Legacy `backtest` without `run` "
            "is fail-closed and is not betting evidence. --database-url is read-only."
        ),
    )
    p_bt.add_argument("--min-train", type=int, default=30, help="Legacy flag; ignored")
    p_bt.add_argument("--min-prior-fights", type=int, default=1, help="Legacy flag; ignored")
    p_bt.add_argument(
        "--max-predictions",
        type=int,
        default=None,
        help="Legacy flag; ignored",
    )
    p_bt.add_argument(
        "--omit-predictions",
        action="store_true",
        help="Legacy flag; ignored",
    )
    bt_sub = p_bt.add_subparsers(dest="backtest_cmd", required=False)
    p_run = bt_sub.add_parser(
        "run",
        help="Replay DWCS cards with timestamp-valid data, prices, and settlement",
    )
    p_run.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Frozen evaluation contract (never mutated)",
    )
    p_run.add_argument(
        "--output",
        type=Path,
        default=Path("output/backtests"),
        help="Directory for versioned JSON/Markdown evidence",
    )
    p_run.add_argument(
        "--fixture",
        choices=("protocol", "manifest"),
        default=None,
        help="protocol = small real pipeline; manifest = frozen 89/440 (default)",
    )
    p_run.add_argument(
        "--from-manifest",
        action="store_true",
        help="Attempt all 89 cards / 440 bouts; missing sources become explicit exclusions",
    )
    p_run.add_argument(
        "--database-url",
        default=None,
        help=(
            "Optional read-only SQLite URL. Mutations are rejected. "
            "Never implied live data/mma.db. Omit for frozen-manifest exclusions."
        ),
    )
    p_run.add_argument(
        "--sealed-holdout",
        action="store_true",
        help="Explicitly score locked 2025 after freeze; 2025 still never enters training",
    )
    p_run.add_argument(
        "--bootstrap-replicates",
        type=int,
        default=DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
        help="Event-block bootstrap replicates (default 200)",
    )
    p_run.add_argument(
        "--bootstrap-seed",
        type=int,
        default=DEFAULT_BACKTEST_BOOTSTRAP_SEED,
        help="Pinned event-block bootstrap seed",
    )
    p_run.add_argument(
        "--generated-at",
        default=None,
        help=(
            "Explicit UTC timestamp hashed with evidence. "
            "Required for --sealed-holdout; wall clock is never used"
        ),
    )
    p_run.add_argument(
        "--expected-data-hash",
        default=None,
        help="Independent expected source-data hash; mismatch fails the run",
    )
    p_run.add_argument(
        "--expected-model-hash",
        default=None,
        help="Independent expected per-card estimator hash digest",
    )
    p_run.add_argument(
        "--expected-calibration-hash",
        default=None,
        help="Independent expected per-card calibrator hash digest",
    )

    p_rec = sub.add_parser(
        "recommend",
        help=(
            "Frozen confirmed-value / price-target policy (DWCS-307). "
            "`recommend replay` applies the pinned policy to DWCS-306 evidence "
            "or the protocol fixture. It never refits models or tunes thresholds."
        ),
    )
    rec_sub = p_rec.add_subparsers(dest="recommend_cmd", required=True)
    p_rec_replay = rec_sub.add_parser(
        "replay",
        help="Apply frozen DWCS-307 policy to backtest evidence or --fixture protocol",
    )
    p_rec_replay.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Frozen evaluation contract (never mutated)",
    )
    p_rec_replay.add_argument(
        "--backtest-json",
        type=Path,
        default=None,
        help="DWCS-306 evidence JSON; content hash is verified before replay",
    )
    p_rec_replay.add_argument(
        "--fixture",
        choices=("protocol",),
        default=None,
        help="protocol = frozen DWCS-307 policy cases (confirmed/unpriced/stale/ties)",
    )
    p_rec_replay.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the recommendation JSON report",
    )

    p_grade = sub.add_parser(
        "grade",
        help=(
            "Append-only prediction / recommendation grading ledgers (DWCS-400). "
            "`grade audit` prints qualified/paper/experimental/model-version views."
        ),
    )
    grade_sub = p_grade.add_subparsers(dest="grade_cmd", required=True)
    p_grade_audit = grade_sub.add_parser(
        "audit",
        help="Audit series ledger counts and performance views (read-only)",
    )
    p_grade_audit.add_argument(
        "--series",
        default="dwcs",
        choices=["dwcs"],
        help="Series to audit (default: dwcs)",
    )
    p_grade_audit.add_argument(
        "--database-url",
        default=None,
        help=(
            "Explicit disposable SQLite URL (required when settings would resolve "
            "to live data/mma.db; never opens the live DB)"
        ),
    )
    p_grade_audit.add_argument(
        "--json",
        action="store_true",
        help="Print deterministic JSON audit (sorted keys)",
    )

    p_source = sub.add_parser("source", help="Source adapter utilities")
    source_sub = p_source.add_subparsers(dest="source_cmd", required=True)
    p_audit = source_sub.add_parser("audit", help="Audit a public source against manifests")
    audit_sub = p_audit.add_subparsers(dest="audit_source", required=True)
    p_ufc = audit_sub.add_parser(
        "ufcstats-public",
        help="Audit UFCStats public coverage for DWCS universe",
    )
    p_ufc.add_argument("--series", default="dwcs", choices=["dwcs"])
    p_ufc.add_argument("--years", default="2017:2025", help="Inclusive year range start:stop")
    p_ufc.add_argument("--json", action="store_true", help="Print JSON report")
    p_ufc.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="Optional fixture root (no network)",
    )
    p_ufc.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Temporary/ignored HTTP cache directory for live probes",
    )
    p_ufc.add_argument(
        "--live",
        action="store_true",
        help="Allow live network probe (operator-only; never used in CI)",
    )
    p_ufc.add_argument(
        "--robots-disallow",
        action="store_true",
        help="Force robots_disallow stop (kill-switch / test aid)",
    )
    p_ufc.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Optional path for sanitized aggregate audit summary JSON",
    )

    p_dwcs = sub.add_parser("dwcs", help="DWCS universe history utilities")
    dwcs_sub = p_dwcs.add_subparsers(dest="dwcs_cmd", required=True)
    p_sync_hist = dwcs_sub.add_parser(
        "sync-history",
        help="Ingest frozen DWCS manifest history into an explicit DB target",
    )
    p_sync_hist.add_argument(
        "--through",
        type=int,
        default=2025,
        help="Inclusive calendar year upper bound (default 2025)",
    )
    p_sync_hist.add_argument(
        "--database-url",
        required=True,
        help="Explicit disposable SQLite URL (never implied live data/mma.db)",
    )
    p_sync_hist.add_argument(
        "--raw-store",
        type=Path,
        required=True,
        help="Explicit disposable content-addressed raw store root",
    )
    p_sync_hist.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate/classify only; do not mutate the database",
    )
    p_sync_hist.add_argument(
        "--json",
        action="store_true",
        help="Print JSON report",
    )
    p_sync_hist.add_argument(
        "--events-manifest",
        type=Path,
        default=None,
        help="Optional override path for dwcs_events_v1.jsonl",
    )
    p_sync_hist.add_argument(
        "--bouts-manifest",
        type=Path,
        default=None,
        help="Optional override path for dwcs_bouts_v1.jsonl",
    )

    p_identity = sub.add_parser("identity", help="Deterministic identity resolution utilities")
    identity_sub = p_identity.add_subparsers(dest="identity_cmd", required=True)

    def _add_identity_db_args(parser: argparse.ArgumentParser, *, mutating: bool) -> None:
        parser.add_argument(
            "--database-url",
            required=True,
            help="Explicit SQLite URL (never implied live data/mma.db)",
        )
        parser.add_argument("--json", action="store_true", help="Print JSON output")
        if mutating:
            parser.add_argument(
                "--allow-user-db",
                action="store_true",
                help=(
                    "Required override to mutate a live/user DB path "
                    "(sqlite:///data/mma.db or MMA_DATABASE_URL default)"
                ),
            )
            parser.add_argument("--actor", required=True, help="Decision actor id")

    p_id_audit = identity_sub.add_parser("audit", help="Read-only identity audit report")
    _add_identity_db_args(p_id_audit, mutating=False)
    p_id_audit.add_argument("--series", default="dwcs", help="Series label for report")

    p_id_list = identity_sub.add_parser("list", help="Read-only list review queue rows")
    _add_identity_db_args(p_id_list, mutating=False)
    p_id_list.add_argument(
        "--status",
        default="pending",
        choices=["pending", "approved", "rejected", "reversed", "all"],
        help="Filter by review status",
    )

    p_id_approve = identity_sub.add_parser("approve", help="Approve a pending review")
    _add_identity_db_args(p_id_approve, mutating=True)
    p_id_approve.add_argument("--review-id", required=True, help="Explicit review id")
    p_id_approve.add_argument(
        "--canonical-id",
        required=True,
        help="Explicit canonical fighter id (must exist)",
    )
    p_id_approve.add_argument(
        "--expected-version",
        type=int,
        default=None,
        help="Optional optimistic lock version",
    )

    p_id_reject = identity_sub.add_parser("reject", help="Reject a pending review")
    _add_identity_db_args(p_id_reject, mutating=True)
    p_id_reject.add_argument("--review-id", required=True, help="Explicit review id")
    p_id_reject.add_argument(
        "--expected-version",
        type=int,
        default=None,
        help="Optional optimistic lock version",
    )

    p_id_reverse = identity_sub.add_parser("reverse", help="Reverse an approved or rejected review")
    _add_identity_db_args(p_id_reverse, mutating=True)
    p_id_reverse.add_argument("--review-id", required=True, help="Explicit review id")
    p_id_reverse.add_argument(
        "--expected-version",
        type=int,
        default=None,
        help="Optional optimistic lock version",
    )

    p_history = sub.add_parser("history", help="Regional/pre-UFC history utilities")
    history_sub = p_history.add_subparsers(dest="history_cmd", required=True)
    p_h_sync = history_sub.add_parser(
        "sync",
        help="Sync upcoming-DWCS regional history from public sources",
    )
    p_h_sync.add_argument(
        "--fighters",
        required=True,
        choices=["upcoming-dwcs"],
        help="Fighter selection (upcoming-dwcs seed or scheduled DWCS events)",
    )
    p_h_sync.add_argument("--database-url", required=True, help="Explicit disposable SQLite URL")
    p_h_sync.add_argument(
        "--raw-store",
        type=Path,
        required=True,
        help="Explicit disposable content-addressed raw store root",
    )
    p_h_sync.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="Offline fixture root with tapology/sherdog/combat_registry dirs",
    )
    p_h_sync.add_argument("--dry-run", action="store_true")
    p_h_sync.add_argument("--json", action="store_true")
    p_h_sync.add_argument(
        "--live",
        action="store_true",
        help="Allow bounded live fetches; refused without this flag",
    )
    p_h_sync.add_argument("--cache-dir", type=Path, default=None)

    p_h_audit = history_sub.add_parser(
        "audit",
        help="Audit reconstructed regional coverage for a year range",
    )
    p_h_audit.add_argument("--years", required=True, help="Year or start:stop inclusive")
    p_h_audit.add_argument("--database-url", required=True, help="Explicit disposable SQLite URL")
    p_h_audit.add_argument("--json", action="store_true")
    p_h_audit.add_argument("--live", action="store_true")
    p_h_audit.add_argument("--cache-dir", type=Path, default=None)
    p_h_audit.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Optional sanitized aggregate JSON path",
    )
    p_h_audit.add_argument(
        "--coverage-doc",
        type=Path,
        default=None,
        help="Optional markdown coverage path",
    )

    p_cov = sub.add_parser(
        "coverage",
        help="Publish DWCS coverage tiers and strict data-health gates",
    )
    p_cov.add_argument("--series", default="dwcs", choices=["dwcs"])
    p_cov.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 when any affected blocking gate fails",
    )
    p_cov.add_argument("--json", action="store_true", help="Print JSON report")
    p_cov.add_argument(
        "--database-url",
        required=True,
        help="Explicit disposable SQLite URL (never implied live data/mma.db)",
    )
    p_cov.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Optional sanitized aggregate JSON path",
    )
    p_cov.add_argument(
        "--coverage-doc",
        type=Path,
        default=None,
        help="Optional markdown coverage path",
    )
    p_cov.add_argument(
        "--raw-store",
        type=Path,
        default=None,
        help="Optional content-addressed raw store for referenced blob verification",
    )

    p_health = sub.add_parser(
        "health",
        help="Emit DWCS operational health contract (DWCS-403)",
    )
    p_health.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 when any red/blocker component is present",
    )
    p_health.add_argument("--json", action="store_true", help="Print sorted-key JSON")
    p_health.add_argument(
        "--database-url",
        default=None,
        help="Optional disposable SQLite URL (never live data/mma.db)",
    )
    p_health.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Optional component state JSON snapshot for health assembly",
    )
    p_health.add_argument(
        "--series",
        default="dwcs",
        choices=["dwcs"],
    )

    p_publish = sub.add_parser(
        "publish",
        help="Build and atomically publish versioned dashboard JSON (DWCS-500)",
    )
    p_publish.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output root for releases/ + current pointer",
    )
    p_publish.add_argument(
        "--database-url",
        default=None,
        help="Disposable SQLite URL (never live data/mma.db)",
    )
    p_publish.add_argument(
        "--event-id",
        default=None,
        help="Optional event id to project; defaults to all latest publications",
    )
    p_publish.add_argument(
        "--release-id",
        default=None,
        help="Optional release id (default: release-<timestamp>)",
    )
    p_publish.add_argument(
        "--window-slot",
        default="manual",
        help="Window slot label recorded in release.json",
    )

    p_public = sub.add_parser(
        "public",
        help="Public static root asset/JSON coexistence tools (DWCS-502)",
    )
    public_sub = p_public.add_subparsers(dest="public_cmd", required=True)
    p_public_sync = public_sub.add_parser(
        "sync-assets",
        help=(
            "Copy web build into the public mount without deleting releases/, "
            "current, or last-known-good dashboard JSON"
        ),
    )
    p_public_sync.add_argument(
        "--from",
        dest="web_dist",
        type=Path,
        required=True,
        help="Web dist directory (e.g. /opt/mma/web)",
    )
    p_public_sync.add_argument(
        "--to",
        dest="public_root",
        type=Path,
        required=True,
        help="Public static root (e.g. /public)",
    )

    p_feat = sub.add_parser("features", help="Cutoff-aware PIT feature tools")
    feat_sub = p_feat.add_subparsers(dest="features_cmd", required=True)
    p_feat_audit = feat_sub.add_parser(
        "audit",
        help="Audit PIT feature future-invariance (DWCS-301)",
    )
    p_feat_audit.add_argument("--series", default="dwcs", choices=["dwcs"])
    p_feat_audit.add_argument(
        "--future-invariance",
        action="store_true",
        help="Fail if appending later observations changes a past feature row",
    )
    p_feat_audit.add_argument(
        "--database-url",
        default=None,
        help="Optional disposable SQLite URL (never implied live data/mma.db)",
    )

    p_eval = sub.add_parser(
        "evaluation",
        help="Frozen evaluation contract tools (DWCS-302)",
    )
    eval_sub = p_eval.add_subparsers(dest="evaluation_cmd", required=True)
    p_inspect = eval_sub.add_parser(
        "inspect-folds",
        help="Inspect event-grouped rolling-origin folds",
    )
    p_inspect.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Evaluation contract path",
    )
    p_inspect.add_argument(
        "--database-url",
        default=None,
        help="Optional disposable SQLite URL (never implied live data/mma.db)",
    )
    p_inspect.add_argument(
        "--fixture",
        choices=("manifest", "protocol"),
        default="manifest",
        help="Card source when --database-url is omitted (default: frozen 89-card DWCS manifest)",
    )
    p_inspect.add_argument(
        "--include-holdout",
        action="store_true",
        help="List locked 2025 holdout folds (omitted by default)",
    )
    p_inspect.add_argument("--json", action="store_true", help="Print JSON fold plan")

    p_model = sub.add_parser(
        "model",
        help="Versioned M1/M2 train through event-grouped folds (DWCS-303/304)",
    )
    model_sub = p_model.add_subparsers(dest="model_cmd", required=True)
    p_mtrain = model_sub.add_parser(
        "train",
        help="Train ridge (M1) or joint competing-risks (M2); never 2025 holdout",
    )
    p_mtrain.add_argument(
        "--spec",
        type=Path,
        default=Path("config/model_specs/ridge_v1.yaml"),
        help="Model spec (ridge_v1.yaml or joint_v1.yaml)",
    )
    p_mtrain.add_argument(
        "--model",
        choices=("auto", "ridge", "joint"),
        default="auto",
        help="Estimator family; auto dispatches from --spec (ridge vs joint)",
    )
    p_mtrain.add_argument(
        "--output",
        type=Path,
        default=Path("data/artifacts/ridge_v1.json"),
        help="Versioned JSON artifact path (sidecar manifest written next to it)",
    )
    p_mtrain.add_argument(
        "--database-url",
        default=None,
        help="Optional disposable SQLite URL (never live data/mma.db)",
    )
    p_mtrain.add_argument(
        "--fixture",
        choices=("protocol", "manifest"),
        default="protocol",
        help="Card source when --database-url is omitted (default: protocol fixture)",
    )
    p_mtrain.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Evaluation contract path (hash-verified and used for folds)",
    )
    p_mpred = model_sub.add_parser(
        "predict",
        help="Score a matchup from a versioned JSON ridge or joint artifact",
    )
    p_mpred.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help="Versioned JSON artifact from mma-model model train",
    )
    p_mpred.add_argument(
        "--features-json",
        type=Path,
        default=None,
        help="JSON object with names (FEATURE_NAMES order) and values",
    )
    p_mpred.add_argument(
        "--fixture",
        choices=("protocol",),
        default=None,
        help="Optional protocol fixture instead of --features-json",
    )
    p_mpred.add_argument(
        "--bout-id",
        default=None,
        help="Protocol bout id when --fixture protocol is set",
    )
    p_mcal = model_sub.add_parser(
        "calibrate",
        help="Fit prior-time OOF calibration and event-block bootstrap (DWCS-305)",
    )
    p_mcal.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help="Versioned JSON ridge or joint artifact from model train",
    )
    p_mcal.add_argument(
        "--contract",
        type=Path,
        default=Path("config/evaluation/dwcs_v1.json"),
        help="Evaluation contract path (hash-verified; never mutated)",
    )
    p_mcal.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Calibrated artifact path (default: <stem>.calibrated.json)",
    )
    p_mcal.add_argument(
        "--database-url",
        default=None,
        help="Optional disposable SQLite URL (never live data/mma.db)",
    )
    p_mcal.add_argument(
        "--fixture",
        choices=("protocol",),
        default="protocol",
        help="Reconstruct source samples without live DB (default: protocol)",
    )
    p_mcal.add_argument(
        "--bootstrap-replicates",
        type=int,
        default=DEFAULT_BOOTSTRAP_REPLICATES,
        help=(
            "Event-block refits (production default 200). "
            "Values other than 200 mark the artifact non-production"
        ),
    )
    p_mcal.add_argument(
        "--bootstrap-seed",
        type=int,
        default=None,
        help="Pinned bootstrap seed (default: module default)",
    )
    p_mretrain = model_sub.add_parser(
        "retrain",
        help="Fixed-spec champion retrain after +24h reconciliation (DWCS-402)",
    )
    p_mretrain.add_argument(
        "--registry",
        type=Path,
        default=Path("config/model_registry.yaml"),
        help="Champion/challenger registry YAML",
    )
    p_mretrain.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("data/artifacts"),
        help="Digest-addressed artifact store directory",
    )
    p_mretrain.add_argument(
        "--database-url",
        default=None,
        help="Disposable SQLite URL for decision audit (never live data/mma.db)",
    )
    p_mpromote = model_sub.add_parser(
        "promote",
        help="Promote a shadow candidate after frozen evaluator gates (DWCS-402)",
    )
    p_mpromote.add_argument(
        "--candidate",
        required=True,
        help="Candidate artifact digest (sha256)",
    )
    p_mpromote.add_argument(
        "--evaluate",
        action="store_true",
        required=True,
        help="Required: run frozen evaluator + health/holdout/artifact gates",
    )
    p_mpromote.add_argument(
        "--registry",
        type=Path,
        default=Path("config/model_registry.yaml"),
        help="Champion/challenger registry YAML",
    )
    p_mpromote.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("data/artifacts"),
        help="Digest-addressed artifact store directory",
    )
    p_mpromote.add_argument(
        "--database-url",
        default=None,
        help="Disposable SQLite URL for decision audit (never live data/mma.db)",
    )
    p_mpromote.add_argument(
        "--reason",
        default="manual promote after gates",
        help="Promotion reason recorded in the decision ledger",
    )
    p_mpromote.add_argument(
        "--artifact",
        type=Path,
        default=None,
        help="Optional explicit candidate artifact path",
    )
    p_mrollback = model_sub.add_parser(
        "rollback",
        help="Restore prior champion digest without DB rollback (DWCS-402)",
    )
    p_mrollback.add_argument(
        "--registry",
        type=Path,
        default=Path("config/model_registry.yaml"),
        help="Champion/challenger registry YAML",
    )
    p_mrollback.add_argument(
        "--database-url",
        default=None,
        help="Disposable SQLite URL for decision audit (never live data/mma.db)",
    )
    p_mrollback.add_argument(
        "--reason",
        default="manual rollback to prior champion",
        help="Rollback reason recorded in the decision ledger",
    )

    args = p.parse_args(argv)

    if args.cmd == "init-db":
        init_db()
        print("Database initialized at", get_settings().mma_database_url)
        return 0

    if args.cmd == "sync":
        init_db()
        client = UFCStatsClient()
        try:
            with session_scope() as session:
                stats = sync_pipeline(
                    session,
                    client,
                    profile_name=args.profile,
                    resume=args.resume,
                    reset_cursor=args.reset_cursor,
                )
                print(json.dumps(stats, indent=2))
        finally:
            client.close()
        return 0

    if args.cmd == "odds":
        odds_cmd = getattr(args, "odds_cmd", None)
        if odds_cmd is None:
            data = fetch_mma_odds()
            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(f"Events with odds: {len(data)}")
            return 0

        if odds_cmd == "audit-bookmakers":
            report = run_bookmaker_audit(next_dwcs=bool(getattr(args, "next_dwcs", False)))
            print(json.dumps(report, indent=2))
            return 0 if not report.get("scraper_paths_present") else 2

        if odds_cmd == "due":
            as_of_raw = str(args.as_of).strip()
            if as_of_raw.endswith("Z"):
                as_of_raw = as_of_raw[:-1] + "+00:00"
            as_of_dt = datetime.fromisoformat(as_of_raw)
            if as_of_dt.tzinfo is None:
                print("--as-of must be timezone-aware UTC")
                return 2
            event_ids = getattr(args, "event_id", None) or []
            event_start = getattr(args, "event_start", None)
            database_url = getattr(args, "database_url", None)
            remaining_override = getattr(args, "remaining_override", None)
            engine = None
            SessionLocal = None
            if event_ids and event_start:
                # Synthetic single-event path for offline/unit checks only.
                events = [
                    {
                        "event_id": event_ids[0],
                        "event_start": event_start,
                    }
                ]
            else:
                if not database_url:
                    print(
                        "odds due requires --database-url for canonical upcoming "
                        "DWCS events (or synthetic --event-id with --event-start)"
                    )
                    return 2
                db_url = str(database_url).strip()
                engine = create_engine(db_url, future=True)
                _attach_sqlite_listeners(engine)
                SessionLocal = sessionmaker(bind=engine, future=True)
                with SessionLocal() as session:
                    events = load_upcoming_dwcs_events_from_db(
                        session, as_of=as_of_dt, series=args.series
                    )
                if event_ids:
                    wanted = set(event_ids)
                    events = [e for e in events if e["event_id"] in wanted]

            # Per-event success + purpose-aware quota (DB-backed live path).
            succeeded: dict[str, bool] = {}
            items_list = []
            if SessionLocal is not None:
                with SessionLocal() as session:
                    for event in events:
                        event_id = str(event["event_id"])
                        event_start = event["event_start"]
                        provisional = compute_due_work(
                            as_of=as_of_dt,
                            event_id=event_id,
                            event_start=event_start,
                            slot_already_succeeded=False,
                            provider="the_odds_api",
                            markets=args.markets,
                            region=parse_single_region(args.regions),
                        )
                        key = provisional.idempotency_key or ""
                        already = bool(
                            key and slot_succeeded(session, idempotency_key=key)
                        )
                        if key:
                            succeeded[key] = already
                        purpose = provisional.purpose or RequestPurpose.LIVE_ORDINARY
                        budget = plan_request_budget(
                            session,
                            endpoint="current_odds",
                            markets=args.markets,
                            regions=parse_single_region(args.regions),
                            provider="the_odds_api",
                            as_of=as_of_dt,
                            purpose=purpose,
                            remaining_override=remaining_override,
                        )
                        quota_state = None
                        if budget.state == QuotaBudgetState.EXHAUSTED:
                            quota_state = "exhausted"
                        elif budget.state == QuotaBudgetState.DEFERRED:
                            quota_state = "deferred"
                        items_list.append(
                            compute_due_work(
                                as_of=as_of_dt,
                                event_id=event_id,
                                event_start=event_start,
                                slot_already_succeeded=already,
                                provider="the_odds_api",
                                markets=args.markets,
                                region=parse_single_region(args.regions),
                                quota_state=quota_state,
                            )
                        )
                items = tuple(items_list)
            else:
                items = compute_due_work_for_events(
                    as_of=as_of_dt,
                    events=events,
                    slot_succeeded_keys=succeeded,
                    provider="the_odds_api",
                    markets=args.markets,
                    region=parse_single_region(args.regions),
                    quota_state=None,
                )
            if engine is not None:
                engine.dispose()
            payload = {
                "as_of": as_of_dt.isoformat().replace("+00:00", "Z"),
                "series": args.series,
                "upcoming_event_count": len(events),
                "due": [
                    {
                        "event_id": item.event_id,
                        "action": item.action.value,
                        "reason": item.reason,
                        "window_name": item.window_name,
                        "idempotency_key": item.idempotency_key,
                        "estimated_cost": item.estimated_cost,
                        "purpose": None if item.purpose is None else item.purpose.value,
                    }
                    for item in items
                    if item.action == DueAction.DUE
                ],
                "no_op": sum(1 for item in items if item.action == DueAction.NO_OP),
                "deferred_quota": sum(
                    1 for item in items if item.action == DueAction.DEFERRED_QUOTA
                ),
                "exhausted_quota": sum(
                    1 for item in items if item.action == DueAction.EXHAUSTED_QUOTA
                ),
                "total": len(items),
            }
            if not events:
                payload["reason"] = "canonical_db_returned_zero_upcoming_dwcs_events"
            print(json.dumps(payload, indent=2, default=str))
            return 0

        if odds_cmd == "backfill":
            as_of_raw = getattr(args, "as_of", None)
            if not as_of_raw:
                print("--as-of is required for deterministic backfill")
                return 2
            text_as_of = str(as_of_raw).strip()
            if text_as_of.endswith("Z"):
                text_as_of = text_as_of[:-1] + "+00:00"
            as_of_dt = datetime.fromisoformat(text_as_of)
            if as_of_dt.tzinfo is None:
                print("--as-of must be timezone-aware UTC")
                return 2
            offline = bool(getattr(args, "offline_fixtures", False))
            fixture_dir = getattr(args, "fixture_dir", None)
            database_url = getattr(args, "database_url", None)
            events = load_dwcs_schedule_events(from_year=int(args.from_year))
            limit = getattr(args, "limit_events", None)
            if limit is not None:
                events = events[: int(limit)]
            if offline:
                db_url = require_disposable_database_url(database_url)
            elif database_url:
                db_url = str(database_url).strip()
            else:
                print("backfill requires --database-url (use disposable URL offline)")
                return 2
            resolve_odds_client(
                provider="the-odds-api",
                fixture_dir=fixture_dir,
                offline_fixtures=offline,
            )
            engine = create_engine(db_url, future=True)
            _attach_sqlite_listeners(engine)
            root = get_settings().project_root
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")
            SessionLocal = sessionmaker(bind=engine, future=True)
            with SessionLocal() as session:
                result = run_odds_backfill(
                    session,
                    series=args.series,
                    from_year=int(args.from_year),
                    events=events,
                    as_of=as_of_dt,
                    finished_at=as_of_dt,
                    markets=args.markets,
                    region=parse_single_region(args.regions),
                    offline_fixtures=offline,
                    fixture_dir=fixture_dir,
                    evaluation_contract_path=Path(args.contract),
                    execute=not bool(getattr(args, "dry_run", False)),
                    remaining_override=(
                        None
                        if getattr(args, "remaining_override", None) is None
                        else validate_remaining_override(args.remaining_override)[0]
                    ),
                    allow_bootstrap=bool(getattr(args, "quota_bootstrap", True)),
                )
                session.commit()
            engine.dispose()
            print(json.dumps(result.as_dict(), indent=2, default=str))
            return 0 if result.failed == 0 else 2


        if odds_cmd == "reconcile":
            provider_arg = str(getattr(args, "provider", "the-odds-api")).strip()
            if provider_arg in {"the-odds-api", "the_odds_api"}:
                provider = "the_odds_api"
            else:
                print(f"unsupported odds provider for reconcile: {provider_arg!r}")
                return 2
            database_url = getattr(args, "database_url", None)
            golden = getattr(args, "golden_card", None)
            offline = bool(getattr(args, "offline_fixtures", False))
            strict = bool(getattr(args, "strict", False))
            next_dwcs = bool(getattr(args, "next_dwcs", False))
            as_of_raw = getattr(args, "as_of", None)
            try:
                as_of = None
                if as_of_raw:
                    text = str(as_of_raw).strip()
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    as_of_dt = datetime.fromisoformat(text)
                    if as_of_dt.tzinfo is None:
                        raise ValueError("--as-of must be timezone-aware UTC")
                    as_of = as_of_dt

                if golden is not None:
                    if not offline:
                        raise OddsReconcileError(
                            "--golden-card requires --offline-fixtures and a "
                            "disposable --database-url"
                        )
                    db_url = require_disposable_database_url(database_url)
                elif database_url:
                    db_url = str(database_url).strip()
                    if not db_url:
                        print("refusing empty --database-url")
                        return 2
                else:
                    db_url = None

                if db_url is not None:
                    engine = create_engine(db_url, future=True)
                    _attach_sqlite_listeners(engine)
                    root = get_settings().project_root
                    cfg = Config(str(root / "alembic.ini"))
                    cfg.set_main_option("script_location", str(root / "migrations"))
                    cfg.set_main_option("sqlalchemy.url", db_url)
                    command.upgrade(cfg, "head")
                    Session = sessionmaker(bind=engine, future=True)
                    with Session() as session:
                        report = run_odds_reconcile(
                            session,
                            next_dwcs=next_dwcs,
                            strict=strict,
                            golden_card_path=golden,
                            provider=provider,
                            as_of=as_of,
                            offline_fixtures=offline,
                            database_url=db_url,
                            allow_golden_seed=bool(golden is not None and offline),
                        )
                        session.commit()
                    engine.dispose()
                else:
                    if golden is not None:
                        raise OddsReconcileError(
                            "--golden-card refuses the default/live database"
                        )
                    init_db()
                    with session_scope() as session:
                        report = run_odds_reconcile(
                            session,
                            next_dwcs=next_dwcs,
                            strict=strict,
                            golden_card_path=None,
                            provider=provider,
                            as_of=as_of,
                            offline_fixtures=False,
                            database_url=None,
                            allow_golden_seed=False,
                        )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                OddsReconcileError,
                OddsOfflineModeError,
            ) as exc:
                print(str(exc))
                return 2
            print(json.dumps(report, indent=2))
            if strict and report.get("blockers"):
                return 2
            return 0

        if odds_cmd == "price-guidance":
            try:
                family = MarketFamily(args.family)
                line_point = getattr(args, "line_point", None)
                if family is MarketFamily.TOTALS and line_point is None:
                    raise ValueError("--line-point is required for totals")
                if family is not MarketFamily.TOTALS and line_point is not None:
                    raise ValueError("--line-point is only valid for totals")
                observed = None
                obs_path = getattr(args, "observation_json", None)
                if obs_path is not None:
                    payload = json.loads(Path(obs_path).read_text(encoding="utf-8"))
                    observed = parse_manual_price_observation(payload)
                row = build_price_guidance(
                    family=family,
                    outcome_key=OutcomeKey(args.outcome),
                    maturity=MarketMaturity(args.maturity),
                    p50=float(args.p50),
                    p25=float(args.p25),
                    gates_pass=True,
                    observed=observed,
                    line_point=line_point,
                    prob_ev_positive=getattr(args, "prob_ev_positive", None),
                )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                PriceGuidanceSelectionError,
            ) as exc:
                print(str(exc))
                return 2
            print(json.dumps(row.as_dict(), indent=2))
            return 0

        if odds_cmd == "record-manual-price":
            try:
                payload = json.loads(
                    Path(args.observation_json).read_text(encoding="utf-8")
                )
                observed = parse_manual_price_observation(payload)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(str(exc))
                return 2
            database_url = getattr(args, "database_url", None)
            if database_url:
                db_url = str(database_url).strip()
                if not db_url:
                    print("refusing empty --database-url")
                    return 2
                engine = create_engine(db_url, future=True)
                _attach_sqlite_listeners(engine)
                root = get_settings().project_root
                cfg = Config(str(root / "alembic.ini"))
                cfg.set_main_option("script_location", str(root / "migrations"))
                cfg.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(cfg, "head")
                Session = sessionmaker(bind=engine, future=True)
                with Session() as session:
                    result = OddsQuoteStore(session).append_manual_prices([observed])
                    session.commit()
                    print(
                        json.dumps(
                            {
                                "inserted": result.inserted,
                                "deduped": result.deduped,
                                "observation": observed.as_identity_dict(),
                            },
                            indent=2,
                        )
                    )
                engine.dispose()
                return 0
            init_db()
            with session_scope() as session:
                result = OddsQuoteStore(session).append_manual_prices([observed])
                print(
                    json.dumps(
                        {
                            "inserted": result.inserted,
                            "deduped": result.deduped,
                            "observation": observed.as_identity_dict(),
                        },
                        indent=2,
                    )
                )
            return 0

        offline = bool(getattr(args, "offline_fixtures", False))
        fixture_dir = getattr(args, "fixture_dir", None)
        database_url = getattr(args, "database_url", None)
        # Fail closed before any migration/write when config is invalid.
        try:
            validate_requested_series(args.series)
            parse_single_region(args.regions)
            resolve_odds_client(
                provider=args.provider,
                fixture_dir=fixture_dir,
                offline_fixtures=offline,
            )
            if offline:
                db_url = require_disposable_database_url(database_url)
            elif database_url:
                db_url = str(database_url).strip()
                if not db_url:
                    print("refusing empty --database-url")
                    return 2
            else:
                db_url = None
        except (OddsConfigurationError, OddsOfflineModeError, OddsApiError, ValueError) as exc:
            print(str(exc))
            return 2

        try:
            if db_url is not None:
                engine = create_engine(db_url, future=True)
                _attach_sqlite_listeners(engine)
                root = get_settings().project_root
                cfg = Config(str(root / "alembic.ini"))
                cfg.set_main_option("script_location", str(root / "migrations"))
                cfg.set_main_option("sqlalchemy.url", db_url)
                command.upgrade(cfg, "head")
                Session = sessionmaker(bind=engine, future=True)
                with Session() as session:
                    if odds_cmd == "snapshot":
                        result = run_odds_snapshot(
                            session,
                            series=args.series,
                            provider=args.provider,
                            markets=args.markets,
                            regions=args.regions,
                            historical_date=args.historical_date,
                            fixture_dir=fixture_dir,
                            offline_fixtures=offline,
                        )
                        session.commit()
                        print(json.dumps(result.as_dict(), indent=2))
                    elif odds_cmd == "audit":
                        report = run_odds_audit(
                            session,
                            series=args.series,
                            provider=args.provider,
                            markets=args.markets,
                            regions=args.regions,
                            fixture_dir=fixture_dir,
                            offline_fixtures=offline,
                        )
                        session.commit()
                        print(json.dumps(report, indent=2))
                    else:
                        print(f"unsupported odds command: {odds_cmd}")
                        engine.dispose()
                        return 1
                engine.dispose()
                return 0

            init_db()
            with session_scope() as session:
                if odds_cmd == "snapshot":
                    result = run_odds_snapshot(
                        session,
                        series=args.series,
                        provider=args.provider,
                        markets=args.markets,
                        regions=args.regions,
                        historical_date=args.historical_date,
                        fixture_dir=fixture_dir,
                        offline_fixtures=offline,
                    )
                    print(json.dumps(result.as_dict(), indent=2))
                elif odds_cmd == "audit":
                    report = run_odds_audit(
                        session,
                        series=args.series,
                        provider=args.provider,
                        markets=args.markets,
                        regions=args.regions,
                        fixture_dir=fixture_dir,
                        offline_fixtures=offline,
                    )
                    print(json.dumps(report, indent=2))
                else:
                    print(f"unsupported odds command: {odds_cmd}")
                    return 1
            return 0
        except (OddsConfigurationError, OddsOfflineModeError, OddsApiError, ValueError) as exc:
            print(str(exc))
            return 2


    if args.cmd == "jobs":
        if args.jobs_cmd == "snapshot-odds":
            as_of_raw = str(args.as_of).strip()
            if as_of_raw.endswith("Z"):
                as_of_raw = as_of_raw[:-1] + "+00:00"
            as_of_dt = datetime.fromisoformat(as_of_raw)
            if as_of_dt.tzinfo is None:
                print("--as-of must be timezone-aware UTC")
                return 2
            offline = bool(getattr(args, "offline_fixtures", False))
            fixture_dir = getattr(args, "fixture_dir", None)
            database_url = getattr(args, "database_url", None)
            if offline:
                db_url = require_disposable_database_url(database_url)
            elif database_url:
                db_url = str(database_url).strip()
            else:
                print("jobs snapshot-odds requires --database-url")
                return 2
            engine = create_engine(db_url, future=True)
            _attach_sqlite_listeners(engine)
            root = get_settings().project_root
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")
            SessionLocal = sessionmaker(bind=engine, future=True)
            with SessionLocal() as session:
                events = load_upcoming_dwcs_events_from_db(
                    session, as_of=as_of_dt, series=args.series
                )
                limit = getattr(args, "limit_events", None)
                if limit is not None:
                    events = events[: int(limit)]
                result = run_snapshot_odds_job(
                    session,
                    as_of=as_of_dt,
                    finished_at=as_of_dt,
                    events=events,
                    markets=args.markets,
                    region=parse_single_region(args.regions),
                    lock_path=getattr(args, "lock_path", None),
                    offline_fixtures=offline,
                    fixture_dir=fixture_dir,
                    execute=not bool(getattr(args, "dry_run", False)),
                    remaining_override=(
                        None
                        if getattr(args, "remaining_override", None) is None
                        else validate_remaining_override(args.remaining_override)[0]
                    ),
                    allow_bootstrap=bool(getattr(args, "quota_bootstrap", True)),
                )
                session.commit()
            engine.dispose()
            print(json.dumps(result.as_dict(), indent=2))
            # Zero upcoming is an explicit report, not a silent success with frozen history.
            return 0 if result.failures == 0 else 2
        if args.jobs_cmd == "tick":
            now_raw = str(args.now).strip()
            if now_raw.endswith("Z"):
                now_raw = now_raw[:-1] + "+00:00"
            try:
                now_dt = datetime.fromisoformat(now_raw)
            except ValueError:
                print("--now must be an ISO-8601 timezone-aware UTC datetime")
                return 2
            if now_dt.tzinfo is None:
                print("--now must be timezone-aware UTC")
                return 2

            events: list[EventContext] = []
            event_id = getattr(args, "event_id", None)
            event_start_raw = getattr(args, "event_start", None)
            if event_id and event_start_raw:
                start_raw = str(event_start_raw).strip()
                if start_raw.endswith("Z"):
                    start_raw = start_raw[:-1] + "+00:00"
                start_dt = datetime.fromisoformat(start_raw)
                if start_dt.tzinfo is None:
                    print("--event-start must be timezone-aware UTC")
                    return 2
                events.append(
                    EventContext(
                        event_id=str(event_id),
                        event_start=start_dt,
                        series=str(args.series),
                    )
                )
            elif event_id or event_start_raw:
                print("--event-id and --event-start must be provided together")
                return 2

            dry_run = bool(getattr(args, "dry_run", False))
            database_url = getattr(args, "database_url", None)

            if dry_run:
                # Dry-run may omit DB; still refuse accidental live URL if passed.
                if database_url is not None:
                    db_url = str(database_url).strip()
                    if not db_url:
                        print("refusing empty --database-url")
                        return 2
                    if is_refused_jobs_tick_database_url(db_url):
                        print(
                            "refusing live data/mma.db "
                            "--database-url for jobs tick"
                        )
                        return 2
                result = run_jobs_tick(
                    None,
                    as_of=now_dt,
                    events=events,
                    dry_run=True,
                    cadence=load_orchestrator_cadence(),
                    acquire_lock=False,
                )
                print(json.dumps(result.dry_run_plan(), sort_keys=True, indent=2))
                return 0

            if database_url is None:
                print("jobs tick requires --database-url (or --dry-run)")
                return 2
            db_url = str(database_url).strip()
            if not db_url:
                print("refusing empty --database-url")
                return 2
            if is_refused_jobs_tick_database_url(db_url):
                print("refusing live data/mma.db --database-url for jobs tick")
                return 2

            engine = create_engine(db_url, future=True)
            _attach_sqlite_listeners(engine)
            root = get_settings().project_root
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")
            SessionLocal = sessionmaker(bind=engine, future=True)
            lock_path = getattr(args, "lock_path", None)
            try:
                with SessionLocal() as session:
                    # When no explicit event is passed, load upcoming from DB.
                    if not events:
                        raw_events = load_upcoming_dwcs_events_from_db(
                            session, as_of=now_dt, series=args.series
                        )
                        for item in raw_events:
                            start_val = item.get("event_start") or item.get("start_time")
                            if start_val is None:
                                continue
                            if isinstance(start_val, datetime):
                                start_dt = start_val
                            else:
                                text = str(start_val).strip()
                                if text.endswith("Z"):
                                    text = text[:-1] + "+00:00"
                                start_dt = datetime.fromisoformat(text)
                            if start_dt.tzinfo is None:
                                continue
                            events.append(
                                EventContext(
                                    event_id=str(
                                        item.get("event_id") or item.get("id") or ""
                                    ),
                                    event_start=start_dt,
                                    series=str(args.series),
                                    bout_ids=tuple(
                                        str(b)
                                        for b in (item.get("bout_ids") or ())
                                    ),
                                )
                            )
                    lock = (
                        FileFlockLock(Path(lock_path))
                        if lock_path is not None
                        else FileFlockLock(Path("/tmp/mma-jobs-tick.lock"))
                    )
                    try:
                        result = run_jobs_tick(
                            session,
                            as_of=now_dt,
                            events=events,
                            dry_run=False,
                            lock=lock,
                            cadence=load_orchestrator_cadence(),
                        )
                    except TickOverlapError as exc:
                        print(f"jobs tick overlap: {exc}")
                        return 2
                    session.commit()
            finally:
                engine.dispose()
            print(json.dumps(result.as_dict(), sort_keys=True, indent=2))
            return 0 if result.failures == 0 else 2
        print(f"unsupported jobs command: {args.jobs_cmd}")
        return 1

    if args.cmd == "train":
        print(f"DEPRECATED: {DEPRECATED_RANDOM_SPLIT_NOTE}", file=sys.stderr)
        init_db()
        with session_scope() as session:
            out = train_and_save(session, args.output, min_prior_fights=args.min_prior_fights)
        print(json.dumps(out, indent=2))
        return 0

    if args.cmd == "predict-fight":
        print(
            "DEPRECATED: predict-fight loads the legacy unversioned joblib only; "
            "it cannot load DWCS-303 JSON artifacts. "
            "Random-split metrics are not betting evidence. "
            "Use mma-model model predict --artifact <json> for versioned models.",
            file=sys.stderr,
        )
        init_db()
        with session_scope() as session:
            prob = predict_fight_a_win_prob(session, args.fight_id, args.model)
        print(
            json.dumps(
                {
                    "deprecation": DEPRECATED_RANDOM_SPLIT_NOTE,
                    "fight_id": args.fight_id,
                    "p_fighter_a": prob,
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "backtest":
        if getattr(args, "backtest_cmd", None) == "run":
            if args.fixture == "protocol" and args.from_manifest:
                print(
                    "backtest configuration error: pass --fixture protocol or "
                    "--from-manifest, not both"
                )
                return EXIT_INTERNAL
            if args.fixture == "protocol" and args.database_url:
                print(
                    "backtest configuration error: pass --fixture protocol or "
                    "--database-url, not both"
                )
                return EXIT_INTERNAL
            generated_at = None
            if args.generated_at:
                try:
                    generated_at = datetime.fromisoformat(
                        str(args.generated_at).replace("Z", "+00:00")
                    )
                except ValueError:
                    print("backtest configuration error: --generated-at must be ISO-8601")
                    return EXIT_INTERNAL
            try:
                payload = execute_backtest_run(
                    contract_path=Path(args.contract),
                    output_dir=Path(args.output),
                    fixture=args.fixture,
                    from_manifest=bool(args.from_manifest) or args.fixture == "manifest",
                    database_url=args.database_url,
                    sealed_holdout=bool(args.sealed_holdout),
                    bootstrap_replicates=int(args.bootstrap_replicates),
                    bootstrap_seed=int(args.bootstrap_seed),
                    generated_at=generated_at,
                    default_database_url=get_settings().mma_database_url,
                    expected_data_hash=getattr(args, "expected_data_hash", None),
                    expected_model_hash=getattr(args, "expected_model_hash", None),
                    expected_calibration_hash=getattr(
                        args, "expected_calibration_hash", None
                    ),
                )
            except EvaluationContractError as exc:
                print(f"evaluation contract error: {exc}")
                return EXIT_INTERNAL
            except DatabaseMutationError as exc:
                print(f"backtest database error: {exc}")
                return EXIT_INTERNAL
            except EvidenceOverwriteError as exc:
                print(f"backtest evidence error: {exc}")
                return EXIT_STRICT_BLOCKERS
            except EvaluatorHashMismatchError as exc:
                print(f"evaluation hash mismatch: {exc}")
                return EXIT_INTERNAL
            except BacktestError as exc:
                print(f"backtest error: {exc}")
                return EXIT_INTERNAL
            except (HoldoutLockedError, ValueError) as exc:
                print(f"backtest error: {exc}")
                return EXIT_INTERNAL
            summary = {
                "accounting_evidence": payload.get("accounting_evidence"),
                "content_hash": payload.get("content_hash"),
                "evidence": payload.get("evidence"),
                "holdout": payload.get("holdout"),
                "output": payload.get("output"),
                "performance_evidence": payload.get("performance_evidence"),
                "production_qualified": payload.get("production_qualified"),
                "universe": payload.get("universe"),
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            return EXIT_OK
        out = walk_forward_backtest(
            None,
            min_train_fights=args.min_train,
            min_prior_fights=args.min_prior_fights,
            max_predictions=args.max_predictions,
        )
        print(json.dumps(out, indent=2, sort_keys=True))
        return EXIT_STRICT_BLOCKERS

    if args.cmd == "recommend":
        if getattr(args, "recommend_cmd", None) != "replay":
            print(f"unsupported recommend command: {getattr(args, 'recommend_cmd', None)}")
            return EXIT_INTERNAL
        try:
            payload = execute_recommend_replay(
                contract_path=Path(args.contract),
                backtest_json=args.backtest_json,
                fixture=args.fixture,
            )
        except EvaluationContractError as exc:
            print(f"evaluation contract error: {exc}")
            return EXIT_INTERNAL
        except (
            PolicyHashMismatch,
            PolicyContractDriftError,
            RecommendationPolicyError,
        ) as exc:
            print(f"recommendation policy error: {exc}")
            return EXIT_INTERNAL
        except RecommendReplayError as exc:
            print(f"recommend replay error: {exc}")
            return EXIT_INTERNAL
        if args.output is not None:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK

    if args.cmd == "grade":
        if getattr(args, "grade_cmd", None) != "audit":
            print(f"unsupported grade command: {getattr(args, 'grade_cmd', None)}")
            return EXIT_INTERNAL
        database_url = getattr(args, "database_url", None)
        default_url = get_settings().mma_database_url
        if database_url is None:
            db_url = str(default_url).strip()
            live = (
                db_url in LIVE_DB_URLS
                or db_url.endswith("/data/mma.db")
                or db_url.endswith("data/mma.db")
                or is_prohibited_live_url(db_url)
            )
        else:
            db_url = str(database_url).strip()
            live = (
                db_url in LIVE_DB_URLS
                or db_url.endswith("/data/mma.db")
                or db_url.endswith("data/mma.db")
                or is_prohibited_live_url(db_url, default_url=default_url)
            )
        if not db_url:
            print("grade audit error: empty database url")
            return EXIT_INTERNAL
        if live:
            print(
                "refusing default live data/mma.db; pass an explicit disposable "
                "--database-url for grade audit"
            )
            return EXIT_INTERNAL
        engine = None
        try:
            engine = create_engine(db_url, future=True)
            _attach_sqlite_listeners(engine)
            Session = sessionmaker(
                bind=engine, autoflush=False, autocommit=False, future=True
            )
            with Session() as session:
                audit = audit_series(session, series=str(args.series))
        except GradeLedgerError as exc:
            print(f"grade audit error: {exc}")
            return EXIT_INTERNAL
        except Exception as exc:
            print(f"grade audit error: {exc}")
            return EXIT_INTERNAL
        finally:
            if engine is not None:
                engine.dispose()
        print(json.dumps(audit.as_dict(), indent=2, sort_keys=True))
        return EXIT_OK

    if args.cmd == "source" and args.source_cmd == "audit":
        if args.audit_source != "ufcstats-public":
            print(f"unsupported audit source: {args.audit_source}")
            return 1
        years = _parse_years(args.years)
        client: UfcstatsPublicClient | None = None
        tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
        live_probe = not_run_live_probe_evidence()
        try:
            if args.fixture_root is not None:
                adapter = UfcstatsPublicAdapter.for_fixtures(
                    fixture_root=args.fixture_root
                )
            else:
                if not args.live:
                    print(
                        "refusing live audit without --live; "
                        "pass --fixture-root for offline fixture mode"
                    )
                    return 2
                cache_dir = args.cache_dir
                if cache_dir is None:
                    tmp_ctx = tempfile.TemporaryDirectory(prefix="ufcstats-audit-cache-")
                    cache_dir = Path(tmp_ctx.name)
                client = UfcstatsPublicClient(
                    cache_dir=cache_dir,
                    robots_disallow=args.robots_disallow,
                )
                # Exactly one bounded probe before manifest audit; raw pages stay in cache_dir.
                live_probe = run_bounded_live_probe(client=client.polite_http)
                adapter = UfcstatsPublicAdapter(client=client)
            report = adapter.audit_manifest_scope(years=years)
            probe_blocked = bool(live_probe.get("block_reason")) and live_probe.get(
                "result"
            ) == "BLOCKED"
            blocked = bool(report["blocked"]) or probe_blocked
            block_reason = report["block_reason"] or (
                live_probe.get("block_reason") if probe_blocked else None
            )
            summary = {
                "source": report["source"],
                "years": report["years"],
                "events_total": report["events_total"],
                "bouts_total": report["bouts_total"],
                "events": report["events"],
                "bouts": report["bouts"],
                "blocked": blocked,
                "block_reason": block_reason,
                "status": "BLOCKED" if blocked else "COMPLETED",
                "live_probe": live_probe,
            }
            report = {**report, "live_probe": live_probe, "blocked": blocked, "block_reason": block_reason}
            if args.summary_out is not None:
                args.summary_out.parent.mkdir(parents=True, exist_ok=True)
                args.summary_out.write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(json.dumps(summary, indent=2, sort_keys=True))
            return 2 if blocked else 0
        except SourceBlockedError as exc:
            payload = {
                "source": "ufcstats_public",
                "blocked": True,
                "block_reason": exc.reason,
                "status": "BLOCKED",
                "live_probe": live_probe,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 2
        finally:
            if client is not None:
                client.close()
            if tmp_ctx is not None:
                tmp_ctx.cleanup()

    if args.cmd == "dwcs" and args.dwcs_cmd == "sync-history":
        db_url = str(args.database_url).strip()
        if not db_url:
            print("refusing empty --database-url")
            return 2
        # Guard against accidental live DB use when operator omits an explicit temp target.
        if db_url in {"sqlite:///data/mma.db", "sqlite:///./data/mma.db"}:
            print(
                "refusing default live data/mma.db; pass an explicit disposable "
                "--database-url for DWCS-103 verification"
            )
            return 2

        engine = create_engine(db_url, future=True)
        _attach_sqlite_listeners(engine)
        if not args.dry_run:
            # Apply migrations against the explicit target only.
            root = get_settings().project_root
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            cfg.set_main_option("sqlalchemy.url", db_url)
            command.upgrade(cfg, "head")

        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        store = ContentAddressedRawStore(Path(args.raw_store))
        repo = IngestRepository(session_factory=Session, raw_store=store)
        try:
            report = sync_dwcs_history(
                through_year=int(args.through),
                repo=repo,
                session_factory=Session,
                adapter=None,
                events_path=args.events_manifest,
                bouts_path=args.bouts_manifest,
                dry_run=bool(args.dry_run),
                provider_blocked=True,
            )
            if args.json:
                print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
            else:
                print(report.human_summary())
            return 0
        finally:
            engine.dispose()

    if args.cmd == "identity":
        db_url = str(args.database_url).strip()
        if not db_url:
            print("refusing empty --database-url")
            return 2

        mutating = args.identity_cmd in {"approve", "reject", "reverse"}
        if mutating:
            default_url = get_settings().mma_database_url
            is_live = db_url in LIVE_DB_URLS or db_url == default_url
            if is_live and not bool(getattr(args, "allow_user_db", False)):
                print(
                    "refusing identity mutation against live/user DB; "
                    "pass --allow-user-db to override, or use an explicit disposable "
                    "--database-url"
                )
                return 2

        engine = create_engine(db_url, future=True)
        _attach_sqlite_listeners(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        try:
            if args.identity_cmd == "audit":
                try:
                    with Session() as session:
                        report = build_identity_audit(session, series=args.series)
                except ValueError as exc:
                    print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
                    return 2
                if args.json:
                    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
                else:
                    print(report.human_summary())
                return 0

            if args.identity_cmd == "list":
                status = None if args.status == "all" else args.status
                with Session() as session:
                    rows = list_reviews(session, status=status)
                    payload = {
                        "reviews": [
                            {
                                "id": r.id,
                                "status": r.status,
                                "version": r.version,
                                "source": r.source,
                                "external_id": r.external_id,
                                "display_name": r.display_name,
                                "normalized_name": r.normalized_name,
                                "rule_id": r.rule_id,
                                "candidate_canonical_ids_json": r.candidate_canonical_ids_json,
                                "bout_id": r.bout_id,
                            }
                            for r in rows
                        ]
                    }
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"reviews={len(payload['reviews'])} status={args.status}")
                    for row in payload["reviews"]:
                        print(
                            f"{row['id']} {row['status']} {row['source']}:{row['external_id']} "
                            f"{row['display_name']}"
                        )
                return 0

            if args.identity_cmd in {"approve", "reject"}:
                decision = "approve" if args.identity_cmd == "approve" else "reject"
                canonical_id = getattr(args, "canonical_id", None)
                try:
                    with Session() as session:
                        review = apply_review_decision(
                            session,
                            review_id=args.review_id,
                            decision=decision,
                            canonical_id=canonical_id,
                            actor=args.actor,
                            expected_version=args.expected_version,
                        )
                        session.commit()
                        payload = {
                            "review_id": review.id,
                            "status": review.status,
                            "decision_canonical_id": review.decision_canonical_id,
                            "version": review.version,
                            "actor": review.decided_by,
                        }
                except ReviewDecisionError as exc:
                    print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
                    return 2
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(
                        f"{decision} review={payload['review_id']} "
                        f"status={payload['status']} version={payload['version']}"
                    )
                return 0

            if args.identity_cmd == "reverse":
                try:
                    with Session() as session:
                        review = reverse_review_decision(
                            session,
                            review_id=args.review_id,
                            actor=args.actor,
                            expected_version=args.expected_version,
                        )
                        session.commit()
                        payload = {
                            "review_id": review.id,
                            "status": review.status,
                            "decision_canonical_id": review.decision_canonical_id,
                            "version": review.version,
                            "actor": review.decided_by,
                        }
                except ReviewDecisionError as exc:
                    print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
                    return 2
                if args.json:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(
                        f"reverse review={payload['review_id']} "
                        f"status={payload['status']} version={payload['version']}"
                    )
                return 0
        finally:
            engine.dispose()

    if args.cmd == "history":
        db_url = str(args.database_url).strip()
        if not db_url:
            print("refusing empty --database-url")
            return 2
        if db_url in LIVE_DB_URLS:
            print(
                "refusing default live data/mma.db; pass an explicit disposable "
                "--database-url for DWCS-105 verification"
            )
            return 2

        engine = create_engine(db_url, future=True)
        _attach_sqlite_listeners(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        try:
            if args.history_cmd == "sync":
                if args.fixture_root is None and not args.live:
                    print(
                        "refusing live history sync without --live; "
                        "pass --fixture-root for offline fixture mode"
                    )
                    return 2
                if not args.dry_run:
                    root = get_settings().project_root
                    cfg = Config(str(root / "alembic.ini"))
                    cfg.set_main_option("script_location", str(root / "migrations"))
                    cfg.set_main_option("sqlalchemy.url", db_url)
                    command.upgrade(cfg, "head")
                store = ContentAddressedRawStore(Path(args.raw_store))
                repo = IngestRepository(session_factory=Session, raw_store=store)
                fixture_roots = {}
                if args.fixture_root is not None:
                    for name in REGIONAL_FALLBACK_ORDER:
                        candidate = args.fixture_root / name
                        fixture_roots[name] = candidate if candidate.is_dir() else args.fixture_root
                clients = {}
                tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
                try:
                    if args.live and args.fixture_root is None:
                        cache_dir = args.cache_dir
                        if cache_dir is None:
                            tmp_ctx = tempfile.TemporaryDirectory(prefix="history-sync-cache-")
                            cache_dir = Path(tmp_ctx.name)
                        clients = {
                            "tapology_public": TapologyPublicClient(cache_dir=cache_dir / "tapology"),
                            "sherdog_public": SherdogPublicClient(cache_dir=cache_dir / "sherdog"),
                            "combat_registry": CombatRegistryPublicClient(
                                cache_dir=cache_dir / "combat_registry"
                            ),
                        }
                    with Session() as session:
                        fighters = load_upcoming_dwcs_fighters(session=session)
                    report = sync_regional_history(
                        repo=repo,
                        session_factory=Session,
                        fighters=fighters,
                        fixture_roots=fixture_roots or None,
                        clients=clients or None,
                        dry_run=bool(args.dry_run),
                    )
                    payload = report.to_dict()
                    if args.json:
                        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
                    else:
                        print(report.human_summary())
                        if report.blockers:
                            print("blockers: " + ", ".join(report.blockers))
                    if report.blockers or report.unresolved_source_ids:
                        return 2
                    return 0
                finally:
                    for client in clients.values():
                        client.close()
                    if tmp_ctx is not None:
                        tmp_ctx.cleanup()

            if args.history_cmd == "audit":
                years = _parse_years(args.years)
                probes = {
                    source: history_not_run_probe(source) for source in REGIONAL_FALLBACK_ORDER
                }
                tmp_ctx = None
                live_clients: dict[str, object] = {}
                try:
                    if args.live:
                        cache_dir = args.cache_dir
                        if cache_dir is None:
                            tmp_ctx = tempfile.TemporaryDirectory(prefix="history-audit-cache-")
                            cache_dir = Path(tmp_ctx.name)
                        live_clients = {
                            "tapology_public": TapologyPublicClient(cache_dir=cache_dir / "tapology"),
                            "sherdog_public": SherdogPublicClient(cache_dir=cache_dir / "sherdog"),
                            "combat_registry": CombatRegistryPublicClient(
                                cache_dir=cache_dir / "combat_registry"
                            ),
                        }
                        for source, (host, path) in PROBE_PATHS.items():
                            client = live_clients[source]
                            probes[source] = history_run_bounded_probe(
                                client=client.polite_http,
                                source=source,
                                host=host,
                                path_category=path,
                            )
                    with Session() as session:
                        probe_mode = "live" if args.live else "offline"
                        report = evaluate_sample_coverage(
                            session,
                            years=years,
                            live_probes={"probes": probes} if args.live else None,
                            probe_mode=probe_mode,
                        )
                        ok, blockers = coverage_gates_ok(report)
                        if args.coverage_doc is not None:
                            write_regional_coverage_doc(
                                report,
                                path=args.coverage_doc,
                                live_probes=report.probe_evidence or probes,
                            )
                    payload = {
                        **report.model_dump(mode="json"),
                        "years": {"start": years.start, "stop": years.stop - 1},
                        "live_probes": report.probe_evidence or probes,
                        "probe_evidence_source": report.probe_evidence_source,
                        "gates_ok": ok,
                        "blockers": list(blockers),
                    }
                    if args.summary_out is not None:
                        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
                        args.summary_out.write_text(
                            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
                            encoding="utf-8",
                        )
                    if args.json:
                        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
                    else:
                        print(
                            f"pro={report.professional_found}/{report.professional_n} "
                            f"am={report.amateur_found}/{report.amateur_n} "
                            f"agree={report.pre_fight_agreement_n}/{report.pre_fight_agreement_d} "
                            f"killed={len(report.source_failed)} hash={report.report_hash}"
                        )
                        if blockers:
                            print("blockers: " + ", ".join(blockers))
                            print(
                                "gates blocked; live unmeasured or insufficient "
                                "comparable records"
                            )
                    return 0 if ok else 2
                finally:
                    for client in live_clients.values():
                        client.close()
                    if tmp_ctx is not None:
                        tmp_ctx.cleanup()
        finally:
            engine.dispose()

    if args.cmd == "coverage":
        db_url = str(args.database_url).strip()
        if not db_url:
            print("coverage configuration error: empty --database-url")
            return EXIT_INTERNAL
        default_url = get_settings().mma_database_url
        if is_prohibited_live_url(db_url, default_url=default_url):
            print(
                "coverage configuration error: refusing default live data/mma.db; "
                "pass an explicit disposable --database-url for DWCS-106 coverage"
            )
            return EXIT_INTERNAL
        raw_store = None
        if args.raw_store is not None:
            raw_store = ContentAddressedRawStore(Path(args.raw_store))
        try:
            engine = open_readonly_sqlite_engine(db_url)
        except CoverageDatabaseError as exc:
            print(f"coverage configuration error: {exc}")
            return EXIT_INTERNAL
        Session = readonly_session_factory(engine)
        try:
            try:
                policy = load_source_policy()
                with Session() as session:
                    report = compute_coverage_report(
                        series=str(args.series),
                        session=session,
                        policy=policy,
                        db_url=db_url,
                        raw_store=raw_store,
                    )
                    report, gates = report_with_gates(report, policy)
                if report.raw_ref_integrity.unverifiable > 0:
                    print(
                        "coverage configuration error: --raw-store required to verify "
                        "referenced raw blobs"
                    )
                    return EXIT_INTERNAL
                if args.summary_out is not None and args.coverage_doc is not None:
                    write_coverage_evidence(
                        report,
                        gates,
                        json_path=args.summary_out,
                        markdown_path=args.coverage_doc,
                    )
                elif args.summary_out is not None or args.coverage_doc is not None:
                    print("coverage evidence requires both --summary-out and --coverage-doc")
                    return EXIT_INTERNAL
                if args.json:
                    print(dumps_report(report), end="")
                else:
                    print(human_report(report, gates, strict=bool(args.strict)))
            except CoverageSchemaError as exc:
                print(f"coverage schema error: {exc}")
                return EXIT_INTERNAL
            except CoverageDatabaseError as exc:
                print(f"coverage configuration error: {exc}")
                return EXIT_INTERNAL
            except ValueError as exc:
                print(f"coverage configuration error: {exc}")
                return EXIT_INTERNAL
            except Exception as exc:
                print(f"coverage internal error: {exc}")
                return EXIT_INTERNAL
            if args.strict:
                return int(gates.exit_code)
            return 0
        finally:
            engine.dispose()

    if args.cmd == "health":
        db_url = getattr(args, "database_url", None)
        if db_url is not None:
            raw_url = str(db_url).strip()
            if not raw_url:
                print("health configuration error: empty --database-url")
                return EXIT_INTERNAL
            default_url = get_settings().mma_database_url
            if (
                is_prohibited_live_url(raw_url, default_url=default_url)
                or raw_url in LIVE_DB_URLS
                or raw_url.endswith("/data/mma.db")
                or raw_url.endswith("data/mma.db")
            ):
                print("health configuration error: refusing live data/mma.db")
                return EXIT_INTERNAL
        try:
            if args.state is not None:
                report = load_health_state(Path(args.state))
            else:
                report = default_missing_report(series=str(args.series))
            validate_health_payload(report.to_dict())
        except (OSError, ValueError, json.JSONDecodeError, HealthSchemaError) as exc:
            print(f"health configuration error: {exc}")
            return EXIT_INTERNAL
        if args.json:
            print(dumps_health(report), end="")
        else:
            print(
                f"health rollup={report.rollup.value} ok={report.ok} "
                f"blockers={','.join(report.blocker_codes) or 'none'}"
            )
            for component in report.components:
                print(
                    f"  {component.name}: {component.status.value}/"
                    f"{component.severity.value} — {component.detail}"
                )
        if args.strict:
            return int(report.exit_code)
        return EXIT_OK

    if args.cmd == "publish":
        output = Path(args.output)
        db_url = getattr(args, "database_url", None)
        if db_url is None:
            db_url = get_settings().mma_database_url
        raw_url = str(db_url).strip()
        if not raw_url:
            print("publish configuration error: empty --database-url")
            return EXIT_INTERNAL
        default_url = get_settings().mma_database_url
        if (
            is_prohibited_live_url(raw_url, default_url=default_url)
            or raw_url in LIVE_DB_URLS
            or raw_url.endswith("/data/mma.db")
            or raw_url.endswith("data/mma.db")
        ):
            print("publish configuration error: refusing live data/mma.db")
            return EXIT_INTERNAL
        release_id = args.release_id or f"release-{datetime.now().strftime('%Y%m%dT%H%M%SZ')}"
        engine = create_engine(raw_url, future=True)
        _attach_sqlite_listeners(engine)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        try:
            with Session() as session:
                try:
                    outcome = publish_dashboard(
                        session,
                        output_root=output,
                        release_id=str(release_id),
                        event_id=args.event_id,
                        window_slot=str(args.window_slot),
                    )
                except PublishValidationError as exc:
                    print(f"publish validation failed; current unchanged: {exc}")
                    return EXIT_INTERNAL
                except Exception as exc:  # noqa: BLE001
                    print(f"publish failed: {exc}")
                    return EXIT_INTERNAL
            live_note = "live_promoted"
            if "live/ promote warning" in (outcome.detail or ""):
                live_note = "live_promote_warning"
                print(f"publish warning: {outcome.detail}")
            print(
                f"publish ok release_id={outcome.current_release_id} "
                f"root={output} {live_note}"
            )
            return EXIT_OK
        finally:
            engine.dispose()

    if args.cmd == "public":
        if args.public_cmd != "sync-assets":
            print(f"public configuration error: unknown command {args.public_cmd!r}")
            return EXIT_INTERNAL
        try:
            result = sync_web_assets(args.web_dist, args.public_root)
        except PublicSyncError as exc:
            print(f"public sync-assets failed; LKG left intact: {exc}")
            return EXIT_INTERNAL
        print(
            f"public sync-assets ok root={result.public_root} "
            f"copied={','.join(result.copied) or 'none'} "
            f"skipped={','.join(result.skipped) or 'none'}"
        )
        return EXIT_OK

    if args.cmd == "features":
        if args.features_cmd != "audit":
            print(f"features configuration error: unknown command {args.features_cmd!r}")
            return EXIT_INTERNAL
        if not args.future_invariance:
            print("features configuration error: audit requires --future-invariance")
            return EXIT_INTERNAL
        db_url = args.database_url
        if db_url is not None:
            db_url = str(db_url).strip()
            if not db_url:
                print("features configuration error: empty --database-url")
                return EXIT_INTERNAL
            default_url = get_settings().mma_database_url
            if is_prohibited_live_url(db_url, default_url=default_url):
                print(
                    "features configuration error: refusing default live data/mma.db; "
                    "pass an explicit disposable --database-url or omit it for the fixture"
                )
                return EXIT_INTERNAL
            try:
                engine = open_readonly_sqlite_engine(db_url)
            except CoverageDatabaseError as exc:
                print(f"features configuration error: {exc}")
                return EXIT_INTERNAL
            Session = readonly_session_factory(engine)
            try:
                try:
                    with Session() as session:
                        run_features_audit(
                            series=str(args.series),
                            future_invariance=True,
                            session=session,
                        )
                except FutureRowLeakageError as exc:
                    print(f"features future-invariance failed: {exc}")
                    return EXIT_STRICT_BLOCKERS
                except ValueError as exc:
                    print(f"features configuration error: {exc}")
                    return EXIT_INTERNAL
                print("features future-invariance ok")
                return 0
            finally:
                engine.dispose()
        try:
            run_features_audit(series=str(args.series), future_invariance=True)
        except FutureRowLeakageError as exc:
            print(f"features future-invariance failed: {exc}")
            return EXIT_STRICT_BLOCKERS
        except ValueError as exc:
            print(f"features configuration error: {exc}")
            return EXIT_INTERNAL
        print("features future-invariance ok")
        return 0

    if args.cmd == "evaluation":
        if args.evaluation_cmd != "inspect-folds":
            print(f"evaluation configuration error: unknown command {args.evaluation_cmd!r}")
            return EXIT_INTERNAL
        if args.fixture == "protocol" and args.database_url:
            print(
                "evaluation configuration error: pass --fixture protocol or --database-url, not both"
            )
            return EXIT_INTERNAL
        try:
            load_evaluation_contract(path=Path(args.contract))
        except EvaluationContractError as exc:
            print(f"evaluation contract error: {exc}")
            return EXIT_INTERNAL

        engine = None
        try:
            if args.database_url is not None:
                db_url = str(args.database_url).strip()
                if not db_url:
                    print("evaluation configuration error: empty --database-url")
                    return EXIT_INTERNAL
                default_url = get_settings().mma_database_url
                if is_prohibited_live_url(db_url, default_url=default_url):
                    print(
                        "evaluation configuration error: refusing default live data/mma.db; "
                        "pass an explicit disposable --database-url or omit it for the DWCS manifest"
                    )
                    return EXIT_INTERNAL
                engine = open_readonly_sqlite_engine(db_url)
                session_factory = readonly_session_factory(engine)
                with session_factory() as session:
                    cards = cards_from_session(session)
                require_target_cards = True
            elif args.fixture == "protocol":
                cards = protocol_fixture_cards()
                require_target_cards = False
            elif args.fixture == "manifest":
                cards = cards_from_manifest()
                require_target_cards = True
            else:
                print(f"evaluation configuration error: unknown fixture {args.fixture!r}")
                return EXIT_INTERNAL
            plan = inspect_folds(
                contract_path=Path(args.contract),
                include_holdout=bool(args.include_holdout),
                cards=cards,
                require_target_cards=require_target_cards,
            )
        except CoverageDatabaseError as exc:
            print(f"evaluation configuration error: {exc}")
            return EXIT_INTERNAL
        except HoldoutLockedError as exc:
            print(f"evaluation holdout locked: {exc}")
            return EXIT_INTERNAL
        except EvaluatorHashMismatchError as exc:
            print(f"evaluation hash mismatch: {exc}")
            return EXIT_INTERNAL
        except EvaluationContractError as exc:
            print(f"evaluation contract error: {exc}")
            return EXIT_INTERNAL
        except (SplitError, ValueError) as exc:
            print(f"evaluation configuration error: {exc}")
            return EXIT_INTERNAL
        finally:
            if engine is not None:
                engine.dispose()
        print(render_fold_plan(plan, as_json=bool(args.json)), end="")
        return 0

    if args.cmd == "model":
        if args.model_cmd == "predict":
            try:
                artifact_path = Path(args.artifact)
                payload_kind = peek_artifact_payload_kind(artifact_path)
                if args.features_json is not None and args.fixture is not None:
                    print(
                        "model configuration error: pass --features-json or "
                        "--fixture protocol, not both"
                    )
                    return EXIT_INTERNAL
                if payload_kind == JOINT_PAYLOAD_KIND:
                    loaded_joint = load_joint_artifact(artifact_path)
                    scheduled_rounds: int | None = None
                    if args.features_json is not None:
                        raw_features = json.loads(
                            Path(args.features_json).read_text(encoding="utf-8")
                        )
                        if not isinstance(raw_features, dict):
                            print(
                                "model configuration error: features-json root must be an object"
                            )
                            return EXIT_INTERNAL
                        if "scheduled_rounds" not in raw_features:
                            print(
                                "model configuration error: joint predict requires "
                                "scheduled_rounds in --features-json"
                            )
                            return EXIT_INTERNAL
                        scheduled_rounds = int(raw_features["scheduled_rounds"])
                        values = load_feature_vector(raw_features)
                        names = loaded_joint.predictor.feature_names
                        bout_id = None
                    elif args.fixture == "protocol":
                        bout_id = str(args.bout_id or "").strip()
                        if not bout_id:
                            print(
                                "model configuration error: --fixture protocol requires --bout-id"
                            )
                            return EXIT_INTERNAL
                        names, values, scheduled_rounds = protocol_joint_feature_vector(
                            bout_id
                        )
                    else:
                        print(
                            "model configuration error: pass --features-json or "
                            "--fixture protocol --bout-id"
                        )
                        return EXIT_INTERNAL
                    scored = predict_loaded_joint(
                        loaded_joint,
                        values,
                        scheduled_rounds=scheduled_rounds,
                    )
                    print(
                        json.dumps(
                            {
                                "artifact_path": str(artifact_path.resolve()),
                                "bout_id": bout_id,
                                "feature_names": list(names),
                                "frozen_probabilities": scored["frozen_probabilities"],
                                "model_id": loaded_joint.manifest.model_id,
                                "p_fighter_a": scored["p_fighter_a"],
                                "prediction_api": scored["prediction_api"],
                                "scheduled_rounds": scored["scheduled_rounds"],
                            },
                            indent=2,
                        )
                    )
                    return 0
                loaded = load_artifact(artifact_path)
                if args.features_json is not None:
                    raw_features = json.loads(
                        Path(args.features_json).read_text(encoding="utf-8")
                    )
                    if not isinstance(raw_features, dict):
                        print("model configuration error: features-json root must be an object")
                        return EXIT_INTERNAL
                    values = load_feature_vector(raw_features)
                    names = loaded.predictor.feature_names
                    bout_id = None
                elif args.fixture == "protocol":
                    bout_id = str(args.bout_id or "").strip()
                    if not bout_id:
                        print("model configuration error: --fixture protocol requires --bout-id")
                        return EXIT_INTERNAL
                    names, values = protocol_feature_vector(bout_id)
                else:
                    print(
                        "model configuration error: pass --features-json or "
                        "--fixture protocol --bout-id"
                    )
                    return EXIT_INTERNAL
                probability = predict_loaded_ridge(loaded, values)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            except ArtifactError as exc:
                print(f"model artifact error: {exc}")
                return EXIT_INTERNAL
            except (TrainError, HoldoutLockedError, SplitError, ValueError) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            print(
                json.dumps(
                    {
                        "artifact_path": str(Path(args.artifact).resolve()),
                        "bout_id": bout_id,
                        "feature_names": list(names),
                        "model_id": loaded.manifest.model_id,
                        "p_fighter_a": probability,
                    },
                    indent=2,
                )
            )
            return 0
        if args.model_cmd == "calibrate":
            try:
                contract = load_evaluation_contract(path=Path(args.contract))
            except EvaluationContractError as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            n_replicates = int(args.bootstrap_replicates)
            if n_replicates < 1:
                print("model configuration error: --bootstrap-replicates must be >= 1")
                return EXIT_INTERNAL
            seed = (
                DEFAULT_BOOTSTRAP_SEED
                if args.bootstrap_seed is None
                else int(args.bootstrap_seed)
            )
            engine = None
            try:
                if args.database_url is not None:
                    db_url = str(args.database_url).strip()
                    if not db_url:
                        print("model configuration error: empty --database-url")
                        return EXIT_INTERNAL
                    default_url = get_settings().mma_database_url
                    if is_prohibited_live_url(db_url, default_url=default_url):
                        print(
                            "model configuration error: refusing live data/mma.db; "
                            "pass an explicit disposable --database-url or --fixture protocol"
                        )
                        return EXIT_INTERNAL
                    engine = open_readonly_sqlite_engine(db_url)
                    session_factory = readonly_session_factory(engine)
                    with session_factory() as session:
                        report = run_model_calibrate(
                            artifact_path=Path(args.artifact),
                            output_path=args.output,
                            fixture=None,
                            session=session,
                            n_replicates=n_replicates,
                            seed=seed,
                            contract=contract,
                        )
                elif args.fixture == "protocol":
                    report = run_model_calibrate(
                        artifact_path=Path(args.artifact),
                        output_path=args.output,
                        fixture="protocol",
                        n_replicates=n_replicates,
                        seed=seed,
                        contract=contract,
                    )
                else:
                    print(
                        "model configuration error: pass --fixture protocol or a "
                        "disposable --database-url"
                    )
                    return EXIT_INTERNAL
            except CoverageDatabaseError as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            except HoldoutLockedError as exc:
                print(f"model holdout locked: {exc}")
                return EXIT_INTERNAL
            except CalibrationLeakageError as exc:
                print(f"model calibration leakage: {exc}")
                return EXIT_INTERNAL
            except (
                CalibrationError,
                BootstrapError,
                ArtifactError,
                UntrustedArtifactError,
                TrainError,
                SplitError,
                JointError,
                ValueError,
            ) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            finally:
                if engine is not None:
                    engine.dispose()
            print(json.dumps(report.to_dict(), indent=2))
            return 0
        if args.model_cmd == "retrain":
            db_url = str(args.database_url or "").strip()
            if not db_url:
                print(
                    "model configuration error: --database-url is required for retrain "
                    "decision audit (never live data/mma.db)"
                )
                return EXIT_INTERNAL
            default_url = get_settings().mma_database_url
            if is_prohibited_live_url(db_url, default_url=default_url):
                print(
                    "model configuration error: refusing live data/mma.db; "
                    "pass an explicit disposable --database-url"
                )
                return EXIT_INTERNAL
            engine = create_engine(db_url, future=True)
            create_all_for_tests(engine)
            factory = sessionmaker(bind=engine, future=True)
            try:
                with factory() as session:
                    outcome = retrain_fixed_spec(
                        session,
                        registry_path=Path(args.registry),
                        artifacts_dir=Path(args.artifacts_dir),
                        actor="cli.retrain",
                    )
                    session.commit()
            except (HoldoutLockedError, TrainError, ArtifactError, ValueError) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            finally:
                engine.dispose()
            print(json.dumps(outcome.to_dict(), indent=2))
            return 0 if outcome.status != "failed" else EXIT_INTERNAL
        if args.model_cmd == "promote":
            if not bool(getattr(args, "evaluate", False)):
                print(
                    "model configuration error: promotion requires --evaluate; "
                    "cannot bypass holdout/health gates"
                )
                return EXIT_INTERNAL
            db_url = str(args.database_url or "").strip()
            if not db_url:
                print(
                    "model configuration error: --database-url is required for promote "
                    "decision audit (never live data/mma.db)"
                )
                return EXIT_INTERNAL
            default_url = get_settings().mma_database_url
            if is_prohibited_live_url(db_url, default_url=default_url):
                print(
                    "model configuration error: refusing live data/mma.db; "
                    "pass an explicit disposable --database-url"
                )
                return EXIT_INTERNAL
            engine = create_engine(db_url, future=True)
            create_all_for_tests(engine)
            factory = sessionmaker(bind=engine, future=True)
            try:
                with factory() as session:
                    payload = promote_candidate(
                        session,
                        registry_path=Path(args.registry),
                        candidate_digest=str(args.candidate),
                        evaluate=True,
                        artifacts_dir=Path(args.artifacts_dir),
                        reason=str(args.reason),
                        actor="cli.promote",
                        artifact_path=Path(args.artifact) if args.artifact else None,
                    )
                    session.commit()
            except PromotionEvaluateRequiredError as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            except (PromotionGateError, PromotionError, ArtifactError, ValueError) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            finally:
                engine.dispose()
            print(json.dumps(payload, indent=2))
            return 0
        if args.model_cmd == "rollback":
            db_url = str(args.database_url or "").strip()
            if not db_url:
                print(
                    "model configuration error: --database-url is required for rollback "
                    "decision audit (never live data/mma.db)"
                )
                return EXIT_INTERNAL
            default_url = get_settings().mma_database_url
            if is_prohibited_live_url(db_url, default_url=default_url):
                print(
                    "model configuration error: refusing live data/mma.db; "
                    "pass an explicit disposable --database-url"
                )
                return EXIT_INTERNAL
            engine = create_engine(db_url, future=True)
            create_all_for_tests(engine)
            factory = sessionmaker(bind=engine, future=True)
            try:
                with factory() as session:
                    payload = rollback_champion(
                        session,
                        registry_path=Path(args.registry),
                        reason=str(args.reason),
                        actor="cli.rollback",
                    )
                    session.commit()
            except (PromotionError, ValueError) as exc:
                print(f"model configuration error: {exc}")
                return EXIT_INTERNAL
            finally:
                engine.dispose()
            print(json.dumps(payload, indent=2))
            return 0
        if args.model_cmd != "train":
            print(f"model configuration error: unknown command {args.model_cmd!r}")
            return EXIT_INTERNAL
        try:
            contract = load_evaluation_contract(path=Path(args.contract))
            requested = str(getattr(args, "model", "auto"))
            family = identify_model_family(Path(args.spec)) if requested == "auto" else requested
            if requested == "joint" and identify_model_family(Path(args.spec)) != "joint":
                print("model configuration error: --model joint requires a joint spec")
                return EXIT_INTERNAL
            if requested == "ridge" and identify_model_family(Path(args.spec)) != "ridge":
                print("model configuration error: --model ridge requires a ridge spec")
                return EXIT_INTERNAL
            ridge_spec = None
            joint_spec = None
            if family == "joint":
                joint_spec = load_joint_spec(path=Path(args.spec))
            elif family == "ridge":
                ridge_spec = load_ridge_spec(path=Path(args.spec))
            else:
                print(f"model configuration error: unknown model family {family!r}")
                return EXIT_INTERNAL
        except (EvaluationContractError, RidgeSpecError, JointSpecError) as exc:
            print(f"model configuration error: {exc}")
            return EXIT_INTERNAL

        engine = None
        try:
            if args.database_url is not None:
                db_url = str(args.database_url).strip()
                if not db_url:
                    print("model configuration error: empty --database-url")
                    return EXIT_INTERNAL
                default_url = get_settings().mma_database_url
                if is_prohibited_live_url(db_url, default_url=default_url):
                    print(
                        "model configuration error: refusing live data/mma.db; "
                        "pass an explicit disposable --database-url or --fixture protocol"
                    )
                    return EXIT_INTERNAL
                engine = open_readonly_sqlite_engine(db_url)
                session_factory = readonly_session_factory(engine)
                with session_factory() as session:
                    if joint_spec is not None:
                        report = train_joint_from_session(
                            session,
                            spec=joint_spec,
                            output_path=Path(args.output),
                            include_holdout=False,
                            contract=contract,
                        )
                    else:
                        report = train_from_session(
                            session,
                            spec=ridge_spec,
                            output_path=Path(args.output),
                            include_holdout=False,
                            contract=contract,
                        )
            elif args.fixture == "protocol":
                if joint_spec is not None:
                    report = run_protocol_joint_train(
                        spec=joint_spec,
                        output_path=Path(args.output),
                        include_holdout=False,
                        contract=contract,
                    )
                else:
                    report = run_protocol_train(
                        spec=ridge_spec,
                        output_path=Path(args.output),
                        include_holdout=False,
                        contract=contract,
                    )
            elif args.fixture == "manifest":
                print(
                    "model configuration error: manifest fixture has no labeled PIT rows; "
                    "pass --fixture protocol or a disposable --database-url"
                )
                return EXIT_INTERNAL
            else:
                print(f"model configuration error: unknown fixture {args.fixture!r}")
                return EXIT_INTERNAL
        except CoverageDatabaseError as exc:
            print(f"model configuration error: {exc}")
            return EXIT_INTERNAL
        except HoldoutLockedError as exc:
            print(f"model holdout locked: {exc}")
            return EXIT_INTERNAL
        except EvaluatorHashMismatchError as exc:
            print(f"model hash mismatch: {exc}")
            return EXIT_INTERNAL
        except (
            TrainError,
            SplitError,
            RidgeSpecError,
            JointSpecError,
            JointError,
            MissingJointClassError,
            EarlyTechnicalOutcomeError,
            UnsupportedScheduleError,
            ValueError,
        ) as exc:
            print(f"model configuration error: {exc}")
            return EXIT_INTERNAL
        finally:
            if engine is not None:
                engine.dispose()
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
