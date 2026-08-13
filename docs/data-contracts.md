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

## Market / settlement contracts (DWCS-200)

| Field | Value |
|-------|--------|
| **Domain enums** | `mma_model.domain.markets` |
| **Price targets** | `mma_model.markets.price_targets` |
| **Settlement** | `mma_model.markets.settlement` |
| **Authoritative rules bytes** | Packaged `mma_model/markets/settlement_v1.yaml` |
| **Plan-visible path** | `config/markets/settlement_v1.yaml` (symlink to packaged file) |
| **Loader** | `mma_model.markets.rules.load_settlement_rules` / `get_rule_set` |
| **Contract id / version** | `dwcs_settlement` / `1.3.0` |
| **Pinned digest** | `PINNED_SETTLEMENT_HASH` in `rules.py` (SHA-256 of canonical JSON) |
| **Default rule set** | `mma_generic` `1.3.0` (`externally_sourced`) |
| **Source notes** | `docs/research/mma-settlement-sources.md` |

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
`elapsed_rounds > line`; under when `elapsed_rounds < line`; at exact equality
the default (`mma_generic`) grades **under** per Sky Bet / Paddy Power public
rules. Bodog’s exact-half **void** is the `bodog_mma` override. Duration comes
from `ending_round` + `elapsed_seconds_in_round`, or `total_elapsed_seconds`.
Ordinary full-distance `decision` / scorecard `draw` may omit clocks when
policy is `full_scheduled`. Technical decision and technical draw use
`stoppage_time` (Bodog explicit; generic refuses to invent scheduled
duration). Represent technical draw as `result_class="draw"` +
`method="technical_draw"`. Partial clock fields are checked for consistency
(including round-boundary dual forms); conflicts raise `SettlementFactsError`.
Missing required clocks → `unresolved` (never invent a grade). Whole-number
totals lines are not offered in v1.

**Moneyline draw / technical draw:** default is `void` (Sky Bet / Paddy Power /
Bodog “no action”), not push.

**Goes the distance:** ordinary decision and full-distance draw count as
`goes_distance`. Early technical decision / technical draw count as
`inside_distance` (Sky/Paddy “concluded before stated rounds”; bet365/Bodog
require full scheduled rounds for Yes).

**Governance:** `mma_generic` and `bodog_mma` are `externally_sourced` and must
cite at least one public `https://` house-rules page with an access date.
Disagreements are documented in `docs/research/mma-settlement-sources.md` and
captured as versioned overrides — never claimed as universal. The `bet365_mma`
lane is `provisional_pending_approved_source` and hard-fails unless
`allow_provisional=True`. Price guidance does not depend on settlement status.

**Pending facts:** `pending=True` is a valid transitional pre-result state only
when no completed `result_class` / winner / method is present. Pending combined
with completed result fields or `cancelled=True` raises `SettlementFactsError`.

**Method / result_class invariants (single fact version):**
- `method="technical_draw"` requires `result_class="draw"` (and no winner).
- Decisive methods (`decision`, `technical_decision`, finishes) require
  `result_class="decisive"`.
- Ordinary scorecard draw is `result_class="draw"` with `method` omitted.
- Incomplete pairs (method without the matching class, or clocks without a
  settled `result_class` for totals) never produce a confident grade:
  structurally impossible combinations raise `SettlementFactsError`; otherwise
  families return `unresolved`.

**Cancelled / no-contest precedence:** On one fact version, `cancelled=True` or
`result_class="no_contest"` must not retain `winner_side` or `method`. If a
source stream previously recorded a method before an NC/cancel revision, keep
that on an earlier version — do not mix contradictory fields into the terminal
version (settlement would otherwise risk consuming stale method data).

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

## Reference odds (DWCS-201)

