# DWCS-003 stats / identity source decision

Phase 0 spike selecting the **production stats and identity** source stack by
measured coverage, written rights, cost, and point-in-time fitness. Sportsbook
odds remain optional enrichment (DWCS-000); a missing Bet365 feed is **irrelevant**
to this decision.

This document records method, citations, gates, and the handoff contract. It does
**not** invent provider coverage from catalogs or product pages.

## Phase 0 acceptance posture (important)

Phase 0 **permits** an explicit hard blocker when any required gate fails or
remains unknown (credentials, entitlements, required features, PIT fitness, or
written vendor quotes). That is a documented risk, not a silent pass.

For this post-entitlement-upgrade revalidation capture, acceptance evidence is:

1. A reproducible committed scorecard in `capture_mode=live` with
   `live_measurements_claimed=true`, `providers.balldontlie.access_status=ok`,
   measured event/bout/outcome aggregates, **universe-wide** required-feature
   scoring (fight fields vs fight_stats fields reported separately), and
   `decision.path=hard_blocker` / `primary=null`.
2. Sanitized aggregate metrics only (no raw licensed payloads).
3. A small fight_stats sample must never produce a global required-features pass.
4. Difficult-identity hits alone do **not** qualify adoption.

It is **not** adoption. Coverage was measured, not invented. HTTP success and
identity hits do not auto-pass PIT or required features.

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

## Measurement definitions (executable measurement path)

| Metric | Definition |
|--------|------------|
| Event coverage | Unique matched manifest events / unique frozen manifest events (denom **30** for 2023–2025). Never years/years. |
| Bout coverage | Unique matched bouts / unique frozen bouts. |
| Outcome agreement | Comparable mapped pairs only: provider winner/result vs manifest **event-night** result. Unmapped/ambiguous excluded (`denominator_policy=comparable_mapped_pairs_only`). |
| Difficult identities | Partition of probed sample into hit / miss / unknown. |
| Required features | Fight-level fields and fight_stats fields scored **separately**. Denominator for every required field is the matched-bout universe (149). Pass requires full-universe stat probing and each field rate ≥ **0.98**. Partial samples ⇒ `unknown` (`stat_probe_incomplete`), never pass. |
| PIT / nulls / revisions / latency / request cost | Measured when evidenced; otherwise `unknown` with reason. HTTP success alone does **not** auto-fail or auto-pass. |
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
`captured_at=2026-08-12T02:20:00+00:00`,
`live_measurements_claimed=true`,
`acceptance_evidence_mode=measured_or_blocked_probe`.

Minimal entitlement probe (sanitized): `/fights` → `access_status=ok`,
observed `x-ratelimit-limit=600`. Full matched-universe `/fight_stats` probe:
**149/149** bouts probed (checkpointed sanitized field-presence only).

| Provider | Credential / access | Outcome |
|----------|---------------------|---------|
| BALLDONTLIE | Key present; fights+stats entitled (600 RPM) | event **30/30 (1.0)**; bout **149/149 (1.0)**; outcome **149/149 (1.0)**; fight fields **pass** (149/149 each); stat fields: `significant_strikes_landed` **149/149**, `takedowns_landed` **149/149**, `control_time_seconds` **98/149 (≈0.658)** → required_features **fail**; PIT **unknown**; identity **50/50 hit** |
| API-Sports | `API_SPORTS_KEY` absent | `not_configured`; non-overlap/accuracy **unknown** |
| SportsDataIO / Combat Registry | No written quote on file | `quote_pending` blockers |

Re-run command:

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T02:20:00+00:00 \
  --env-file /path/to/.env \
  --redact \
  --max-live-requests-balldontlie 300 \
  --stat-checkpoint output/research/.balldontlie-stat-probe-checkpoint.json
```

Synthetic unit fixtures under
`tests/fixtures/spikes/stats_source_synthetic_observations.json` prove metric math
and threshold decisions only. They are **not** live provider evidence.

## Decision recorded

**Hard production blocker** — `decision.path = hard_blocker`, `primary = null`.

Technical gate snapshot (BALLDONTLIE):

| Gate | Result |
|------|--------|
| event_coverage | 30/30 = 1.0 (≥0.98) |
| bout_coverage | 149/149 = 1.0 (≥0.98) |
| outcome_agreement | 149/149 = 1.0 (≥0.99) |
| required_features | **fail** (`control_time_seconds` 98/149 < 0.98) |
| pit_fitness | **unknown** (reconstruction + revision unproven) |
| rights | pass |
| budget | pass (6999¢ ≤ 10000¢) |
| technical_pass / adopt | **false** |

Reasons:

1. BALLDONTLIE clears coverage and outcome agreement, but required features fail
   under the universe-wide fight_stats contract (`control_time_seconds` coverage
   below 0.98). PIT fitness also remains unknown. Decision tree keeps
   `primary=null`.
2. SportsDataIO / Combat Registry lack complete written quotes with measured
   thresholds.
3. API-Sports probe cannot be kept without measured ≥10% non-overlap + accuracy.
4. Prohibited scraping was not selected.

Identity probe note (sanitized): difficult-identity sample completed at 50/50
hits, but identity success does **not** clear required-features or PIT gates.

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

Next evidence needed for BALLDONTLIE adoption (not claimed here): raise
`control_time_seconds` universe support to ≥98% (or document an explicit
contract change with new thresholds + regressions), plus explicit pass/fail
measurement of pre-fight reconstruction and revision support.

## Commands

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-12T02:20:00+00:00 \
  --env-file /path/to/.env \
  --redact \
  --max-live-requests-balldontlie 300

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
