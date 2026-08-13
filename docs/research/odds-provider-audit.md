# Odds provider audit (DWCS-000)

Phase 0 spike for live DWCS odds enrichment. This document records the audit
method, the fields that must appear in evidence, and how the decision gate is
applied. It does **not** invent Bet365 coverage from generic catalogs, and a
missing Bet365 feed does not block sportsbook-agnostic price guidance.

Odds feeds are optional line enrichment. Exact EV/ROI/CLV still require a
timestamped observed or user-recorded price. Do not infer provider coverage or
scrape as fallback.

## Goal

Prove actual provider, bookmaker, market, timestamp, suspension/lock, and quota
behavior on the current or next DWCS card. Determine which markets can show
automatic offered prices and which must fall back to sportsbook-agnostic price
targets (downstream of this spike).

## Bet365 catalog identity (The Odds API)

The Odds API catalogs Bet365 Australia as `bet365_au` (AU bookmakers list).
Do **not** treat a hardcoded bare `bet365` key under `us,uk,eu` as universal
Bet365 coverage evidence.

- Configurable aliases default to `bet365,bet365_au`.
- Default CLI regions include `au` (`us,uk,eu,au`) for informational request
  context.
- When both `bookmakers` and `regions` are sent, The Odds API gives
  **`bookmakers` precedence**. Observation scope records
  `effective_query_mode=bookmakers` and sets
  `regions_effective_for_bookmaker_probe=null` so region selection is not
  described as scoping the bookmaker probe.
- Absence is always scoped to the effective probe (provider + queried bookmaker
  keys in bookmakers mode). Universal Bet365 status stays `unresolved` unless
  positive evidence exists.
- Request failures are never collapsed into absence.

### Catalog interpretation context (not event evidence)

The Odds API bookmaker catalog currently lists `bet365_au` as **paid-tier** and
**limited to AFL/NRL** `h2h` / spreads / totals. Therefore MMA non-observation
on a live DWCS query may reflect provider/catalog/plan coverage rather than
proof that the underlying sportsbook universally lacks DWCS. The live event
query remains the only event-specific evidence; do not infer beyond it.

## Live capture: Season 10 Episode 1

