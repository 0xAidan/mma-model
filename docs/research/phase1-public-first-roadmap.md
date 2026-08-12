# Phase 1 roadmap amendment (public-first)

**Date:** 2026-08-12  
**Depends on:** DWCS-100/101 (merged), DWCS-002 manifests, DWCS-003 licensed audit evidence  
**Policy:** `config/sources/source_policy_v1.json`

## Exit criteria (unchanged strength)

- Migrations work from clean and legacy DBs.
- All 89/440 manifest rows ingest idempotently with every exclusion categorized.
- Identity and result versions reconcile (≥98% cross-source where comparable;
  ≥99% result agreement).
- Zero unresolved evaluated/upcoming identity conflicts.
- Zero future-row leakage failures; no mutable-current historical features.
- Coverage report publishes gold/silver/bronze/missing/conflict tiers and fails
  closed on blockers.

## Ticket order

| Ticket | Focus under public-first policy |
|--------|----------------------------------|
| DWCS-102 | UFCStats public core adapter + mma-ai bootstrap reconcile; polite HTTP |
| DWCS-103 | Manifest-first DWCS history classification and sync |
| DWCS-104 | Exact-ID / Wikidata identity + reversible review queue |
| DWCS-105 | Tapology → Sherdog → Combat Registry/commission regional PIT enrichment |
| DWCS-106 | Strict coverage/health/leakage gates |

Executable steps: `docs/superpowers/plans/2026-08-12-public-first-mma-history.md`.

## Explicit non-changes

- Do not set scorecard `decision.primary` without measured audits.
- Preserve BALLDONTLIE/SportsDataIO evidence as validation history.
- Odds archive/API remain a separate lane (Phase 2 seam) unless evaluation
  contract enables a challenger.
