# ADR 0001: DWCS system boundaries

- Status: Accepted
- Date: 2026-08-11
- Ticket: DWCS-001

## Context

The repository already contains a UFC Stats ETL, SQLite store, rolling-feature
helpers, a logistic baseline, walk-forward backtest CLI, and The Odds API
pass-through. The DWCS value system extends this codebase rather than starting a
new service. Before changing evaluators or models, the retained foundation,
replacement targets, and known defects must be frozen in writing.

## Decision

Keep the useful foundation; treat unsafe evaluation and incomplete odds plumbing
as prototypes to replace under later tickets. Do **not** fix the defects listed
here in DWCS-001.

### Retained (compatibility / foundation)

| Area | Path / surface | Why retained |
|------|----------------|--------------|
| Language / packaging | Python 3.11, `pyproject.toml`, `mma-model` CLI entry | Shared pipeline language and install path |
| Settings | `src/mma_model/config.py` | Env + YAML flags |
| SQLite session | `src/mma_model/db/session.py` | One-writer local store |
| Current ORM facade | `src/mma_model/db/models.py` | Compatibility until canonical schema tickets |
| UFCStats client/parsers/ingest | `src/mma_model/ufcstats/` | Research / reconciliation adapter; production-disabled by later policy |
| Rolling / matchup / pillars | `composites/`, `features/matchup.py` | Point-in-time intent to evolve, not discard |
| EV / Kelly helpers | `src/mma_model/value/` | Basic math reused after validation |
| CLI commands | `init-db`, `sync`, `odds`, `train`, `predict-fight`, `backtest` | Must keep working unless a ticket adds tested wrappers |
| Test style | `tests/` fixtures + pytest | Extend in place |

### Replaced or superseded (later tickets; not in DWCS-001)

| Area | Current behavior | Replacement direction |
|------|------------------|------------------------|
| Fight-by-fight backtest | `predict/backtest.py` advances one fight at a time | Card-grouped rolling-origin evaluator consuming `config/evaluation/dwcs_v1.json` |
| Random train holdout | `predict/train.py` `train_test_split` | Chronological / card-blocked splits only for performance claims |
| Odds pass-through | `odds/the_odds_api.py` untyped JSON, no durable quotes | Typed adapters, snapshots, optional bookmaker enrichment |
| Ambiguous labels / duration | `method` / `time_str` strings on `Fight` | Leakage-safe label + elapsed-time modules |
| Event discovery seed | UFCStats completed index | Verified DWCS manifest first |
| Dashboard / jobs | none | Static JSON publisher + systemd/Compose worker (later phases) |

### Architecture constraints (do not redesign)

1. Extend `mma-model` in Python 3.11; do not spawn a duplicate Next.js domain stack.
2. SQLite WAL, Alembic later, one writer.
3. Static React/TS/Tailwind dashboard reading versioned JSON.
4. Host systemd timers + `docker compose run` under one `flock`.
5. Typed source adapters; no provider-specific fields leaking through features.
6. Production data rights (amended 2026-08-12, DWCS-003): personal-project
   **public-first hybrid** per `config/sources/source_policy_v1.json` using
   canonical source IDs (`ufcstats_public`, `mma_ai_bootstrap`, `dwcs_manifest`,
   `tapology_public`, `sherdog_public`, `combat_registry`, `wikidata`,
   `bestfightodds_archive`, `the_odds_api`, `sportsdataio`, `balldontlie`,
   `explicit_missing`). `dwcs_manifest` seeds the frozen DWCS universe/results
   and is not an external observation fallback. Canonical UFC/DWCS facts come from `ufcstats_public`
   snapshots (optional `mma_ai_bootstrap` only after hash/count/schema
   reconciliation). Licensed providers remain validation/enrichment until a
   measured audit sets `decision.primary`. Never bypass logins, paywalls,
   CAPTCHAs, robots/access controls, or technical restrictions. Identity,
   leakage, provenance, explicit-missingness, and coverage gates are **not**
   weakened. DWCS-102 must persist four-clock PIT/quality metadata
   (`source_published_at`, `proxy_published_at`, `timestamp_quality`,
   `quality_tier`, `attributes_json`) before production public ingest.
7. Pooled regularized competing-risks GLM with ridge logistic fallback.
8. Bookmaker odds are **optional enrichment**. Missing Bet365 does not block core
   sportsbook-agnostic fair / actionable / strong-value guidance. Exact EV / ROI /
   CLV require timestamped observed or user-recorded prices; price-target-only rows
   never receive synthetic betting performance.
9. At most one official pick per matchup; otherwise `No bet`.
10. Config hashes from `config/` are stamped on outputs; evaluation contract is
    immutable to normal model runs. Authoritative contract bytes ship inside the
    package (`mma_model.evaluation`); `config/evaluation/dwcs_v1.json` is the
    plan-visible symlink to that same file. Default loads always verify
    `PINNED_CONTRACT_HASH`.

## Known current defects (documented, not fixed here)

1. **Same-card leakage** — walk-forward training can include earlier fights from the
   same event card when predicting a later bout on that card
   (`src/mma_model/predict/backtest.py`).
2. **Random split** — `train_and_save` uses `sklearn.model_selection.train_test_split`,
   which is not a valid temporal performance estimate
   (`src/mma_model/predict/train.py`).
3. **Duration / elapsed time** — fight clock is stored as `time_str` / round fields
   without validated elapsed-second bins for rate features and totals markets.
4. **Parser fragility** — method detection uses substring / keyword heuristics over
   table text; fighter names or malformed cells can be misread as methods
   (`src/mma_model/ufcstats/parsers.py`).
5. **Odds limitations** — The Odds API integration historically was live `h2h`
   pass-through without durable bout-matched snapshots. DWCS-201 adds typed
   events/markets/current/historical normalization, quota persistence, and
   append-only quote storage. DWCS-202 records that Phase 0 did not authorize a
   licensed bookmaker adapter and implements sportsbook-agnostic price targets
   plus optional `user_observed` prices for exact EV. DWCS-203 attaches provider
   events to canonical bouts via exact provider IDs / participant pairs, versions
   aliases through replacements/cancellations, and blocks ambiguous or
   non-active lifecycles from value calculations. Missing Bet365 must not block
   sportsbook-agnostic price targets; reference quotes are never labeled as
   Bet365.

## Consequences

- Later evaluators must load `config/evaluation/dwcs_v1.json` by version and content
  hash and hard-fail on mismatch.
- Defect fixes land only under their dedicated tickets (for example DWCS-300 labels,
  DWCS-302 card-grouped backtest, DWCS-200 odds).
- Existing CLI commands remain callable after this ticket.
