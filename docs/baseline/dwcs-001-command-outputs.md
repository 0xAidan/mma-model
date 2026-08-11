# DWCS-001 baseline command outputs

Captured on clean worktree `feat/dwcs-001` from `origin/main` @ `82c18d7`
(merge of PR #4) after implementing the evaluation contract. No local DB or
trained model artifacts are committed.

Environment: Python 3.11 venv, `pip install -e ".[dev]"`.

## `pytest -q`

```
.................................................                        [100%]
49 passed in 1.61s
```

## `ruff check .`

```
All checks passed!
```

## `python -m mma_model.cli backtest --help`

```
usage: mma-model backtest [-h] [--min-train MIN_TRAIN]
                          [--min-prior-fights MIN_PRIOR_FIGHTS]
                          [--max-predictions MAX_PREDICTIONS]
                          [--omit-predictions]

options:
  -h, --help            show this help message and exit
  --min-train MIN_TRAIN
                        Minimum fights before first prediction
  --min-prior-fights MIN_PRIOR_FIGHTS
  --max-predictions MAX_PREDICTIONS
                        Stop after this many out-of-sample predictions (faster
                        smoke test)
  --omit-predictions    Omit per-fight rows from JSON (metrics only)
```

## `python -m mma_model.cli --help` (CLI surface retained)

```
usage: mma-model [-h] {init-db,sync,odds,train,predict-fight,backtest} ...

positional arguments:
  {init-db,sync,odds,train,predict-fight,backtest}
    init-db             Create SQLite tables
    sync                Sync events/fights from ufcstats.com
    odds                Fetch current MMA odds (needs ODDS_API_KEY)
    train               Train logistic model on DB fights
    predict-fight       P(fighter A wins) for a fight id
    backtest            Walk-forward evaluation: retrain on past fights only,
                        predict next (point-in-time)
```

## Frozen contract digest

Canonical SHA-256 of `config/evaluation/dwcs_v1.json` at freeze time:

`4cde78748d9b9a17eeeb3431c74d0062c305808f74e072023612448508f1c438`

Holdout window: seasons `[2025]`, `locked: true`.
