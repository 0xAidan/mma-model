# DWCS coverage manifest (DWCS-002)

Frozen 2017–2025 Dana White’s Contender Series event/bout universe used by every
later stage. Phase 1 must ingest these manifests first and must **not** rediscover
history from a mutable completed-event index alone.

## Exact counts (through 2025)

Pinned research targets live in one file:
`config/manifests/dwcs_expected_universe_v1.json`
(digest pinned as `PINNED_EXPECTED_UNIVERSE_HASH` in
`scripts/spikes/build_dwcs_manifest.py`). Dual edits of fixtures + magic constants
cannot silently redefine the target without also bumping the contract version and
pinned digest.

| Universe | Cards | Occurred bouts |
|----------|------:|---------------:|
| All DWCS (standard + Brazil) | **89** | **440** |
| Standard-only | **86** | **425** |
| Brazil 2018 | **3** | **15** |

### Result reconciliation

| Lens | Decisive | Draw | No contest |
|------|---------:|-----:|-----------:|
| Event-night | **438** | **1** | **1** |
| Current | **431** | **1** | **8** |

Version states: 431 `assumed_equal_to_current`, 7 `reversed_to_no_contest`,
2 `unchanged`.

Independent official cross-checks (not used as the ledger seed):

- UFC eight-year review: 76 standard episodes + 3 Brazil episodes and 389 fights
  through Season 8 ([UFC](https://www.ufc.com/news/dana-whites-contender-series-eight-years)).
- UFC Season 9 review: 10 weeks / 46 contracts
  ([UFC](https://www.ufc.com/news/best-of-dana-whites-contender-series-season-9)).
- Season 10 Week 1 (2026-08-11) is **outside** this completed universe.

## Artifacts

| Path | Role |
|------|------|
| `config/manifests/dwcs_expected_universe_v1.json` | Pinned expected-universe contract (single research-target source) |
| `tests/fixtures/manifests/source/espn_events_facts_v1.jsonl` | Minimal pre-classified ESPN-derived event facts |
| `tests/fixtures/manifests/source/espn_bouts_facts_v1.jsonl` | Minimal pre-classified ESPN-derived bout facts |
| `tests/fixtures/manifests/source/event_night_reconciliations_v1.jsonl` | Documented event-night vs current overrides + provenance |
| `data/manifests/dwcs_events_v1.jsonl` | Versioned event manifest |
| `data/manifests/dwcs_bouts_v1.jsonl` | Versioned bout manifest |
| `data/manifests/dwcs_counts_v1.json` | Deterministic season + result counts |
| `data/manifests/dwcs_mismatches_v1.json` | Explicit mismatches + deferred open gaps |

## Sources and lawful-use caveats

1. **Primary offline ledger:** committed minimal factual JSONL fixtures under
   `tests/fixtures/manifests/source/`. Verification does **not** require network.
2. **ESPN fixture status (candid):** fixtures are **minimal pre-classified source
   facts** (event/bout IDs, participants, current result class, dates), not raw
   ESPN payloads and **not** proof of current API drift. Default CI never hits the
   network.
3. **Optional manual/network refresh (not CI):**
   `python scripts/spikes/build_dwcs_manifest.py --through 2025 --refresh-espn --verify`
   may be run by an operator to spot ESPN drift. It remains spike-only and must
   not be treated as a production dependency.
4. **Prohibited for this ticket:** automated scraping of Bet365, Tapology,
   Sherdog, FightMatrix, UFC.com HTML, or UFCStats HTML. No silent scrape fallback.
5. **Event-night evidence:** structured citation provenance
   (`evidence[]`, `evidence_checked_at`, `evidence_grade`, `citation_only`,
   `evidence_limitations`). CI validates URL shape and required fields offline; it
   **does not fetch** external pages or machine-verify content/liveness.
6. **Timestamps:** occurrence timestamps come from ESPN event/competition dates when
   present. Publication timestamps are unknown and remain `null` (never invented).
   Brazil rows carry a flag that air dates may differ from occurrence dates.

## Open gaps deferred to later tickets

These are **incomplete**, not silently complete. Phase 1 must not treat nulls as done.

| Gap | Deferred to | Why |
|-----|-------------|-----|
| UFC.com / UFCStats event & bout ID mapping | **DWCS-103**, identity fallout **DWCS-104** | Manifest freezes ESPN-linked IDs; provider mapping happens during history ingest / identity resolution |
| Publication timestamps | **DWCS-103** | Not evidenced by scoreboard facts |
| Full cancellation / replacement ledger | **DWCS-103** | Scoreboard facts expose occurred bouts only; DWCS-103 classifies cancelled/replacement states |

## Event-night vs current special cases

| Bout | Event-night | Current | State | Evidence grade |
|------|-------------|---------|-------|----------------|
| Holobaugh vs Bessette (2017 W1) | Decisive (Holobaugh) | NC | Reversed | contemporaneous_news |
| Hernandez vs Wright (2018 W2) | Decisive (Hernandez) | NC | Reversed | contemporaneous_news |
| Williams vs Caron (2018 W3) | Decisive (Williams) | NC | Reversed | contemporaneous_news |
| Trocoli vs Bergh (2019 W3) | Decisive (Trocoli) | NC | Reversed | contemporaneous_news |
| Sosoli vs Joynson (2019 W10) | NC (eye poke) | NC | Unchanged | contemporaneous_news |
| Quinlan vs Urban (2021 W2) | Decisive (Quinlan) | NC | Reversed | contemporaneous_news |
| Brzeski vs Potter (2021 W3) | Decisive (Brzeski) | NC | Reversed | contemporaneous_news |
| Alencar vs Luciano (2023 W7) | Draw | Draw | Unchanged | contemporaneous_news |
| Rodrigues vs Vidal (2025 W10) | Decisive (Rodrigues) | NC | Reversed | contemporaneous_news |

## How Phase 1 consumes this

1. Load `data/manifests/dwcs_events_v1.jsonl` and `dwcs_bouts_v1.jsonl` as the
   immutable DWCS universe seed.
2. Resolve fighters/bouts through source IDs on the manifest first.
3. Attach licensed stats/odds adapters afterward; never replace the frozen card
   list by crawling a mutable completed-events index.
4. Keep event-night and current result fields separate for labeling and settlement.
5. Treat `dwcs_mismatches_v1.json` open gaps as explicit unknowns deferred to
   DWCS-103 / DWCS-104 — null UFCStats IDs and empty cancellation ledgers are not done.

## Commands

```bash
# Offline rebuild + verify (CI / local)
python scripts/spikes/build_dwcs_manifest.py --through 2025 --verify

# Optional operator-only ESPN refresh (not default CI)
python scripts/spikes/build_dwcs_manifest.py --through 2025 --refresh-espn --verify

pytest tests/test_dwcs_manifest.py -q
```

## Integrity rules enforced by the builder

- Exactly two participants per occurred bout; canonical unordered pair is unique
  within an event.
- Deterministic `event_id` / `bout_id` (`dwcs:event:espn:…`, `dwcs:bout:espn:…`).
- Participant ordering by normalized name.
- Schema/referential integrity between bout `event_id` and event rows.
- Reconciliation provenance fields validated offline.
- Every count discrepancy or unverifiable field is written to the mismatch report.
