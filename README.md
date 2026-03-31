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
mma-model sync --profile default # see feature_flags.yaml / profiles.yaml
```

With `ODDS_API_KEY` set:

```bash
mma-model odds
```

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
