"""CLI: init-db, sync, odds, train, predict, source audit."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mma_model.config import get_settings
from mma_model.db.session import _attach_sqlite_listeners, init_db, session_scope
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
from mma_model.quality.constants import EXIT_INTERNAL
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.readonly import (
    CoverageDatabaseError,
    is_prohibited_live_url,
    open_readonly_sqlite_engine,
    readonly_session_factory,
)
from mma_model.quality.report import dumps_report, human_report, write_coverage_evidence
from mma_model.quality.schema import CoverageSchemaError
from mma_model.sources.policy import load_source_policy
from mma_model.predict.backtest import walk_forward_backtest
from mma_model.predict.train import predict_fight_a_win_prob, train_and_save
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
        help="Walk-forward evaluation: retrain on past fights only, predict next (point-in-time)",
    )
    p_bt.add_argument("--min-train", type=int, default=30, help="Minimum fights before first prediction")
    p_bt.add_argument("--min-prior-fights", type=int, default=1)
    p_bt.add_argument(
        "--max-predictions",
        type=int,
        default=None,
        help="Stop after this many out-of-sample predictions (faster smoke test)",
    )
    p_bt.add_argument(
        "--omit-predictions",
        action="store_true",
        help="Omit per-fight rows from JSON (metrics only)",
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
        print(f"unsupported jobs command: {args.jobs_cmd}")
        return 1

    if args.cmd == "train":
        init_db()
        with session_scope() as session:
            out = train_and_save(session, args.output, min_prior_fights=args.min_prior_fights)
        print(json.dumps(out, indent=2))
        return 0

    if args.cmd == "predict-fight":
        init_db()
        with session_scope() as session:
            prob = predict_fight_a_win_prob(session, args.fight_id, args.model)
        print(json.dumps({"fight_id": args.fight_id, "p_fighter_a": prob}, indent=2))
        return 0

    if args.cmd == "backtest":
        init_db()
        with session_scope() as session:
            out = walk_forward_backtest(
                session,
                min_train_fights=args.min_train,
                min_prior_fights=args.min_prior_fights,
                max_predictions=args.max_predictions,
            )
        if args.omit_predictions:
            out = {k: v for k, v in out.items() if k != "predictions"}
        print(json.dumps(out, indent=2))
        return 0

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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