The original `T-3h` capture on 2026-08-11 reconciled all five matchups from the
[official UFC card](https://www.ufc.com/news/dana-white-contender-series-season-10-episode-1-preview-athletes-bouts-start-times-streaming)
to The Odds API events.

Corrective recapture `T-3h-corrective` (2026-08-11) sent regions
`us,uk,eu,au` together with an explicit `bookmakers` list that included
`bet365_au` (and bare `bet365`). Effective query mode is `bookmakers`:

- All five official DWCS bouts reconciled as `present`.
- FanDuel `h2h` remained present on reconciled DWCS events (`markets_observed`
  includes `h2h`).
- Bet365/`bet365_au` h2h was not observed for those queried bookmaker keys →
  `bet365_dwcs_status=scoped_absent` (not universal Bet365 absence; catalog
  AFL/NRL/paid-tier limits are interpretation context only).
- `totals`, `method`, and `round` were absent for the tracked bookmakers when
  evaluated.
- Event timestamps, quota headers, and sanitized per-market `last_update`
  evidence were retained; prices and credentials were redacted.
- Lock/suspension behavior, historical replay, provider rights, monthly vendor
  quotes, and manual Bet365 comparisons remain unevaluated.

The evidence gate therefore selects `the_odds_api_reference_fallback`; it does
not authorize a licensed Bet365-primary path. This is an acceptable core-product
path for line enrichment: The Odds API can provide reference moneyline context
while downstream product surfaces publish sportsbook-agnostic price targets.

## Committed artifact integrity

`output/research/odds-coverage-summary.json` must never invent live coverage.
When `ODDS_API_KEY` is unavailable, rejected, or quota-limited, the committed
file records a sanitized failure and unresolved/unknown/blocked cells only.
Missing credentials are not evidence of Bet365 absence, quota, timestamps,
prices, or a provider decision.

## Commands

Example replay of the committed corrective capture (snapshot label matches the
artifact):

```bash
# Official bout list: bout_id, fighter_a, fighter_b, scheduled_start (UTC ISO)
python scripts/spikes/audit_dwcs_odds.py \
  --sport mma_mixed_martial_arts \
  --regions us,uk,eu,au \
  --bookmakers bet365,bet365_au,draftkings,fanduel,betmgm,williamhill_us \
  --bet365-aliases bet365,bet365_au \
  --redact \
  --official-bouts tests/fixtures/spikes/dwcs_s10e1_official_bouts.json \
  --snapshot-label T-3h-corrective \
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
- `--bet365-aliases` — explicit Odds API keys treated as Bet365 (default
  `bet365,bet365_au`).

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
| `absent` | Successful response without that market (explicit absence for that key) |
| `request_failed` | HTTP/transport failure — never treated as absence |

Bet365 DWCS aggregate status:

| Status | Meaning |
|--------|---------|
| `present` | At least one configured Bet365 alias showed h2h on a reconciled DWCS event |
| `scoped_absent` | Successful discovery found no queried Bet365 aliases for the effective probe |
| `request_failed` | Discovery/request failed; distinct from absence |
| `unresolved` | Insufficient evidence for a universal Bet365 claim |

## Documented fields

### Timestamps

- Event `commence_time` from `/v4/sports/{sport}/events`
- Sanitized per-market `last_update` from
  `/v4/sports/{sport}/events/{eventId}/markets` when present (prices,
  credentials, and full market payloads are redacted)
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

From the governing product rule (odds are optional enrichment):

1. A licensed bookmaker adapter is optional enrichment **only if** Phase 0 shows
   evidence-backed DWCS coverage and acceptable rights notes.
2. The Odds API remains a valid reference/historical moneyline source even when
   Bet365 is absent for the queried bookmaker keys.
3. Missing automatic lines disable exact current EV, ROI, and CLV; they do not
   block downstream sportsbook-agnostic price-target recommendations.
4. Do not invent coverage or label consensus books as Bet365.

`decision.path` values:

- `licensed_bet365_primary`
- `the_odds_api_reference_fallback`
- `hard_blocker`

The existing path names describe line-source evidence, not overall product
viability. `the_odds_api_reference_fallback` is sufficient for the core
sportsbook-agnostic product. `hard_blocker` means the audit itself could not
establish usable line-enrichment evidence; it does not mean the model cannot
publish price targets once its data and calibration gates pass.

## Secrets

Committed artifacts must use `--redact`. Redaction removes API keys, auth
headers, and price fields. Never commit `.env`, sportsbook credentials, or full
licensed vendor payloads.

## Handoff

Attach the sanitized matrix and vendor rights/price notes to DWCS-200 / DWCS-202.
The downstream implementation must treat automatic bookmaker lines as optional
enrichment and actionable price thresholds as the required fallback.
Out of scope for this ticket: production ingestion, betting recommendations,
orchestration prompts, and scraping fallbacks.

### DWCS-202 implementation status

DWCS-202 reads the packaged immutable contract
(`mma_model/odds/odds_decision_v1.yaml`, plan-visible via
`config/sources/odds.yaml`). Because
`decision.path=the_odds_api_reference_fallback` and no licensed trial provider
passed, DWCS-202 does **not** ship an automated bookmaker adapter. It ships:

- sportsbook-agnostic fair / actionable / strong-value guidance
- optional `user_observed` numeric price/book/time for exact EV confirmation
- explicit lock / removal / entitlement-failed lifecycle states (no forward-fill)
- Bet365 identity via explicit aliases only; automated/reference paths reject them
- `mma-model odds audit-bookmakers --next-dwcs`

### Downstream handoff policy: actionable prices (not implemented by DWCS-000)

DWCS-000 records evidence and decision-path semantics only. The formulas below
are handoff policy for later tickets; this spike does **not** compute or render
them.

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

Downstream product converts decimal thresholds to American odds and displays:

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
