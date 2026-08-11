# DWCS coverage manifest (DWCS-002)

Frozen 2017–2025 Dana White’s Contender Series event/bout universe used by every
later stage. Phase 1 must ingest these manifests first and must **not** rediscover
history from a mutable completed-event index alone.

## Exact counts (through 2025)

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

These totals match the Phase 0 research target and are reproduced offline from
committed minimal factual fixtures plus documented event-night overrides.

Independent official cross-checks (not used as the ledger seed):

- UFC eight-year review: 76 standard episodes + 3 Brazil episodes and 389 fights
  through Season 8 ([UFC](https://www.ufc.com/news/dana-whites-contender-series-eight-years)).
- UFC Season 9 review: 10 weeks / 46 contracts
  ([UFC](https://www.ufc.com/news/best-of-dana-whites-contender-series-season-9)).
- Season 10 Week 1 (2026-08-11) is **outside** this completed universe.

## Artifacts

| Path | Role |
|------|------|
| `tests/fixtures/manifests/source/espn_events_facts_v1.jsonl` | Minimal ESPN-derived event facts (committed; offline source of truth) |
| `tests/fixtures/manifests/source/espn_bouts_facts_v1.jsonl` | Minimal ESPN-derived bout facts |
| `tests/fixtures/manifests/source/event_night_reconciliations_v1.jsonl` | Documented event-night vs current overrides (9 special bouts) |
| `data/manifests/dwcs_events_v1.jsonl` | Versioned event manifest |
| `data/manifests/dwcs_bouts_v1.jsonl` | Versioned bout manifest |
| `data/manifests/dwcs_counts_v1.json` | Deterministic season + result counts |
| `data/manifests/dwcs_mismatches_v1.json` | Explicit mismatches + open gaps |

## Sources and lawful-use caveats

1. **Primary offline ledger:** committed minimal factual JSONL fixtures under
   `tests/fixtures/manifests/source/`. Verification does **not** require network.
2. **Optional refresh:** `python scripts/spikes/build_dwcs_manifest.py --refresh-espn`
   re-reads ESPN’s undocumented public site/core JSON scoreboard/status endpoints.
   Label: **undocumented / public / spike-only**. Not a production dependency and
   not a licensed feed.
3. **Prohibited for this ticket:** automated scraping of Bet365, Tapology,
   Sherdog, FightMatrix, UFC.com HTML, or UFCStats HTML. No silent scrape fallback.
4. **Event-night evidence:** UFC athlete bios and linked public news articles are
   cited only for the nine non-trivial draw/NC/reversal cases. Full provider
   payloads and secrets are never committed.
5. **UFCStats / UFC.com IDs:** left `null` with explicit quality flags. Do not invent
   IDs. Later licensed adapters may map them.
6. **Timestamps:** occurrence timestamps come from ESPN event/competition dates when
   present. Publication timestamps are unknown and remain `null` (never invented).
   Brazil rows carry a flag that air dates may differ from occurrence dates.
7. **Cancellations / replacements:** the ESPN scoreboard exposes occurred bouts
   only. A complete announced-card cancellation/replacement ledger is an explicit
   open gap unless separately evidenced (mini fixtures cover the code paths).

## Event-night vs current special cases

| Bout | Event-night | Current | State |
|------|-------------|---------|-------|
| Holobaugh vs Bessette (2017 W1) | Decisive (Holobaugh) | NC | Reversed (IV / NSAC) |
| Hernandez vs Wright (2018 W2) | Decisive (Hernandez) | NC | Reversed (drug test) |
| Williams vs Caron (2018 W3) | Decisive (Williams) | NC | Reversed (NSAC) |
| Trocoli vs Bergh (2019 W3) | Decisive (Trocoli) | NC | Reversed (drug test) |
| Sosoli vs Joynson (2019 W10) | NC (eye poke) | NC | Unchanged |
| Quinlan vs Urban (2021 W2) | Decisive (Quinlan) | NC | Reversed (drug test) |
| Brzeski vs Potter (2021 W3) | Decisive (Brzeski) | NC | Reversed (drug test) |
| Alencar vs Luciano (2023 W7) | Draw | Draw | Unchanged |
| Rodrigues vs Vidal (2025 W10) | Decisive (Rodrigues) | NC | Reversed (drug test) |

## How Phase 1 consumes this

1. Load `data/manifests/dwcs_events_v1.jsonl` and `dwcs_bouts_v1.jsonl` as the
   immutable DWCS universe seed.
2. Resolve fighters/bouts through source IDs on the manifest first.
3. Attach licensed stats/odds adapters afterward; never replace the frozen card
   list by crawling a mutable completed-events index.
4. Keep event-night and current result fields separate for labeling and settlement.
5. Treat `dwcs_mismatches_v1.json` open gaps as explicit unknowns, not silent defaults.

## Commands

```bash
# Offline rebuild + verify (CI / local)
python scripts/spikes/build_dwcs_manifest.py --through 2025 --verify

# Optional ESPN refresh of factual fixtures, then verify
python scripts/spikes/build_dwcs_manifest.py --through 2025 --refresh-espn --verify

pytest tests/test_dwcs_manifest.py -q
```

## Integrity rules enforced by the builder

- Exactly two participants per occurred bout; canonical unordered pair is unique
  within an event.
- Deterministic `event_id` / `bout_id` (`dwcs:event:espn:…`, `dwcs:bout:espn:…`).
- Participant ordering by normalized name.
- Schema/referential integrity between bout `event_id` and event rows.
- Every count discrepancy or unverifiable field is written to the mismatch report.
