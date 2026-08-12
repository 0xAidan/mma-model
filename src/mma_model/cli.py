"""CLI: init-db, sync, odds, train, predict, source audit."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from mma_model.config import get_settings
from mma_model.db.session import init_db, session_scope
from mma_model.odds.the_odds_api import fetch_mma_odds
from mma_model.predict.backtest import walk_forward_backtest
from mma_model.predict.train import predict_fight_a_win_prob, train_and_save
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.ufcstats_public.adapter import UfcstatsPublicAdapter
from mma_model.sources.ufcstats_public.client import UfcstatsPublicClient
from mma_model.sources.ufcstats_public.probe import (
    not_run_live_probe_evidence,
    run_bounded_live_probe,
)
from mma_model.ufcstats.client import UFCStatsClient
from mma_model.ufcstats.ingest import sync_pipeline


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

    p_odds = sub.add_parser("odds", help="Fetch current MMA odds (needs ODDS_API_KEY)")
    p_odds.add_argument("--json", action="store_true", help="Print raw JSON")

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
        data = fetch_mma_odds()
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(f"Events with odds: {len(data)}")
        return 0

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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
