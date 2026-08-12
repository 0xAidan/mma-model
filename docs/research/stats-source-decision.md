# DWCS-003 stats / identity source decision

Phase 0 spike selecting the **production stats and identity** source stack by
measured coverage, written rights, cost, and point-in-time fitness. Sportsbook
odds remain optional enrichment (DWCS-000); a missing Bet365 feed is **irrelevant**
to this decision.

## Policy amendment (2026-08-12) — public-first hybrid

The user explicitly approved replacing the Phase 1 **licensed-provider-only**
ingest rule with a personal-project **public-first hybrid**. Machine-readable
contract: [`config/sources/source_policy_v1.json`](../../config/sources/source_policy_v1.json).
Design: [`docs/superpowers/specs/2026-08-12-public-first-mma-history-design.md`](../superpowers/specs/2026-08-12-public-first-mma-history-design.md).
Plan: [`docs/superpowers/plans/2026-08-12-public-first-mma-history.md`](../superpowers/plans/2026-08-12-public-first-mma-history.md).

**Unchanged by the amendment:**

- `decision.primary` remains `null` until a **measured** public-source (or later
  licensed) audit passes all technical gates.
- BALLDONTLIE and SportsDataIO scorecard rows stay **historical audit evidence**.
- Gates remain: 89/440 universe; every exclusion categorized; ≥98% cross-source
  reconciliation where comparable; ≥99% result agreement; zero unresolved
  evaluated/upcoming identity conflicts; zero future-row leakage failures; no
  mutable-current historical feature leakage.

**Changed by the amendment:**

- Phase 1 may implement labeled public observation adapters (UFCStats direct,
  Tapology/Sherdog public pages where accessible, Combat Registry public
  results, BestFightOdds archive) under access-ethics and PIT rules in the
  source policy.
- Silent “scrape anything to unblock licensed hard_blocker” remains rejected.
- Licensed `hard_blocker` still means **no licensed primary adoption**; it does
  not forbid the public-first Phase 1 path.

This document records method, citations, gates, and the handoff contract. It does
**not** invent provider coverage from catalogs or product pages.

## Phase 0 acceptance posture (important)

Phase 0 **permits** an explicit hard blocker when any required gate fails or
remains unknown (credentials, entitlements, required features, PIT fitness, or
written vendor quotes). That is a documented risk, not a silent pass.

For this SportsDataIO credentialed refresh, acceptance evidence is:

1. A reproducible committed scorecard in `capture_mode=live` with
   `live_measurements_claimed=true`, preserved BALLDONTLIE measured history,
   SportsDataIO classified separately for auth / entitlement / quota / schema /
   rights / quote, and `decision.path=hard_blocker` / `primary=null`.
2. Sanitized aggregate metrics only (no raw licensed payloads).
3. A small accessible-season sample must never produce a global required-features
   pass. Entitlement-blocked seasons are **not** scored as coverage absences.
4. Difficult-identity hits alone do **not** qualify adoption.

It is **not** adoption. Coverage was measured or honestly blocked, not invented.
HTTP success and identity hits do not auto-pass PIT or required features.

## Goal

Originally: choose exactly one primary licensed/official stats+identity source
for DWCS production ingest, or record a hard production blocker with ranked
lawful fallbacks.

Amended 2026-08-12: licensed primary selection may remain blocked (`primary=null`)
while Phase 1 proceeds under the public-first hybrid policy. Never silently fall
back to unrestricted scraping, Bet365 scraping, or access-control bypass. Public
adapters must be explicit, labeled, rate-limited, and killable per
`source_policy_v1.json`.

## Inputs

