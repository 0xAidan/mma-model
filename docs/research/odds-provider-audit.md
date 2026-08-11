# Odds provider audit (DWCS-000)

Phase 0 spike for live DWCS odds feasibility. This document records the audit
method, the fields that must appear in evidence, and how the decision gate is
applied. It does **not** invent Bet365 coverage from generic catalogs.

## Goal

Prove actual provider, bookmaker, market, timestamp, suspension/lock, and quota
behavior on the current or next DWCS card.

## Committed artifact integrity

`output/research/odds-coverage-summary.json` must never invent live coverage.
When `ODDS_API_KEY` is unavailable, the committed file is a machine-readable
`run_status=not_run` / `status=blocked` artifact with unresolved/unknown/blocked
cells only. Missing credentials are not evidence of Bet365 absence, quota,
timestamps, prices, or a provider decision.

## Commands

```bash
# Official bout list: bout_id, fighter_a, fighter_b, scheduled_start (UTC ISO)
python scripts/spikes/audit_dwcs_odds.py \
  --sport mma_mixed_martial_arts \
  --redact \
  --official-bouts tests/fixtures/spikes/dwcs_official_bouts.example.json \
  --snapshot-label T-6h \
  --out output/research/odds-coverage-summary.json

pytest tests/spikes/test_audit_dwcs_odds.py -q
ruff check scripts/spikes tests/spikes
```

Capture windows when possible: `T-24h`, `T-6h`, `T-1h`, `T-10m`. If Week 1 is
missed, rerun against the next DWCS card with an updated official bout file.

Optional inputs:

- `--manual-bet365-samples path.json` — at most five manually observed Bet365
  displays (region + time + bout id). The CLI rejects more than five. Do not
  store sportsbook logins. Mark `matches_provider: true` only when the sampled
  display matches a provider quote.
- `--vendor-notes path.json` — OpticOdds / SportsGameOdds / SportsDataIO trial
  status blocks once credentials exist.

## Classification rules

Every official bout is labeled:

| Status | Meaning |
|--------|---------|
| `present` | Unique participant + commence-time match within the configured window |
| `absent` | No participant match in the provider event list |
| `unresolved` | Ambiguous matches or timing conflict |

Bookmaker × market cells are labeled:

| Status | Meaning |
|--------|---------|
| `present` | Market key observed for that bookmaker on a successful response |
| `absent` | Successful response without that market (explicit absence) |
| `request_failed` | HTTP/transport failure — never treated as absence |

## Documented fields

### Timestamps

- Event `commence_time` from `/v4/sports/{sport}/events`
- Market `last_update` from `/v4/sports/{sport}/events/{eventId}/markets` when present
- Capture `captured_at` and operator `snapshot_label` on the summary

### Lock / suspension

- The Odds API MMA path in this spike does **not** provide Bet365 streaming lock
  events. Lock support is recorded only from authenticated trial-vendor evidence.
- `pass_fail_matrix.lock_events` stays `fail`/`unknown` until that evidence exists.

### Quota

Every Odds API response should expose:

- `x-requests-remaining`
- `x-requests-used`
- `x-requests-last`

These headers are copied into the sanitized summary. Usage should be capped with
`--max-events-for-markets`.

## Pass/fail matrix

The summary includes evidence-backed rows for:

- moneyline
- totals
- method
- round
- lock_events
- historical_replay
- rights
- monthly_quote

Statuses are `pass`, `fail`, `blocked`, or `unknown`.

## Decision gate

From the accepted plan:

1. Licensed Bet365 adapter is primary **only if** Phase 0 shows evidence-backed
   Bet365 × DWCS coverage and acceptable rights notes.
2. Otherwise The Odds API remains reference/historical moneyline only.
3. Do not invent coverage. Missing trial credentials are a hard blocker for the
   Bet365-complete path, not a reason to label consensus books as Bet365.

`decision.path` values:

- `licensed_bet365_primary`
- `the_odds_api_reference_fallback`
- `hard_blocker`

## Secrets

Committed artifacts must use `--redact`. Redaction removes API keys, auth
headers, and price fields. Never commit `.env`, sportsbook credentials, or full
licensed vendor payloads.

## Handoff

Attach the sanitized matrix and vendor rights/price notes to DWCS-200 / DWCS-202.
Out of scope for this ticket: production ingestion and betting recommendations.
