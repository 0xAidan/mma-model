# DWCS-001 baseline command outputs

Captured on worktree `feat/dwcs-001` after hardening the evaluation contract
(pinned digest, deep immutability, wheel packaging proof, stricter protocol).
No local DB or trained model artifacts are committed.

Environment: Python 3.11 venv, `pip install -e ".[dev]"`.

## `pytest -q`

See the latest local verification in the PR; packaging smoke builds a real wheel
and loads the contract from a non-editable install (`WHEEL_CONTRACT_OK`).

## `ruff check .`

Repository-wide correctness gate (`E4,E7,E9,F`):

```
All checks passed!
```

Focused stricter gate for evaluation code/tests (`E,F,I,UP,B,SIM`):

```
ruff check src/mma_model/evaluation tests/test_evaluation_contract.py \
  tests/test_evaluation_contract_packaging.py --select E,F,I,UP,B,SIM
```

Scope is intentional: pre-existing modules still carry style debt; only the new
evaluation surface is held to the broader rule set.

## `python -m mma_model.cli backtest --help`

CLI surface retained (`init-db`, `sync`, `odds`, `train`, `predict-fight`,
`backtest`).

## Frozen contract digest

Pinned SHA-256 (`PINNED_CONTRACT_HASH` / `contract_version` `1.0.1`):

`af0ad518a6417ac7d67e5f56fe836ab58afe55d8ac70813bf6045307ea6fb2cf`

Holdout window: seasons `(2025,)`, `locked: true`.
UCB gate: event-block 90% delta log-loss UCB must be **strictly below** `+0.02`.
