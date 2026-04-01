# MMA Model

In-house **Data Golf–style** pipeline for UFC: ingest official **[UFC Stats](http://www.ufcstats.com/)** data, maintain **point-in-time rolling profiles**, **composite pillar scores**, **win probability vs market odds**, and **Kelly-style sizing** (internal product spec).

## Features

- **ETL** from ufcstats.com → SQLite (`events`, `fights`, `fighters`, `fight_fighter_stats`)
- **The Odds API** integration for live MMA lines (`ODDS_API_KEY`)
- **Rolling / matchup features** and heuristic **pillar** scores (strike / grapple / pace / momentum)
- **EV & fractional Kelly** helpers

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
| **Review / priorities** | Optional | Say if you want next: more backfill, a small HTTP API, or calibration vs closing lines. |

## Configuration

| File | Purpose |
|------|---------|
| `.env` | `MMA_DATABASE_URL`, `ODDS_API_KEY`, scrape delay / user-agent |
| `feature_flags.yaml` | Default sync limits, Kelly caps |
| `profiles.yaml` | Named profiles (`default`, `quick`, `full_backfill`) |

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
