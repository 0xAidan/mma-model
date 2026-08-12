# DWCS-003 stats / identity source decision

Phase 0 spike selecting the **production stats and identity** source stack by
measured coverage, written rights, cost, and point-in-time fitness. Sportsbook
odds remain optional enrichment (DWCS-000); a missing Bet365 feed is **irrelevant**
to this decision.

This document records method, citations, gates, and the handoff contract. It does
**not** invent provider coverage from catalogs or product pages.

## Phase 0 acceptance posture (important)

Phase 0 **permits** an explicit hard blocker when credentials, entitlements, or
written vendor quotes are missing. That is a documented risk, not a silent pass.

For the credentialed refresh capture, acceptance evidence is:

1. A reproducible committed scorecard in `capture_mode=live` with
   `live_measurements_claimed=false` (technical coverage not measured),
   `providers.balldontlie.access_status=entitlement_blocked`, null coverage
   numerators, and `decision.path=hard_blocker` / `primary=null`.
2. Sanitized aggregate probe notes only (no raw licensed payloads): DWCS-named
   event discovery count, difficult-identity hit/miss/unknown partition, rate-limit
   tier header, and explicit fights-endpoint entitlement classification.
3. Audit-code fixes required for a valid executable measurement path (cursor
   pagination / date-based event discovery, strict DWCS name matcher, 429 retry,
   entitlement-after-auth classification) covered by unit tests.

It is **not** acceptance of measured BALLDONTLIE bout/outcome coverage and **not**
adoption. Coverage was **not invented**. A present API key or successful
`/events`/`/fighters` call alone is not adoption evidence.

## Goal

Choose exactly one primary licensed/official stats+identity source for DWCS
production ingest, or record a hard production blocker with ranked lawful
fallbacks. Never silently fall back to Tapology, Sherdog, FightMatrix, UFC.com
HTML, UFCStats HTML, or Bet365 scraping.

## Inputs

| Input | Role |
|-------|------|
| `data/manifests/dwcs_bouts_v1.jsonl` | Frozen DWCS-002 universe (89 cards / 440 bouts) |
| 2023–2025 bout slice | Audit universe: **30 unique events**, 149 bouts |
| Deterministic 50 difficult-identity sample | Identity stress set (seeded ranking) |
| Public provider docs/terms | Rights, list price, documented fields only |
| Optional `BALLDONTLIE_API_KEY` / `API_SPORTS_KEY` | Live measured probes only when present |
| Optional vendor-notes JSON | SportsDataIO / Combat Registry written quote gates |

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

The BALLDONTLIE live path probes **this 50-sample** (hit / miss / unknown), with
bounded request budgets. It does not silently substitute a smaller generic
entrant slice as the identity metric.

## Measurement definitions (executable path)

| Metric | Definition |
|--------|------------|
| Event coverage | Unique matched manifest events / unique frozen manifest events (denom **30** for 2023–2025). Never years/years. |
| Bout coverage | Unique matched bouts / unique frozen bouts. |
| Outcome agreement | Comparable mapped pairs only: provider winner/result vs manifest **event-night** result. Unmapped/ambiguous excluded (`denominator_policy=comparable_mapped_pairs_only`). |
| Difficult identities | Partition of probed sample into hit / miss / unknown. |
| PIT / required features / nulls / revisions / latency / request cost | Measured when evidenced; otherwise `unknown` with reason. HTTP success alone does **not** auto-fail or auto-pass. |
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
   Missing quote/credentials ⇒ hard blocker (no silent selection).
4. Keep API-Sports for one paid month only if non-overlap ≥10% **and** accuracy
   pass; else cancel.
5. Never silently fall back to prohibited scraping.

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

Public field/workflow/criteria citations only. Monthly price, retention, revision
semantics, SLA contract language, and commercial reuse rights remain **quote
blockers** until answered in writing
([workflow](https://sportsdata.io/developers/workflow-guide/mma),
[dictionary](https://sportsdata.io/developers/data-dictionary/mma),
[ABC criteria](https://www.abcboxing.com/mma-record-keeper-criteria/),
[portal](https://app.combatreg.com/)).

## Live credential outcomes (this PR capture)

Committed scorecard: **`live`**,
`captured_at=2026-08-12T01:00:00+00:00`,
`live_measurements_claimed=false`.

| Provider | Credential / access | Outcome |
|----------|---------------------|---------|
| BALLDONTLIE | Key present; observed `x-ratelimit-limit=5` | `/events` + `/fighters` ok; **`/fights` → `entitlement_blocked`** (tier lacks Fights; Fight Statistics also GOAT-only). Coverage numerators remain **null/unknown** (not zero). Strict date-based discovery found **30/30** DWCS-named events on manifest dates. Difficult-identity sample **50 hit / 0 miss / 0 unknown**. |
| API-Sports | `API_SPORTS_KEY` absent | `not_configured`; non-overlap/accuracy **unknown** |
| SportsDataIO / Combat Registry | No written quote on file | `quote_pending` blockers |

Re-run command:

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T01:00:00+00:00 \
  --env-file /path/to/.env \
  --redact
```

Synthetic unit fixtures under
`tests/fixtures/spikes/stats_source_synthetic_observations.json` prove metric math
and threshold decisions only. They are **not** live provider evidence.

## Decision recorded

**Hard production blocker** — `decision.path = hard_blocker`, `primary = null`.

Reasons:

1. BALLDONTLIE technical gates cannot pass: `/fights` is **entitlement_blocked** on
   the configured key (Free/trial-class RPM observed at 5 req/min). Event/bout
   coverage and outcome agreement therefore remain **blocked/unknown**, not
   measured pass. Required features + PIT stay unknown (fight_stats/PIT evidence
   not available without entitled fight access).
2. SportsDataIO / Combat Registry lack complete written quotes with measured
   thresholds.
3. API-Sports probe cannot be kept without measured ≥10% non-overlap + accuracy.
4. Prohibited scraping was not selected.

Identity probe note (sanitized): difficult-identity sample completed at 50/50
hits under free-tier `/fighters`, but identity success does **not** clear
event/bout/outcome/feature/PIT gates.

### Rights / budget conclusion

- BALLDONTLIE written rights: **pass** (public terms §6); list price **3999¢**.
- Hypothetical stack if later adopted: Odds API 3000¢ + GOAT 3999¢ = **6999¢
  ($69.99) ≤ 10000¢ cap**.
- API-Sports storage/modeling rights: **unknown**.
- SportsDataIO / Combat Registry commercial rights+price: **unanswered blockers**.

### Ranked lawful fallback paths

1. SportsDataIO — complete quote + same technical thresholds.
2. Combat Registry — complete quote + same technical thresholds.
3. API-Sports — one-month probe only after credentials exist and non-overlap≥10%
   + accuracy pass; otherwise cancel.

## Commands

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T01:00:00+00:00 \
  --env-file /path/to/.env \
  --redact

pytest tests/spikes/test_stats_source_scorecard.py -q
ruff check scripts/spikes/audit_stats_sources.py tests/spikes/test_stats_source_scorecard.py
```

## Handoff contract (DWCS-102)

1. Read `decision.primary` from the scorecard.
2. If set, implement exactly that adapter.
3. If `hard_blocker`, keep production stats ingest blocked; pursue ranked lawful
   fallbacks; do not enable scrapers.
4. Preserve DWCS-002 manifests as the universe seed.
5. Missing bookmaker lines do not unblock or re-rank stats sources.