| Input | Role |
|-------|------|
| `data/manifests/dwcs_bouts_v1.jsonl` | Frozen DWCS-002 universe (89 cards / 440 bouts) |
| 2023–2025 bout slice | Audit universe: **30 unique events**, 149 bouts |
| Deterministic 50 difficult-identity sample | Identity stress set (seeded ranking) |
| Public provider docs/terms | Rights, list price, documented fields only |
| Optional `BALLDONTLIE_API_KEY` / `API_SPORTS_KEY` / `SPORTSDATAIO_API_KEY` | Live measured probes only when present |
| Optional vendor-notes JSON | SportsDataIO / Combat Registry written quote gates |
| Prior committed scorecard | Preserve sanitized BALLDONTLIE measured history when that key is absent |

DWCS-002 caveats carried forward: Phase 1 must ingest the frozen manifests first;
null UFCStats IDs and publication timestamps remain open gaps for later tickets;
prohibited scraping stays rejected.

## Difficult-identity sample (deterministic)

Selection is encoded in `scripts/spikes/audit_stats_sources.py`:

1. Collect unique 2023–2025 DWCS entrants (ESPN athlete id, else normalized name).
2. Score identity difficulty (unicode, hyphen/apostrophe, multi-token names, short
   / colliding last names, Brazil series, reversals, jr/sr).
3. Tie-break with `sha256("dwcs-003-difficult-identities-v1\|{entrant_key}")`.
4. Take the top 50.

## Measurement definitions (executable measurement path)

| Metric | Definition |
|--------|------------|
| Event coverage | Unique matched manifest events / unique frozen manifest events (denom **30** for 2023–2025). Never years/years. |
| Bout coverage | Unique matched bouts / unique frozen bouts. |
| Outcome agreement | Comparable mapped pairs only: provider winner/result vs manifest **event-night** result. Unmapped/ambiguous excluded (`denominator_policy=comparable_mapped_pairs_only`). |
| Difficult identities | Partition of probed sample into hit / miss / unknown. |
| Required features | Fight-level fields and fight_stats fields scored **separately**. Denominator for every required field is the matched-bout universe (149). Pass requires full-universe stat probing and each field rate ≥ **0.98**. Partial samples ⇒ `unknown` (`stat_probe_incomplete`), never pass. SportsDataIO also records diagnostic result/elapsed-time field presence on accessible seasons only. |
| PIT / nulls / revisions / latency / request cost | PIT **pass** requires every explicit dimension: pre-fight reconstruction, revision/correction support, **and** publication/source-update timestamps. Missing/unknown timestamp proof can never pass. HTTP success alone does **not** auto-fail or auto-pass. |
| API-Sports non-overlap | Fingerprinted provider pre-DWCS history bouts not present in the DWCS universe / total fingerprintable history. |
| Year diagnostics | Informational only (`years_with_any_provider_dwcs_named_events`); never used as event coverage. |

Money amounts are quantized to integer cents with decimal-safe USD strings
(e.g. `{"usd_cents": 6999, "usd": "69.99"}`).

## Decision tree (plan §4) — applied exactly

1. Trial BALLDONTLIE GOAT against 2023–2025 DWCS entrants + difficult identities.
2. Adopt BALLDONTLIE only if **all** hold: event ≥98%, bout ≥98%, outcome ≥99%,
   required features + PIT fitness, written storage/modeling rights, budget ≤~$100.
3. Else adopt SportsDataIO or Combat Registry only when a **complete written quote**
   supplies rights/budget **and** the same technical thresholds are measured.
   Missing quote/credentials/entitlement ⇒ hard blocker (no silent selection).
4. Keep API-Sports for one paid month only if non-overlap ≥10% **and** accuracy
   pass; else cancel.
5. Never silently fall back to prohibited / access-bypassing scraping. Public-first
   adapters are a separate, explicitly approved path (see policy amendment).

## Public evidence checked (citations)

Checked at scorecard `evidence_timestamps` (committed capture uses fixed
`--capture-time`).

### BALLDONTLIE MMA GOAT

