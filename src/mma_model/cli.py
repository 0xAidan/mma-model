"""CLI: init-db, sync, odds, train, predict."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mma_model.config import get_settings
from mma_model.db.session import init_db, session_scope
from mma_model.odds.the_odds_api import fetch_mma_odds
from mma_model.predict.train import predict_fight_a_win_prob, train_and_save
from mma_model.ufcstats.client import UFCStatsClient
from mma_model.ufcstats.ingest import sync_pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mma-model")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Create SQLite tables")

    p_sync = sub.add_parser("sync", help="Sync events/fights from ufcstats.com")
    p_sync.add_argument("--profile", default="default", help="profiles.yaml key")

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
                stats = sync_pipeline(session, client, profile_name=args.profile)
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

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
