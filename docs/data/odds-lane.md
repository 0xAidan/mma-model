# Odds lane (Phase 2)

Odds stay a separate lane from outcome-feature training unless an evaluation
contract explicitly enables a challenger.

## Sources

- **The Odds API** (`the_odds_api`): optional structured current/historical
  snapshots from mid-2020. DWCS-205 schedules live collection and sparse-first
  historical backfill under quota.
- **BestFightOdds archive** (`bestfightodds_archive`): public historical odds
  reconciliation only. Never stats or PIT feature evidence. Never direct
  Bet365/sportsbook-page scraping. Uses the shared polite HTTP client.

## Scheduling (DWCS-205)

See `config/odds/schedule_v1.yaml` and `docs/data-contracts.md`.