| Field | Value |
|-------|--------|
| **Provider** | `the_odds_api` (CLI label `the-odds-api`) |
| **Client** | `mma_model.odds.the_odds_api.TheOddsApiClient` |
| **Normalize** | `mma_model.odds.normalize.normalize_odds_payload` |
| **Storage** | `odds_events`, `odds_quotes` (append-only + dedupe), `odds_quota_observations`, `odds_availability_observations` (append-only unknown markets) |
| **Quote dedupe key** | v2 SHA-256 over provider/event/book/region/market/outcome/line/price/`source_updated_at`/commence/`snapshot_at` **plus** sanitized `raw_ref` and order-independent participant names (`dedupe_version=2`). Legacy v1 keys omit raw/participants (`dedupe_version=1`). Store dedupes on v2 hit **or** legacy v1 key with matching `raw_ref` (identical re-poll); different `raw_ref` inserts under v2 so same-ID replacements never collide. Append-only: legacy rows are not rewritten. |
| **Migrations** | `0011_odds_quotes`, `0012_odds_availability` |
| **CLI** | `mma-model odds` (legacy live fetch); `mma-model odds snapshot …`; `mma-model odds audit …` |
| **Markets** | Supported provider keys: `h2h` → `moneyline`, `totals` → `totals` with DWCS-200 catalog line points `(1.5, 2.5)`. Unsupported props/points are skipped, never fabricated. |
| **Region** | Exactly one region per snapshot/storage operation; multi-region rejected. |
| **Availability** | Missing requested markets recorded as `unknown` per provider event + bookmaker (when known). Never `suspended` without provider evidence. |
| **Series scope** | `--series` is a requested label only (`canonical_series_verified=false`, `provider_scope=provider_unmatched`) until DWCS-203 bout matching. |
| **Offline** | Explicit only: `--offline-fixtures --fixture-dir PATH --database-url disposable-sqlite`. No implicit tests/ fixtures; fail closed without `ODDS_API_KEY`. |
| **Product rule** | Exact bookmaker lines remain optional enrichment. Reference quotes are never labeled Bet365. |

Quota headers persisted: raw `x-requests-remaining` / `x-requests-used` / `x-requests-last`, plus `requests_last_inferred` and `requests_last_source` (`provider` | `missing` | `inferred_empty_zero`). Empty responses with a missing last header keep `requests_last=NULL` and record inferred cost `0` separately; provider-reported `0` stays `requests_last=0` with source `provider`.

## Optional bookmaker enrichment + price fallback (DWCS-202)

| Field | Value |
|-------|--------|
| **Authoritative contract** | Packaged `mma_model/odds/odds_decision_v1.yaml` (`PINNED_ODDS_DECISION_HASH`) |
| **Plan-visible path** | `config/sources/odds.yaml` (symlink to packaged file) |
| **Phase 0 gate** | Packaged decision + optional checkout cross-check of `output/research/odds-coverage-summary.json` → `decision.path=the_odds_api_reference_fallback` |
| **Licensed adapter** | **Not authorized.** Bet365×DWCS was `scoped_absent`; OpticOdds / SportsGameOdds / SportsDataIO were `not_configured`. Do not invent automated bookmaker adapters. |
| **Manual prices** | `user_observed` (non-automated); `mma_model.odds.manual_price` |
| **Guidance** | `mma_model.odds.price_guidance` — catalog-validated selection; observation must match family/outcome/line; fair / actionable / strong-value for qualified unpriced rows |
| **Exact EV** | Only when an available observed price exists on a qualified + gates-pass selection (`compute_exact_ev`); never synthetic ROI/CLV |
| **Lifecycle** | Explicit `available` / `unknown` / `suspended` / `locked` / `removed` / `entitlement_failed` — no forward-fill; `attempted_provider` required for entitlement failures |
| **Selection identity** | Canonical `selection_identity` = `family:outcome` or `family:outcome:line` from DWCS-200 catalog; computed when omitted, rejected on mismatch. Not the settlement rule-set content hash. |
| **Manual provenance** | Parser always sets `user_observed` / non-automated; rejects caller `source_kind` / `automated` / non-entitlement `provider` (no silent relabel). |
| **Clocks** | When both exist, `source_updated_at <= observed_at` (UTC-normalized). |
| **Storage** | `odds_manual_price_observations` (append-only); migration `0013_odds_manual_prices` (final integrity CHECKs + `attempted_provider` + `selection_identity`) |
| **Bet365 identity** | Explicit aliases only (`bet365`, `bet365_au`); automated/reference observations reject those keys while fallback is active; only non-automated `user_observed` may claim Bet365 |
| **CLI** | `mma-model odds audit-bookmakers --next-dwcs`; `mma-model odds price-guidance … [--line-point]`; `mma-model odds record-manual-price …` |
| **Prohibited** | Sportsbook website scraping, credential/cookie storage, Bet365 claims for reference consensus |

## Odds event matching + bout lifecycle (DWCS-203)