| Topic | Evidence | Citation |
|-------|----------|----------|
| List price / GOAT RPM | $39.99/mo (3999¢), 600 req/min; 48h trial | [mma.balldontlie.io](https://mma.balldontlie.io/) |
| Storage + modeling rights | Terms §6 allow store/archive/modify/analyze and AI/ML training | [terms](https://balldontlie.io/terms.html) |
| Coverage caveat | UFC comprehensive; other leagues limited — league listing ≠ coverage | [mma.balldontlie.io](https://mma.balldontlie.io/) |

### API-Sports MMA

| Topic | Evidence | Citation |
|-------|----------|----------|
| Product / pricing | Free 100 req/day; paid plans; recent MMA launch | [product](https://api-sports.io/sports/mma) |
| Auth | `x-apisports-key` | [docs](https://api-sports.io/documentation/mma/v1) |
| Rights | Customer must obtain league/rights-holder authorization; rights gate unknown until clarified | [terms](https://api-sports.io/terms) |

### SportsDataIO / Combat Registry

Official endpoints used for credentialed probing:
[API docs](https://sportsdata.io/developers/api-documentation/mma)
(`Schedule/{league}/{season}`, `Event/{eventid}`, `FightFinal/{fightid}`,
`FightersBasic`, `Leagues`). Auth uses the least-exposing supported mechanism:
`Ocp-Apim-Subscription-Key` **header only** (never query/URL).

Access classification rule: entitlement may be claimed only after an earlier
request in the same probe authenticated successfully. A first-call 401/403 with
generic “access” text is `auth_failed`, not entitlement.

Public field/workflow citations only for rights/price. Monthly price, retention,
revision semantics, SLA contract language, and commercial reuse rights remain
**quote blockers** until answered in writing
([workflow](https://sportsdata.io/developers/workflow-guide/mma),
[dictionary](https://sportsdata.io/developers/data-dictionary/mma),
[ABC criteria](https://www.abcboxing.com/mma-record-keeper-criteria/),
[portal](https://app.combatreg.com/)). Key access alone does **not** pass rights
or budget.

## Live credential outcomes (this PR capture)

Committed scorecard: **`live`**,
`captured_at=2026-08-12T14:30:00+00:00`,
`live_measurements_claimed=true`,
`acceptance_evidence_mode=measured_or_blocked_probe`.

BALLDONTLIE measured history is **preserved** from the prior entitlement-upgrade
capture (key may be absent locally). SportsDataIO was freshly probed with a
configured key.

| Provider | Credential / access | Outcome |
|----------|---------------------|---------|
| BALLDONTLIE | Preserved measured history (fights+stats entitled at capture) | event **30/30 (1.0)**; bout **149/149 (1.0)**; outcome **149/149 (1.0)**; fight fields **pass** (149/149 each); stat fields: `significant_strikes_landed` **149/149**, `takedowns_landed` **149/149**, `control_time_seconds` **98/149 (≈0.658)** → required_features **fail**; PIT **unknown**; identity **50/50 hit** |
| SportsDataIO | Key present; auth **ok**; seasons 2023–2024 **entitlement_blocked**; 2025 **ok** | Full-universe event/bout/outcome numerators **null** (not zero); metrics_status **blocked**; required_features **unknown**; PIT **unknown**; rights **unknown**; quote **quote_pending**. Accessible-season diagnostic only: 49 matched bouts vs denom 149; identity **50/50 hit**; result/elapsed fields present on accessible sample — **not** a global pass |
| API-Sports | `API_SPORTS_KEY` absent | `not_configured`; non-overlap/accuracy **unknown** |
| Combat Registry | No written quote on file | `quote_pending` blockers |

Re-run command:

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T14:30:00+00:00 \
  --env-file /path/to/.env \
  --redact \
  --prior-scorecard output/research/stats-source-scorecard.json \
  --max-live-requests-sportsdataio 120
```

Synthetic unit fixtures under
`tests/fixtures/spikes/stats_source_synthetic_observations.json` prove metric math
and threshold decisions only. They are **not** live provider evidence.

## Decision recorded

**Hard production blocker** — `decision.path = hard_blocker`, `primary = null`.

Technical gate snapshot (BALLDONTLIE, preserved):

| Gate | Result |
|------|--------|
| event_coverage | 30/30 = 1.0 (≥0.98) |
| bout_coverage | 149/149 = 1.0 (≥0.98) |
| outcome_agreement | 149/149 = 1.0 (≥0.99) |
| required_features | **fail** (`control_time_seconds` 98/149 < 0.98) |
| pit_fitness | **unknown** (reconstruction + revision + publication timestamps unproven) |
| rights | pass |
| budget | pass (6999¢ ≤ 10000¢) |
| technical_pass / adopt | **false** |

SportsDataIO gate snapshot:

| Classification | Status |
|----------------|--------|
| auth | ok |
| subscription entitlement | historical_seasons_blocked (2023–2024) |
| quota | ok |
| schema | ok_on_accessible_endpoints |
| missing data | not_assessed_for_blocked_seasons |
| rights | unknown (no written storage/modeling/retention terms) |
| quote / budget | quote_pending / unknown |
| technical_pass / adopt | **false** |

Reasons:

1. BALLDONTLIE clears coverage and outcome agreement, but required features fail
   under the universe-wide fight_stats contract (`control_time_seconds` coverage
   below 0.98). PIT fitness also remains unknown (reconstruction, revision, and
   publication/source-update timestamps all unproven). Decision tree keeps
   `primary=null`.
2. SportsDataIO auth was established independently (Leagues ok), then audit
   seasons 2023–2024 returned post-auth feed denials classified as
   entitlement-blocked. Full-universe technical gates remain unknown/blocked
   (not invented zeros). Written rights and monthly quote remain unanswered
   blockers.
3. API-Sports probe cannot be kept without measured ≥10% non-overlap + accuracy.
4. Prohibited scraping was not selected.

### Rights / budget conclusion

- BALLDONTLIE written rights: **pass** (public terms §6); list price **3999¢**.
- Hypothetical BALLDONTLIE stack if later adopted: Odds API 3000¢ + GOAT 3999¢ =
  **6999¢ ($69.99) ≤ 10000¢ cap**.
- SportsDataIO rights+price: **unknown / quote_pending** (do not infer from key
  access or marketing pages).
- Combat Registry commercial rights+price: **unanswered blockers**.

### Ranked lawful fallback paths

1. SportsDataIO — entitle full 2023–2025 history + complete written quote/rights/
   budget + same technical thresholds.
2. Combat Registry — complete quote + same technical thresholds.
3. API-Sports — one-month probe only after credentials exist and non-overlap≥10%
   + accuracy pass; otherwise cancel.

## Commands

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T14:30:00+00:00 \
  --env-file /path/to/.env \
  --redact \
  --prior-scorecard output/research/stats-source-scorecard.json \
  --max-live-requests-sportsdataio 120

pytest tests/spikes/test_stats_source_scorecard.py -q
ruff check scripts/spikes/audit_stats_sources.py tests/spikes/test_stats_source_scorecard.py
```

## Handoff contract (DWCS-102)

1. Read `config/sources/source_policy_v1.json` (`policy_mode` must be
   `public_first_hybrid_personal_project`).
2. Read `decision.primary` from the scorecard. It remains `null` until a measured
   audit passes; do **not** invent a licensed primary.
3. Implement Phase 1 core ingest per the public-first plan (UFCStats public
   snapshots first; mma-ai bootstrap only after reconciliation). Treat
   SportsDataIO/BALLDONTLIE as validation/enrichment only under measured limits.
4. Preserve DWCS-002 manifests as the universe seed.
5. Missing bookmaker lines do not unblock or re-rank stats sources.
6. Never bypass logins, paywalls, CAPTCHAs, robots/access controls, or technical
   restrictions; stop on block signals and follow kill/fallback order.
