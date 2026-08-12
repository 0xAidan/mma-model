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
| **Canonical source IDs** | `ufcstats_public`, `mma_ai_bootstrap`, `dwcs_manifest`, `tapology_public`, `sherdog_public`, `combat_registry`, `wikidata`, `bestfightodds_archive`, `the_odds_api`, `sportsdataio`, `balldontlie`, `explicit_missing`. `dwcs_manifest` is a frozen internal universe/result seed (not an external observation fallback; never `explicit_missing` for manifest facts). |
| **Licensed primary** | Remains `null` in the scorecard until a measured audit passes |
| **Gates** | 89/440; exclusions categorized; ≥98% reconciliation; ≥99% result agreement; zero unresolved evaluated/upcoming identity conflicts; zero future-row leakage; no mutable-current historical features |
| **Observation metadata** | Required clocks/fields: `observed_at`, `source_published_at`, `source_updated_at`, `effective_at`, `proxy_published_at`, `timestamp_quality`, `timestamp_quality_source`, `quality_tier`, `payload_hash`, `raw_ref`. `attributes_json` is source-specific only; reserved contract keys must never appear inside attributes. |
| **DWCS-102 persistence** | Documented under `dwcs_102_persistence` (`0006_observation_pit_metadata`); not implemented in the policy PR |
| **Design / plan** | `docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md`, `docs/superpowers/plans/2026-08-12-public-first-mma-history.md` |

Public observations require source labels and quality tiers (`gold` / `silver` /
`bronze` / `missing` / `conflict`). Odds stay a separate lane unless the
evaluation contract explicitly enables a challenger. Loader returns a deeply
immutable `SourcePolicy` and fail-closes on nested ID/tier/clock drift.

## Existing prototype stores (retained, not yet DWCS-canonical)

| Store | Module / table | Notes |
|-------|----------------|-------|
| Fighters / events / fights / stats | `src/mma_model/db/models.py` | UFCStats-shaped; to be superseded by bitemporal canonical tables |
| Ingest cursor | `ingest_cursors` | Paginated UFCStats resume |
| Odds snapshot blob | `odds_snapshots` | Untyped JSON payload; not bout-matched quotes |
| Fighter composites | `fighter_composites` | Rolling scores; watch for current-aggregate leakage |

## Market / settlement contracts (DWCS-200)

| Field | Value |
|-------|--------|
| **Domain enums** | `mma_model.domain.markets` |
| **Price targets** | `mma_model.markets.price_targets` |
| **Settlement** | `mma_model.markets.settlement` |
| **Authoritative rules bytes** | Packaged `mma_model/markets/settlement_v1.yaml` |
| **Plan-visible path** | `config/markets/settlement_v1.yaml` (symlink to packaged file) |
| **Loader** | `mma_model.markets.rules.load_settlement_rules` / `get_rule_set` |
| **Contract id / version** | `dwcs_settlement` / `1.1.0` |
| **Pinned digest** | `PINNED_SETTLEMENT_HASH` in `rules.py` (SHA-256 of canonical JSON) |
| **Default rule set** | `mma_generic` `1.1.0` (`internal_contract`) |

### v1 market families and outcomes

- **moneyline:** `fighter_a`, `fighter_b`
- **totals:** outcomes `over` / `under`; canonical lines `1.5`, `2.5` only
- **goes_distance:** `goes_distance`, `inside_distance`
- **method:** `ko_tko`, `submission`, `decision`, `other_stoppage`
- **fighter_by_method:** `a_*` / `b_*` method atoms
- **exact_round:** schedule-specific — 3-round bouts use `round_1`…`round_3`;
  5-round bouts use `round_1`…`round_5`. Out-of-schedule selections are rejected.

Market-family maturity is `qualified` / `experimental` / `blocked`. Failed or
non-qualified families may not emit `confirmed_value` or `price_target`.

### Sportsbook-agnostic price guidance (required)

Computed from calibrated `p50` / `p25` with no bookmaker dependency:

- Fair decimal: `1 / p50`
- Actionable decimal: `max(1 / p25, 1.05 / p50)` (exact round uses `1.10` EV target)
- Strong-value decimal: `max(1 / p25, 1.10 / p50)`
- Recommendation states: `confirmed_value` (timestamped offer meets actionable +
  confidence), `price_target` (no offer; thresholds still published), `no_bet`

Exact bookmaker lines remain optional enrichment (later tickets). Missing Bet365
does not block these thresholds. Price guidance does **not** depend on settlement
rule-set approval status.

### Settlement

Pure functions return `win` / `loss` / `push` / `void` / `unresolved` plus reason,
rule-set id/version, and the contract `content_hash`.

**Totals boundary (v1):** half-round lines use fight duration in rounds
(`elapsed_rounds = total_elapsed_seconds / 300`). Over wins when
`elapsed_rounds > line`; under when `elapsed_rounds < line`; exact equality
(e.g. 2:30 of round 2 for 1.5) is `push`. Duration comes from
`ending_round` + `elapsed_seconds_in_round`, or `total_elapsed_seconds`, or
full scheduled duration for decisions/draws. Missing clocks → `unresolved`
(never invent a grade). Whole-number totals lines are not offered in v1.

**Governance:** `mma_generic` is `internal_contract` — repository-governed grading
policy with durable internal references (evaluation terminal atoms, data-contracts,
ADR 0001). It is **not** an approved external sportsbook house-rules source and
must not be described as universal sportsbook rules. The `bet365_mma` override
lane is `provisional_pending_approved_source` and hard-fails unless
`allow_provisional=True` after an approved citation exists.

**Pinned digest bump procedure:** edit packaged `settlement_v1.yaml` → bump
`contract_version` and affected rule-set `version` → recompute
`compute_settlement_hash(payload)` → update `PINNED_SETTLEMENT_HASH` and
`EXPECTED_CONTRACT_VERSION` in `rules.py` in the same change. Default loads
always verify the pin (fail-closed on silent drift).

**Immutability:** loaded `rule_sets` is a `MappingProxyType`; nested rule models
are frozen Pydantic models.

**Fact validation:** structurally impossible facts raise `SettlementFactsError`
(unsupported schedule, out-of-range clocks, contradictory cancel/draw/NC/winner
combinations). Genuinely incomplete/pending facts settle as `unresolved`.

Out of scope for DWCS-200: odds HTTP ingestion (DWCS-201+).

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
