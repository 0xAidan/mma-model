# MMA Model

In-house **Data Golf–style** pipeline for UFC: ingest official **[UFC Stats](http://www.ufcstats.com/)** data, maintain **point-in-time rolling profiles**, **composite pillar scores**, **win probability vs market odds**, and **Kelly-style sizing** (internal product spec).

## Features

- **ETL** from ufcstats.com → SQLite (`events`, `fights`, `fighters`, `fight_fighter_stats`)
- **The Odds API** integration for live MMA lines (`ODDS_API_KEY`)
- **Rolling / matchup features** and heuristic **pillar** scores (strike / grapple / pace / momentum)
- **EV & fractional Kelly** helpers
- **Paginated historical sync** with a resumable cursor (deep backfill over many runs)
- **Walk-forward backtest** (`mma-model backtest`) — point-in-time evaluation without lookahead

## What you asked for: point-in-time research loop

**Yes, this matches that intent.** Rolling stats already use only fights **before** each bout’s event date. The `backtest` command pushes that further: for each fight in chronological order, it **retrains the logistic model only on earlier fights**, then predicts that fight’s outcome. You get aggregate **accuracy / log loss / Brier** plus per-fight predictions to compare theories and iterate on features or models.

## Historical backfill (as much UFC Stats as practical)

The completed-events list is available as **`page=all`** (one large HTML) or **paginated** (`page=1`, `2`, …, ~25 events per page).

| Profile | Behavior |
|---------|----------|
| **`full_backfill`** | **Paginated**: fetches `sync_pages_per_run` pages per run, saves **`ingest_cursors`** so the next run can continue. Use: `mma-model sync --profile full_backfill` then **`mma-model sync --profile full_backfill --resume`** repeatedly until the cursor resets (empty page). |
| **`one_shot_all`** | One request to `page=all`, **`sync_max_events_per_run: 0`** = process every event returned in that HTML (typically hundreds). |
| **`one_shot_500`** | Same as older “full” single-shot: first **500** events from `page=all`. |
| **`upcoming_weekend`** | **`sync_events_list: upcoming`** — ingests the **next** scheduled card (first row on the upcoming list), e.g. for predictions before results exist. Fight **Totals** stay empty until after the event. |

**Note:** Large backfills take time and many HTTP requests (delay defaults to **0.75s** between requests).

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
mma-model init-db
mma-model sync --profile quick   # 1 event, no fight-detail scrape
mma-model sync --profile default # events + fight-detail stats; see profiles.yaml
```

### Train and predict (needs labeled fights from `default`-style sync)

After `sync` with fight details, build a baseline model and score a fight:

```bash
mma-model train --output data/model_logistic.joblib --min-prior-fights 0
mma-model predict-fight --fight-id <ufcstats_hex_id> --model data/model_logistic.joblib
```

Use `--min-prior-fights 0` on small databases (many fighters have no prior bouts in your DB). Raise it (e.g. `1` or `2`) once you have run `full_backfill` or a larger `sync_max_events_per_run`.

### Point-in-time walk-forward backtest

Requires enough labeled fights in the DB (sync with fight details). Example:

```bash
mma-model backtest --min-train 40 --min-prior-fights 1 --omit-predictions
mma-model backtest --min-train 40 --max-predictions 200   # cap work for a quick run
```

Add `--omit-predictions` to print only summary metrics; omit it to include every out-of-sample row in the JSON.

With `ODDS_API_KEY` set:

```bash
mma-model odds
```

## What I need from you

| Item | Required? | Notes |
|------|-----------|--------|
| **Nothing for core ETL + training** | — | UFC Stats is public; SQLite path works out of the box. |
| **`ODDS_API_KEY`** | Optional | Only for `mma-model odds` (The Odds API). Get a key at [the-odds-api.com](https://the-odds-api.com/) and put it in `.env`. |
| **GitHub** | Done if the repo is yours | `main` includes CI; pushes run `pytest`. |
| **Review / priorities** | Optional | e.g. HTTP API, calibration vs closing lines, richer fight features. |
| **Patience for backfill** | Implicit | Deep history = long runs; use `--resume` and overnight batches. |

## Configuration

| File | Purpose |
|------|---------|
| `.env` | `MMA_DATABASE_URL`, `ODDS_API_KEY`, scrape delay / user-agent |
| `feature_flags.yaml` | Default sync limits, Kelly caps |
| `profiles.yaml` | Named profiles: `default`, `quick`, `full_backfill`, `one_shot_all`, `one_shot_500`, `upcoming_weekend` (`sync_events_list: upcoming`) |

## Scraping

Be polite: default **0.75s** delay between requests. Review [UFC](https://ufc.com) / site terms for production use.

## Tests

```bash
pytest
```

## Related

- [golf-model](https://github.com/0xAidan/golf-model) — sister project patterns (value, calibration, learning loop).

## License

Private / MIT — set as appropriate for your org.
