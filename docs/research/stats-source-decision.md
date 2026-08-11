# DWCS-003 stats / identity source decision

Phase 0 spike selecting the **production stats and identity** source stack by
measured coverage, written rights, cost, and point-in-time fitness. Sportsbook
odds remain optional enrichment (DWCS-000); a missing Bet365 feed is **irrelevant**
to this decision.

This document records method, citations, gates, and the handoff contract. It does
**not** invent provider coverage from catalogs or product pages.

## Goal

Choose exactly one primary licensed/official stats+identity source for DWCS
production ingest, or record a hard production blocker with ranked lawful
fallbacks. Never silently fall back to Tapology, Sherdog, FightMatrix, UFC.com
HTML, UFCStats HTML, or Bet365 scraping.

## Inputs

| Input | Role |
|-------|------|
| `data/manifests/dwcs_bouts_v1.jsonl` | Frozen DWCS-002 universe (89 cards / 440 bouts) |
| 2023–2025 bout slice | Audit universe for entrants + event/bout denominators |
| Deterministic 50 difficult-identity sample | Identity stress set (see scorecard) |
| Public provider docs/terms | Rights, list price, documented fields only |
| Optional `BALLDONTLIE_API_KEY` / `API_SPORTS_KEY` | Live measured probes only when present |

DWCS-002 caveats carried forward: Phase 1 must ingest the frozen manifests first;
null UFCStats IDs and publication timestamps remain open gaps for later tickets;
prohibited scraping stays rejected.

## Difficult-identity sample (deterministic)

Selection is encoded in `scripts/spikes/audit_stats_sources.py` and echoed in the
scorecard:

1. Collect unique 2023–2025 DWCS entrants (ESPN athlete id, else normalized name).
2. Score identity difficulty: unicode/non-ASCII, hyphen/apostrophe, multi-token
   names, short last names, colliding last names, Brazil series, reversal
   participants, jr/sr suffixes.
3. Tie-break with `sha256("dwcs-003-difficult-identities-v1\|{entrant_key}")`.
4. Take the top 50.

Re-running with the same manifest and seed yields the same sample.

## Decision tree (plan §4) — applied exactly

1. Trial BALLDONTLIE GOAT against 2023–2025 DWCS entrants + difficult identities.
2. Adopt BALLDONTLIE for UFC/DWCS core **only if all** hold:
   - event coverage ≥ **98%**
   - bout coverage ≥ **98%**
   - outcome agreement ≥ **99%**
   - required feature fields + point-in-time fitness
   - written storage/modeling rights
   - combined recurring spend within **~$100/month** target
3. Otherwise obtain SportsDataIO and Combat Registry **written quotes**. Adopt the
   first source that clears the same technical gates and budget; prefer
   SportsDataIO for DWCS event/stats and Combat Registry for pro/amateur identity.
4. Keep API-Sports for one paid month **only if** it adds ≥ **10%** non-overlapping
   pre-DWCS bouts **and** passes accuracy; else cancel.
5. Never silently fall back to prohibited scraping.

## Public evidence checked (citations)

Checked at scorecard `evidence_timestamps` (committed capture uses a fixed
`--capture-time`).

### BALLDONTLIE MMA GOAT (provisional primary)

