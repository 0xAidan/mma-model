# MMA settlement source notes (DWCS-200)

Accessed: **2026-08-12**. Public help-center pages only (no login, scraping
bypass, or credentials). These notes reconcile versioned settlement defaults to
durable sportsbook house-rule documents. They do **not** claim universal
sportsbook rules.

Sportsbook-agnostic fair / actionable / strong-value price guidance remains the
required product fallback and does not depend on settlement-source approval.

## Primary public sources

| ID | Locator | Operator notes |
|----|---------|----------------|
| `skybet_ufc_mma_rules` | https://support.skybet.com/app/answers/detail/ufc-mma-rules | Flutter/Sky Bet UFC & MMA rules |
| `paddypower_ufc_mma_rules` | https://helpcenter.paddypower.com/app/answers/detail/ufc-mma-rules | Flutter/Paddy Power UFC/MMA rules |
| `bodog_boxing_mma_betting_rules` | https://www.bodog.eu/help/common-faq/boxing-mma-betting-rules | Bodog Boxing/MMA betting rules |

DraftKings’ MMA rules URL
(`https://sportsbook.draftkings.com/help/sport-rules/mma`) did not yield usable
rule text in a public fetch on 2026-08-12 (geo/licensing shell only) and is
**not** used as evidence.

## Reconciled defaults (`mma_generic`)

| Topic | Default | Supported by | Disagreement / notes |
|-------|---------|--------------|----------------------|
| Moneyline draw (2-way, draw not offered) | `void` | Sky Bet (fight outright void if draw not offered); Paddy Power (match bets void); Bodog (fight winner “no action”) | Some U.S. books treat 2-way moneylines as push; that is **not** selected here. 3-way markets with an explicit draw selection are out of v1 scope. |
| No contest | `void` | Sky Bet, Paddy Power, Bodog | Exceptions for already-determined props are not modeled in v1 moneyline/method families. |
| Cancellation / does not take place | `void` | Sky Bet, Paddy Power, Bodog | Timing windows (e.g. 48h postpone) are orchestration concerns, not v1 grading. |
| Technical decision | settle as `decision` | Sky Bet (scorecards → decision/points); Paddy Power (Won by Points); Bodog (Decision/Technical Decision) | — |
| Totals half-round mark | 2:30 of the relevant round (`round_seconds=300`) | Sky Bet, Paddy Power, Bodog | — |
| Totals at **exact** 2:30 | `under` wins (over loses) | Sky Bet; Paddy Power | **Bodog voids** both sides at exact 2:30 — captured in `bodog_mma` override (`exact_half_result: void`). |
| Totals over | fight lasts **beyond** the half mark | Sky Bet, Paddy Power, Bodog | — |
| Exact-round / finish markets on decision | `loss` | Sky Bet / Paddy Power round betting (decision is not a round finish) | — |
| Method NC / DQ | `void` | Sky Bet, Paddy Power | — |

## Versioned overrides

| Rule set | Status | Purpose |
|----------|--------|---------|
| `mma_generic` | `externally_sourced` | Defaults reconciled above |
| `bodog_mma` | `externally_sourced` | `totals.exact_half_result: void` per Bodog |
| `bet365_mma` | `provisional_pending_approved_source` | Empty override lane until a public/licensed Bet365 citation exists |

## Governance rules encoded in the loader

- `externally_sourced` rule sets **must** cite at least one `https://` public
  reference with an access date.
- `provisional_pending_approved_source` sets require `allow_provisional=True` and
  must not be the default.
- Price-target classification never consults settlement source status.
