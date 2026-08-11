# Odds provider audit (DWCS-000)

Phase 0 spike for live DWCS odds enrichment. This document records the audit
method, the fields that must appear in evidence, and how the decision gate is
applied. It does **not** invent Bet365 coverage from generic catalogs, and a
missing Bet365 feed does not block sportsbook-agnostic price guidance.

## Goal

Prove actual provider, bookmaker, market, timestamp, suspension/lock, and quota
behavior on the current or next DWCS card. Determine which markets can show
automatic offered prices and which must show fair and actionable price targets.

## Live capture: Season 10 Episode 1

The `T-3h` capture on 2026-08-11 reconciled all five matchups from the
[official UFC card](https://www.ufc.com/news/dana-white-contender-series-season-10-episode-1-preview-athletes-bouts-start-times-streaming)
to The Odds API events.

- FanDuel `h2h` was present for all five DWCS bouts.
- Bet365 was absent for all requested markets on all five reconciled bouts.
- `totals`, `method`, and `round` were absent for the tracked bookmakers.
- Event timestamps and quota headers were captured successfully.
- Lock/suspension behavior, historical replay, provider rights, monthly vendor
  quotes, and manual Bet365 comparisons remain unevaluated.

The evidence gate therefore selects `the_odds_api_reference_fallback`; it does
not authorize a licensed Bet365-primary path. This is an acceptable core-product
path: The Odds API can provide reference and historical moneyline context while
the dashboard provides book-agnostic actionable prices for the user to compare
against any sportsbook.

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

1. A licensed bookmaker adapter is optional enrichment **only if** Phase 0 shows
   evidence-backed DWCS coverage and acceptable rights notes.
2. The Odds API remains a valid reference/historical moneyline source even when
   Bet365 is absent.
3. Every qualified model output must be able to publish a sportsbook-agnostic fair
   price, minimum actionable price, and strong-value price without a live quote.
4. Do not invent coverage or label consensus books as Bet365.
5. Missing automatic lines disable exact current EV, ROI, and CLV; they do not
   block price-target recommendations.

`decision.path` values:

- `licensed_bet365_primary`
- `the_odds_api_reference_fallback`
- `hard_blocker`

The existing path names describe line-source evidence, not overall product
viability. `the_odds_api_reference_fallback` is sufficient for the core
sportsbook-agnostic product. `hard_blocker` means the audit itself could not
establish usable evidence; it does not mean the model cannot publish price
targets once its data and calibration gates pass.

## Actionable price policy

For calibrated median win probability `p50`, conservative 25th-percentile
probability `p25`, and target EV `t`:

```text
fair_decimal = 1 / p50
conservative_break_even_decimal = 1 / p25
target_ev_decimal = (1 + t) / p50
minimum_actionable_decimal = max(
    conservative_break_even_decimal,
    target_ev_decimal,
)
```

The dashboard converts decimal thresholds to American odds and displays:

- **Fair price:** break-even odds from `p50`; informational, not actionable.
- **Actionable at:** `minimum_actionable_decimal` using the standard 5% target EV.
- **Strong value at:** the same calculation using a 10% target EV.
- **Confirmed value:** shown only when a timestamped automatic or user-recorded
  offered price meets the actionable threshold.
- **Price target:** shown when no offered price is available; the user compares
  the threshold with any sportsbook.

Higher decimal odds are always better for the user. For American odds, the
dashboard must render the equivalent “or better” threshold correctly for both
positive and negative prices.

Outcome accuracy and calibration can grade every model prediction. ROI and CLV
must include only recommendations with a timestamped observed or user-recorded
price. Threshold-only rows are reported separately and never assigned synthetic
profit.

## Secrets

Committed artifacts must use `--redact`. Redaction removes API keys, auth
headers, and price fields. Never commit `.env`, sportsbook credentials, or full
licensed vendor payloads.

## Handoff

Attach the sanitized matrix and vendor rights/price notes to DWCS-200 / DWCS-202.
The downstream implementation must treat automatic bookmaker lines as optional
enrichment and actionable price thresholds as the required fallback.
Out of scope for this ticket: production ingestion and betting recommendations.