| Topic | Evidence | Citation |
|-------|----------|----------|
| List price / GOAT RPM | $39.99/mo, 600 req/min; 48h GOAT trial exists | [mma.balldontlie.io](https://mma.balldontlie.io/) |
| Storage + modeling rights | Terms §6 allow store/archive/modify/analyze and AI/ML training on lawfully obtained Data | [terms](https://balldontlie.io/terms.html) |
| Coverage caveat | Docs: UFC comprehensive; other leagues limited/incomplete (DWCS listed in leagues endpoint is **not** coverage proof) | [mma.balldontlie.io](https://mma.balldontlie.io/) |
| Endpoints | events, fighters, fights, fight_stats, odds (tier-gated) | [mma.balldontlie.io](https://mma.balldontlie.io/) |

### API-Sports MMA (one-month probe only)

| Topic | Evidence | Citation |
|-------|----------|----------|
| Product / pricing surface | Free 100 req/day; paid PRO/ULTRA/MEGA; MMA API launched recently (2023–2024 news) | [api-sports.io/sports/mma](https://api-sports.io/sports/mma) |
| Auth | `x-apisports-key` on `https://v1.mma.api-sports.io/` | [docs](https://api-sports.io/documentation/mma/v1) |
| Rights | Terms push league/rights-holder authorization onto the customer for betting/commercial uses; **no** explicit storage+modeling grant comparable to BALLDONTLIE §6 → rights gate stays **unknown** until written clarification | [terms](https://api-sports.io/terms) |

### SportsDataIO (preferred paid fallback / upgrade)

| Topic | Evidence | Citation |
|-------|----------|----------|
| DWCS schedules (public claim) | Workflow guide states DWCS events are confirmed/updated when announced | [workflow](https://sportsdata.io/developers/workflow-guide/mma) |
| Fight/stat fields | Public data dictionary field list | [data dictionary](https://sportsdata.io/developers/data-dictionary/mma) |
| SLA marketing | Product page advertises SLAs / 24/7 support (not a signed contract) | [mma-ufc-api](https://sportsdata.io/mma-ufc-api) |
| Monthly price / retention / revision / signed rights | **Unanswered** — sales quote required (blockers in scorecard checklist) | — |

### Combat Registry / ABC (identity record layer)

| Topic | Evidence | Citation |
|-------|----------|----------|
| Registry criteria | ABC MMA record-keeper criteria require official result-backed records, backups, ABC access | [ABC criteria](https://www.abcboxing.com/mma-record-keeper-criteria/) |
| Portal | Commission/promoter portal at combatreg | [app.combatreg.com](https://app.combatreg.com/) |
| Commercial API price + reuse rights | **Unanswered** — written quote required | — |

## Live credential outcomes (this PR capture)

Committed scorecard capture mode: **`fixtures`** with fixed
`captured_at=2026-08-11T21:00:00+00:00`.

| Provider | Credential / access | Outcome |
|----------|---------------------|---------|
| BALLDONTLIE | `BALLDONTLIE_API_KEY` **absent** in environment and root `.env` | `access_status=not_configured`. Metrics remain **unknown** with non-null denominators and **null numerators**. Not classified as zero coverage. |
| API-Sports | `API_SPORTS_KEY` / `API_SPORTS_API_KEY` **absent** | `access_status=not_configured`. Non-overlap / accuracy **unknown**. |
| SportsDataIO | No authorized trial; no written quote on file | `quote_pending` checklist blockers |
| Combat Registry | No authorized API; no written quote on file | `quote_pending` checklist blockers |

Operator live mode (not claimed by the committed artifact):

```bash
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode live \
  --capture-time 2026-08-11T21:00:00+00:00 \
  --env-file /path/to/.env \
  --redact
```

Do **not** treat fixture regeneration as live measurement.

## Decision recorded

**Hard production blocker** — `decision.path = hard_blocker`, `primary = null`.

Reasons (honest, not invented):

1. BALLDONTLIE technical gates cannot pass without measured event/bout/outcome/PIT
   metrics; credentials were not configured, so metrics are unknown (not zero).
2. SportsDataIO and Combat Registry still lack written price/rights/retention/SLA
   responses (checklist blockers).
3. API-Sports probe cannot be kept without measured ≥10% non-overlapping pre-DWCS
   bouts + accuracy.
4. Prohibited scraping was **not** selected.

### Rights / budget conclusion

- BALLDONTLIE **written rights**: pass (public terms §6), list price $39.99/mo.
- Combined reference stack if BALLDONTLIE were adopted: The Odds API ~$30 + GOAT
  ~$39.99 ≈ **$69.99/mo** ≤ $100 cap (budget gate would pass **if** technical gates
  also passed).
- SportsDataIO / Combat Registry commercial rights + price: **unknown / blocker**.
- API-Sports storage/modeling rights: **unknown** under current public terms.

### Ranked lawful fallback paths

1. SportsDataIO — written quote (fields, rights, retention, revisions, SLA, price).
2. Combat Registry — written quote (API access, rights, price) for identity/history.
3. API-Sports — one-month probe only after credentials exist and non-overlap≥10% +
   accuracy pass; otherwise cancel.

## Classification rules

| Access status | Meaning |
|---------------|---------|
| `not_configured` | No credential available — never treat as absence/zero coverage |
| `auth_failed` | Key rejected |
| `entitlement_blocked` | Key present but tier/plan blocks endpoint |
| `ok` | Successful authenticated responses |
| `request_failed` | Transport/HTTP failure — not absence |

| Metric status | Meaning |
|---------------|---------|
| `measured` | Numerator/denominator from live or fixture observations |
| `unknown` | Denominator known where applicable; numerator null |
| `blocked` | Auth/entitlement prevented measurement |

## Commands

```bash
# Deterministic offline regeneration (committed artifact mode)
python scripts/spikes/audit_stats_sources.py \
  --manifest data/manifests/dwcs_bouts_v1.jsonl \
  --out output/research/stats-source-scorecard.json \
  --capture-mode fixtures \
  --capture-time 2026-08-11T21:00:00+00:00 \
  --redact

pytest tests/spikes/test_stats_source_scorecard.py -q
ruff check scripts/spikes/audit_stats_sources.py tests/spikes/test_stats_source_scorecard.py
```

## Handoff contract (DWCS-102)

1. Read `output/research/stats-source-scorecard.json` `decision.primary`.
2. If `primary` is set, implement **exactly** that adapter contract.
3. If `hard_blocker` is true (current state), keep production stats ingest blocked;
   pursue ranked lawful fallbacks; do not enable scrapers.
4. Preserve DWCS-002 manifests as the universe seed; never rediscover history from a
   mutable index alone.
5. Missing bookmaker lines do not unblock or re-rank stats sources.

## Artifact integrity

`output/research/stats-source-scorecard.json` must remain sanitized: no API keys,
no full licensed payloads. Regeneration with the same `--capture-time` and fixture
mode is deterministic.
