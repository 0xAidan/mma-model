# Data contracts

Canonical contracts for DWCS evaluation, later storage, and dashboard publish.
**Rule:** pick the contract for your question — do not mix priced betting metrics
with price-target-only rows.

## EvaluationContract (DWCS-001)

| Field | Value |
|-------|--------|
| **File** | `config/evaluation/dwcs_v1.json` |
| **Loader** | `mma_model.evaluation.load_evaluation_contract` |
| **Schema version** | `1` |
| **Contract id / version** | `dwcs_evaluation` / `1.0.0` |
| **Mutability** | Read-only for normal model runs; frozen pydantic model at runtime |
| **Hash** | SHA-256 of canonical JSON (`sort_keys`, compact separators); stamped on evaluator outputs later |
| **Failure mode** | Schema / version / id / hash mismatch **hard-fails** |

### Locked protocol highlights

- Universes: all-DWCS 89 cards / 440 bouts; standard-only 86 / 425; Brazil 3 / 15.
- Splits: card-grouped rolling-origin; development 2017–2023; validation 2024;
  **locked holdout 2025**.
- Prediction cutoff: scheduled start minus **60 minutes**; identical cutoff for every
  bout on a card.
- Mutable facts require `effective_at < cutoff` and `observed_at <= cutoff`.
- Outcome metrics include joint/market log loss, Brier, calibration
  intercept/slope, reliability bins, descriptive accuracy, skill vs baselines.
- Betting metrics (ROI, CLV, drawdown, …) apply only to rows with timestamped
  observed or user-recorded prices.
- Price-target-only rows never receive synthetic betting performance.
- Bookmaker odds are optional enrichment; missing Bet365 does not block core
  fair / actionable / strong-value guidance.

### Recommendation price formulas (encoded)

- Fair decimal odds: `1 / p50`
- Actionable decimal price: `max(1 / p25, 1.05 / p50)`
- Strong-value decimal price: `max(1 / p25, 1.10 / p50)`
- Confirmed-value gate (when a timestamped offer exists): offered price meets
  actionable threshold and bootstrap `P(EV > 0) ≥ 0.70` (exact round uses 0.10 EV
  target and `≥ 0.75`).

## Existing prototype stores (retained, not yet DWCS-canonical)

| Store | Module / table | Notes |
|-------|----------------|-------|
| Fighters / events / fights / stats | `src/mma_model/db/models.py` | UFCStats-shaped; to be superseded by bitemporal canonical tables |
| Ingest cursor | `ingest_cursors` | Paginated UFCStats resume |
| Odds snapshot blob | `odds_snapshots` | Untyped JSON payload; not bout-matched quotes |
| Fighter composites | `fighter_composites` | Rolling scores; watch for current-aggregate leakage |

## Governing product rule (odds)

Bookmaker odds are optional enrichment. Missing Bet365 does not block core
sportsbook-agnostic fair / actionable / strong-value guidance. Exact EV / ROI /
CLV require timestamped observed or user-recorded prices; price-target-only rows
never receive synthetic betting performance.

## Related

- [ADR 0001: DWCS system boundaries](adr/0001-dwcs-system-boundaries.md)
- [Baseline command outputs](baseline/dwcs-001-command-outputs.md)
- [Odds provider audit](research/odds-provider-audit.md)
