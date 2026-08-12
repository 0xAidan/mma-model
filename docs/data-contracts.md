# Data contracts

Canonical contracts for DWCS evaluation, later storage, and dashboard publish.
**Rule:** pick the contract for your question — do not mix priced betting metrics
with price-target-only rows.

## EvaluationContract (DWCS-001)

| Field | Value |
|-------|--------|
| **Authoritative bytes** | Packaged resource `mma_model/evaluation/dwcs_v1.json` |
| **Plan-visible path** | `config/evaluation/dwcs_v1.json` (symlink to the packaged file in checkout) |
| **Loader** | `mma_model.evaluation.load_evaluation_contract` |
| **Schema version** | `1` |
| **Contract id / version** | `dwcs_evaluation` / `1.0.1` |
| **Mutability** | Frozen pydantic models; nested sequences are tuples (no append/item-assign) |
| **Pinned digest** | `PINNED_CONTRACT_HASH` in `contract.py`; default loads **always** verify it |
| **Hash algorithm** | SHA-256 of canonical JSON (`sort_keys`, compact separators) |
| **Failure mode** | Schema / version / id / protocol / pinned-hash mismatch **hard-fails** |

Content changes require bumping `contract_version` **and** updating
`PINNED_CONTRACT_HASH`. There is one authoritative byte source; the visible
config path must resolve to the same bytes (enforced by tests). Wheel installs
load the packaged resource via `importlib.resources`.

### Locked protocol highlights

- Universes: all-DWCS 89 cards / 440 bouts; standard-only 86 / 425; Brazil 3 / 15.
- Splits: **event-card** rolling-origin; development **2017–2023**; validation
  **2024**; **locked holdout 2025**.
- Prediction cutoff: scheduled start minus **60 minutes**; identical cutoff for
  every bout on a card.
- Mutable facts require `effective_at < cutoff` and `observed_at <= cutoff`.
- Outcome metrics include joint/market log loss, Brier, calibration
  intercept/slope, reliability bins, **ECE**, descriptive accuracy, skill vs
  baselines.
- Betting metrics (ROI, CLV, drawdown, …) apply only to rows with timestamped
  observed or user-recorded prices.
- Price-target-only rows never receive synthetic betting performance.
- Bookmaker odds are optional enrichment; missing Bet365 does not block core
  fair / actionable / strong-value guidance.

### Confidence intervals (event-block)

- Offline bootstrap refits: **200**, unit **`event_block`** (not fight-iid).
- Probability / EV intervals: event-block at **90%** and **95%**.
- Betting-metric intervals (ROI/CLV/drawdown/etc.): event-block at **90%** and
  **95%**, encoded separately under
  `confidence_intervals.betting_metrics`.

### Recommendation price formulas (encoded)

- Fair decimal odds: `1 / p50`
- Actionable decimal price: `max(1 / p25, 1.05 / p50)`
- Strong-value decimal price: `max(1 / p25, 1.10 / p50)`
- Confirmed-value gate (when a timestamped offer exists): offered price meets
  actionable threshold and bootstrap `P(EV > 0) ≥ 0.70` (exact round uses 0.10 EV
  target and `≥ 0.75`).

### Strict holdout UCB gate

`go_live_gates.moneyline.holdout_2025_event_block_90pct_ucb_delta_log_loss` uses
`comparison: "lt"` with `strict_upper_bound: 0.02`. Pass only when the
event-block 90% UCB on delta log loss versus M1 is **strictly below** `+0.02`
(`ucb < 0.02`). Equality at `0.02` fails.

## Source policy contract (DWCS-003 amendment)

| Field | Value |
|-------|--------|
| **Path** | `config/sources/source_policy_v1.json` |
| **Loader** | `mma_model.sources.policy.load_source_policy` |
| **Mode** | `public_first_hybrid_personal_project` |
| **Licensed primary** | Remains `null` in the scorecard until a measured audit passes |
| **Gates** | 89/440; exclusions categorized; ≥98% reconciliation; ≥99% result agreement; zero unresolved evaluated/upcoming identity conflicts; zero future-row leakage; no mutable-current historical features |
| **Design / plan** | `docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md`, `docs/superpowers/plans/2026-08-12-public-first-mma-history.md` |

Public observations require source labels and quality tiers (`gold` / `silver` /
`bronze` / `missing` / `conflict`). Odds stay a separate lane unless the
evaluation contract explicitly enables a challenger.

## Existing prototype stores (retained, not yet DWCS-canonical)

| Store | Module / table | Notes |
|-------|----------------|-------|
| Fighters / events / fights / stats | `src/mma_model/db/models.py` | UFCStats-shaped; to be superseded by bitemporal canonical tables |
| Ingest cursor | `ingest_cursors` | Paginated UFCStats resume |
| Odds snapshot blob | `odds_snapshots` | Untyped JSON payload; not bout-matched quotes |
| Fighter composites | `fighter_composites` | Rolling scores; watch for current-aggregate leakage |

## Governing product rule (odds)

Bookmaker odds are optional enrichment. Missing Bet365 does not block core
sportsbook-agnostic fair / actionable / strong-value guidance. Exact EV / ROI /
CLV require timestamped observed or user-recorded prices; price-target-only rows
never receive synthetic betting performance.

## Related

- [ADR 0001: DWCS system boundaries](adr/0001-dwcs-system-boundaries.md)
- [Stats / identity source decision](research/stats-source-decision.md)
- [Baseline command outputs](baseline/dwcs-001-command-outputs.md)
- [Odds provider audit](research/odds-provider-audit.md)
