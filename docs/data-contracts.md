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

## Coverage and data-health gates (DWCS-106)

| Field | Value |
|-------|--------|
| **Modules** | `src/mma_model/quality/coverage.py`, `gates.py`, `leakage.py` |
| **Schema** | `output/contracts/coverage.schema.json` |
| **CLI** | `mma-model coverage --series dwcs [--strict] [--json] [--raw-store DIR] --database-url ...` |
| **Tiers** | Every frozen bout is in exactly one overall core category. Gold = direct/revision timestamp; silver = proxy/stable fact plus independent **source-family** agreement; bronze = single-family retrospective/proxy; conflict = independent disagreement on the **same** version/cutoff, or explicit `quality_tier=conflict` on a result fact; missing = no visible fact. Manifest-only is bronze. Same-source `event_night` vs `current` revisions are a chronological ledger, not conflict. `mma_ai_bootstrap` is derived from UFCStats and cannot independently agree with `ufcstats_public`. Persisted SportsDataIO/BALLDONTLIE facts may agree when they qualify; access `validation_only` stays separate. |
| **Strict exit** | `0` pass; `2` any affected blocking gate; `1` configuration/schema/internal error |
| **Non-strict** | Exit `0` while still listing blockers |
| **Licensed status** | `decision.primary=null` / `licensed_primary_unselected` / `licensed_adoption_not_selected` / legacy `licensed_hard_blocker` are informational only and **never** a Phase 1 global blocker |
| **Zero denominator** | Regional live sample universe is always professional n=9 and amateur n=2; empty evidence fails 0/9 and 0/2, never n=0. Other required live gates with n=0 stay `insufficient_sample` (block), never a pass |
| **Fixtures** | Identity/regional fixture metrics are labeled validation-only and never fill live denominators. Frozen manifest skeleton is universe metadata only; result values require a visible persisted result version with exact provenance. |

Phase 3 train/eval consumers must refuse segments whose strict health fails. Healthy public-source segments may proceed. Public accessibility is not accuracy, PIT, or rights proof.

### Coverage hash contracts

| Hash | Canonical bytes |
|------|-----------------|
| **`config_hash`** | SHA-256 of canonical JSON over exactly: `series`, `as_of`, `policy_hash` (canonical `source_policy_v1.json`), `evaluation_contract_hash` (`PINNED_CONTRACT_HASH`), `evaluation_contract_version`, `expected_universe_hash` (`PINNED_EXPECTED_UNIVERSE_HASH`), `coverage_contract_version`, `coverage_schema_version`. Temp paths and insertion order are not inputs. |
| **`db_hash`** | SHA-256 of canonical JSON over inventory counts plus the sorted semantic fingerprint of every table/column that can change the report: raw observations, result versions, identity reviews, fighter source IDs, fighter profile observations **including `mutable_current`**, regional history bouts, source failures, ingest runs, and source checkpoints. Coverage **features** still exclude mutable-current rows. For `as_of=None`, the fingerprint includes every influencing row. For a PIT cutoff, future-clock rows are dropped from the fingerprint while current global health still includes them. Do not run the coverage visibility filter before deciding fingerprint inclusion for profiles/history. Insertion order and disposable DB path do not change the hash. |
| **`report_hash`** | SHA-256 of the complete canonical emitted coverage report **excluding only** the `report_hash` field itself. Gates, blockers, cutoff/`as_of`, `config_hash`, and `db_hash` are included. After gates are attached, the hash is recomputed so the published report is self-describing. |

CLI: `mma-model coverage --series dwcs [--strict] [--json] [--raw-store DIR] --database-url ...`. Empty/malformed/live `data/mma.db` URLs, missing raw store when referenced blobs exist, and schema/config/internal errors exit **1**. Strict data blockers exit **2**. Non-strict valid reports exit **0**. Coverage opens SQLite read-only (`mode=ro` + `PRAGMA query_only=ON`); no migrations, writes, or network.

Quality tiers (overall): **gold** = qualifying direct/revision timestamp evidence; **silver** = documented proxy or stable immutable fact **plus** independent agreeing **source family**; **bronze** = single-family retrospective/proxy; **conflict** = independent disagreement on the same semantic fact/version/cutoff; **missing** = no visible fact. Manifest-only proxy rows are bronze. Event-night vs later current NC is counted in `result_transitions`, not as conflict. Source **access** status (killed/failed/unmeasured/validation_only) is reported separately from mapped **data coverage**. Each `BoutResultVersion` revision links to its exact originating `RawObservation` (`raw_observation_id` + `provenance_status`). Result versions are visible at a cutoff only when that link is `linked`, `effective_at < cutoff`, **and** the linked observation's semantic visibility clock is allowed; unlinked/unknown/ambiguous rows fail closed at a cutoff (`as_of=None` still reports them). Unknown clocks use acquisition `observed_at`, so a future-acquired or untimestamped current correction cannot leak. Reversed-to-NC current lanes keep historical visibility unknown until a real adjudication/source publication timestamp exists; equal event-night/current lanes may share the immutable event proxy. Regional live sample scoring uses subject-keyed found/failed/missing against the fixed adjudicated denominator; frozen global source kills are access state, not per-subject failures. Mutable-current leakage reports `rows_examined`, `applicable_rows`, `synthetic_guard_checks`, and `violations`, with gate status `pass` / `not_applicable` / `fail`.

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
