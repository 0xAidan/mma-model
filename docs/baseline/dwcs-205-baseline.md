# DWCS-205 baseline (2026-08-13T07:26:57Z)

## Identity
- branch: feat/dwcs-205-odds-scheduling
- HEAD: 43b7393b959f05715d0cffb1d60925fc4146730b
- origin/main: 43b7393b959f05715d0cffb1d60925fc4146730b
- clean: yes (pre-edit)

## Collect
920 tests collected before DWCS-205 edits

## Acceptance sources
- Plan ticket DWCS-205 in `~/.cursor/plans/dwcs-value-system_bbd59984.plan.md`
  - Cadence: T−72h→−24h /30m; −24h→−6h /10m; −6h→−1h /5m; final hour /2m when quota permits
  - Sparse historical checkpoints first: T−24h / T−6h / T−1h / close-proxy from 2020
  - Acceptance: no-op outside windows; monthly usage within plan; historical ≤ cutoff; absent vs failed separated
- `config/sources/source_policy_v1.json` (`bestfightodds_archive` = public_historical_odds_reconciliation; never stats/PIT)
- `config/sources/odds.yaml` / `odds_decision_v1.yaml` (`licensed_bookmaker_adapter_authorized: false`)
- Reuse: DWCS-201 snapshots/quota persistence; DWCS-203 matching/PIT lifecycle; flock one-writer examples
- GitHub: prior Phase 2 PRs #20–#24 on `0xAidan/mma-model` (Notion MCP unavailable / needsAuth)
