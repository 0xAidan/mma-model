# DWCS coverage and data-health gates

Sanitized DWCS-106 evidence. No database files, raw HTML, or secrets.

- Report hash: `dfdaa67bb61c862ebb0c54b3e339680083b2c91fd8b28a910a5ddc04e0e2b16a`
- Config hash: `497eb548fca8ad1e550dc5a6f712c4227f03b5a4fedbd185a4bbb0066d6a467e`
- DB hash: `b67814ed72903f82684d2cecd11657940c789050b49439c88e365666caae75d7`
- Universe: 89 cards / 440 bouts
- Standard: 86/425; Brazil: 3/15
- Event-night results: decisive=438 draw=1 nc=1
- Current results: decisive=431 draw=1 nc=8
- Result transitions: reversed_to_nc=7 both_lanes_nc=1 lanes_equal=433
- Core tiers: gold=0 silver=0 bronze=440 missing=0 conflict=0
- DB counts: events=89 bouts=440 fighters=796 result_versions=880 provenance=880
- PIT: proxy=880 unknown=0 direct=0 left_truncated=0 conflicting_outcomes=0 missing_required_details=440 mutable_status=not_applicable mutable_examined=0 mutable_applicable=0 mutable_guard=1
- Raw-ref: ok=true absent_explicit=880 dangling=0 present=0
- Checkpoint/run: ingest_runs=1 succeeded=1 completed=0 failed=0 checkpoints=1
- Passing gates: core_denominator, identity_conflict, manifest_representation, future_row_leakage, raw_ref_integrity
- Not-applicable gates: mutable_current_leakage
- Blocking gates: pre_fight_agreement, live_source_killed:combat_registry, regional_amateur_sample, regional_professional_sample, live_accessibility_only:sherdog_public, live_source_killed:tapology_public, missing_required_details, result_agreement, ufcstats_public_live, cross_source_reconciliation
- Non-strict CLI exit: 0; strict CLI exit: 2
- Licensed status is `licensed_primary_unselected` / `decision.primary=null` and is **not** a Phase 1 blocker.
- Fixture identity/regional metrics are validation only and never live coverage.
- Public accessibility is not accuracy, PIT, or rights proof.

## Sources

- `dwcs_manifest` (internal_manifest): status=`present` reason=`None` mapped=440 missing=0 validation_only=False
- `ufcstats_public` (public_extraction): status=`source_killed` reason=`cloudflare_challenge` mapped=0 missing=440 validation_only=False
- `mma_ai_bootstrap` (public_dataset): status=`unmeasured` reason=`no_observations` mapped=0 missing=440 validation_only=False
- `tapology_public` (public_extraction): status=`source_killed` reason=`http_403` mapped=0 missing=440 validation_only=False
- `sherdog_public` (public_extraction): status=`accessibility_only` reason=`listing_only_not_fighter_history` mapped=0 missing=440 validation_only=False
- `combat_registry` (official_record): status=`source_killed` reason=`login_wall` mapped=0 missing=440 validation_only=False
- `sportsdataio` (licensed_api): status=`validation_only` reason=`licensed_validation_only_not_live_coverage` mapped=0 missing=440 validation_only=True
- `balldontlie` (licensed_api): status=`validation_only` reason=`licensed_validation_only_not_live_coverage` mapped=0 missing=440 validation_only=True
- `explicit_missing` (public_dataset): status=`unmeasured` reason=`explicit_missing_unused` mapped=0 missing=440 validation_only=False

## Fields

- `result_type`: present=440 missing=0 unknown=0 denominator=440 status=`measured`
- `winner_fighter_id`: present=433 missing=7 unknown=0 denominator=440 status=`measured`
- `method`: present=0 missing=440 unknown=0 denominator=440 status=`measured`
- `ending_round`: present=0 missing=440 unknown=0 denominator=440 status=`measured`
- `time_str`: present=0 missing=440 unknown=0 denominator=440 status=`measured`
- `quality_tier`: present=440 missing=0 unknown=0 denominator=440 status=`measured`
- `timestamp_quality`: present=440 missing=0 unknown=0 denominator=440 status=`measured`
- `payload_hash`: present=440 missing=0 unknown=0 denominator=440 status=`measured`

## Identity

- scoped pending=0; unscoped pending=0; upcoming blocks=0; unmatched=0; unmatched_source_identities=0