| Field | Value |
|-------|--------|
| **Contract** | Packaged `mma_model/odds/matching_v1.yaml` (`config/odds/matching_v1.yaml` symlink); pinned content hash + frozen load |
| **Matcher** | Stored provider IDs are a strong candidate only after active-DWCS / participant / time-window verification; then exact participant pair within `match_window_minutes` (default 30). |
| **Value eligibility** | Bout `eligible_for_value` / `match_value_gate` = matched + nonterminal only (does **not** mean every quote is usable). Quote-level resolver requires the **latest persisted match observation at cutoff** to be `matched` to the **same bout** as the effective alias; newer `ambiguous_blocked` / `unmatched` decisions block all quote value even if an old alias remains. Caller bout/status may only further restrict. Also requires alias-visible row + bout nonterminal + selection not locked + market not UNKNOWN + quote AVAILABLE + quote’s own freshness. |
| **Availability** | `odds_availability_observations` UNKNOWN blocks that bookmaker/region/market when UNKNOWN is ≥ quote evidence; only a *strictly newer* available quote clears prior UNKNOWN (`availability_note=recovered_by_newer_quote`, non-blocking). Equal timestamps stay fail-closed. Never infers SUSPENDED/LOCKED. |
| **Ordering** | Provider home/away ignored; both canonical fighters required |
| **Ambiguity** | Dedicated `odds_bout_match_reviews` queue (not fighter identity); approve **claims** the review via version CAS first (savepoint), then activates/attaches the owned alias; CAS loss rolls back claim+alias with zero side effects; reject stays blocked; no fighter mapping writes |
| **Aliases / source IDs** | Immutable `BoutSourceId` + versioned `odds_provider_event_aliases`; exactly one active alias per provider/external ID (partial unique index). Same-ID replacements supersede history and do not expose prior quotes under the new active alias |
| **Lifecycle** | Bout-scoped latest explicit lifecycle wins for cancel/replace/review/global lock. Selection-scoped lock/removal uses optional book/region/market/outcome/line/`quote_id` on lifecycle rows (null = bout-wide). Terminal→ACTIVE matrix unchanged; no inference/forward-fill. |
| **Storage** | `odds_provider_event_aliases`, `odds_match_observations`, `odds_bout_lifecycle_observations` (+ selection scope cols), `odds_bout_match_reviews`; `odds_quotes.dedupe_version`; migrations `0014_odds_matching`, `0015_quote_eligibility_scope` |
| **Next DWCS** | `--next-dwcs --as-of <UTC>` selects nearest upcoming DWCS card (fight-night cluster), scopes provider events, fail-closed on zero bouts/events |
| **CLI** | `mma-model odds reconcile --next-dwcs --strict [--as-of …]`; golden seeding only with `--golden-card` + `--offline-fixtures` + disposable `--database-url` |
| **Golden** | Offline/disposable-DB only; committed fixtures under `tests/fixtures/odds/golden/` must achieve 100% exact active-bout matches |
| **Prohibited** | Home/away guess, fuzzy auto-merge, silent replacement quote inheritance, forward-fill, inferred lock/suspension without evidence, live-DB golden seeding |

Value calculations (DWCS-204+) may consume only `matched` + `active` quotes. Exact bookmaker lines remain optional enrichment; sportsbook-agnostic actionable price guidance remains the mandatory fallback. Snapshot/reference rows stay `provider_unmatched` until proven by this matcher.

## Value math, EV, CLV, and staking (DWCS-204)

| Field | Value |
|-------|--------|
| **Package** | `mma_model.value` (`odds`, `devig`, `thresholds`, `ev`, `kelly`, `portfolio`, `priced`) |
| **Method / version** | `VALUE_MATH_METHOD=dwcs_value_math` / `VALUE_MATH_VERSION=1.0.0`; de-vig `proportional_complete_set` `1.0.0` |
| **Odds interface** | Validated decimal (`> 1`) and American (`<= -100` or `>= +100`); reject `0` American and impossible probabilities |
| **De-vig** | Complete-set proportional only; incomplete sets return `IncompleteMarketSet` / raise; fair probs sum to 1 |
| **Thresholds** | Fair `1/p50`, p25 break-even, actionable `max(1/p25,(1+target)/p50)`, strong-value at 10% (exact-round actionable 10%) — pinned to DWCS-001 formulas |
| **Priced metrics** | EV, same-line probability CLV, closing EV, flat 1-unit profit, quarter-Kelly capped at 1% bankroll |
| **Eligibility** | Provider quotes require DWCS-203 quote-level eligibility (match gate alone insufficient); user-observed uses DWCS-202 product eligibility |
| **Unpriced** | Price-target rows never receive EV / ROI / CLV / realized profit / stake |
| **Push / void** | Realized flat-unit profit is exactly `0` |
| **Rounding** | Full precision internally; display helpers only at boundaries |
| **Out of scope** | Bet ranking, portfolio selection beyond per-bet cap, model fitting, dashboard, sportsbook scraping |

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
