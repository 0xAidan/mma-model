# MMA settlement source notes (DWCS-200)

Accessed: **2026-08-12**. Public help-center pages only (no login, scraping
bypass, or credentials). These notes reconcile versioned settlement defaults to
statements that appear in the cited text. They do **not** claim universal
sportsbook rules.

Sportsbook-agnostic fair / actionable / strong-value price guidance remains the
required product fallback and does **not** depend on settlement-source approval.

## Primary public sources

| ID | Locator | What was used |
|----|---------|---------------|
| `skybet_ufc_mma_rules` | https://support.skybet.com/app/answers/detail/ufc-mma-rules | Flutter/Sky Bet UFC & MMA rules |
| `paddypower_ufc_mma_rules` | https://helpcenter.paddypower.com/app/answers/detail/ufc-mma-rules | Flutter/Paddy Power UFC/MMA rules |
| `bodog_boxing_mma_betting_rules` | https://www.bodog.eu/help/common-faq/boxing-mma-betting-rules | Bodog Boxing/MMA betting rules |
| `bet365_nj_mma_rules_goes_distance` | https://help.nj.bet365.com/s/en-us/sportsrules/mma | bet365 NJ MMA rules (goes-distance wording) |

DraftKings’ MMA rules URL
(`https://sportsbook.draftkings.com/help/sport-rules/mma`) did not yield usable
rule text in a public fetch on 2026-08-12 (geo/licensing shell only) and is
**not** used as evidence.

## Reconciled defaults (`mma_generic` `1.3.0`)

Claims below are limited to what the cited pages actually say. Where the pages
are silent, the contract fails closed (`unresolved`) or uses an explicitly
labeled modeling choice.

| Topic | Encoded default | Direct support | Hedge / disagreement |
|-------|-----------------|----------------|----------------------|
| 2-way moneyline draw (draw not offered) | `void` | Sky Bet: fight outright void if draw not offered; Paddy Power: match bets void; Bodog Fight Winner: “no action” | Some U.S. books push 2-way moneylines; not selected. 3-way draw selections are out of v1 scope. |
| Technical draw on 2-way moneyline | `void` | Same draw / “no action” settlement family; Sky defines technical draw as early scorecard stoppage with no winner | Not a separate moneyline product on the cited pages; treated like other non-winner outcomes on 2-way. |
| No contest | `void` | Sky Bet, Paddy Power, Bodog (with already-determined exceptions not modeled in v1 moneyline/method) | — |
| Cancellation / does not take place | `void` | Sky Bet / Paddy Power void language for fights that do not take place | Timing windows (e.g. postpone rules) are orchestration, not v1 grading. |
| Technical decision on moneyline | settle winner as decision | Sky Bet Fight Outcome (scorecards → decision); Paddy Power (Won by Points); Bodog Decision/Technical Decision | — |
| Totals half-round mark (5-min rounds) | 2:30 (`round_seconds=300`) | Sky Bet, Paddy Power, Bodog MMA 5-minute section | Bodog also documents 3-min/2-min boxing half marks; v1 UFC/MMA path uses 5-minute only. |
| Totals at **exact** 2:30 | `under` wins | Sky Bet and Paddy Power: under wins at exact 2:30 | **Bodog voids** both sides — `bodog_mma.totals.exact_half_result: void`. |
| Totals over | lasts **beyond** the half mark | Sky Bet, Paddy Power, Bodog | — |
| Ordinary decision / ordinary draw duration for totals | `full_scheduled` (clocks may be omitted) | Sky/Paddy/Bodog treat full-distance decision/draw as completing the schedule for distance markets; totals examples assume elapsed time | Modeling choice when clocks omitted: inventing less than scheduled distance for a completed decision would under-grade overs. |
| Technical decision / technical draw duration for totals | `stoppage_time` (clocks required) | Bodog Total Rounds: “In the event of a technical decision or technical draw, the point at which the fight was stopped will be used for settlement purposes.” | Sky/Paddy define tech decision/draw as early stoppages but do not restate Bodog’s totals sentence. Using full scheduled duration for those outcomes would invent time past the stoppage, so generic also requires stoppage clocks and does not auto-fill the schedule. |
| Goes the distance — ordinary decision / scorecard draw | Yes (`goes_distance`) | Sky / Paddy: not gone distance only if concluded before stated rounds; bet365 / Bodog: Yes requires full scheduled rounds | — |
| Goes the distance — technical decision / technical draw | No (`inside_distance`) | Sky: “If the fight is concluded before the stated rounds are over, then the match has not gone the distance”; Paddy: same; bet365 Will Fight go the Distance / To Go the Distance: full rounds required for Yes; Bodog Will The Fight Go The Distance: scheduled rounds must be fully completed for Yes | bet365’s separate “When Will Fight End” market settles tech TD/tech draw by last completed round for that multi-way product — that is **not** the Yes/No goes-distance family encoded here. |
| Exact-round on ordinary decision / tech decision / tech draw | `loss` | Round-finish markets lose when there is no KO/TKO/submission-style finish in a round (Sky/Paddy round + decision language; Bodog round betting focuses on KO/TKO/DQ finishes) | Not a claim that every book voids these; v1 grades them as losing round selections. |
| Method family on ordinary draw / technical draw | `void` | Sky Method of Victory voids on NC/DQ; draw/tech-draw are not offered as v1 method outcomes | Fail closed: do not invent a winning method selection. |

## Versioned overrides

| Rule set | Status | Encoded difference | Evidence bound |
|----------|--------|--------------------|----------------|
| `mma_generic` | `externally_sourced` | Defaults above | Sky / Paddy / Bodog / bet365 citations listed in YAML |
| `bodog_mma` | `externally_sourced` | `totals.exact_half_result: void`; reaffirms tech decision/draw `stoppage_time` | Bodog totals exact 2:30 void + tech stoppage sentence |
| `bet365_mma` | `provisional_pending_approved_source` | Empty override lane | Full Bet365 DWCS adoption still provisional; goes-distance wording is already folded into `mma_generic` via the bet365 citation |

## Governance rules encoded in the loader

- `externally_sourced` rule sets **must** cite at least one `https://` public
  reference with an access date.
- `provisional_pending_approved_source` sets require `allow_provisional=True` and
  must not be the default.
- Price-target classification never consults settlement source status.

## Fact-version modeling (implementation invariant)

Cancelled and no-contest are terminal on a **single** `BoutSettlementFacts`
version: that version must not also carry `winner_side` / `method`. Public
pages that void “unless already determined” are about prop markets already
resolved before an NC, not permission to mix a stale finish method into the
same NC fact object. Retain earlier methods on prior versions if a source
stream revises the result.

## Remaining ambiguity (explicit)

1. **Totals duration for technical decision/draw outside Bodog** — Sky/Paddy define
   the events but do not publish Bodog’s exact “point at which the fight was
   stopped” totals sentence. Generic uses `stoppage_time` + required clocks so
   we do not invent full-distance elapsed time.
2. **Method markets and technical draw** — cited pages define the result type;
   v1 has no `technical_draw` outcome atom, so settlement voids rather than
   inventing a grade against `decision` / finish selections.
3. **U.S. push-vs-void on 2-way moneylines** — not encoded; would need a
   versioned override with an approved citation if a target book differs.
